"""
Switch platform for PowerOcean PowerGlow heating rod.

Provides an on/off switch for the PowerGlow via the runFlag parameter.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import PowerGlowCoordinator
from .utils import ReportMode

# Parameter name on the EcoFlow device
_PARAM_RUN_FLAG = "runFlag"
_REPORT = ReportMode.HEATING_ROD.value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerGlow switch from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: PowerGlowCoordinator = data["coordinator"]
    endpoints = data["endpoints"]

    # Find all PowerGlow endpoints and collect their device SNs + device_info
    powerglow_devices: dict[str, DeviceInfo] = {}
    for ep in endpoints.values():
        if _REPORT in ep.internal_unique_id and ep.device_info is not None:
            powerglow_devices[ep.serial] = ep.device_info

    entities = []
    for sn, device_info in powerglow_devices.items():
        entities.append(PowerGlowSwitch(coordinator, sn, device_info))

    if entities:
        async_add_entities(entities)
    else:
        LOGGER.debug("No PowerGlow device found -- skipping switch setup")


class PowerGlowSwitch(CoordinatorEntity[PowerGlowCoordinator], SwitchEntity):
    """
    Switch entity for turning the PowerGlow heating rod on or off.

    State priority
    --------------
    1. coordinator._desired_state["runFlag"]  -- set when the user presses ON/OFF
       in HA, and also synced to 1 whenever the device reports heatingPower > 0.
       This keeps the switch pressable (shows ON) while Smart Modus is armed but
       the tank temperature is already above the target (heatingPower == 0).
       The user can then press OFF to explicitly disarm the schedule from HA.

    2. Raw device runFlag from MQTT data -- used as fallback only at startup,
       before the user has pressed anything this session.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-boiler"
    _attr_translation_key = "powerglow_switch"

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the PowerGlow switch."""
        super().__init__(coordinator)
        self._sn = sn
        self._state_key = f"{sn}_{_REPORT}_{_PARAM_RUN_FLAG}"
        self._attr_unique_id = f"{sn}_{_PARAM_RUN_FLAG}_switch"
        self._attr_name = "PowerGlow Ein/Aus"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        """Return True if the heating rod is running or was last commanded ON.

        Priority:
          1. _desired_state["runFlag"] -- the user's last intent, also synced to 1
             whenever the device actively heats (heatingPower > 0 from MQTT).
             Not reset to 0 on heatingPower == 0, so the switch stays ON while
             Smart Modus is armed but the tank is already warm enough.
          2. Raw device runFlag -- fallback at startup before first user action.
        """
        # 1. Prefer the optimistic desired state
        desired = self.coordinator._desired_state.get("runFlag")
        if desired is not None:
            return bool(desired)

        # 2. Fall back to raw device value on first startup
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            return int(val) == 1
        except (ValueError, TypeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the heating rod on."""
        data = self.coordinator.data or {}
        temp = data.get(self._sn + "_" + _REPORT + "_temp")
        target = data.get(self._sn + "_" + _REPORT + "_targetTemp")
        LOGGER.debug(
            "PowerGlow %s: EIN-Befehl aus HA | Speicher=%.1f C | Ziel=%.1f C",
            self._sn,
            temp if temp is not None else -1,
            target if target is not None else -1,
        )
        await self.coordinator.async_send_command({_PARAM_RUN_FLAG: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the heating rod off."""
        LOGGER.debug("PowerGlow %s: AUS-Befehl aus HA", self._sn)
        await self.coordinator.async_send_command({_PARAM_RUN_FLAG: 0})
