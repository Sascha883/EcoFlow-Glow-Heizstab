"""
Config flow for the PowerOcean integration.

This module defines the configuration flow and options flow for
adding and managing PowerOcean devices in Home Assistant.

Features:
- User authentication via EcoFlow API.
- Device model selection.
- Optional configuration for friendly name and scan interval.
- Support for reconfiguring existing entries.
- Sanitization of device names to ensure valid formatting.

Classes:
- PowerOceanConfigFlow: Handles the main configuration flow.
- PowerOceanOptionsFlow: Handles options for existing entries.

Functions:
- validate_input_for_device: Validates user input by attempting login.
- sanitize_device_name: Cleans and validates device names.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_FRIENDLY_NAME,
    CONF_MODEL_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, IntegrationError
from homeassistant.helpers.selector import selector

from .const import (
    CONF_POWERGLOW_ONLY,
    CONF_PV_CHECK_INTERVAL,
    CONF_PV_ENABLED,
    CONF_PV_MAX_POWER,
    CONF_PV_MAX_TEMP,
    CONF_PV_MIN_SURPLUS,
    CONF_PV_POWER_SENSOR,
    CONF_PV_RAMP_STEP,
    CONF_PV_STARTUP_DELAY,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MODEL_NAME_MAP,
    POWERGLOW_STANDALONE_MODELS,
    PV_DEFAULT_CHECK_INTERVAL,
    PV_DEFAULT_MAX_POWER,
    PV_DEFAULT_MAX_TEMP,
    PV_DEFAULT_MIN_SURPLUS,
    PV_DEFAULT_RAMP_STEP,
    PV_DEFAULT_STARTUP_DELAY,
    PV_MAX_POWER,
    PowerOceanModel,
)
from .ecoflow import AuthenticationFailedError, HAEcoflowApi

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Schema for the user step and device options step
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID, default=""): str,
        vol.Required(CONF_EMAIL, default=""): str,
        vol.Required(CONF_PASSWORD, default=""): str,
        vol.Required(CONF_MODEL_ID, default="83"): selector(
            {
                "select": {
                    "options": [
                        {"label": "PowerOcean", "value": "83"},
                        {"label": "PowerOcean DC Fit", "value": "85"},
                        {"label": "PowerOcean Single Phase", "value": "86"},
                        {"label": "PowerOcean Plus", "value": "87"},
                        {"label": "PowerGlow (Eigenständig)", "value": "88"},
                    ],
                    "mode": "dropdown",
                }
            }
        ),
    }
)

STEP_DEVICE_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRIENDLY_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): selector(
            {
                "number": {
                    "min": 10,
                    "max": 60,
                    "unit_of_measurement": "s",
                    "mode": "box",
                }
            }
        ),
        vol.Optional(CONF_POWERGLOW_ONLY, default=False): selector({"boolean": {}}),
    }
)


async def validate_input_for_device(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the user input allows us to connect."""
    api = HAEcoflowApi(
        hass,
        data[CONF_DEVICE_ID],
        data[CONF_EMAIL],
        data[CONF_PASSWORD],
        data[CONF_MODEL_ID],
    )

    try:
        # Check for authentication
        await api.async_authorize()
    except IntegrationError as err:
        LOGGER.exception("Failed to connect to PowerOcean device")
        msg = "cannot_connect"
        raise HomeAssistantError(msg) from err
    except AuthenticationFailedError as err:
        LOGGER.exception("Authentication failed")
        msg = "cannot_connect"
        raise HomeAssistantError(msg) from err


class PowerOceanConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Handle the configuration flow for adding a PowerOcean device to Home Assistant.

    This flow guides the user through:
      1. Authenticating with the EcoFlow API.
      2. Selecting the device model.
      3. Providing optional device settings (friendly name, scan interval).
    """

    VERSION = 2

    def __init__(self) -> None:
        """Instanzvariablen für den Flow-Verlauf."""
        self._cloud_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Leerzeichen aus Gerätedaten entfernen (häufig beim Kopieren entstanden)
            user_input[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID].strip()
            user_input[CONF_EMAIL] = user_input[CONF_EMAIL].strip()

            try:
                # 1. Validierung
                await validate_input_for_device(self.hass, user_input)

                # 2. Unique ID prüfen
                unique_id = f"PowerOcean {user_input[CONF_DEVICE_ID]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # 3. Daten für Schritt 2 zwischenspeichern
                self._cloud_data = user_input

                # 4. Weiter zu Schritt 2 (kein return async_create_entry!)
                return await self.async_step_device_options()
            except HomeAssistantError:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_device_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Schritt 2: Zusätzliche Optionen abfragen."""
        errors = {}
        if user_input is not None:
            model = PowerOceanModel(self._cloud_data[CONF_MODEL_ID])
            model_name = MODEL_NAME_MAP[model]
            friendly_name = sanitize_device_name(
                user_input[CONF_FRIENDLY_NAME],
                fall_back=DEFAULT_NAME,
            )
            # Bei eigenständigem PowerGlow automatisch powerglow_only aktivieren
            options = {
                **user_input,
                CONF_FRIENDLY_NAME: friendly_name,
            }
            if model in POWERGLOW_STANDALONE_MODELS:
                options[CONF_POWERGLOW_ONLY] = True

            return self.async_create_entry(
                title=model_name,
                data=self._cloud_data,
                options=options,
            )

        return self.async_show_form(
            step_id="device_options",
            data_schema=STEP_DEVICE_OPTIONS_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Standard-Reconfigure.

        Bestehende Instanz korrigieren (z.B. Passwort, Device ID, Model).
        """
        entry = self._get_reconfigure_entry()

        errors = {}

        if user_input is not None:
            # Leerzeichen entfernen
            user_input[CONF_DEVICE_ID] = user_input.get(CONF_DEVICE_ID, "").strip()
            user_input[CONF_EMAIL] = user_input.get(CONF_EMAIL, "").strip()
            merged = {**entry.data, **user_input}

            try:
                await validate_input_for_device(self.hass, merged)
                new_unique_id = f"PowerOcean {merged[CONF_DEVICE_ID]}"

                if entry.unique_id != new_unique_id:
                    await self.async_set_unique_id(new_unique_id)

                # Entry aktualisieren
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=merged,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfiguration_completed")
            except AuthenticationFailedError:
                errors["base"] = "invalid_auth"
            except IntegrationError:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._get_reconfigure_schema(entry),
            errors=errors,
        )

    def _get_reconfigure_schema(self, entry: ConfigEntry) -> vol.Schema:
        """Hilfsfunktion, um Schema für Reconfigure zu erstellen."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_ID, default=entry.data.get(CONF_DEVICE_ID, "")
                ): str,
                vol.Required(CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(
                    CONF_MODEL_ID, default=entry.data.get(CONF_MODEL_ID, "83")
                ): selector(
                    {
                        "select": {
                            "options": [
                                {"label": "PowerOcean", "value": "83"},
                                {"label": "PowerOcean DC Fit", "value": "85"},
                                {"label": "PowerOcean Single Phase", "value": "86"},
                                {"label": "PowerOcean Plus", "value": "87"},
                                {"label": "PowerGlow (Eigenständig)", "value": "88"},
                            ],
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """
        Return the options flow handler for this config entry.

        This method is called by Home Assistant to retrieve
        an OptionsFlow instance that allows the user to adjust
        settings for an existing PowerOcean integration entry.

        Args:
            config_entry (ConfigEntry): The configuration entry for which
                the options flow is requested.

        Returns:
            OptionsFlow: An instance of the PowerOceanOptionsFlow class.

        """
        return PowerOceanOptionsFlow()


class PowerOceanOptionsFlow(OptionsFlow):
    """Handle adjustable options for an existing PowerOcean config entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Hauptmenü: Grundeinstellungen | PV-Überschuss | Regellogik."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["grundeinstellungen", "pv_ueberschuss", "regellogik"],
        )

    async def async_step_grundeinstellungen(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Grundeinstellungen: Gerätename, Abrufintervall, Sensorfilter."""
        errors: dict[str, str] = {}
        if user_input is not None:
            new_options = dict(self.config_entry.options)
            new_options.update(user_input)
            return self.async_create_entry(title="", data=new_options)

        options = self.config_entry.options
        suggested = {
            CONF_FRIENDLY_NAME:  options.get(CONF_FRIENDLY_NAME,  DEFAULT_NAME),
            CONF_SCAN_INTERVAL:  options.get(CONF_SCAN_INTERVAL,  DEFAULT_SCAN_INTERVAL),
            CONF_POWERGLOW_ONLY: options.get(CONF_POWERGLOW_ONLY, False),
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_FRIENDLY_NAME): str,
                vol.Required(CONF_SCAN_INTERVAL): selector(
                    {
                        "number": {
                            "min": 10,
                            "max": 60,
                            "unit_of_measurement": "s",
                            "mode": "box",
                        }
                    }
                ),
                vol.Optional(CONF_POWERGLOW_ONLY): selector({"boolean": {}}),
            }
        )
        return self.async_show_form(
            step_id="grundeinstellungen",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            last_step=False,
        )

    async def async_step_pv_ueberschuss(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """PV-Überschuss: Aktivierung, Sensor, Leistungsauswahl, Mindestüberschuss."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # pv_max_power kommt als String aus dem Select-Selector → in int umwandeln
            if CONF_PV_MAX_POWER in user_input:
                user_input[CONF_PV_MAX_POWER] = int(user_input[CONF_PV_MAX_POWER])
            new_options = dict(self.config_entry.options)
            new_options.update(user_input)
            return self.async_create_entry(title="", data=new_options)

        options = self.config_entry.options
        current_max = str(options.get(CONF_PV_MAX_POWER, PV_DEFAULT_MAX_POWER))
        suggested = {
            CONF_PV_ENABLED:      options.get(CONF_PV_ENABLED,      False),
            CONF_PV_POWER_SENSOR: options.get(CONF_PV_POWER_SENSOR, None),
            CONF_PV_MAX_POWER:    current_max,
            CONF_PV_MIN_SURPLUS:  options.get(CONF_PV_MIN_SURPLUS,  PV_DEFAULT_MIN_SURPLUS),
            CONF_PV_MAX_TEMP:     options.get(CONF_PV_MAX_TEMP,     PV_DEFAULT_MAX_TEMP),
        }
        schema = vol.Schema(
            {
                vol.Optional(CONF_PV_ENABLED): selector({"boolean": {}}),
                vol.Optional(CONF_PV_POWER_SENSOR): selector(
                    {"entity": {"domain": "sensor", "device_class": "power"}}
                ),
                vol.Required(CONF_PV_MAX_POWER): selector(
                    {
                        "select": {
                            "options": [
                                {"label": "3,5 kW (3500 W)", "value": "3500"},
                                {"label": "6 kW (6000 W)",   "value": "6000"},
                                {"label": "9 kW (9000 W)",   "value": "9000"},
                            ],
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Required(CONF_PV_MIN_SURPLUS): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": PV_MAX_POWER,
                            "step": 50,
                            "unit_of_measurement": "W",
                            "mode": "box",
                        }
                    }
                ),
                vol.Required(CONF_PV_MAX_TEMP): selector(
                    {
                        "number": {
                            "min": 20,
                            "max": 75,
                            "step": 1,
                            "unit_of_measurement": "°C",
                            "mode": "slider",
                        }
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="pv_ueberschuss",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            last_step=False,
        )

    async def async_step_regellogik(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Regellogik: Prüfintervall und Anlaufverzögerung anpassen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            new_options = dict(self.config_entry.options)
            new_options.update(user_input)
            return self.async_create_entry(title="", data=new_options)

        options = self.config_entry.options
        suggested = {
            CONF_PV_RAMP_STEP:      options.get(CONF_PV_RAMP_STEP,      PV_DEFAULT_RAMP_STEP),
            CONF_PV_CHECK_INTERVAL: options.get(CONF_PV_CHECK_INTERVAL, PV_DEFAULT_CHECK_INTERVAL),
            CONF_PV_STARTUP_DELAY:  options.get(CONF_PV_STARTUP_DELAY,  PV_DEFAULT_STARTUP_DELAY),
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_PV_RAMP_STEP): selector(
                    {
                        "number": {
                            "min": 50,
                            "max": 500,
                            "step": 50,
                            "unit_of_measurement": "W",
                            "mode": "slider",
                        }
                    }
                ),
                vol.Required(CONF_PV_CHECK_INTERVAL): selector(
                    {
                        "number": {
                            "min": 5,
                            "max": 600,
                            "step": 5,
                            "unit_of_measurement": "s",
                            "mode": "box",
                        }
                    }
                ),
                vol.Required(CONF_PV_STARTUP_DELAY): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 300,
                            "step": 5,
                            "unit_of_measurement": "s",
                            "mode": "box",
                        }
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="regellogik",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            last_step=False,
        )


# Helper function to sanitize
def sanitize_device_name(
    device_name: str, fall_back: str, max_length: int = 255
) -> str:
    """Sanitize device name, falling back to fall_back if empty."""
    sanitized = device_name.strip()
    if not sanitized:
        return fall_back
    return sanitized[:max_length]
