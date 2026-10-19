"""
Async EcoFlow PowerOcean API client.

This module provides a standalone, asyncio-based client for communicating
with the EcoFlow cloud API. It supports:

- User authentication
- Automatic region detection (EU/US/Global) for PowerOcean
  (provider-service) and standalone IoT devices like the PowerGlow
  (iot-service)
- Fetching raw device data
- Sending control commands
- Retrieving MQTT credentials for push-based devices

Classes:
    EcoflowApi: Main API client class.
    EcoflowApiError: Base exception.
    AuthenticationError: Raised on login failure.
    RegionDetectionError: Raised if API region cannot be detected.
"""

import asyncio
import base64
from typing import Any, ClassVar

import aiohttp

from .const import LOGGER

HTTP_OK = 200


class EcoflowApiError(Exception):
    """Base exception for all EcoFlow API errors."""


class AuthenticationError(EcoflowApiError):
    """Raised when authentication with the EcoFlow API fails."""


class RegionDetectionError(EcoflowApiError):
    """Raised when automatic API region detection fails."""


class EcoflowApi:
    """Async client for the EcoFlow PowerOcean cloud API."""

    REGION_HOSTS: ClassVar[dict[str, str]] = {
        "eu": "api-e.ecoflow.com",
        "us": "api-a.ecoflow.com",
        "global": "api.ecoflow.com",
    }

    _PROBE_PATHS: ClassVar[list[tuple[str, str]]] = [
        ("provider", "/provider-service/user/device/detail?sn={sn}"),
        ("iot",      "/iot-service/v1/user/device/quota/all?sn={sn}"),
        ("iot",      "/iot-service/v1/user/device?sn={sn}"),
        ("iot",      "/iot-service/v2/user/device/quota/all?sn={sn}"),
        ("provider", "/provider-service/user/device/quota/all?sn={sn}"),
    ]

    def __init__(
        self,
        serialnumber: str,
        username: str,
        password: str,
        variant: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.sn = serialnumber
        self.username = username
        self.password = password
        self.variant = variant

        self.token: str | None = None
        self.user_id: str | None = None
        self.api_host: str | None = None
        self.url_authorize = "https://api.ecoflow.com/auth/login"

        self._active_headers: dict[str, str] = {}
        self._api_type: str = "provider"

        self._external_session = session
        self._session: aiohttp.ClientSession | None = session

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return an existing session or create a new aiohttp session."""
        if self._session:
            return self._session
        self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close internally created aiohttp session."""
        if self._session and not self._external_session:
            await self._session.close()

    async def async_authorize(self) -> None:
        """Authorize user and retrieve authentication token."""
        session = await self._get_session()

        headers = {"lang": "en_US", "content-type": "application/json"}
        data = {
            "email": self.username,
            "password": base64.b64encode(self.password.encode()).decode(),
            "scene": "IOT_APP",
            "userType": "ECOFLOW",
        }

        LOGGER.debug("Attempting login for user %s", self.username)
        try:
            async with asyncio.timeout(10):
                async with session.post(
                    self.url_authorize, json=data, headers=headers
                ) as resp:
                    LOGGER.debug("Login response status: %s", resp.status)
                    resp.raise_for_status()
                    response_data = await resp.json()
        except Exception as e:
            LOGGER.error("Login request failed: %s", e)
            msg = "Login failed"
            raise AuthenticationError(msg) from e

        token = response_data.get("data", {}).get("token")
        if not token:
            LOGGER.error(
                "Login: no token in response. Full response: %s", response_data
            )
            msg = "Missing token in response"
            raise AuthenticationError(msg)

        self.token = token
        # Store user ID - needed for MQTT client ID and set-topic
        self.user_id = (
            response_data.get("data", {}).get("user", {}).get("userId")
        )
        LOGGER.debug(
            "Login OK. user_id=%s. Starting region detection for SN %s",
            self.user_id, self.sn,
        )
        await self._detect_region()

    async def async_get_mqtt_credentials(self) -> dict[str, Any]:
        """
        Retrieve MQTT broker credentials from EcoFlow API.

        Returns a dict with keys: url, port, certificateAccount, certificatePassword.
        Call this after async_authorize().
        """
        if not self.token:
            msg = "Not authenticated - call async_authorize() first"
            raise EcoflowApiError(msg)

        session = await self._get_session()
        url = "https://api.ecoflow.com/iot-auth/app/certification"
        headers = {
            "lang": "en_US",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
        }
        # userId sent as form data (matching EcoFlow app behaviour)
        form_data = {"userId": self.user_id or ""}

        LOGGER.debug("Requesting MQTT credentials for user_id=%s", self.user_id)
        try:
            async with asyncio.timeout(10):
                async with session.get(
                    url, headers=headers, data=form_data
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
        except Exception as e:
            LOGGER.error("Failed to retrieve MQTT credentials: %s", e)
            msg = f"MQTT credential request failed: {e}"
            raise EcoflowApiError(msg) from e

        creds = body.get("data", {})
        if not creds or not creds.get("url"):
            LOGGER.error("MQTT credential response has no usable data: %s", body)
            msg = "No MQTT credentials in response"
            raise EcoflowApiError(msg)

        LOGGER.info(
            "MQTT credentials obtained: url=%s port=%s account=%s",
            creds.get("url"),
            creds.get("port"),
            creds.get("certificateAccount"),
        )
        return creds

    async def _probe_once(
        self,
        session: aiohttp.ClientSession,
        region: str,
        host: str,
        api_type: str,
        path: str,
        headers: dict[str, str],
    ) -> bool:
        """Probe one URL. Returns True on HTTP 200 + non-null data (strong match)."""
        url = f"https://{host}{path}"
        try:
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as resp:
                    status = resp.status
                    try:
                        body = await resp.json()
                    except Exception:
                        body = {}

                    code = body.get("code", "?")
                    data_val = body.get("data")

                    LOGGER.debug(
                        "PowerOcean probe %s/%s: HTTP %s | code=%s | data=%s | url=%s",
                        region, api_type, status, code,
                        "present" if data_val is not None else "null",
                        url,
                    )

                    if status == HTTP_OK and data_val is not None:
                        self.api_host = host
                        self._active_headers = headers
                        self._api_type = api_type
                        LOGGER.info(
                            "SUCCESS: region=%s host=%s api_type=%s",
                            region, host, api_type,
                        )
                        return True

                    # code=0 aber data=null: vollstaendige Antwort loggen
                    if status == HTTP_OK and str(code) == "0":
                        LOGGER.debug(
                            "PowerOcean probe %s/%s: HTTP 200 code=0 aber data=null. "
                            "Vollstaendige Antwort: %s | url=%s",
                            region, api_type, body, url,
                        )

        except (TimeoutError, aiohttp.ClientError) as err:
            LOGGER.debug(
                "PowerOcean probe %s/%s NETWORK ERROR: %s | url=%s",
                region, api_type, err, url,
            )

        return False

    async def _probe_weak(
        self,
        session: aiohttp.ClientSession,
        region: str,
        host: str,
        api_type: str,
        path: str,
        headers: dict[str, str],
    ) -> bool:
        """Returns True if HTTP 200 + code='0' (even if data=null)."""
        url = f"https://{host}{path}"
        try:
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != HTTP_OK:
                        return False
                    try:
                        body = await resp.json()
                    except Exception:
                        return False
                    return str(body.get("code", "")) == "0"
        except (TimeoutError, aiohttp.ClientError):
            return False

    async def _detect_region(self) -> None:
        """Detect API region by probing all known endpoint combinations.

        Hosts: EU, US, Global.
        Paths: provider-service (PowerOcean) + iot-service v1/v2 (PowerGlow etc.)

        Fallback: wenn code=0 aber data=null, Geraet ist bekannt aber
        liefert Daten moeglicherweise nur via MQTT.
        """
        session = await self._get_session()
        auth = f"Bearer {self.token}"

        headers_with_type = {"authorization": auth, "product-type": self.variant}
        headers_plain = {"authorization": auth}

        weak_host: str | None = None
        weak_headers: dict[str, str] = {}
        weak_api_type: str = "provider"

        LOGGER.debug(
            "Starting region detection for SN=%s variant=%s", self.sn, self.variant
        )

        for region, host in self.REGION_HOSTS.items():
            for api_type, path_template in self._PROBE_PATHS:
                path = path_template.format(sn=self.sn)

                headers_list = (
                    [headers_with_type, headers_plain]
                    if api_type == "provider"
                    else [headers_plain]
                )

                for hdrs in headers_list:
                    if await self._probe_once(
                        session, region, host, api_type, path, hdrs
                    ):
                        return

                    if weak_host is None:
                        result = await self._probe_weak(
                            session, region, host, api_type, path, hdrs
                        )
                        if result:
                            weak_host = host
                            weak_headers = hdrs
                            weak_api_type = api_type
                            LOGGER.debug(
                                "Schwache Uebereinstimmung: region=%s host=%s "
                                "api_type=%s (code=0, data=null). "
                                "Geraet bekannt aber Datenformat unklar.",
                                region, host, api_type,
                            )

        if weak_host is not None:
            self.api_host = weak_host
            self._active_headers = weak_headers
            self._api_type = weak_api_type
            LOGGER.debug(
                "Verbinde mit schwacher Uebereinstimmung: host=%s api_type=%s. "
                "Geraet antwortet mit code=0 aber data=null - "
                "vollstaendige Antwort erscheint beim Datenabruf im Log.",
                weak_host, weak_api_type,
            )
            return

        msg = (
            f"Could not detect API region for SN {self.sn!r}. "
            "All probes failed (EU/US/Global x provider/iot). "
            "Possible causes: "
            "(1) wrong serial number, "
            "(2) device not linked to this EcoFlow account, "
            "(3) EcoFlow API uses a different endpoint for this product."
        )
        LOGGER.error(msg)
        raise RegionDetectionError(msg)

    def _fetch_url(self) -> str:
        """Return the device-data URL for the detected API type."""
        if self._api_type == "iot":
            path = f"/iot-service/v1/user/device/quota/all?sn={self.sn}"
        else:
            path = f"/provider-service/user/device/detail?sn={self.sn}"
        return f"https://{self.api_host}{path}"

    async def fetch_raw(self) -> dict[str, Any]:
        """Fetch raw device data from the EcoFlow cloud API."""
        if not self.api_host:
            msg = "API host not set - call async_authorize() first"
            raise EcoflowApiError(msg)

        session = await self._get_session()
        url = self._fetch_url()

        LOGGER.debug("fetch_raw: url=%s api_type=%s", url, self._api_type)
        try:
            async with asyncio.timeout(10):
                async with session.get(url, headers=self._active_headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    LOGGER.debug(
                        "fetch_raw: api_type=%s top-keys=%s",
                        self._api_type,
                        list(data.keys()),
                    )
                    if data.get("data") is None:
                        LOGGER.warning(
                            "fetch_raw: data=null - vollstaendige API-Antwort: %s",
                            data,
                        )
                    else:
                        inner = data.get("data")
                        if isinstance(inner, dict):
                            LOGGER.debug(
                                "fetch_raw inner keys=%s", list(inner.keys())
                            )
                    return data
        except aiohttp.ClientError as err:
            msg = f"Failed to fetch device data: {err}"
            raise EcoflowApiError(msg) from err

    async def send_command(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send a control command to the device."""
        if not self.api_host:
            msg = "API host not set - call async_authorize() first"
            raise EcoflowApiError(msg)

        session = await self._get_session()
        headers = {**self._active_headers, "content-type": "application/json"}

        if self._api_type == "iot":
            url = f"https://{self.api_host}/iot-service/v1/user/device/quota"
        else:
            url = f"https://{self.api_host}/provider-service/user/device/property"

        payload = {"sn": self.sn, "params": params}
        LOGGER.debug("send_command: url=%s params=%s", url, params)
        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientError as err:
            msg = f"Command failed: {err}"
            raise EcoflowApiError(msg) from err
