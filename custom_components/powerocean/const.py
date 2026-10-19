"""Constants for the PowerOcean integration."""

from enum import StrEnum
from logging import Logger, getLogger
from pathlib import Path

from homeassistant.const import Platform

LOGGER: Logger = getLogger(__package__)
DOMAIN = "powerocean"
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_NAME = "PowerOcean"
ISSUE_URL = "https://github.com/Sascha883/EcoFlow-PowerGlow/issues"
ISSUE_URL_ERROR_MESSAGE = " Please log any issues here: " + ISSUE_URL
USE_MOCKED_RESPONSE = False  # Set to True to use mocked responses for testing
# Mock path to response.json file
MOCKED_RESPONSE = (
    Path(__file__).parent / "tests" / "fixtures" / "response_modified.json"
)
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.TIME,
]
ATTR_PRODUCT_DESCRIPTION = "Product Description"
ATTR_PRODUCT_SERIAL = "Vendor Product Serial"
CONF_POWERGLOW_ONLY = "powerglow_only"


class PowerOceanModel(StrEnum):
    """Supported EcoFlow device model identifiers."""

    POWEROCEAN = "83"
    POWEROCEAN_DC_FIT = "85"
    POWEROCEAN_SINGLE_PHASE = "86"
    POWEROCEAN_PLUS = "87"
    POWERGLOW = "88"


MODEL_NAME_MAP: dict[PowerOceanModel, str] = {
    PowerOceanModel.POWEROCEAN: "PowerOcean",
    PowerOceanModel.POWEROCEAN_DC_FIT: "PowerOcean DC Fit",
    PowerOceanModel.POWEROCEAN_SINGLE_PHASE: "PowerOcean Single Phase",
    PowerOceanModel.POWEROCEAN_PLUS: "PowerOcean Plus",
    PowerOceanModel.POWERGLOW: "PowerGlow",
}

# Models that use MQTT push (standalone mode, no HTTP polling)
POWERGLOW_STANDALONE_MODELS: frozenset[PowerOceanModel] = frozenset({
    PowerOceanModel.POWERGLOW,
})

# --- PV-Überschuss-Konfigurationsschlüssel ---
CONF_PV_POWER_SENSOR = "pv_power_sensor"
CONF_PV_MIN_SURPLUS = "pv_min_surplus"
CONF_PV_MAX_POWER = "pv_max_power"
CONF_PV_ENABLED = "pv_enabled"
CONF_PV_CHECK_INTERVAL = "pv_check_interval"
CONF_PV_STARTUP_DELAY = "pv_startup_delay"
CONF_PV_RAMP_STEP = "pv_ramp_step"
CONF_PV_MAX_TEMP = "pv_max_temp"

# Auswahlmöglichkeiten Maximale Heizleistung (W)
PV_MAX_POWER_OPTIONS: list[int] = [3500, 6000, 9000]

PV_DEFAULT_MIN_SURPLUS: int = 200    # W - Mindestüberschuss zum Aktivieren
PV_DEFAULT_MAX_POWER: int = 3500     # W - Maximale Heizleistung (Standard: 3,5 kW)
PV_MAX_POWER: int = 9000             # W - Absolutes Gerätemaximum
PV_DEFAULT_CHECK_INTERVAL: int = 10  # s - Rampen-Intervall
PV_DEFAULT_STARTUP_DELAY: int = 30   # s - Anlaufverzögerung nach HA-Start
PV_DEFAULT_RAMP_STEP: int = 100      # W - Leistungsänderung pro Rampen-Schritt
PV_DEFAULT_MAX_TEMP: int = 60        # °C - Abschalt-Temperatur PV-Betrieb
CONF_PV_SENSOR_TYPE = "pv_sensor_type"
CONF_PV_MIN_ON_TIME = "pv_min_on_time"
CONF_PV_MIN_OFF_TIME = "pv_min_off_time"
# Sensor-Typen für die PV-Regelung:
#  surplus     – Sensor liefert bereits den verfügbaren Überschuss (inkl. Heizstab) → Ziel = Sensor
#  grid_export – Netzleistung, positiv = Einspeisung (z. B. GoodWe active_power) → Ziel = Heizstab + Sensor
#  grid_import – Netzleistung, positiv = Bezug (z. B. Shelly 3EM)               → Ziel = Heizstab − Sensor
PV_SENSOR_TYPE_SURPLUS = "surplus"
PV_SENSOR_TYPE_GRID_EXPORT = "grid_export"
PV_SENSOR_TYPE_GRID_IMPORT = "grid_import"
PV_DEFAULT_SENSOR_TYPE = PV_SENSOR_TYPE_SURPLUS   # rückwärtskompatibel
PV_DEFAULT_MIN_ON_TIME: int = 60     # s - Mindestlaufzeit nach dem Einschalten
PV_DEFAULT_MIN_OFF_TIME: int = 60    # s - Mindestpause nach dem Ausschalten
PV_TEMP_HYSTERESIS: int = 5          # K  - Einschaltdifferenz (Wiedereinschalten bei max_temp - 5K)
