"""
Number platform for PowerOcean PowerGlow heating rod.

Provides adjustable number entities for:
- targetTemp  -- target water temperature (degC)
  Range 20-75 degC confirmed from EcoFlow app.
  ioBroker shows 0-90 degC as the protocol maximum.

- targetPower -- maximum heating power (W)
  Controls the power cap in PV-priority mode (mode=1).
  The PowerGlow 4 kW model heats at ~4000 W; ioBroker shows 0-10000 W.
  We cap at 4000 W to match the device rated power.

Debounce note:
  async_set_native_value() is called for every step when the user drags the
  slider or holds the +/- arrow.  Without debouncing this floods MQTT with one
  command per step.  A 0.8 s debounce timer is used: the command is sent only
  if no new value arrives within 0.8 s.  This keeps the UI responsive while
  reducing actual MQTT traffic to a single message per gesture.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import PowerGlowCoordinator
from .utils import ReportMode

_REPORT = ReportMode.HEATING_ROD.value

# Debounce delay: wait this long after the last slider step before sending MQTT.
_DEBOUNCE_S: float = 0.8


@dataclass(frozen=True)
class PowerGlowNumberDescription(NumberEntityDescription):
    """Extended description for PowerGlow number entities."""

    param_name: str = ""


POWERGLOW_NUMBER_DESCRIPTIONS: tuple[PowerGlowNumberDescription, ...] = (
    PowerGlowNumberDescription(
        key="targetTemp",
        name="Temperatur (Manuell)",
        icon="mdi:thermometer-water",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Field 6 (varint×2): Manueller Modus Eingestellte Temperatur.
        # Confirmed 2026-06-21: app shows 27°C → field6=54=27×2.
        # Range: 20-75 °C (EcoFlow app); protocol max 90 °C.
        # Step 1.0°C: EcoFlow app uses 1°C steps; 0.5°C works technically
        # (odd raw values like field3=93→46.5°C are accepted by the device)
        # but the app displays integer °C only, making half-degrees confusing.
        native_min_value=20.0,
        native_max_value=75.0,
        native_step=1.0,
        mode=NumberMode.BOX,
        param_name="targetTemp",
    ),
    PowerGlowNumberDescription(
        key="targetPower",
        name="Heizleistung",
        icon="mdi:lightning-bolt",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        # PowerGlow 4 kW rated power. Used as power cap in PV-priority mode.
        # ioBroker protocol maximum is 10000 W.
        native_min_value=0.0,
        native_max_value=4000.0,
        native_step=100.0,
        mode=NumberMode.SLIDER,
        param_name="targetPower",
    ),
    # --- Smart Schedule slot temperatures (confirmed 2026-06-21) ---
    # Sent via cmd_id=16 field5 (Slot 1) / field7 (Slot 2), ×2 encoding.
    # These correspond to the temperature each slot heats to when activated.
    PowerGlowNumberDescription(
        key="scheduleSlot1Temp",
        name="Temperatur Timer 1",
        icon="mdi:thermometer-chevron-up",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=20.0,
        native_max_value=75.0,
        native_step=1.0,
        mode=NumberMode.BOX,
        param_name="scheduleSlot1Temp",
    ),
    PowerGlowNumberDescription(
        key="scheduleSlot2Temp",
        name="Temperatur Timer 2",
        icon="mdi:thermometer-chevron-up",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=20.0,
        native_max_value=75.0,
        native_step=1.0,
        mode=NumberMode.BOX,
        param_name="scheduleSlot2Temp",
    ),
    # --- Tankvolumen (Anpassen) – confirmed 2026-06-21 ---
    # cmd_id=16 field9 = Liter * 2.
    # Reads current volume from waterTankVolume (decoded as litres in proto.py: field22 // 2).
    # Sending: user enters litres → coordinator calls encode_powerglow_cmd16_tank_volume(L)
    #          which sends field9 = L * 2  (e.g. 300 L → field9=600, 550 L → field9=1100).
    # Use this entity to set any non-preset volume (e.g. 350 L).
    # Presets (300 / 400 / 500 L) can also be set via the Tankvolumen select entity.
    PowerGlowNumberDescription(
        key="waterTankVolume",          # reads from REPORT (already decoded as litres)
        name="Tankvolumen (Anpassen)",
        icon="mdi:storage-tank",
        native_unit_of_measurement="L",
        # EcoFlow app allows tanks from ~50 L up to ~2000 L.
        # Range chosen conservatively; real device limits unknown.
        native_min_value=50.0,
        native_max_value=2000.0,
        native_step=1.0,
        mode=NumberMode.BOX,
        param_name="tankVolume",        # coordinator routes → encode × 2 → field9
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerGlow number entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: PowerGlowCoordinator = data["coordinator"]
    endpoints = data["endpoints"]

    # Find all PowerGlow device SNs + device_info
    powerglow_devices: dict[str, DeviceInfo] = {}
    for ep in endpoints.values():
        if _REPORT in ep.internal_unique_id and ep.device_info is not None:
            powerglow_devices[ep.serial] = ep.device_info

    entities: list[PowerGlowNumber] = []
    for sn, device_info in powerglow_devices.items():
        entities.extend(
            PowerGlowNumber(coordinator, sn, device_info, description)
            for description in POWERGLOW_NUMBER_DESCRIPTIONS
        )

    if entities:
        async_add_entities(entities)
    else:
        LOGGER.debug("No PowerGlow device found -- skipping number setup")


class PowerGlowNumber(CoordinatorEntity[PowerGlowCoordinator], NumberEntity):
    """
    Number entity for adjustable PowerGlow parameters.

    Supports targetTemp (degC) and targetPower (W).
    Commands are sent via cmd_id=1 (HEATING_ROD_PARAM_REPORT) with an 0.8 s
    debounce so that slider drags only produce a single MQTT message.

    Important: In Smart Modus (mode=8, field25=8) the EcoFlow cloud ignores targetTemp
    and targetPower changes via cmd_id=1.  Switch to 'Manueller Modus' in the
    EcoFlow app or via the Betriebsmodus select entity first.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
        description: PowerGlowNumberDescription,
    ) -> None:
        """Initialize the PowerGlow number entity."""
        super().__init__(coordinator)
        self._sn = sn
        self.entity_description = description
        self._state_key = f"{sn}_{_REPORT}_{description.key}"
        self._attr_unique_id = f"{sn}_{description.key}_number"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._debounce_task: asyncio.Task[None] | None = None

    @property
    def native_value(self) -> float | None:
        """Return the current value from coordinator data."""
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """
        Send the new value to the device via cmd_id=1 with debounce.

        If the user moves the slider quickly, each step calls this method.
        We cancel any pending send and restart the 0.8 s timer so that
        only the final resting value is transmitted.
        """
        param = self.entity_description.param_name

        # Cancel the previous pending send (slider still moving)
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
            LOGGER.debug(
                "PowerGlow %s: Debounce – vorheriger %s-Befehl verworfen, neuer Wert=%.1f",
                self._sn, param, value,
            )

        async def _debounced_send(v: float) -> None:
            await asyncio.sleep(_DEBOUNCE_S)
            LOGGER.debug(
                "PowerGlow %s: %s → %.1f – sende Befehl",
                self._sn, param, v,
            )
            try:
                await self.coordinator.async_send_command({param: v})
            except Exception as exc:
                LOGGER.error(
                    "PowerGlow %s: Fehler beim Senden %s=%.1f: %s",
                    self._sn, param, v, exc,
                )

        self._debounce_task = self.hass.async_create_task(
            _debounced_send(value)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending debounce task on entity removal."""
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None
