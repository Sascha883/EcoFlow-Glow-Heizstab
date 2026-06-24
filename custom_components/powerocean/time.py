"""
Time platform for PowerOcean PowerGlow heating rod.

Provides two time entities for the Smart Schedule (Zeitplan) slots.
Each slot has an independent switch-on time (HH:MM) and a matching
temperature number entity in number.py.

Protocol confirmed 2026-06-21 from iOS MQTT set-topic log:
  Slot 1 time  ->  cmd_id=16 field6 = minutes_from_midnight * 2
  Slot 2 time  ->  cmd_id=16 field8 = minutes_from_midnight * 2

Examples:
  10:28  ->  field6 = (10*60+28)*2 = 628*2 = 1256
  20:28  ->  field8 = (20*60+28)*2 = 1228*2 = 2456
"""

from __future__ import annotations

import datetime

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import PowerGlowCoordinator
from .utils import ReportMode

_REPORT = ReportMode.HEATING_ROD.value

# State keys where the device echoes schedule times back (via cmd_id=1 REPORT).
# field6 in the REPORT = scheduleTime1 (raw ÷ 2 = minutes)
# field8 in the REPORT = scheduleTime2 (raw ÷ 2 = minutes)
_SLOT1_TIME_REPORT_KEY = "scheduleTime1"   # parsed from REPORT field6
_SLOT2_TIME_REPORT_KEY = "scheduleTime2"   # parsed from REPORT field8

# Minutes-from-midnight param names used by coordinator.async_send_command
_SLOT1_TIME_PARAM = "scheduleSlot1Time"
_SLOT2_TIME_PARAM = "scheduleSlot2Time"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerGlow time entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: PowerGlowCoordinator = data["coordinator"]
    endpoints = data["endpoints"]

    powerglow_devices: dict[str, DeviceInfo] = {}
    for ep in endpoints.values():
        if _REPORT in ep.internal_unique_id and ep.device_info is not None:
            powerglow_devices[ep.serial] = ep.device_info

    entities: list[PowerGlowScheduleTime] = []
    for sn, device_info in powerglow_devices.items():
        entities.append(PowerGlowScheduleTime(
            coordinator, sn, device_info,
            slot=1,
            param=_SLOT1_TIME_PARAM,
            state_key_suffix=_SLOT1_TIME_REPORT_KEY,
            name="Zeitplan Timer 1",
            unique_id_suffix="schedule_slot1_time",
        ))
        entities.append(PowerGlowScheduleTime(
            coordinator, sn, device_info,
            slot=2,
            param=_SLOT2_TIME_PARAM,
            state_key_suffix=_SLOT2_TIME_REPORT_KEY,
            name="Zeitplan Timer 2",
            unique_id_suffix="schedule_slot2_time",
        ))

    if entities:
        async_add_entities(entities)
    else:
        LOGGER.debug("No PowerGlow device found – skipping time setup")


class PowerGlowScheduleTime(CoordinatorEntity[PowerGlowCoordinator], TimeEntity):
    """
    Time entity for one Smart-Schedule slot switch-on time.

    Displays and sets the time at which the PowerGlow starts heating in
    Smart Modus for that slot.  The value is stored as a datetime.time.

    Sending is done via coordinator.async_send_command which routes
    scheduleSlot1Time / scheduleSlot2Time to the appropriate cmd_id=16
    field encoder.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
        *,
        slot: int,
        param: str,
        state_key_suffix: str,
        name: str,
        unique_id_suffix: str,
    ) -> None:
        """Initialize the schedule time entity."""
        super().__init__(coordinator)
        self._sn = sn
        self._param = param
        self._state_key = f"{sn}_{_REPORT}_{state_key_suffix}"
        self._attr_unique_id = f"{sn}_{unique_id_suffix}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._slot = slot

    @property
    def native_value(self) -> datetime.time | None:
        """
        Return the current schedule time from coordinator data.

        The device reports scheduleTime1/2 as total minutes from midnight.
        We convert to a datetime.time for HA's time component.
        """
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            mins = int(val)
            return datetime.time(hour=mins // 60, minute=mins % 60)
        except (ValueError, TypeError, OverflowError):
            return None

    async def async_set_value(self, value: datetime.time) -> None:
        """Send the selected time to the device."""
        mins = value.hour * 60 + value.minute
        LOGGER.debug(
            "PowerGlow %s: Zeitplan Slot %d Zeit → %02d:%02d (%d min)",
            self._sn, self._slot, value.hour, value.minute, mins,
        )
        await self.coordinator.async_send_command({self._param: mins})
