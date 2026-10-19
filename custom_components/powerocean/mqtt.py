"""
mqtt.py: MQTT client for EcoFlow PowerGlow standalone devices.

The PowerGlow transmits live sensor data exclusively via MQTT (not HTTP REST).
This module provides a lightweight paho-mqtt wrapper that:

- Connects to the EcoFlow MQTT broker using credentials from /iot-auth/app/certification
- Subscribes to the device data topic /app/device/property/{sn}
- Bridges paho-mqtt's threaded callbacks to Home Assistant's asyncio event loop
- Calls an async callback whenever a new payload arrives

Usage:
    client = PowerGlowMqttClient(
        hass=hass,
        url="mqtt.ecoflow.com",
        port=8883,
        username="cert_account",
        password="cert_password",
        client_id="ANDROID_UUID_userid",
        sn="HF31ZE1AZHAG0125",
        on_message=my_async_callback,
    )
    # Later:
    client.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class PowerGlowMqttClient:
    """
    MQTT client for a standalone EcoFlow PowerGlow heating rod.

    Uses paho-mqtt with a background thread and bridges callbacks
    to the Home Assistant asyncio event loop via run_coroutine_threadsafe.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        port: int,
        username: str,
        password: str,
        client_id: str,
        sn: str,
        user_id: str,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """
        Initialize and connect the MQTT client.

        Args:
            hass: Home Assistant instance (for loop bridging).
            url: MQTT broker hostname.
            port: MQTT broker port (usually 8883 for TLS).
            username: MQTT certificateAccount from EcoFlow credentials.
            password: MQTT certificatePassword from EcoFlow credentials.
            client_id: Unique client identifier (ANDROID_{uuid}_{user_id}).
            sn: Device serial number.
            user_id: EcoFlow user ID (needed for the command set-topic).
            on_message: Async callback receiving the parsed JSON payload dict.

        """
        self._hass = hass
        self._sn = sn
        self._on_message_cb = on_message
        self._data_topic = f"/app/device/property/{sn}"
        # Topic the app publishes commands TO (device listens here)
        self._set_topic = f"/app/{user_id}/{sn}/thing/property/set"

        # Import paho with compatibility for both v1 and v2 API
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]

        try:
            from paho.mqtt.enums import CallbackAPIVersion  # paho >= 2.0
            self._client = mqtt.Client(
                client_id=client_id,
                clean_session=True,
                callback_api_version=CallbackAPIVersion.VERSION1,
            )
        except ImportError:
            # paho-mqtt < 2.0
            self._client = mqtt.Client(client_id=client_id, clean_session=True)

        self._client.username_pw_set(username, password)

        # TLS - EcoFlow broker requires TLS
        self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.tls_insecure_set(False)

        # Callbacks
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

        _LOGGER.info(
            "PowerGlow MQTT: connecting to %s:%s for SN %s (client_id=%s)",
            url, port, sn, client_id,
        )
        self._client.connect_async(url, port, keepalive=30)
        self._client.loop_start()

    # ------------------------------------------------------------------
    # paho callbacks (run in paho background thread)
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        """Called when MQTT connection is established."""
        if rc == 0:
            # Primary subscription: device -> cloud status/reports
            client.subscribe(self._data_topic, qos=1)
            # Diagnostic subscription: cloud/app -> device commands.
            # This lets us see what the EcoFlow app sends when you
            # change settings there - useful for protocol analysis.
            # We register a separate per-topic callback so these messages
            # are logged but NOT forwarded to the coordinator as sensor data.
            client.subscribe(self._set_topic, qos=1)
            client.message_callback_add(self._set_topic, self._on_set_message)
            _LOGGER.info(
                "PowerGlow MQTT: connected. Subscribed to %s and %s",
                self._data_topic,
                self._set_topic,
            )
        else:
            _LOGGER.error("PowerGlow MQTT: connection failed (rc=%d)", rc)

    def _on_subscribe(
        self, client: Any, userdata: Any, mid: int, granted_qos: Any
    ) -> None:
        """Called after the broker responds to a subscribe request (SUBACK).

        granted_qos is a list/tuple of integers - one per subscribed topic.
        A value of 128 (0x80) means the broker REFUSED the subscription,
        which would happen if our certificate account lacks ACL permission.
        """
        qos_list = list(granted_qos) if hasattr(granted_qos, "__iter__") else [granted_qos]
        for qos in qos_list:
            if qos == 128:
                _LOGGER.warning(
                    "PowerGlow MQTT: subscription REFUSED by broker (mid=%d). "
                    "The EcoFlow broker may not allow this account to subscribe "
                    "to this topic (ACL restriction).",
                    mid,
                )
            else:
                _LOGGER.debug(
                    "PowerGlow MQTT: subscription confirmed (mid=%d, qos=%d).",
                    mid, qos,
                )

    def _on_disconnect(
        self, client: Any, userdata: Any, rc: int
    ) -> None:
        """Called on disconnect (auto-reconnect is handled by paho loop)."""
        if rc != 0:
            _LOGGER.warning(
                "PowerGlow MQTT: unexpected disconnect (rc=%d). "
                "Paho will attempt reconnect.",
                rc,
            )

    def _on_set_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """
        Diagnostic callback for the command set-topic (cloud/app -> device).

        Decodes every packet arriving on /app/{user_id}/{sn}/thing/property/set
        and logs it in human-readable form.  cmd_id=16 (the real ON/OFF command)
        is highlighted with * so it is easy to spot in the log.

        Does NOT forward anything to the coordinator - these are outbound
        cloud commands, not device sensor data.
        """
        raw: bytes = (
            msg.payload
            if isinstance(msg.payload, (bytes, bytearray))
            else msg.payload.encode()
        )
        try:
            from .proto import decode_set_topic_packet

            info = decode_set_topic_packet(raw)
            if info:
                cmd_id = info.get("cmd_id", "?")
                interp = info.get("interpretation", "")
                pdata_hex = info.get("pdata_hex", "")
                seq = info.get("seq", "?")

                cmd_func = info.get("cmd_func", 0)

                # cmd_id=16 is the real ON/OFF control -> highlight it
                if cmd_id == 16 and "Zielleistung" in interp:
                    # targetPower-Befehle kommen von der PV-Regelung alle paar Sekunden
                    _LOGGER.debug(
                        "  CLOUD cmd_id=16 -> %s  [seq=0x%x pdata=%s]",
                        interp, seq, pdata_hex,
                    )
                elif cmd_id == 16:
                    # cmd_id=16 is the real control command (ON/OFF/Temp/Zeit)
                    _LOGGER.info(
                        "CLOUD ON/OFF cmd_id=16 -> %s  [seq=0x%x pdata=%s raw=%s]",
                        interp, seq, pdata_hex, raw.hex(),
                    )
                elif cmd_func == 254:
                    # cmd_func=254: Smart Modus Schedule-Befehl (Temp/Zeit/Slots).
                    _LOGGER.info(
                        "  SMART-SCHEDULE cmd_func=254/cmd_id=%s -> %s  [seq=0x%x pdata=%s raw=%s]",
                        cmd_id, interp, seq, pdata_hex, raw.hex(),
                    )
                elif cmd_id == 97:
                    # cmd_id=97 keepalive fires every ~3 s - debug only
                    _LOGGER.debug(
                        "  Cloud-Keepalive cmd_id=97 -> %s  [seq=0x%x pdata=%s]",
                        interp, seq, pdata_hex,
                    )
                elif cmd_id == 1:
                    _LOGGER.debug(
                        "  Cloud/HA Param-Set cmd_id=1 -> %s  [seq=0x%x pdata=%s]",
                        interp, seq, pdata_hex,
                    )
                else:
                    _LOGGER.debug(
                        "  SET-Topic cmd_id=%s -> %s  [seq=0x%x pdata=%s raw=%s]",
                        cmd_id, interp, seq, pdata_hex, raw.hex(),
                    )
            else:
                _LOGGER.warning(
                    "  SET-Topic (nicht dekodierbar, %d bytes): %s",
                    len(raw), raw.hex(),
                )
        except Exception as exc:
            _LOGGER.warning(
                "  SET-Topic (%d bytes, Fehler %s): %s",
                len(raw), exc, raw.hex(),
            )

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Called when a message arrives on a subscribed topic (paho thread)."""
        raw: bytes = (
            msg.payload
            if isinstance(msg.payload, (bytes, bytearray))
            else msg.payload.encode()
        )
        try:
            # Step 1: protobuf decode (Delta3HeaderMessage / PowerGlow)
            from .proto import decode_powerglow_mqtt  # local import avoids cycles

            decoded = decode_powerglow_mqtt(raw)
            if decoded is not None:
                if decoded:
                    _LOGGER.debug(
                        "PowerGlow MQTT protobuf on %s: %s", msg.topic, decoded
                    )
                    asyncio.run_coroutine_threadsafe(
                        self._on_message_cb({"params": decoded}),
                        self._hass.loop,
                    )
                else:
                    # Empty dict = known packet with no sensor data (e.g. cmd_id=32 ACK)
                    _LOGGER.debug(
                        "PowerGlow MQTT: known ACK packet on %s (%d bytes) - no sensor data",
                        msg.topic, len(raw),
                    )
                return

            # Step 2: plain JSON fallback (some EcoFlow devices / topics)
            text = raw.decode("utf-8", errors="ignore").strip()
            brace = text.find("{")
            if brace > 0:
                text = text[brace:]
            payload = json.loads(text)
            _LOGGER.debug("PowerGlow MQTT JSON on %s: %s", msg.topic, payload)
            asyncio.run_coroutine_threadsafe(
                self._on_message_cb(payload),
                self._hass.loop,
            )

        except (json.JSONDecodeError, ValueError):
            # Not JSON and not a recognisable protobuf - log for analysis.
            _LOGGER.debug(
                "PowerGlow MQTT unrecognised payload on %s (%d bytes): %s",
                msg.topic,
                len(raw),
                raw.hex(),
            )
        except Exception:
            _LOGGER.exception(
                "PowerGlow MQTT: unexpected error in on_message - hex: %s",
                raw.hex() if raw else "(empty)",
            )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def publish_raw(self, topic: str, payload: bytes) -> None:
        """Publish a raw protobuf payload to the given MQTT topic."""
        try:
            result = self._client.publish(topic, payload, qos=1)
            _LOGGER.debug(
                "PowerGlow MQTT protobuf publish -> topic=%s rc=%s mid=%s bytes=%d",
                topic, result.rc, result.mid, len(payload),
            )
        except Exception:
            _LOGGER.exception("PowerGlow MQTT: publish_raw error")

    def stop(self) -> None:
        """Disconnect and stop the MQTT background thread.

        Call disconnect() first so paho sends the MQTT DISCONNECT packet and
        the network thread exits cleanly.  Then loop_stop() waits for the
        thread to finish.  Reversing the order (loop_stop first) can block
        for up to keepalive seconds because the thread is still running.
        """
        try:
            self._client.disconnect()
            self._client.loop_stop()
            _LOGGER.info("PowerGlow MQTT: stopped for SN %s", self._sn)
        except Exception as e:
            _LOGGER.debug("PowerGlow MQTT: error during stop: %s", e)
