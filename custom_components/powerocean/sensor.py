"""
PowerOcean sensor integration for Home Assistant.

This module defines the setup and management of PowerOcean sensor entities,
including data fetching, entity registration, and periodic updates.
"""

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_PV_POWER_SENSOR,
    DOMAIN,
)
from .coordinator import PowerGlowCoordinator, PowerOceanCoordinator
from .types import PowerOceanEndPoint
from .utils import ReportMode


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """
    Set up PowerOcean sensors for a config entry.

    This function is called by Home Assistant when a config entry
    is set up. It creates sensor entities for each endpoint and
    registers them with Home Assistant.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry to set up.
        async_add_entities: Callback to add entities to Home Assistant.

    Returns:
        None

    """
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    endpoints = data["endpoints"]

    entities: list[PowerOceanSensor | PowerGlowPvSurplusSensor] = [
        PowerOceanSensor(coordinator, endpoint) for endpoint in endpoints.values()
    ]

    # PV-Überschuss-Proxy-Sensor: nur für PowerGlow, wenn ein PV-Sensor konfiguriert ist
    if isinstance(coordinator, PowerGlowCoordinator):
        pv_sensor_id: str = entry.options.get(CONF_PV_POWER_SENSOR, "")
        if pv_sensor_id:
            _report = ReportMode.HEATING_ROD.value
            powerglow_devices: dict[str, DeviceInfo] = {
                ep.serial: ep.device_info
                for ep in endpoints.values()
                if _report in ep.internal_unique_id and ep.device_info is not None
            }
            for sn, device_info in powerglow_devices.items():
                entities.append(
                    PowerGlowPvSurplusSensor(coordinator, sn, device_info, pv_sensor_id)
                )

    async_add_entities(entities)


class PowerOceanSensor(CoordinatorEntity, SensorEntity):
    """Representation of a PowerOcean Sensor using DataUpdateCoordinator."""

    def __init__(
        self, coordinator: PowerOceanCoordinator, endpoint: PowerOceanEndPoint
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.endpoint = endpoint
        self._endpoint_id = endpoint.internal_unique_id
        self._endpoint_name = endpoint.name
        self._endpoint_friendly_name = endpoint.friendly_name
        self._endpoint_icon = endpoint.icon
        self._endpoint_description = endpoint.description
        self._endpoint_serial = endpoint.serial
        self._endpoint_device_info = endpoint.device_info

        (
            self._attr_device_class,
            self._attr_native_unit_of_measurement,
            self._attr_state_class,
        ) = getattr(endpoint, "cls", None) or (None, None, None)

        # HA attributes
        self._attr_has_entity_name = True
        self._attr_unique_id = self._endpoint_id
        self._attr_name = self._endpoint_friendly_name
        self._attr_icon = self._endpoint_icon
        if self._attr_native_unit_of_measurement is None:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Any:
        """Return the current value of the sensor from coordinator."""
        return self.coordinator.data.get(self._endpoint_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for this sensor."""
        attr = {}
        if self._endpoint_description:
            attr["product_description"] = self._endpoint_description
        if self._endpoint_serial:
            attr["product_serial"] = self._endpoint_serial
        return attr

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info for Home Assistant."""
        return self._endpoint_device_info


class PowerGlowPvSurplusSensor(CoordinatorEntity[PowerGlowCoordinator], SensorEntity):
    """
    Proxy-Sensor der den konfigurierten PV-Überschuss-Sensor spiegelt.

    Zeigt den aktuellen Wert des in der Integration konfigurierten
    PV-Überschuss-Sensors unter einem festen Entity-ID im PowerGlow-Gerät an.
    Aktualisiert sich automatisch bei jeder Zustandsänderung der Quell-Entität.
    """

    _attr_has_entity_name = True
    _attr_name = "PV-Überschuss"
    _attr_icon = "mdi:solar-power-variant"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: PowerGlowCoordinator,
        sn: str,
        device_info: DeviceInfo,
        pv_sensor_id: str,
    ) -> None:
        """Initialize the PV surplus proxy sensor."""
        super().__init__(coordinator)
        self._pv_sensor_id = pv_sensor_id
        self._attr_unique_id = f"{sn}_pv_surplus_proxy"
        self._attr_device_info = device_info
        self._unsub_state_change: Any = None

    async def async_added_to_hass(self) -> None:
        """Zustandsänderungen des Quell-Sensors abonnieren."""
        await super().async_added_to_hass()
        self._unsub_state_change = async_track_state_change_event(
            self.hass,
            [self._pv_sensor_id],
            self._handle_pv_state_change,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Abonnement beim Entfernen beenden."""
        if self._unsub_state_change is not None:
            self._unsub_state_change()
            self._unsub_state_change = None

    @callback
    def _handle_pv_state_change(self, event: Any) -> None:  # noqa: ANN401
        """HA-State aktualisieren wenn sich der PV-Sensor ändert."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Aktuellen Wert des konfigurierten PV-Sensors zurückgeben."""
        state = self.hass.states.get(self._pv_sensor_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Verfügbar wenn die Quell-Entität existiert und verfügbar ist."""
        state = self.hass.states.get(self._pv_sensor_id)
        return state is not None and state.state not in ("unavailable", "unknown")

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Quell-Entität als Attribut ausgeben."""
        return {"source_entity": self._pv_sensor_id}
