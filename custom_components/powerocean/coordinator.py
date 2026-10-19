"""
coordinator.py: Home Assistant DataUpdateCoordinator for PowerOcean.

This module defines two coordinator classes:

- `PowerOceanCoordinator`: Periodic HTTP polling for PowerOcean inverter devices.
- `PowerGlowCoordinator`: MQTT push-based coordinator for standalone PowerGlow
  heating rods. Instead of polling, it receives real-time data via MQTT and
  calls async_set_updated_data() to notify HA entities.

Usage:
  # HTTP polling (PowerOcean):
  coordinator = PowerOceanCoordinator(hass, api, update_interval=timedelta(seconds=30))
  await coordinator.async_config_entry_first_refresh()

  # MQTT push (PowerGlow standalone):
  coordinator = PowerGlowCoordinator(hass, api)
  await coordinator.async_config_entry_first_refresh()
  await coordinator.async_setup_mqtt(mqtt_credentials)
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    LOGGER,
)
from .ecoflow import HAEcoflowApi
from .parser import EcoflowParser

if TYPE_CHECKING:
    from .mqtt import PowerGlowMqttClient

# Parameters routed via cmd_id=1 (HEATING_ROD_PARAM_REPORT set command).
# These are sent once per user action via _publish_params().
# NOTE: targetTemp is intentionally absent here — it is routed via cmd_id=16 field3
# (encode_powerglow_cmd16_settemp) which works in both Manuell and Smart Modus.
# cmd_id=1 field6 is silently ignored by the EcoFlow cloud in Smart Modus.
_CMD1_PARAMS = frozenset({"keepWarmTemp"})
# NOTE: mode is handled specially — see async_send_command mode routing block.
#   mode=8 (Smart):   cmd_id=16 field2=4 — activates Smart Modus (confirmed 2026-06-21).
#                     iOS pdata=1004. Device ACK field25=8 when Smart aktiv.
#   mode=6 (Manuell): cmd_id=16 field2=2 — exits Smart → Manueller Modus (confirmed 2026-06-21).
#                     iOS pdata=1002. Device ACK field25=6 when Manuell aktiv.
# WICHTIG: field25=6 = Manueller Modus, field25=8 = Smart Modus.
# field37 ist IMMER 2 und ist KEIN Modus-Indikator!
#   Solarmodus (1) removed — protocol unknown, cloud silently ignores cmd_id=1 field37=1.
# In all cases, _desired_state["mode"] is updated so the entity shows the correct value
# even though the device firmware always reports field37=2 in every cmd_id=1 report.
# NOTE: smartTargetTemp is routed via cmd_id=16 field5 (encode_powerglow_cmd16_smart_settemp).
# cmd_id=1 field38 for smartTargetTemp was SILENTLY IGNORED by the EcoFlow cloud.
# NOTE: targetPower is routed via cmd_id=16 field4 (encode_powerglow_cmd16_setpower).
# cmd_id=1 field7 for targetPower was SILENTLY IGNORED by the EcoFlow cloud.
# Confirmed 2026-06-21: iOS app sends field4=7000 for 3500W, field4=0 for 0W (W×2 encoding).

# Coordinator-data key prefix for HEATING_ROD_PARAM_REPORT fields.
# Full key format: f"{sn}_{_REPORT_SUFFIX}_{param_name}"
_REPORT_SUFFIX = "HEATING_ROD_PARAM_REPORT"

# Coordinator-data key for the device's current operating mode.
# Built as: f"{sn}_HEATING_ROD_PARAM_REPORT_mode"  (matches parse_mqtt_payload output)
_MODE_REPORT_SUFFIX = "HEATING_ROD_PARAM_REPORT_mode"

# Mode constants (field25 values, confirmed 2026-06-21)
# field25=6 = Manueller Modus, field25=8 = Smart Modus
# field37 is ALWAYS 2 and is NOT the mode indicator!
_MODE_SMART   = 8   # field25=8 → Smart Modus aktiv
_MODE_MANUELL = 6   # field25=6 → Manueller Modus (device default)


class PowerOceanCoordinator(DataUpdateCoordinator[dict[str, float | int | str]]):
    """Coordinate periodic fetching and parsing of PowerOcean sensor values."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: HAEcoflowApi,
        update_interval: timedelta,
        powerglow_only: bool = False,
    ) -> None:
        """Initialize the PowerOcean data update coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name="PowerOcean",
            update_interval=update_interval,
        )
        self.api = api
        self.parser = EcoflowParser(
            variant=self.api.variant, sn=self.api.sn, powerglow_only=powerglow_only
        )

    async def _async_update_data(self) -> dict[str, float | int | str]:
        """Fetch live data via HTTP and parse sensor values."""
        response = await self.api.fetch_raw()
        return self.parser.parse_values(response)


class PowerGlowCoordinator(DataUpdateCoordinator[dict[str, float | int | str]]):
    """
    MQTT-based coordinator for a standalone EcoFlow PowerGlow heating rod.

    Unlike PowerOceanCoordinator, this coordinator does NOT poll HTTP.
    Live data arrives via MQTT push and is injected into HA via
    async_set_updated_data(). The coordinator starts with empty data
    and becomes populated once the first MQTT message is received.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: HAEcoflowApi,
    ) -> None:
        """Initialize the PowerGlow MQTT coordinator (no polling)."""
        super().__init__(
            hass,
            LOGGER,
            name="PowerGlow",
            update_interval=None,  # No periodic polling
        )
        self.api = api
        self.parser = EcoflowParser(
            variant=self.api.variant, sn=self.api.sn, powerglow_only=True
        )
        self._mqtt_client: PowerGlowMqttClient | None = None
        # Optimistic desired state: tracks what the user last commanded.
        # Keys: "runFlag" -> 0 or 1 (absent = no command sent yet this session).
        # The switch entity reads this first; raw MQTT runFlag is the fallback.
        # This allows the HA switch to stay ON when Smart Modus arms the device
        # but the tank is already above the target temperature (heatingPower==0),
        # so the user can still press OFF to explicitly disarm the schedule.
        self._desired_state: dict[str, Any] = {
            # "mode": None → show raw device value (field25)
            # After user selects a mode via HA, set optimistically to 6 or 8
            # until the device confirms via MQTT (then cleared/synced in _handle_mqtt_message).
            "mode": None,
        }
        # Tracks the last field25 value reported by the device (NOT from desired-state override).
        # field25=6 → Manueller Modus, field25=8 → Smart Modus.
        # Used by async_send_command guards to avoid redundant commands.
        self._actual_device_mode: int | None = None
        # Last run state reported by the device itself (for cmd_id=97 keepalive).
        self._reported_run_flag: int = 0
        self._persist_task: asyncio.Task | None = None
        self._pv_task: asyncio.Task | None = None
        self._pv_config: dict = {}
        # Aktuell gesetzter targetPower-Wert der PV-Rampenregelung (W).
        # Startet bei 0; wird von _pv_regulation_step schrittweise angepasst.
        self._pv_current_power: float = 0.0
        # Temperatursperre: True wenn Wasser-Solltemperatur erreicht.
        # Wird erst aufgehoben wenn temp <= (pv_max_temp - PV_TEMP_HYSTERESIS).
        self._pv_temp_blocked: bool = False
        # Zeitpunkt (loop.time()) des letzten Ein-/Ausschaltens durch die PV-Regelung
        # für Mindestlaufzeit / Mindestpause (gegen Takten).
        self._pv_last_switch: float = 0.0

    async def _async_update_data(self) -> dict[str, float | int | str]:
        """Return last known MQTT data (or empty dict on first call)."""
        # For MQTT devices, data comes from push - just return current state
        return self.data or {}

    async def async_setup_mqtt(self, mqtt_creds: dict) -> None:
        """
        Connect to the EcoFlow MQTT broker and start receiving PowerGlow data.

        Safe to call multiple times (e.g. on config entry reload) - any
        existing MQTT client and persist task are stopped first.

        Args:
            mqtt_creds: Dict from async_get_mqtt_credentials() with keys:
                url, port, certificateAccount, certificatePassword.

        """
        # Tear down any existing connection before re-initialising
        if self._persist_task is not None:
            self._persist_task.cancel()
            self._persist_task = None
        if self._mqtt_client is not None:
            old_client = self._mqtt_client
            self._mqtt_client = None
            await self.hass.async_add_executor_job(old_client.stop)

        from .mqtt import PowerGlowMqttClient

        user_id = self.api.user_id or "unknown"
        client_id = f"ANDROID_{uuid.uuid4().hex.upper()}_{user_id}"

        LOGGER.info(
            "PowerGlow MQTT setup: url=%s port=%s sn=%s",
            mqtt_creds.get("url"),
            mqtt_creds.get("port"),
            self.api.sn,
        )

        # tls_set() inside PowerGlowMqttClient.__init__ calls
        # ssl.SSLContext.load_default_certs() which is blocking I/O.
        # Run the entire client construction in a thread-pool executor
        # to keep the HA event loop unblocked.
        def _create_client() -> PowerGlowMqttClient:
            return PowerGlowMqttClient(
                hass=self.hass,
                url=mqtt_creds["url"],
                port=int(mqtt_creds["port"]),
                username=mqtt_creds["certificateAccount"],
                password=mqtt_creds["certificatePassword"],
                client_id=client_id,
                sn=self.api.sn,
                user_id=user_id,
                on_message=self._handle_mqtt_message,
            )

        self._mqtt_client = await self.hass.async_add_executor_job(_create_client)

        # Start background loop that keeps re-sending the desired state.
        # IMPORTANT: use asyncio.ensure_future() here, NOT hass.async_create_task().
        # hass.async_create_task() registers the task with HA's startup tracker,
        # causing "Setup timed out for bootstrap" warnings because _persist_loop
        # sleeps for 20 s before doing anything and never completes during startup.
        # ensure_future() creates a "fire-and-forget" task that HA does not wait for.
        self._persist_task = asyncio.ensure_future(self._persist_loop())

    async def async_teardown_mqtt(self) -> None:
        """
        Stop the MQTT client and background task cleanly.

        Called by async_unload_entry() in __init__.py when the config entry
        is unloaded or reloaded. Safe to call even if never connected.
        """
        if self._persist_task is not None:
            self._persist_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._persist_task
            self._persist_task = None
        if self._pv_task is not None:
            self._pv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pv_task
            self._pv_task = None
        if self._mqtt_client is not None:
            client = self._mqtt_client
            self._mqtt_client = None
            await self.hass.async_add_executor_job(client.stop)
        LOGGER.info("PowerGlow %s: MQTT teardown complete", self.api.sn)

    # How often to send cmd_id=17 (full status query).
    # The EcoFlow cloud/app sends this roughly every 5 min, but the device's
    # autonomous cmd_id=1 reports are NOT guaranteed without an active app
    # session, so we poll more often.
    _STATUS_POLL_INTERVAL_S: float = 60.0

    # How often to send cmd_id=97 (app keepalive).
    # While the EcoFlow app is open it sends cmd_id=97 every ~3 s. The device
    # only streams live telemetry (cmd_id=33/34: heatingPower, hrPwr, temp)
    # while it sees these keepalives. Without them the live values freeze
    # until someone opens the app again.
    _KEEPALIVE_INTERVAL_S: float = 10.0

    async def _persist_loop(self) -> None:
        """
        Background task: keep the device's live-data stream alive.

        - cmd_id=97 keepalive every _KEEPALIVE_INTERVAL_S seconds
          (emulates an open EcoFlow app session -> device keeps streaming)
        - cmd_id=17 full status query every _STATUS_POLL_INTERVAL_S seconds
          (re-syncs mode/targetTemp/targetPower/heatingPower)

        Each iteration is wrapped in try/except: this task is created via
        ensure_future(), so an uncaught exception would kill it silently.
        """
        # Short initial delay so subscriptions are confirmed by the broker
        await asyncio.sleep(3.0)
        loop = asyncio.get_running_loop()
        next_status = 0.0

        while True:
            try:
                await self.async_send_keepalive()
                now = loop.time()
                if now >= next_status:
                    await self.async_request_status()
                    next_status = now + self._STATUS_POLL_INTERVAL_S
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception(
                    "PowerGlow %s: Fehler im Keepalive-/Status-Loop", self.api.sn
                )
            await asyncio.sleep(self._KEEPALIVE_INTERVAL_S)

    async def async_send_keepalive(self) -> None:
        """
        Send cmd_id=97 keepalive, mirroring the device's own run state.

        field2 carries the run state exactly like the EcoFlow cloud does
        (1 = heating, absent = idle). We mirror the value the DEVICE last
        reported (not the optimistic HA switch state) so the keepalive never
        acts as a control command.
        """
        if self._mqtt_client is None:
            return

        from .proto import encode_powerglow_cmd97

        raw = encode_powerglow_cmd97(self.api.sn, run_flag=self._reported_run_flag)
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def async_request_status(self) -> None:
        """
        Send cmd_id=17 to request an immediate full status report from the device.

        The device responds with a cmd_id=1 (HEATING_ROD_PARAM_REPORT) containing
        the current mode, targetTemp, targetPower, heatingPower, runFlag, etc.
        This is used on startup and periodically to synchronise HA state with
        changes made in the EcoFlow app without waiting for the next autonomous
        60-second report.
        """
        if self._mqtt_client is None:
            return

        from .proto import encode_powerglow_cmd17

        raw = encode_powerglow_cmd17(self.api.sn)
        LOGGER.debug(
            "PowerGlow %s: cmd_id=17 Statusabfrage gesendet (%d bytes)",
            self.api.sn, len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def async_send_command(self, params: dict[str, Any]) -> None:
        """
        Send a control command to the PowerGlow via MQTT.

        Command routing:
          runFlag (0/1)     ->  cmd_id=16 field1  (real EcoFlow ON/OFF command, confirmed working)
          targetTemp        ->  cmd_id=16 field3  (N=temp×2; works in Manuell AND Smart Modus)
                                  Confirmed 2026-06-21: field3=68→34°C, field3=120→60°C.
                                  cmd_id=1 field6 is silently ignored by the cloud in Smart Modus.
          smartTargetTemp   ->  cmd_id=16 field5 (N=temp×2; Smart Modus Zieltemperatur)
                                  Confirmed 2026-06-21: field5=60→30°C, field5=120→60°C.
                                  cmd_id=1 field38 was silently IGNORED by the EcoFlow cloud!
          keepWarmTemp      ->  cmd_id=1  (field 4 float; comfort setpoint, purpose TBD)
          targetPower       ->  cmd_id=16 field4 (N=watts×2; Zielleistung)
                                  Confirmed 2026-06-21: field4=7000→3500W, field4=0→0W.
                                  cmd_id=1 field7 was SILENTLY IGNORED by the EcoFlow cloud!
          mode              ->  cmd_id=1  (HEATING_ROD_PARAM_REPORT field 37, varint)

        Smart Modus auto-switch (REMOVED):
          If the device is in Smart Modus (mode=2) and the user changes
          targetTemp or targetPower, the EcoFlow cloud will immediately
          override these values with the schedule's temperature.  To work
          around this, we automatically send mode=0 (Manuell) first, wait
          briefly, then send the parameter change.  This gives the cloud time
          to switch modes before accepting the new value.

        runFlag is stored in _desired_state so the switch entity reflects the last
        user intent even when heatingPower==0 (Smart Modus armed, not heating).

        Args:
            params: Dict of parameter names -> values.
        """
        # --- runFlag: ON/OFF via cmd_id=16 -----------------------------------
        if "runFlag" in params:
            run_flag = int(bool(params["runFlag"]))
            # Persist user intent so the switch stays in the correct state.
            self._desired_state["runFlag"] = run_flag
            LOGGER.info(
                "PowerGlow %s: Switch gedruckt - sende cmd_id=16 run_flag=%d (einmalig)",
                self.api.sn, run_flag,
            )
            # Rampe zurücksetzen wenn Heizstab manuell ausgeschaltet wird,
            # damit beim nächsten Einschalten wieder sanft von 0 W hochgefahren wird.
            if run_flag == 0:
                self._pv_current_power = 0.0
            await self._publish_cmd16(run_flag)

        # --- targetTemp: via cmd_id=16 field3 (works in Manuell AND Smart Modus) ---
        if "targetTemp" in params:
            temp = float(params["targetTemp"])
            LOGGER.info(
                "PowerGlow %s: Zieltemperatur → %.1f°C via cmd_id=16 field3 (raw=%d)",
                self.api.sn, temp, round(temp * 2),
            )
            await self._publish_cmd16_settemp(temp)

        # --- mode: Betriebsmodus ---
        # field25=6 = Manueller Modus, field25=8 = Smart Modus
        # Confirmed 2026-06-21 from full iOS log analysis:
        #   Smart:   cmd_id=16 field2=4  (pdata=1004) → device ACK {field1:8, field25:8}
        #   Manuell: cmd_id=16 field2=2  (pdata=1002) → device ACK {field1:6, field25:6}
        # WRONG ASSUMPTION CORRECTED: field37 is ALWAYS 2 and is NOT the mode indicator!
        if "mode" in params:
            mode_val = int(params["mode"])
            mode_name = {6: "Manueller Modus", 8: "Smart Modus"}.get(mode_val, f"unbekannt ({mode_val})")
            LOGGER.info(
                "PowerGlow %s: Betriebsmodus → field25=%d (%s) [_actual=%s]",
                self.api.sn, mode_val, mode_name, self._actual_device_mode,
            )
            # Persist optimistic display
            self._desired_state["mode"] = mode_val

            if mode_val == 8:
                # Smart Modus: cmd_id=16 {field2: 4}
                if self._actual_device_mode == 8:
                    LOGGER.debug(
                        "PowerGlow %s: Smart Modus bereits aktiv (field25=8) – kein Befehl",
                        self.api.sn,
                    )
                else:
                    from .proto import encode_powerglow_cmd16_set_smart
                    raw = encode_powerglow_cmd16_set_smart(self.api.sn)
                    LOGGER.info(
                        "PowerGlow %s: Smart Modus → cmd_id=16 field2=4 (%d bytes) pdata=1004",
                        self.api.sn, len(raw),
                    )
                    self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

            elif mode_val == 6:
                # Manueller Modus: cmd_id=16 {field2: 2}
                # Confirmed 2026-06-21: iOS sends field2=2 when switching Smart→Manuell.
                # Device ACK {field1:6, field25:6} confirms return to Manuell.
                if self._actual_device_mode == 6:
                    LOGGER.debug(
                        "PowerGlow %s: Manueller Modus bereits aktiv (field25=6) – kein Befehl",
                        self.api.sn,
                    )
                else:
                    from .proto import encode_powerglow_cmd16_set_manuell
                    raw = encode_powerglow_cmd16_set_manuell(self.api.sn)
                    LOGGER.info(
                        "PowerGlow %s: Manueller Modus → cmd_id=16 field2=2 (%d bytes) pdata=1002",
                        self.api.sn, len(raw),
                    )
                    self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

        # --- targetPower: via cmd_id=16 field4 (Zielleistung) ---
        if "targetPower" in params:
            power_w = float(params["targetPower"])
            LOGGER.info(
                "PowerGlow %s: Zielleistung → %.0f W via cmd_id=16 field4 (raw=%d)",
                self.api.sn, power_w, round(power_w * 2),
            )
            await self._publish_cmd16_setpower(power_w)

        # --- smartTargetTemp: via cmd_id=16 field5 (Smart Modus Zieltemperatur) ---
        if "smartTargetTemp" in params:
            smart_temp = float(params["smartTargetTemp"])
            LOGGER.info(
                "PowerGlow %s: Smart-Modus-Zieltemperatur → %.1f°C via cmd_id=16 field5 (raw=%d)",
                self.api.sn, smart_temp, round(smart_temp * 2),
            )
            await self._publish_cmd16_smart_settemp(smart_temp)

        # --- Smart Schedule Slot 1: Temperatur (field5) und Zeit (field6) ---
        if "scheduleSlot1Temp" in params:
            temp = float(params["scheduleSlot1Temp"])
            from .proto import encode_powerglow_cmd16_schedule_slot1_temp
            raw = encode_powerglow_cmd16_schedule_slot1_temp(temp, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: Zeitplan Slot-1 Temp → %.1f°C (field5=%d)",
                self.api.sn, temp, round(temp * 2),
            )
            await self._publish_raw(raw)

        if "scheduleSlot1Time" in params:
            mins = int(params["scheduleSlot1Time"])  # minutes from midnight
            hour, minute = divmod(mins, 60)
            from .proto import encode_powerglow_cmd16_schedule_slot1_time
            raw = encode_powerglow_cmd16_schedule_slot1_time(hour, minute, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: Zeitplan Slot-1 Zeit → %02d:%02d (field6=%d)",
                self.api.sn, hour, minute, mins * 2,
            )
            await self._publish_raw(raw)

        # --- Smart Schedule Slot 2: Temperatur (field7) und Zeit (field8) ---
        if "scheduleSlot2Temp" in params:
            temp = float(params["scheduleSlot2Temp"])
            from .proto import encode_powerglow_cmd16_schedule_slot2_temp
            raw = encode_powerglow_cmd16_schedule_slot2_temp(temp, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: Zeitplan Slot-2 Temp → %.1f°C (field7=%d)",
                self.api.sn, temp, round(temp * 2),
            )
            await self._publish_raw(raw)

        if "scheduleSlot2Time" in params:
            mins = int(params["scheduleSlot2Time"])  # minutes from midnight
            hour, minute = divmod(mins, 60)
            from .proto import encode_powerglow_cmd16_schedule_slot2_time
            raw = encode_powerglow_cmd16_schedule_slot2_time(hour, minute, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: Zeitplan Slot-2 Zeit → %02d:%02d (field8=%d)",
                self.api.sn, hour, minute, mins * 2,
            )
            await self._publish_raw(raw)

        # --- targetPower / mode: via cmd_id=1 -------------------
        param_fields = {k: v for k, v in params.items() if k in _CMD1_PARAMS}
        if param_fields:
            # Only keepWarmTemp goes via cmd_id=1; all other params use cmd_id=16.
            LOGGER.info(
                "PowerGlow %s: Parameter-Set via cmd_id=1: %s",
                self.api.sn, param_fields,
            )
            await self._publish_params(param_fields)

        # --- Tankvolumen: cmd_id=16 field9 = Liter × 2 (bestätigt 2026-06-21) ---
        if "tankVolume" in params:
            liters = int(params["tankVolume"])
            from .proto import encode_powerglow_cmd16_tank_volume
            raw = encode_powerglow_cmd16_tank_volume(liters, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: Tankvolumen → %d L (field9=%d)",
                self.api.sn, liters, liters * 2,
            )
            await self._publish_raw(raw)

        # --- Tanktyp: cmd_id=16 field14 = 0 (Wasser) / 2 (Puffer) (bestätigt 2026-06-21) ---
        if "tankType" in params:
            tank_type = int(params["tankType"])
            from .proto import encode_powerglow_cmd16_tank_type
            raw = encode_powerglow_cmd16_tank_type(tank_type, self.api.sn)
            type_name = "Puffertank (80°C)" if tank_type == 0 else "Wassertank (60°C)"
            LOGGER.info(
                "PowerGlow %s: Tanktyp → %s (field14=%d)",
                self.api.sn, type_name, tank_type,
            )
            await self._publish_raw(raw)

        # --- Unbekannte Parameter protokollieren ------------------------------
        _KNOWN_PARAMS = _CMD1_PARAMS | {
            "runFlag", "targetTemp", "smartTargetTemp", "targetPower", "mode",
            "scheduleSlot1Temp", "scheduleSlot1Time",
            "scheduleSlot2Temp", "scheduleSlot2Time",
            "tankVolume", "tankType",
        }
        unknown = {k: v for k, v in params.items() if k not in _KNOWN_PARAMS}
        if unknown:
            LOGGER.warning(
                "PowerGlow %s: unbekannte params %s - ignoriert",
                self.api.sn, unknown,
            )

    async def _clear_schedule(self) -> None:
        """
        Erase all three Smart Modus schedule slots on the device.

        The PowerGlow stores a weekly heating schedule that it re-activates
        every ~60 s, overriding manual cmd_func=2 commands.  Sending all
        three schedule slots (1, 2, 3) with 24 zero-bytes of data via
        cmd_func=254 / cmd_id=32 tells the firmware to discard the schedule.

        This is called automatically when the user selects mode=0 (Manuell).
        """
        if self._mqtt_client is None:
            return

        from .proto import encode_powerglow_clear_schedule

        for slot in (1, 2, 3):
            raw = encode_powerglow_clear_schedule(slot, self.api.sn)
            LOGGER.info(
                "PowerGlow %s: clearing schedule slot %d via cmd_func=254 (%d bytes)",
                self.api.sn, slot, len(raw),
            )
            self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_cmd16(self, run_flag: int) -> None:
        """
        Send cmd_id=16 - the real EcoFlow cloud ON/OFF control command.

        Discovered from log analysis: the EcoFlow iOS app sends cmd_id=16 with
        pdata={field1:0} to START heating, and the cloud sends pdata={field1:?}
        to STOP.  This is the only command the device actually obeys for ON/OFF
        while in Smart Modus.  Our earlier cmd_id=1 {runFlag:1} approach was
        being completely ignored by the device firmware.

        Args:
            run_flag: 1 = turn ON, 0 = turn OFF.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot send cmd16 - MQTT client not connected",
                self.api.sn,
            )
            return

        from .proto import encode_powerglow_cmd16

        raw = encode_powerglow_cmd16(run_flag, self.api.sn)
        LOGGER.info(
            "PowerGlow %s: cmd_id=16 (ON/OFF) run_flag=%d (%d bytes)",
            self.api.sn, run_flag, len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_cmd16_settemp(self, temp_celsius: float) -> None:
        """
        Send cmd_id=16 {field3: N} to set the target temperature.

        This is the command the EcoFlow cloud uses to update targetTemp.
        Works in both Manueller Modus and Smart Modus.
        N = round(temp_celsius * 2)  (half-degree unit encoding).

        Confirmed 2026-06-21: field3=120 → 60°C, device echoes field6=120 in next report.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot send cmd16_settemp - MQTT client not connected",
                self.api.sn,
            )
            return

        from .proto import encode_powerglow_cmd16_settemp

        raw = encode_powerglow_cmd16_settemp(temp_celsius, self.api.sn)
        LOGGER.info(
            "PowerGlow %s: cmd_id=16 field3 Zieltemperatur=%.1f°C raw=%d (%d bytes)",
            self.api.sn, temp_celsius, round(temp_celsius * 2), len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_cmd16_setpower(self, watts: float) -> None:
        """
        Send cmd_id=16 {field4: N} to set the target power (Zielleistung).

        Confirmed 2026-06-21: field4=7000 → 3500W, field4=0 → 0W (W×2 encoding).
        Device echoes the new value as field7 in the next cmd_id=1 report,
        which our parser decodes as targetPower = field7 // 2.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot send cmd16_setpower - MQTT client not connected",
                self.api.sn,
            )
            return

        from .proto import encode_powerglow_cmd16_setpower

        raw = encode_powerglow_cmd16_setpower(watts, self.api.sn)
        LOGGER.info(
            "PowerGlow %s: cmd_id=16 field4 Zielleistung=%.0fW raw=%d (%d bytes)",
            self.api.sn, watts, round(watts * 2), len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_cmd16_smart_settemp(self, temp_celsius: float) -> None:
        """
        Send cmd_id=16 {field5: N} to set the Smart Modus target temperature.

        Confirmed 2026-06-21: field5=60 → 30°C, field5=120 → 60°C.
        Device echoes the new value as field8 in the next cmd_id=1 report,
        which our parser decodes as smartTargetTemp = field8 ÷ 2.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot send cmd16_smart_settemp - MQTT client not connected",
                self.api.sn,
            )
            return

        from .proto import encode_powerglow_cmd16_smart_settemp

        raw = encode_powerglow_cmd16_smart_settemp(temp_celsius, self.api.sn)
        LOGGER.info(
            "PowerGlow %s: cmd_id=16 field5 Smart-Zieltemperatur=%.1f°C raw=%d (%d bytes)",
            self.api.sn, temp_celsius, round(temp_celsius * 2), len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_raw(self, raw: bytes) -> None:
        """
        Publish a pre-encoded protobuf payload to the device set-topic.

        Shared helper used by all schedule slot encoders to avoid repeating
        the MQTT-client-None check in every caller.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot publish - MQTT client not connected",
                self.api.sn,
            )
            return
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _publish_params(self, params: dict[str, Any]) -> None:
        """
        Encode and publish parameter changes (targetPower / mode)
        to the device via cmd_id=1.

        This method handles only the cmd_id=1 HEATING_ROD_PARAM_REPORT SET
        command for numeric/mode parameters.  ON/OFF control is handled
        separately by _publish_cmd16().

        NOTE: This is called ONCE per user action - it is NOT called from the
        persist loop, to avoid conflicting with the cloud's Smart Modus settings.
        """
        if self._mqtt_client is None:
            LOGGER.error(
                "PowerGlow %s: cannot send params - MQTT client not connected",
                self.api.sn,
            )
            return

        from .proto import encode_powerglow_command

        raw = encode_powerglow_command(params, self.api.sn)
        if raw is None:
            LOGGER.warning(
                "PowerGlow %s: no settable fields in params=%s - cmd_id=1 dropped",
                self.api.sn, params,
            )
            return

        LOGGER.info(
            "PowerGlow %s: cmd_id=1 params=%s (%d bytes)",
            self.api.sn, params, len(raw),
        )
        self._mqtt_client.publish_raw(self._mqtt_client._set_topic, raw)

    async def _handle_mqtt_message(self, payload: dict) -> None:
        """Process an incoming MQTT message."""
        parsed = self.parser.parse_mqtt_payload(payload)
        new_data: dict[str, Any] = dict(self.data or {})
        new_data.update(parsed)

        sn = self.api.sn

        # Decoded protobuf data arrives wrapped as {"params": {...}} (see mqtt.py);
        # JSON payloads may be flat. Read raw device values from the right level.
        raw_params = payload.get("params")
        if not isinstance(raw_params, dict):
            raw_params = payload

        reported_run = raw_params.get("runFlag")
        if reported_run is not None:
            with contextlib.suppress(ValueError, TypeError):
                self._reported_run_flag = 1 if int(reported_run) else 0

        heating_power = raw_params.get("heatingPower")
        if heating_power is not None:
            try:
                if float(heating_power) > 0:
                    self._desired_state["runFlag"] = 1
            except (ValueError, TypeError):
                pass

        desired_run = self._desired_state.get("runFlag")
        if desired_run is not None:
            new_data[f"{sn}_{_REPORT_SUFFIX}_runFlag"] = desired_run

        # --- Mode sync (field25) -------------------------------------------
        # The device reports mode as field25 (parsed as "mode" by proto.py).
        # When confirmed, update _actual_device_mode and clear the optimistic
        # _desired_state["mode"] so entities reflect the real device state.
        reported_mode = raw_params.get("mode")
        if reported_mode is not None:
            try:
                mode_int = int(reported_mode)
                self._actual_device_mode = mode_int
                # Clear optimistic override once device confirms the mode
                if self._desired_state.get("mode") == mode_int:
                    self._desired_state["mode"] = None
            except (ValueError, TypeError):
                pass

        # Apply optimistic mode override to prefixed coordinator key
        desired_mode = self._desired_state.get("mode")
        if desired_mode is not None:
            new_data[f"{sn}_{_REPORT_SUFFIX}_mode"] = desired_mode

        # Notify all HA entities with merged data
        self.async_set_updated_data(new_data)

    # ------------------------------------------------------------------
    # PV-Überschuss-Regelung
    # ------------------------------------------------------------------

    async def async_setup_pv_regulation(self, options: dict) -> None:
        """
        Konfiguriere und starte die PV-Überschuss-Regelung.

        Wird nach async_setup_mqtt() aufgerufen. Der _pv_config wird immer
        befüllt (auch wenn deaktiviert), damit async_pv_start() über den
        Betriebsmodus-Select jederzeit greifen kann.

        Args:
            options: config_entry.options-Dict mit CONF_PV_* Schlüsseln.
        """
        from .const import (
            CONF_PV_CHECK_INTERVAL,
            CONF_PV_ENABLED,
            CONF_PV_MAX_POWER,
            CONF_PV_MAX_TEMP,
            CONF_PV_MIN_SURPLUS,
            CONF_PV_POWER_SENSOR,
            CONF_PV_MIN_OFF_TIME,
            CONF_PV_MIN_ON_TIME,
            CONF_PV_RAMP_STEP,
            CONF_PV_SENSOR_TYPE,
            CONF_PV_STARTUP_DELAY,
            PV_DEFAULT_MIN_OFF_TIME,
            PV_DEFAULT_MIN_ON_TIME,
            PV_DEFAULT_SENSOR_TYPE,
            PV_DEFAULT_CHECK_INTERVAL,
            PV_DEFAULT_MAX_POWER,
            PV_DEFAULT_MAX_TEMP,
            PV_DEFAULT_MIN_SURPLUS,
            PV_DEFAULT_RAMP_STEP,
            PV_DEFAULT_STARTUP_DELAY,
        )

        # Bestehenden Task stoppen
        if self._pv_task is not None:
            self._pv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pv_task
            self._pv_task = None

        sensor = options.get(CONF_PV_POWER_SENSOR, "")

        # _pv_config immer befüllen (auch wenn deaktiviert) damit async_pv_start() funktioniert
        if sensor:
            self._pv_config = {
                "sensor": sensor,
                "min_surplus": int(options.get(CONF_PV_MIN_SURPLUS, PV_DEFAULT_MIN_SURPLUS)),
                # max_power kann als String ("3500") aus dem Select-Selector kommen
                "max_power": int(options.get(CONF_PV_MAX_POWER, PV_DEFAULT_MAX_POWER)),
                "ramp_step": int(options.get(CONF_PV_RAMP_STEP, PV_DEFAULT_RAMP_STEP)),
                "check_interval": float(options.get(CONF_PV_CHECK_INTERVAL, PV_DEFAULT_CHECK_INTERVAL)),
                "startup_delay": float(options.get(CONF_PV_STARTUP_DELAY, PV_DEFAULT_STARTUP_DELAY)),
                "max_temp": int(options.get(CONF_PV_MAX_TEMP, PV_DEFAULT_MAX_TEMP)),
                "sensor_type": str(options.get(CONF_PV_SENSOR_TYPE, PV_DEFAULT_SENSOR_TYPE)),
                "min_on_time": float(options.get(CONF_PV_MIN_ON_TIME, PV_DEFAULT_MIN_ON_TIME)),
                "min_off_time": float(options.get(CONF_PV_MIN_OFF_TIME, PV_DEFAULT_MIN_OFF_TIME)),
            }
        else:
            self._pv_config = {}

        # Temperatursperre beim Neustart der Regelung zurücksetzen
        self._pv_temp_blocked = False

        pv_enabled = options.get(CONF_PV_ENABLED, False)
        if not pv_enabled:
            LOGGER.debug("PowerGlow %s: PV-Überschuss deaktiviert", self.api.sn)
            return

        if not sensor:
            LOGGER.warning(
                "PowerGlow %s: PV-Überschuss aktiviert aber kein Sensor konfiguriert",
                self.api.sn,
            )
            return

        LOGGER.info(
            "PowerGlow %s: PV-Überschuss aktiv | sensor=%s (%s, ein>=%.0fs aus>=%.0fs) min=%dW max=%dW rampe=%dW/%.0fs delay=%.0fs max_temp=%d°C",
            self.api.sn,
            self._pv_config["sensor"],
            self._pv_config["sensor_type"],
            self._pv_config["min_on_time"],
            self._pv_config["min_off_time"],
            self._pv_config["min_surplus"],
            self._pv_config["max_power"],
            self._pv_config["ramp_step"],
            self._pv_config["check_interval"],
            self._pv_config["startup_delay"],
            self._pv_config["max_temp"],
        )

        # fire-and-forget: HA soll nicht auf diesen Task beim Start warten
        self._pv_task = asyncio.ensure_future(self._pv_regulation_loop())

    async def _pv_regulation_loop(self) -> None:
        """
        Hintergrund-Loop: prüft den PV-Überschuss-Sensor im konfigurierten
        Intervall und regelt die Heizleistung des PowerGlow entsprechend.
        """
        startup_delay = self._pv_config.get("startup_delay", 30.0)
        interval = self._pv_config.get("check_interval", 60.0)

        # Wartezeit nach HA-Start damit MQTT-Verbindung und States bereit sind
        await asyncio.sleep(startup_delay)

        while True:
            try:
                await self._pv_regulation_step()
            except Exception:
                LOGGER.exception(
                    "PowerGlow %s: Fehler in PV-Regelungsschritt", self.api.sn
                )
            await asyncio.sleep(interval)

    async def _pv_regulation_step(self) -> None:
        """
        Einen PV-Regelungsschritt ausführen: Sensor lesen, Leistung sanft anpassen.

        Die Leistung wird pro Schritt um maximal PV_RAMP_STEP_W (100 W) erhöht
        oder gesenkt. Der Schritt wird im konfigurierten check_interval aufgerufen
        (z. B. alle 10 s), sodass sich die Rampe mit 100 W / Intervall ergibt.
        """
        from .const import PV_TEMP_HYSTERESIS

        sensor_entity: str = self._pv_config.get("sensor", "")
        min_surplus: int = self._pv_config.get("min_surplus", 200)
        max_power: int = self._pv_config.get("max_power", 3500)
        _PV_RAMP_STEP_W: int = self._pv_config.get("ramp_step", 100)  # W pro Schritt (konfigurierbar)
        max_temp: int = self._pv_config.get("max_temp", 60)

        if not sensor_entity:
            return

        state = self.hass.states.get(sensor_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            LOGGER.debug(
                "PowerGlow %s: PV-Sensor %s nicht verfügbar (state=%s)",
                self.api.sn, sensor_entity, state.state if state else "None",
            )
            return

        try:
            sensor_w = float(state.state)
        except (ValueError, TypeError):
            LOGGER.debug(
                "PowerGlow %s: PV-Sensor ungültiger Wert: %s", self.api.sn, state.state
            )
            return

        # ── Verfügbare Leistung je nach Sensor-Typ ─────────────────────────────
        # Bei Netz-Sensoren enthält der Messwert die Leistung des Heizstabs
        # bereits (Einspeisung sinkt / Bezug steigt, sobald er heizt). Ohne
        # Rückrechnung schaltet sich die Regelung selbst wieder aus (Takten).
        from .const import PV_SENSOR_TYPE_GRID_EXPORT, PV_SENSOR_TYPE_GRID_IMPORT
        sensor_type = self._pv_config.get("sensor_type", "surplus")
        if sensor_type == PV_SENSOR_TYPE_GRID_EXPORT:
            surplus_w = self._pv_current_power + sensor_w
        elif sensor_type == PV_SENSOR_TYPE_GRID_IMPORT:
            surplus_w = self._pv_current_power - sensor_w
        else:
            surplus_w = sensor_w

        # ── Temperatursperre: Heizung pausieren wenn Solltemperatur erreicht ────────────────
        temp_key = f"{self.api.sn}_HEATING_ROD_PARAM_REPORT_temp"
        current_temp: float | None = None
        if self.data:
            raw = self.data.get(temp_key)
            try:
                current_temp = float(raw) if raw is not None else None
            except (ValueError, TypeError):
                current_temp = None

        if current_temp is not None:
            if not self._pv_temp_blocked and current_temp >= max_temp:
                # Temperatur erreicht → Sperre aktivieren
                self._pv_temp_blocked = True
                LOGGER.info(
                    "PowerGlow %s: PV-Sperre aktiv (%.1f°C >= %d°C) – warte auf %.1f°C",
                    self.api.sn, current_temp, max_temp, float(max_temp - PV_TEMP_HYSTERESIS),
                )
            elif self._pv_temp_blocked and current_temp <= (max_temp - PV_TEMP_HYSTERESIS):
                # Temperatur unter Einschaltdifferenz → Sperre aufheben
                self._pv_temp_blocked = False
                LOGGER.info(
                    "PowerGlow %s: PV-Sperre aufgehoben (%.1f°C <= %d°C) – Regelung fortgesetzt",
                    self.api.sn, current_temp, max_temp - PV_TEMP_HYSTERESIS,
                )

        if self._pv_temp_blocked:
                      # Sperre aktiv → Leistung auf 0 rampen
            desired_w = 0.0
        # ── Zielleistung aus PV-Überschuss berechnen ────────────────────────────────────────
        elif surplus_w < min_surplus:
            # Zu wenig Überschuss → Ziel 0 W (sanft runterrampen, nicht sofort)
            desired_w = 0.0
        else:
            desired_w = float(min(surplus_w, max_power))
            # Abrunden (nicht runden): verhindert, dass die Regelung durch
            # Aufrunden in den Netzbezug läuft und dann wieder zurückregelt.
            desired_w = (desired_w // _PV_RAMP_STEP_W) * _PV_RAMP_STEP_W

        # ── Mindestlaufzeit / Mindestpause (gegen Takten) ──────────────────────
        now = asyncio.get_running_loop().time()
        since_switch = now - self._pv_last_switch
        is_on = self._pv_current_power > 0
        if not self._pv_temp_blocked:
            min_on = self._pv_config.get("min_on_time", 60.0)
            min_off = self._pv_config.get("min_off_time", 60.0)
            if not is_on and desired_w > 0 and since_switch < min_off:
                LOGGER.debug(
                    "PowerGlow %s: PV-Einschalten verzögert (Mindestpause %.0f/%.0fs)",
                    self.api.sn, since_switch, min_off,
                )
                return
            if is_on and desired_w <= 0 and since_switch < min_on:
                LOGGER.debug(
                    "PowerGlow %s: PV-Ausschalten verzögert (Mindestlaufzeit %.0f/%.0fs)",
                    self.api.sn, since_switch, min_on,
                )
                desired_w = min(self._pv_current_power, float(_PV_RAMP_STEP_W))

        # Heizstab einschalten falls noch aus und Leistung > 0 geplant —
        # Leistungssteuerung erfolgt ausschließlich über targetPower.
        if desired_w > 0 and self._desired_state.get("runFlag") != 1:
            LOGGER.info(
                "PowerGlow %s: PV-Regelung aktiviert Heizstab (Leistung per targetPower geregelt)",
                self.api.sn,
            )
            await self.async_send_command({"runFlag": 1})

        # ── Rampe: hoch sanft (±ramp_step), runter sofort bei Netzbezug ───────
        # Wenn der Überschuss kleiner als die aktuelle Leistung ist, bezieht der
        # Heizstab bereits Netzstrom → direkt auf das Ziel absenken statt in
        # 200-W-Schritten (sonst minutenlanger Netzbezug bei Wolken).
        delta = desired_w - self._pv_current_power
        if abs(delta) <= _PV_RAMP_STEP_W:
            new_power = desired_w
        elif delta > 0:
            new_power = self._pv_current_power + _PV_RAMP_STEP_W
        elif surplus_w < self._pv_current_power:
            new_power = desired_w
        else:
            new_power = self._pv_current_power - _PV_RAMP_STEP_W

        # Auf Geräte-Limits begrenzen
        new_power = max(0.0, min(float(max_power), new_power))

        # Nur senden wenn sich die Leistung tatsächlich ändert
        if new_power != self._pv_current_power:
            if (new_power > 0) != is_on:
                self._pv_last_switch = now
            LOGGER.debug(
                "PowerGlow %s: PV-Rampe sensor=%.0fW surplus=%.0fW desired=%.0fW current=%.0fW new=%.0fW delta=%.0fW",
                self.api.sn,
                sensor_w,
                surplus_w,
                desired_w,
                self._pv_current_power,
                new_power,
                new_power - self._pv_current_power,
            )
            await self.async_send_command({'targetPower': new_power})
            self._pv_current_power = new_power
