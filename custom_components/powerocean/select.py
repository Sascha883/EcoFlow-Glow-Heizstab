"""
Select platform for PowerOcean PowerGlow heating rod.

Provides selectable entities for:
  - Betriebsmodus (Manueller Modus / Smart Modus)
  - Tankvolumen  (300 L / 400 L / 500 L / Anpassen)
  - Tanktyp      (Wassertank 60°C / Puffertank 80°C)

Mode values confirmed 2026-06-21 from live MQTT log (field25):
  field25=6 – Manueller Modus  Device default. Activated via cmd_id=16 field2=2.
  field25=8 – Smart Modus      Schedule-based. Activated via cmd_id=16 field2=4.

Tank volume (confirmed 2026-06-21):
  cmd_id=16 field9 = Liter * 2
  300 L → field9=600,  400 L → field9=800,  500 L → field9=1000

Tank type (confirmed 2026-06-21):
  cmd_id=16 field14 = 0 (Wassertank, max 60°C) or 2 (Puffertank, max 80°C)

Note: field37 is ALWAYS 2 and is NOT the mode indicator — do not use it for mode detection.
Solarmodus removed — activation protocol unknown, silently ignored by EcoFlow cloud.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import PowerGlowCoordinator
from .utils import ReportMode

_REPORT = ReportMode.HEATING_ROD.value
_PARAM_MODE = "mode"

# Mapping: display label → field25 value (confirmed 2026-06-21 from full log analysis)
#   field25=6 = Manueller Modus (device default, ACK shows field1=6, field25=6)
#   field25=8 = Smart Modus aktiv (ACK shows field1=8, field25=8 after field2=4)
# NOTE: field37 was previously (incorrectly) used as mode — field37 is ALWAYS 2 and is NOT the mode!
# Solarmodus removed — activation protocol unknown.
POWERGLOW_MODES: dict[str, int] = {
    "Manueller Modus": 6,   # field25=6 — device default
    "Smart Modus":     8,   # field25=8 — active after cmd_id=16 field2=4
}

# Reverse mapping: field25 integer → display label
_MODE_REVERSE: dict[int, str] = {
    6: "Manueller Modus",   # field25=6 → Manuell
    8: "Smart Modus",       # field25=8 → Smart aktiv
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerGlow select entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: PowerGlowCoordinator = data["coordinator"]
    endpoints = data["endpoints"]

    # Find all PowerGlow device SNs + device_info
    powerglow_devices: dict[str, DeviceInfo] = {}
    for ep in endpoints.values():
        if _REPORT in ep.internal_unique_id and ep.device_info is not None:
            powerglow_devices[ep.serial] = ep.device_info

    entities: list[Any] = []
    for sn, device_info in powerglow_devices.items():
        entities.append(PowerGlowSelect(coordinator, sn, device_info))
        entities.append(PowerGlowTankVolumeSelect(coordinator, sn, device_info))
        entities.append(PowerGlowTankTypeSelect(coordinator, sn, device_info))

    if entities:
        async_add_entities(entities)
    else:
        LOGGER.debug("No PowerGlow device found – skipping select setup")


class PowerGlowSelect(CoordinatorEntity[PowerGlowCoordinator], SelectEntity):
    """
    Select entity for the PowerGlow operating mode.

    Maps to the ``mode`` parameter of HEATING_ROD_PARAM_REPORT.
    Available options: Manueller Modus (0), Smart Modus (2).
    Solarmodus removed — protocol unknown.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:tune"
    _attr_options: ClassVar[list[str]] = list(POWERGLOW_MODES.keys())

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the PowerGlow select entity."""
        super().__init__(coordinator)
        self._sn = sn
        self._state_key = f"{sn}_{_REPORT}_{_PARAM_MODE}"
        self._attr_unique_id = f"{sn}_{_PARAM_MODE}_select"
        self._attr_name = "Betriebsmodus"
        self._attr_device_info = device_info

    @property
    def current_option(self) -> str | None:
        """Return the currently active mode as a human-readable label."""
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            return _MODE_REVERSE.get(int(val))
        except (ValueError, TypeError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Send the selected mode to the device."""
        mode_value = POWERGLOW_MODES.get(option)
        if mode_value is None:
            LOGGER.warning("PowerGlow %s: unknown mode '%s'", self._sn, option)
            return
        LOGGER.debug(
            "PowerGlow %s: setting mode = %s (%s)", self._sn, mode_value, option
        )
        await self.coordinator.async_send_command({_PARAM_MODE: mode_value})


# ---------------------------------------------------------------------------
# Tank volume select
# ---------------------------------------------------------------------------

