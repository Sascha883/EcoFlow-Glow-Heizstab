"""
PowerOcean integration for Home Assistant.

This module sets up the PowerOcean integration, handling:

- Initialization of the integration.
- Authentication and device setup via EcoFlow API.
- Migration of old config entries to new structure.
- Creation of a DataUpdateCoordinator for periodic polling.
- Registration in the Home Assistant device registry.
- Forwarding entry setups to sensor/platform components.
- Reloading the config entry when options are updated.

Functions:
- async_setup: Set up the integration and log basic info.
- async_setup_entry: Set up a PowerOcean device from a config entry.
- async_migrate_entry: Migrate older config entries to the latest version.
- async_unload_entry: Unload a config entry and clean up resources.
- update_listener: Reload a config entry when its options are updated.
"""

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_FRIENDLY_NAME,
    CONF_MODEL_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    IntegrationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import (
    CONF_POWERGLOW_ONLY,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MODEL_NAME_MAP,
    PLATFORMS,
    POWERGLOW_STANDALONE_MODELS,
    PowerOceanModel,
)
from .coordinator import PowerGlowCoordinator, PowerOceanCoordinator
from .ecoflow import HAEcoflowApi
from .parser import EcoflowParser

# PowerGlow standalone: SENSOR + SWITCH + NUMBER + SELECT + TIME.
_POWERGLOW_PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.TIME,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """
    Set up the PowerOcean integration.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        config (ConfigType): The configuration dictionary.

    Returns:
        bool: True if setup was successful, False otherwise.

    """
    try:
        # Integration laden
        integration = await async_get_integration(hass, DOMAIN)

        # Zugriff auf manifest.json-Inhalte
        manifest = integration.manifest
        name = manifest.get("name", DOMAIN)
        version = manifest.get("version", "unknown")
        requirements = manifest.get("requirements", [])

        LOGGER.debug(
            "Loading %s v%s (requirements: %s)",
            name,
            version,
            requirements,
        )

    except KeyError as err:
        LOGGER.error("Missing expected key in integration manifest: %s", err)
        return False
    except HomeAssistantError as err:
        LOGGER.error("Home Assistant error during PowerOcean setup: %s", err)
        return False
    except Exception:  # optional fallback, nur loggen
        LOGGER.exception("Unexpected error loading PowerOcean integration")
        raise  # Fehler weiterwerfen, damit HA korrekt reagiert

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PowerOcean from a config entry."""
    options = dict(entry.options)
    updated = False

    # Migration fehlender Optionen
    if CONF_SCAN_INTERVAL not in options:
        options[CONF_SCAN_INTERVAL] = entry.data.get("options", {}).get(
            "scan_interval", DEFAULT_SCAN_INTERVAL
        )
        updated = True

    if CONF_FRIENDLY_NAME not in options:
        options[CONF_FRIENDLY_NAME] = entry.data.get("options", {}).get(
            "friendly_name", DEFAULT_NAME
        )
        updated = True

    if updated:
        hass.config_entries.async_update_entry(entry, options=options)
        LOGGER.debug("Migrated missing options for %s: %s", entry.title, options)

    # --- EcoFlow API initialisieren ---
    device_id = entry.data.get(CONF_DEVICE_ID)
    model_id = entry.data.get(CONF_MODEL_ID)
    if not device_id or not model_id:
        msg = "Missing device_id or model_id in config entry"
        raise ConfigEntryNotReady(msg)

    api = HAEcoflowApi(
        hass,
        device_id,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        model_id,
    )

    # --- Authentifizieren ---
    try:
        await api.async_authorize()
    except ConfigEntryNotReady:
        # Netzwerkproblem -> Setup wird retryt
        raise
    except IntegrationError as e:
        # Auth-Fehler oder unerwartete API-Probleme -> Setup schlaegt fehl
        LOGGER.error("Failed to authenticate EcoFlow device %s: %s", entry.title, e)
        return False

    # --- Struktur & Parser ---
    # Eigenstaendiger PowerGlow -> powerglow_only immer aktiviert,
    # unabhaengig von der Optionseinstellung
    model = PowerOceanModel(model_id)
    powerglow_only = (
        True
        if model in POWERGLOW_STANDALONE_MODELS
        else options.get(CONF_POWERGLOW_ONLY, False)
    )

    if model in POWERGLOW_STANDALONE_MODELS:
        # ----------------------------------------------------------------
        # PowerGlow standalone: MQTT-basierter Datenpfad
        # ----------------------------------------------------------------
        # Vordefinierte Endpoints - Werte kommen via MQTT, nicht HTTP
        parser = EcoflowParser(variant=api.variant, sn=api.sn, powerglow_only=True)
        endpoints = parser.create_powerglow_endpoints()

        coordinator: PowerOceanCoordinator | PowerGlowCoordinator = PowerGlowCoordinator(
            hass=hass, api=api
        )
        await coordinator.async_config_entry_first_refresh()

        # MQTT-Verbindung aufbauen
        try:
            mqtt_creds = await api.async_get_mqtt_credentials()
            await coordinator.async_setup_mqtt(mqtt_creds)  # type: ignore[attr-defined]
        except Exception as mqtt_err:
            LOGGER.warning(
                "PowerGlow MQTT setup failed (device still added, no live data): %s",
                mqtt_err,
            )

        # PV-Überschuss-Regelung starten (nur wenn konfiguriert)
        if isinstance(coordinator, PowerGlowCoordinator):
            await coordinator.async_setup_pv_regulation(options)

        active_platforms = _POWERGLOW_PLATFORMS
    else:
        # ----------------------------------------------------------------
        # PowerOcean: HTTP-Polling wie bisher
        # ----------------------------------------------------------------
        raw = await api.fetch_raw()
        parser = EcoflowParser(variant=api.variant, sn=api.sn, powerglow_only=powerglow_only)
        endpoints = parser.parse_structure(raw)

        scan_interval = timedelta(
            seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        coordinator = PowerOceanCoordinator(
            hass=hass, api=api, update_interval=scan_interval, powerglow_only=powerglow_only
        )
        await coordinator.async_config_entry_first_refresh()

        active_platforms = PLATFORMS  # voller Plattformumfang fuer PowerOcean

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "endpoints": endpoints,
        "platforms": active_platforms,
    }

    # --- Sensor-Plattformen laden ---
    await hass.config_entries.async_forward_entry_setups(entry, active_platforms)

    model_name = MODEL_NAME_MAP[PowerOceanModel(model_id)]
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_id)},
        manufacturer="EcoFlow",
        serial_number=device_id,
        name=options.get(CONF_FRIENDLY_NAME),
        model=model_name,
        model_id=model_id,
        configuration_url="https://user-portal.ecoflow.com/",
    )

    # Listener fuer Optionsaenderungen
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to new structure."""
    version = entry.version
    data = dict(entry.data)
    options = dict(entry.options)

    if version < 2:
        # ALT -> NEU
        if "user_input" in data:
            old = data.pop("user_input")

            data.update(
                {
                    CONF_DEVICE_ID: old.get(CONF_DEVICE_ID),
                    CONF_EMAIL: old.get(CONF_EMAIL),
                    CONF_PASSWORD: old.get(CONF_PASSWORD),
                    CONF_MODEL_ID: old.get(CONF_MODEL_ID),
                }
            )

            # Optionen auslagern
            options.setdefault(
                CONF_FRIENDLY_NAME,
                old.get(CONF_FRIENDLY_NAME, DEFAULT_NAME),
            )
        version = 2

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=version,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and clean up resources."""
    # Stop MQTT client and background task if this is a PowerGlow entry
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    if coordinator is not None and hasattr(coordinator, "async_teardown_mqtt"):
        await coordinator.async_teardown_mqtt()

    # Use the same platform list that was registered during setup
    active_platforms = entry_data.get("platforms", PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, active_platforms)
    if not unload_ok:
        LOGGER.warning("Failed to unload platforms for %s", entry.entry_id)
        return False

    # Remove from hass.data
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload theconfig_entry when options are updated."""
    LOGGER.debug("Reloading PowerOcean entry %s due to options change", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