# Display label -> Liter (int).  "Anpassen" sends no command itself -
# the user then adjusts the companion NumberEntity "Tankvolumen (Anpassen)".
_TANK_VOLUME_OPTIONS: dict[str, int | None] = {
    "300 L":    300,
    "400 L":    400,
    "500 L":    500,
    "Anpassen": None,   # custom: use number entity
}

# Reverse map: liters -> label (only for the three fixed presets)
_TANK_VOLUME_REVERSE: dict[int, str] = {
    300: "300 L",
    400: "400 L",
    500: "500 L",
}


class PowerGlowTankVolumeSelect(CoordinatorEntity[PowerGlowCoordinator], SelectEntity):
    """
    Select entity for the PowerGlow tank volume.

    Offers three preset volumes (300 L / 400 L / 500 L) and an 'Anpassen'
    option.  When a preset is selected the command is sent immediately.
    When 'Anpassen' is selected, no command is sent -- the user should then
    enter the desired volume via the 'Tankvolumen (Anpassen)' number entity.

    Protocol confirmed 2026-06-21:
      cmd_id=16  field9 = liters * 2
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:storage-tank"
    _attr_options: ClassVar[list[str]] = list(_TANK_VOLUME_OPTIONS.keys())

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._state_key = f"{sn}_{_REPORT}_waterTankVolume"
        self._attr_unique_id = f"{sn}_tank_volume_select"
        self._attr_name = "Tankvolumen"
        self._attr_device_info = device_info

    @property
    def current_option(self) -> str | None:
        """Map current waterTankVolume (L) to label, or 'Anpassen' for custom."""
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            liters = int(val)
        except (ValueError, TypeError):
            return None
        return _TANK_VOLUME_REVERSE.get(liters, "Anpassen")

    async def async_select_option(self, option: str) -> None:
        """Send the tank volume command (preset only; 'Anpassen' is a no-op)."""
        liters = _TANK_VOLUME_OPTIONS.get(option)
        if liters is None:
            # 'Anpassen' selected -- user will set custom value via number entity
            LOGGER.debug(
                "PowerGlow %s: Tankvolumen 'Anpassen' ausgewaehlt - bitte Wert in "
                "'Tankvolumen (Anpassen)' eingeben.",
                self._sn,
            )
            return
        LOGGER.info(
            "PowerGlow %s: Tankvolumen -> %d L (field9=%d)",
            self._sn, liters, liters * 2,
        )
        await self.coordinator.async_send_command({"tankVolume": liters})


# ---------------------------------------------------------------------------
# Tank type select
# ---------------------------------------------------------------------------

# Display label -> field14 value (corrected 2026-06-22: 0=Puffertank, 2=Wassertank)
_TANK_TYPE_OPTIONS: dict[str, int] = {
    "Puffertank (80°C)": 0,   # field14=0, max 80 degC
    "Wassertank (60°C)": 2,   # field14=2, max 60 degC
}

_TANK_TYPE_REVERSE: dict[int, str] = {
    0: "Puffertank (80°C)",
    2: "Wassertank (60°C)",
}


class PowerGlowTankTypeSelect(CoordinatorEntity[PowerGlowCoordinator], SelectEntity):
    """
    Select entity for the PowerGlow tank type.

    Affects the device's maximum target temperature:
      Puffertank (80 degC) - buffer/heating circuit, max 80 degC (field14=0)
      Wassertank (60 degC) - potable water, max 60 degC (field14=2)

    Protocol corrected 2026-06-22 (user-confirmed against EcoFlow app):
      cmd_id=16  field14 = 0 (Puffertank) or 2 (Wassertank, max 60 degC)

    Protocol corrected 2026-06-22 (user-confirmed against EcoFlow app):
      cmd_id=16  field14 = 0 (Puffertank) or 2 (Wassertank)
      field41 in REPORT = 0 (Puffertank) or 2 (Wassertank)
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-thermometer"
    _attr_options: ClassVar[list[str]] = list(_TANK_TYPE_OPTIONS.keys())

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._state_key = f"{sn}_{_REPORT}_tankType"
        self._attr_unique_id = f"{sn}_tank_type_select"
        self._attr_name = "Typ Wassertank"
        self._attr_device_info = device_info

    @property
    def current_option(self) -> str | None:
        """Map current tankType raw value to label."""
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._state_key)
        if val is None:
            return None
        try:
            return _TANK_TYPE_REVERSE.get(int(val))
        except (ValueError, TypeError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Send the tank type command."""
        tank_type = _TANK_TYPE_OPTIONS.get(option)
        if tank_type is None:
            LOGGER.warning("PowerGlow %s: unknown tank type '%s'", self._sn, option)
            return
        LOGGER.info(
            "PowerGlow %s: Tanktyp -> %s (field14=%d)",
            self._sn, option, tank_type,
        )
        await self.coordinator.async_send_command({"tankType": tank_type})
