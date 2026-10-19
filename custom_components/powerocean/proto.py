"""
proto.py - EcoFlow PowerGlow protobuf decoder (no google-protobuf dependency).

The PowerGlow uses the same Delta3Header / Delta3HeaderMessage wrapper as
newer EcoFlow devices (Delta3, DeltaPro3, ...).  Wire format:

  MQTT payload
  L- Delta3HeaderMessage  { field 1 (LEN) = Delta3Header }
     L- Delta3Header {
           field  1  (LEN)   pdata     - XOR-encoded device payload
           field  2  (int32) src       - source address
           field  6  (int32) enc_type  - 1 -> XOR with (seq & 0xFF)
           field  8  (int32) cmd_func  - always 2 for PowerGlow
           field  9  (int32) cmd_id    - 1 = state report, 34 = energy stream
           field 14  (int32) seq       - XOR key (lowest 8 bits)
        }

Confirmed field mappings (from captured hex payload analysis):

  cmd_id = 1  (HEATING_ROD_PARAM_REPORT):
    pdata field  2  float32  -> temp         (degrees C, current water temperature)
    pdata field  4  float32  -> keepWarmTemp  (°C, comfort/keep-warm setpoint; purpose TBD)
                                Confirmed 2026-06-21: value=44.0°C while device was in Smart Modus
    pdata field  6  varint   -> targetTemp    (°C×2; display = field6 // 2)
                                Confirmed 2026-06-21: Manueller Modus app shows 27°C → field6=54
    pdata field  7  varint   -> targetPower  (W×2; display = field7 // 2)
                                Confirmed 2026-06-21: app shows 1999 W → field7 = 3998 = 1999×2
    pdata field  8  varint   -> smartTargetTemp (°C×2; Smart Modus Zieltemperatur)
                                Confirmed 2026-06-21: Smart Modus 30°C → field8=60 (60÷2=30)
                                                      Smart Modus 60°C → field8=120 (120÷2=60)
                                PREVIOUSLY WRONG: was decoded as scheduleTime × 10 = minutes
    pdata field  9  varint   -> grid/breaker limit (const 2400 = 10A×240V; NOT targetPower)
    pdata field 17  varint   -> heatingPower (W×2; display = field17 // 2; 0 when off)
                                Confirmed 2026-06-21: app shows 2.22 kW → field17=4446=2223×2
    pdata field 25  varint   -> mode  (6=Manueller Modus, 8=Smart Modus)
                                Confirmed 2026-06-21: field25 is ALWAYS 6 in Manuell and ALWAYS 8 in Smart.
                                field37 was INCORRECTLY mapped as mode — it is ALWAYS 2, not a mode indicator!
    pdata field 38  varint   -> smartTempRef (unbekannt; direkte °C; bleibt konstant bei Tempänderungen)
                                Confirmed 2026-06-21: Smart Modus 30°C gesetzt → field38 bleibt 60

  cmd_id = 33 (HEATING_ROD_TEMP_STREAM_REPORT):
    Sent every ~5 seconds. Short form: temp only. Extended form: temp + power.
    pdata field  1  float32  -> temp         (degrees C, current water temperature)
    pdata field  2  varint   -> heatingPower (W,  same as cmd_id=1 field 17)

cmd_id=16 is multi-function (confirmed 2026-06-21 from set-topic logs):
  ON:      {field1: 0}  → start heating  (HA cmd format)
  OFF:     {field1: 2}  → stop heating   (HA cmd format)
  OFF-B:   {field2: 2}  → stop heating   (cloud/iOS format; confirmed 2026-06-21)
  Smart:   {field2: 4}  → switch to Smart Modus (confirmed 2026-06-21: iOS sends this
                          when user taps "Smart" in app; device activates Smart schedule)
                          device subsequently reports field37=0 (Smart Modus aktiv).
  Temp:    {field3: N}  → set targetTemp (Manuell) = N÷2°C (device echoes field6=N in next cmd_id=1)
                          Confirmed 2026-06-21: field3=68→34°C, field3=92→46°C
  Power:   {field4: N}  → set targetPower = N÷2 W (device echoes field7=N in next cmd_id=1)
                          Confirmed 2026-06-21: field4=7000→3500W, field4=0→0W
                          Slider intermediate: field4=3030→1515W
  SmartT:  {field5: N}  → set smartTargetTemp (Smart Modus) = N÷2°C (device echoes field8=N)
                          Confirmed 2026-06-21: field5=60→30°C, field5=120→60°C
  Limit:   {field6: N}  → set grid/breaker limit (device echoes field9=N)

  cmd_id = 34 (HEATING_ROD_ENERGY_STREAM_REPORT):
    pdata field 1 (LEN) = sub-message {
        sub-field 2  varint -> hrPwr    (W×2; decode = raw//2; total power drawn by the rod)
                               Confirmed 2026-06-21: raw=4446 -> 2223 W (app: 2.22 kW)
        sub-field 4  varint -> fromGrid (W×2; decode = raw//2; grid-sourced share)
                               Confirmed 2026-06-21: raw=5002 -> 2501 W (app: 2.5 kW)
    }

cmd_id=16 ON/OFF encoding (confirmed from log 2026-06-20):
  ON:  pdata = {field1: 0} -> 0800  (device starts heating)
  OFF: pdata = {field1: 2} -> 0802  (device stops heating)
  NOTE: {field1: 1} (0801) is IGNORED by the device firmware!
  Cloud also uses {field3: 56} (1838) as an alternative stop signal.
"""
from __future__ import annotations

import logging
import random
import struct
import time
from typing import Any

_LOGGER = logging.getLogger("custom_components.powerocean.proto")

__all__ = [
    "decode_powerglow_mqtt",
    "decode_set_topic_packet",
    "encode_powerglow_clear_schedule",
    "encode_powerglow_cmd16",
    "encode_powerglow_cmd16_schedule_slot1_temp",
    "encode_powerglow_cmd16_schedule_slot1_time",
    "encode_powerglow_cmd16_schedule_slot2_temp",
    "encode_powerglow_cmd16_schedule_slot2_time",
    "encode_powerglow_cmd16_set_manuell",
    "encode_powerglow_cmd16_set_smart",
    "encode_powerglow_cmd16_setpower",
    "encode_powerglow_cmd16_settemp",
    "encode_powerglow_cmd16_smart_settemp",
    "encode_powerglow_cmd16_tank_type",
    "encode_powerglow_cmd16_tank_volume",
    "encode_powerglow_cmd17",           # alias for encode_powerglow_cmd17_request_all
    "encode_powerglow_cmd17_request_all",
    "encode_powerglow_cmd97",
    "encode_powerglow_command",
]

# ---------------------------------------------------------------------------
# Low-level wire-format helpers
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf base-128 varint; return (value, new_pos).

    Returns (0, original_pos) if there are no bytes to read (empty / truncated
    data), which the caller in _parse_fields detects via the tag being 0.
    """
    result, shift = 0, 0
    start = pos
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos   # success: MSB clear means last byte
    # Reached end of data without a terminating byte -> truncated varint.
    # Return 0 with the original position so the caller loop terminates.
    return 0, start


def _parse_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """
    Yield (field_number, wire_type, value) for each field in raw proto bytes.

    Supported wire types:
      0  varint          -> int
      2  length-delimited -> bytes
      5  32-bit fixed    -> float (IEEE-754 little-endian)
      1  64-bit fixed    -> skipped (position advanced, not yielded)
    Unknown wire types cause parsing to stop (remaining bytes ignored).
    """
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        fn, wt = tag >> 3, tag & 7

        if fn == 0:
            break   # field 0 is invalid in protobuf - truncated/corrupt tag

        if wt == 0:                       # varint
            v, pos = _read_varint(data, pos)
            fields.append((fn, 0, v))
        elif wt == 2:                     # length-delimited (bytes / sub-msg)
            n, new_pos = _read_varint(data, pos)
            pos = new_pos
            end = pos + n
            if end > len(data):           # truncated: clamp silently
                end = len(data)
            v = data[pos:end]
            pos = end
            fields.append((fn, 2, v))
        elif wt == 5:                     # 32-bit fixed (float32)
            if pos + 4 > len(data):
                break                     # truncated fixed32 - stop
            v = struct.unpack_from("<f", data, pos)[0]
            pos += 4
            fields.append((fn, 5, v))
        elif wt == 1:                     # 64-bit fixed - skip
            if pos + 8 > len(data):
                break                     # truncated fixed64 - stop
            pos += 8
        else:
            break   # unknown wire type - stop to avoid corrupt reads
    return fields


# ---------------------------------------------------------------------------
# XOR decoding (EcoFlow enc_type=1)
# ---------------------------------------------------------------------------

def _xor_decode(pdata: bytes, seq: int) -> bytes:
    """XOR-decode payload using the lowest byte of seq as key."""
    key = seq & 0xFF
    return bytes(b ^ key for b in pdata)


# ---------------------------------------------------------------------------
# Field-name mappings (verified against captured messages)
# ---------------------------------------------------------------------------

# cmd_id=1 -> HEATING_ROD_PARAM_REPORT
_CMD1_FIELDS: dict[int, str] = {
    2:  "temp",             # float32, current water temperature (°C)
    4:  "keepWarmTemp",     # float32, comfort/keep-warm setpoint (°C); purpose not yet confirmed
                            # Observed: 44.0°C while device was in Smart Modus
    6:  "targetTemp",       # varint, stored as °C×2; decode = field6 // 2
                            # Confirmed 2026-06-21: Manueller Modus app 27°C → field6=54=27×2
    7:  "targetPower",      # varint, stored as W×2; decode = field7 // 2
                            # Confirmed 2026-06-21: app 1999 W → field7=3998=1999×2
                            # field9=2400 is the hardware grid/breaker limit, NOT targetPower
    17: "heatingPower",     # varint, stored as W×2; decode = field17 // 2
                            # Confirmed 2026-06-21: app 2.22 kW → field17=4446=2223×2; 0 when off
    22: "waterTankVolume",  # varint, stored as liters×2; decode = field22 // 2
                            # Confirmed 2026-06-21: 300 L tank → field22=600=300×2
    8:  "smartTargetTemp",  # varint, Smart Modus Zieltemperatur (°C×2 encoding, same as field6)
                            # Confirmed 2026-06-21: Smart Modus app 30°C → field8=60 (60÷2=30)
                            #                       Smart Modus app 60°C → field8=120 (120÷2=60)
                            # Set via cmd_id=16 field5 (same ×2 encoding as targetTemp/field3)
                            # PREVIOUSLY MISIDENTIFIED as scheduleTime (×10 = minutes) — WRONG!
    25: "mode",             # varint: 6=Manueller Modus, 8=Smart Modus
                            # Confirmed 2026-06-21 from log analysis:
                            #   field25=6 → Manueller Modus (device default, also field1=6)
                            #   field25=8 → Smart Modus aktiv (also field1=8)
                            # WRONG ASSUMPTION CORRECTED: field37 was previously mapped as mode
                            # but field37 is ALWAYS 2 regardless of mode. field25 is the real indicator.
    38: "smartTempRef",     # varint, Referenzwert unbekannt (°C, direkt). NICHT die Smart-Temp!
                            # field38=60 bleibt konstant auch wenn Smart-Temp auf 30°C gesetzt wird.
    41: "tankType",         # varint, direkt: 0=Puffertank(80°C), 2=Wassertank(60°C)
                            # KORRIGIERT 2026-06-22 (user-bestätigt gegen EcoFlow App):
                            # field41=0 → Puffertank, field41=2 → Wassertank
                            # (frühere Annahme 0=Wassertank war falsch)
    # Unmapped fields (seen in log, purpose unknown):
    #  1=6/8 (mirrors field25), 33=0, 34=0, 35=200, 37=2 (ALWAYS 2, NOT mode!), 39=0, 40=0
}

# cmd_id=34 -> HEATING_ROD_ENERGY_STREAM_REPORT (inner sub-message)
_CMD34_SUB_FIELDS: dict[int, str] = {
    2: "hrPwr",
    4: "fromGrid",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decode_powerglow_mqtt(raw: bytes) -> dict[str, Any] | None:
    """
    Decode a raw MQTT payload from a PowerGlow heating rod.

    Returns a flat {sensor_key: value} dict where sensor_key matches
    the field names in REPORT_DATAPOINTS (utils.py) so they map directly
    to coordinator sensor UIDs.

    Returns None when the payload cannot be decoded (wrong format, no
    matching cmd_id, or empty pdata).

    Example return values:
      {"temp": 59.3, "targetTemp": 56.0, "heatingPower": 2146, "targetPower": 1999}
      {"hrPwr": 798, "fromGrid": 798}
    """
    try:
        # Outer Delta3HeaderMessage: field 1 holds the Delta3Header
        inner_raw: bytes | None = None
        for fn, wt, v in _parse_fields(raw):
            if fn == 1 and wt == 2:
                inner_raw = v
                break
        if inner_raw is None:
            return None

        # Delta3Header: extract control fields
        hdr: dict[int, Any] = {}
        for fn, _wt, v in _parse_fields(inner_raw):
            hdr.setdefault(fn, v)   # first occurrence wins

        pdata: bytes = hdr.get(1, b"")
        src: int     = hdr.get(2, 0)
        enc_type: int = hdr.get(6, 0)
        cmd_id: int  = hdr.get(9, 0)
        seq: int     = hdr.get(14, 0)

        if not pdata:
            return None

        # XOR-decode when enc_type=1 and message comes from the device (src != 32)
        if enc_type == 1 and src != 32:
            pdata = _xor_decode(pdata, seq)

        result: dict[str, Any] = {}

        if cmd_id == 1:
            # HEATING_ROD_PARAM_REPORT
            # Field mapping confirmed 2026-06-21 from full packet dump:
            #   field7=3998 = targetPower × 2 (app showed 1999 W → 3998 half-watts)
            #   field9=2400 = hardware grid/breaker limit (10A × 240V), not user-set
            for fn, wt, v in _parse_fields(pdata):
                name = _CMD1_FIELDS.get(fn)
                if name is not None:
                    if wt == 5:  # float32
                        result[name] = round(float(v), 2)
                    elif name in ("targetTemp", "targetPower", "waterTankVolume", "heatingPower", "smartTargetTemp"):
                        # Stored as value×2 (half-unit resolution):
                        # targetTemp:       °C×2  → field6=54   → 27 °C  (Manueller Modus)
                        # targetPower:      W×2   → field7=3998 → 1999 W
                        # waterTankVolume:  L×2   → field22=600 → 300 L
                        # heatingPower:     W×2   → field17=4446 → 2223 W (confirmed 2026-06-21)
                        # smartTargetTemp:  °C×2  → field8=60   → 30 °C  (Smart Modus, confirmed 2026-06-21)
                        #                          → field8=120  → 60 °C  (Smart Modus)
                        result[name] = int(v) // 2
                    elif name == "smartTempRef":
                        # Referenzwert field38 — direkte °C-Darstellung (kein ×2).
                        # Bedeutung unklar; bleibt konstant bei Smart-Modus-Tempänderungen.
                        result[name] = int(v)
                    else:
                        result[name] = int(v)

            # Derive runFlag from heatingPower (field 38 is smartTargetTemp, not runFlag).
            if "heatingPower" in result:
                result["runFlag"] = 1 if result["heatingPower"] > 0 else 0
                if result["heatingPower"] == 0:
                    # Energy stream (cmd_id=34) stops when device is off.
                    # Explicitly reset hrPwr/fromGrid so HA shows 0 W, not stale/unknown.
                    result["hrPwr"] = 0
                    result["fromGrid"] = 0

            # ── Diagnostic: log ALL raw fields to help identify unknowns ──────
            # This WARNING appears every ~60 s (device param report interval).
            # Use the output to find: Smart-Modus Zeit (20:00), scheduleSlot, etc.
            all_fields = {}
            for fn2, wt2, v2 in _parse_fields(pdata):
                all_fields[fn2] = round(float(v2), 4) if wt2 == 5 else (
                    v2.hex() if isinstance(v2, bytes) else int(v2)
                )
            _LOGGER.debug(
                "[PowerGlow DIAG cmd_id=1] Alle Felder: %s", all_fields
            )

        elif cmd_id == 33:
            # HEATING_ROD_TEMP_STREAM_REPORT
            # Sent every ~5 seconds. Short form (7 bytes): temp + status only.
            # Extended form (13 bytes): temp + heatingPower + status.
            #   field 1  float32 -> temp         (degrees C, current water temperature)
            #   field 2  varint  -> heatingPower (W, identical to cmd_id=1 field 17)
            #   field 4  varint  -> heatingPower (mirror of field 2, same value)
            #   field 6  varint  -> status       (2 = idle, 46 = actively heating)
            for fn, wt, v in _parse_fields(pdata):
                if fn == 1 and wt == 5:
                    result["temp"] = round(float(v), 2)
                elif fn == 2 and wt == 0:
                    # heatingPower encoded as W×2 (confirmed 2026-06-21)
                    result["heatingPower"] = int(v) // 2
            # Derive runFlag: present when heatingPower is known, else from field 6
            if "heatingPower" in result:
                result["runFlag"] = 1 if result["heatingPower"] > 0 else 0

        elif cmd_id == 34:
            # HEATING_ROD_ENERGY_STREAM_REPORT
            # Two variants share cmd_id=34:
            #
            # Variant A - energy measurement (appears ~every 3 s while heating):
            #   pdata field 1 (LEN) = sub-message {
            #       sub-field 2 varint -> hrPwr    (W, total rod power)
            #       sub-field 4 varint -> fromGrid (W, grid-sourced share)
            #   }
            #
            # Variant B - temperature heartbeat (appears every 60 s):
            #   pdata field 1 (LEN) = sub-message {
            #       sub-field 1 float32 -> temp       (degrees C, same as cmd_id=33)
            #       sub-field 5 varint  -> unix_ts    (current time)
            #       sub-field 7 varint  -> status     (4 = idle/standby)
            #   }
            #   pdata field 2 varint -> msg_id (large integer, not a sensor)
            for fn, wt, v in _parse_fields(pdata):
                if fn == 1 and wt == 2:
                    for sfn, swt, sv in _parse_fields(v):
                        if sfn == 1 and swt == 5:   # Variant B: temperature
                            result["temp"] = round(float(sv), 2)
                        name = _CMD34_SUB_FIELDS.get(sfn)
                        if name is not None:
                            if swt == 5:
                                result[name] = round(float(sv), 2)
                            else:
                                # Both hrPwr and fromGrid encoded as W×2 (confirmed 2026-06-21):
                                # hrPwr:    raw=4446 → 4446//2=2223 W (app: 2.22 kW)
                                # fromGrid: raw=5002 → 5002//2=2501 W (app: 2.5 kW)
                                result[name] = int(sv) // 2
                    break   # only the first sub-message field needed

        if result:
            return result

        # Control-format packets (cmd_id=32 Device ACK, etc.) use a different
        # Delta3Header layout where inner field 2 = cmd_func, field 3 = cmd_id.
        # decode_powerglow_mqtt interprets these incorrectly (reads cmd_id from
        # field 9 which contains a header flag, not the actual cmd_id).
        # Detect them by checking field 3 of the inner header and return an
        # empty dict (= "known packet, no sensor data") so mqtt.py does not
        # log them as "unrecognised".
        inner_cmd_id_alt = hdr.get(3, 0)
        if inner_cmd_id_alt == 32:
            # cmd_id=32: Device ACK after cmd_id=16 ON/OFF commands.
            # No sensor data. Return {} so the caller knows it was recognised.
            return {}
        # Any other control-format packet with no data - also harmless
        if inner_cmd_id_alt > 0 and inner_cmd_id_alt != cmd_id:
            return {}

        return None

    except Exception:  # never crash the MQTT thread
        return None


# ===========================================================================
# ENCODER - build protobuf command messages for the PowerGlow
# ===========================================================================

def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf base-128 varint."""
    if value == 0:
        return b"\x00"
    out = bytearray()
    while value > 0:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
    return bytes(out)


def _enc_varint(fn: int, value: int) -> bytes:
    """Encode field_number + wire_type=0 + varint value."""
    return _encode_varint((fn << 3) | 0) + _encode_varint(value)


def _enc_bytes(fn: int, data: bytes) -> bytes:
    """Encode field_number + wire_type=2 + length-prefixed bytes."""
    return _encode_varint((fn << 3) | 2) + _encode_varint(len(data)) + data


def _enc_float(fn: int, value: float) -> bytes:
    """Encode field_number + wire_type=5 + 32-bit IEEE-754 float."""
    return _encode_varint((fn << 3) | 5) + struct.pack("<f", value)


def _enc_str(fn: int, value: str) -> bytes:
    """Encode a UTF-8 string field."""
    return _enc_bytes(fn, value.encode("utf-8"))


# Field numbers in the pdata (same as in HEATING_ROD_PARAM_REPORT)
_SET_FIELDS: dict[str, tuple[int, str]] = {
    # param_name -> (proto field number, wire type: "float" | "varint" | "varint_x2")
    # keepWarmTemp: comfort/keep-warm setpoint — stored as float32 on field 4 (purpose TBD)
    "keepWarmTemp":     (4,  "float"),
    # targetTemp: Manueller Modus Eingestellte Temperatur — stored as °C×2 on field 6
    # Confirmed 2026-06-21: Manuell app 27°C → field6=54=27×2; we write 27 → encodes 54
    "targetTemp":       (6,  "varint_x2"),
    # targetPower: stored in half-watt units — send field7 = desired_watts × 2
    # Confirmed 2026-06-21: app 1999 W → field7=3998; we write 1999 → encodes 3998
    "targetPower":      (7,  "varint_x2"),
    "mode":             (37, "varint"),
    # NOTE: smartTargetTemp is NO LONGER in _SET_FIELDS!
    # Smart Modus temperature is set via cmd_id=16 field5 (encode_powerglow_cmd16_smart_settemp).
    # The old route (cmd_id=1 field38) was silently ignored by the EcoFlow cloud.
    # Confirmed 2026-06-21: field8=60→30°C, field8=120→60°C (field8÷2 = decoded smartTargetTemp)
    # NOTE: runFlag is handled via cmd_id=16 (encode_powerglow_cmd16), NOT here.
}

_PRODUCT_ID = 8449  # confirmed from captured messages


def _build_pdata(params: dict[str, Any]) -> bytes:
    """
    Encode params dict into the PowerGlow command pdata bytes.

    Only fields present in _SET_FIELDS are encoded; unknown keys are skipped.
    """
    pdata = bytearray()
    for key, value in params.items():
        mapping = _SET_FIELDS.get(key)
        if mapping is None:
            continue
        fn, wtype = mapping
        if wtype == "float":
            pdata += _enc_float(fn, float(value))
        elif wtype == "varint_x2":
            # Fields stored in half-unit resolution (value×2):
            #   targetTemp  (°C×2): 27°C   → 54,   27.5°C → 55
            #   targetPower (W×2):  1999 W → 3998, 1999.5 W → 3999
            # Use round(value * 2) for best half-unit precision
            pdata += _enc_varint(fn, round(float(value) * 2))
        else:
            pdata += _enc_varint(fn, int(value))
    return bytes(pdata)


# ---------------------------------------------------------------------------
# Set-topic packet decoder - decodes cloud->device commands for log analysis
# ---------------------------------------------------------------------------

def decode_set_topic_packet(raw: bytes) -> dict[str, Any] | None:
    """
    Decode a raw MQTT payload from the set-topic (cloud->device commands).

    This is used for diagnostic logging of what the EcoFlow cloud/iOS app
    sends to the device.  Returns a structured dict with the most important
    fields so the log is human-readable.

    Returns:
        Dict with keys:
          cmd_func, cmd_id, seq, src, version, pdata_hex, pdata_fields
          and for cmd_id=16: interpretation (human-readable)
        None if the packet cannot be parsed.
    """
    try:
        outer = _parse_fields(raw)
        if not outer:
            return None
        inner_bytes = outer[0][2]  # field1 = inner header
        hdr = {fn: (wt, v) for fn, wt, v in _parse_fields(inner_bytes)}

        cmd_func = hdr.get(8, (0, 0))[1]
        cmd_id   = hdr.get(9, (0, 0))[1]
        seq      = hdr.get(14, (0, 0))[1]
        src      = hdr.get(2,  (0, 0))[1]
        version  = hdr.get(16, (0, 0))[1]
        pdata    = hdr.get(1,  (0, b""))[1]

        pdata_fields: dict[int, Any] = {
            fn: v
            for fn, _wt, v in _parse_fields(pdata if isinstance(pdata, bytes) else b"")
        }

        result: dict[str, Any] = {
            "cmd_func": cmd_func,
            "cmd_id":   cmd_id,
            "seq":      seq,
            "src":      src,
            "version":  version,
            "pdata_hex": pdata.hex() if pdata else "",
            "pdata_fields": pdata_fields,
        }

        # Human-readable interpretation for the important cmd_ids
        if cmd_func == 2:
            if cmd_id == 16:
                f1 = pdata_fields.get(1)
                f2 = pdata_fields.get(2)
                f3 = pdata_fields.get(3)
                f4 = pdata_fields.get(4)
                f5 = pdata_fields.get(5)
                if f1 == 0:
                    result["interpretation"] = "cmd_id=16: EIN-Befehl (field1=0) Geraet soll heizen"
                elif f1 == 2:
                    result["interpretation"] = "cmd_id=16: AUS-Befehl (field1=2) Geraet stoppt"
                elif f2 == 2:
                    # Cloud/iOS OFF format: {field2: 2}
                    result["interpretation"] = "cmd_id=16: AUS-Befehl (field2=2) Geraet stoppt (Variante B)"
                elif f2 == 4:
                    # Confirmed 2026-06-21: iOS sends this when tapping "Smart" in app.
                    # field2=4 ACTIVATES Smart Modus. Device reports field37=0 afterwards.
                    result["interpretation"] = "cmd_id=16: *** SMART MODUS aktivieren (field2=4) – Device wechselt zu Smart, field37→0 ***"
                elif f3 is not None:
                    # Sets targetTemp: field3=raw → targetTemp=raw÷2°C
                    # Confirmed 2026-06-21: field3=68→34°C, field3=92→46°C
                    result["interpretation"] = f"cmd_id=16: Zieltemperatur field3={f3} → {f3 // 2}°C"
                elif f4 is not None:
                    # Sets targetPower: field4=N → N÷2 W (z. B. von der PV-Regelung)
                    result["interpretation"] = f"cmd_id=16: Zielleistung field4={f4} → {f4 // 2} W"
                elif f5 is not None:
                    # Sets Smart Modus target temperature: field5=N → smartTargetTemp=N÷2°C
                    # Same ×2 encoding as field3/targetTemp.
                    # Confirmed 2026-06-21: field5=60→30°C, field5=120→60°C.
                    result["interpretation"] = f"cmd_id=16: Smart-Zieltemperatur field5={f5} → {f5 // 2}°C"
                elif f1 == 1:
                    # field1=1 does NOT stop the device - firmware ignores it!
                    result["interpretation"] = "cmd_id=16: ACHTUNG field1=1 wird ignoriert (kein Stop)"
                else:
                    result["interpretation"] = f"cmd_id=16: unbekannte pdata={pdata_fields}"
            elif cmd_id == 97:
                f2 = pdata_fields.get(2, 0)
                suffix = " Geraet laeuft)" if f2 else " Geraet aus/idle)"
                result["interpretation"] = f"cmd_id=97: Cloud-Keepalive (field2={f2}{suffix}"
            elif cmd_id == 1:
                result["interpretation"] = f"cmd_id=1: Parameter-Set pdata={pdata_fields}"
            elif cmd_id == 17:
                result["interpretation"] = "cmd_id=17: Cloud fragt alle Parameter ab"
            elif cmd_id in (20, 24):
                result["interpretation"] = f"cmd_id={cmd_id}: Cloud-ACK (keine Nutzlast)"
            else:
                result["interpretation"] = f"cmd_id={cmd_id}: unbekannt"
        elif cmd_func == 254:
            # Schedule packets carry the weekly Smart Modus plan (slots with start,
            # end time and target temperature). Full raw is logged at WARNING level
            # by mqtt.py so we can decode the smartTargetTemp protocol from it.
            pdata_len = len(pdata) if isinstance(pdata, bytes) else 0
            result["interpretation"] = f"cmd_func=254/cmd_id={cmd_id}: Smart-Modus-Schedule ({pdata_len} pdata bytes)"

        return result
    except Exception:
        return None


def encode_powerglow_command(params: dict[str, Any], device_sn: str) -> bytes | None:
    """
    Build a Delta3SendHeaderMsg protobuf command for the PowerGlow.

    Wire structure (app->device, src=32, dest=2, enc_type=0 = no XOR):

      Delta3SendHeaderMsg {
        field 1 (LEN) = Delta3Header {
          field  1 (LEN)   pdata      - unencrypted param payload
          field  2 (int32) src        = 32   (cloud/app)
          field  3 (int32) dest       = 2    (device)
          field  4 (int32) d_src      = 1
          field  5 (int32) d_dest     = 1
          field  6 (int32) enc_type   = 0    (no XOR for app->device)
          field  8 (int32) cmd_func   = 2
          field  9 (int32) cmd_id     = 1    (HEATING_ROD_PARAM_REPORT set)
          field 10 (int32) data_len   = len(pdata)
          field 11 (int32) need_ack   = 1
          field 14 (int32) seq        = random
          field 15 (int32) product_id = 8449
          field 16 (int32) version    = 19
          field 17 (int32) payload_ver= 1
          field 25 (LEN)   device_sn
        }
      }

    Returns serialised bytes, or None if params contains no settable fields.
    """
    pdata = _build_pdata(params)
    if not pdata:
        return None

    seq = random.randint(0, 0x7FFFFFFF)

    # Build Delta3Header
    # Field ordering matches what the EcoFlow iOS/cloud app sends (observed via
    # set-topic subscription).  Key differences from device-originated packets:
    #   field 7  = 3   (cloud always includes this in app->device messages)
    #   field 16 = 3   (cloud "app" version, not device firmware version 19)
    header = bytearray()
    header += _enc_bytes(1,  pdata)          # pdata
    header += _enc_varint(2,  32)            # src  = app (32)
    header += _enc_varint(3,  2)             # dest = device
    header += _enc_varint(4,  1)             # d_src
    header += _enc_varint(5,  1)             # d_dest
    header += _enc_varint(6,  0)             # enc_type = 0 (no XOR)
    header += _enc_varint(7,  3)             # field 7 = 3 (present in all cloud->device msgs)
    header += _enc_varint(8,  2)             # cmd_func
    header += _enc_varint(9,  1)             # cmd_id  = 1 (param report/set)
    header += _enc_varint(10, len(pdata))    # data_len
    header += _enc_varint(11, 1)             # need_ack
    header += _enc_varint(14, seq)           # seq
    header += _enc_varint(15, _PRODUCT_ID)   # product_id = 8449
    header += _enc_varint(16, 3)             # version = 3 (cloud/app format)
    header += _enc_varint(17, 1)             # payload_ver
    header += _enc_str(25,    device_sn)     # device_sn

    # Wrap in Delta3SendHeaderMsg
    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# Schedule-clear encoder (cmd_func=254, cmd_id=32)
# ---------------------------------------------------------------------------

def encode_powerglow_clear_schedule(slot: int, device_sn: str) -> bytes:
    """
    Build a cmd_func=254 / cmd_id=32 "clear schedule slot" message.

    The PowerGlow stores up to 3 schedule slots (1, 2, 3).  When all three
    are sent with 24 zero-bytes as schedule data the device discards its
    Smart Modus schedule and switches to Manueller Modus (mode=0) permanently.

    Wire format of pdata (matches what the device itself reports):
      field 2 (LEN) = sub-message {
          sub-field 1 (varint)  = current Unix timestamp
          sub-field 2 (varint)  = slot number  (1 / 2 / 3)
          sub-field 3 (LEN, 24) = schedule bytes, all 0x00 = empty/cleared
      }

    Header uses cmd_func=254, cmd_id=32, src=32 (app), enc_type=0 (no XOR).

    Args:
        slot: Schedule slot to clear (1, 2, or 3).
        device_sn: Device serial number string.

    Returns:
        Serialised bytes ready to publish via MQTT.
    """
    # Build inner sub-message
    sub_msg = bytearray()
    sub_msg += _enc_varint(1, int(time.time()))   # current timestamp
    sub_msg += _enc_varint(2, slot)                # slot number
    sub_msg += _enc_bytes(3, bytes(24))            # 24 zero-bytes = no schedule

    # pdata: field 2 wraps the sub-message
    pdata = _enc_bytes(2, bytes(sub_msg))

    seq = random.randint(0, 0x7FFFFFFF)

    # Delta3Header
    header = bytearray()
    header += _enc_bytes(1,  pdata)           # pdata
    header += _enc_varint(2,  32)             # src  = app
    header += _enc_varint(3,  2)              # dest = device
    header += _enc_varint(4,  1)              # d_src
    header += _enc_varint(5,  1)              # d_dest
    header += _enc_varint(6,  0)              # enc_type = 0 (no XOR)
    header += _enc_varint(7,  3)              # field 7 = 3 (present in device msgs)
    header += _enc_varint(8,  254)            # cmd_func = 254
    header += _enc_varint(9,  32)             # cmd_id  = 32 (schedule)
    header += _enc_varint(10, len(pdata))     # data_len
    header += _enc_varint(11, 1)              # need_ack
    header += _enc_varint(14, seq)            # seq
    header += _enc_varint(15, _PRODUCT_ID)    # product_id = 8449
    header += _enc_varint(16, 19)             # version
    header += _enc_varint(17, 1)              # payload_ver
    header += _enc_str(25,    device_sn)      # device_sn

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD16 encoder - the REAL on/off control command used by the EcoFlow cloud
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16(run_flag: int, device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 control command - the actual ON/OFF
    command that the EcoFlow iOS app / cloud sends to the device.

    Confirmed from MQTT set-topic log analysis (2026-06-20):
      ON:  pdata = {field1: 0} -> 0800  -- device starts heating immediately
      OFF: pdata = {field1: 2} -> 0802  -- device stops heating
      WRONG: pdata = {field1: 1} -> 0801 -- IGNORED by device firmware!
      ALT-OFF: pdata = {field3: 56} -> 1838 -- also stops, used by Smart Modus

    IMPORTANT: In mode=2 (Smart Modus), the EcoFlow cloud will automatically send
    a stop signal (field1=2 or field3=56) within seconds if temp > targetTemp.
    This is correct device behavior.  To keep heating, either:
      a) Set targetTemp higher than currentTemp in the EcoFlow app, or
      b) Re-enable the persist_loop in coordinator.py (sends ON every few sec)

    Header format is identical to the cloud's observed packets:
      field7=3, field16=3, field17=1
      No field15 (product_id) - cloud omits it in cmd_id=16 packets.

    Args:
        run_flag: 1 = turn device ON, 0 = turn device OFF.
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    # ON:  pdata = protobuf {field1: 0}  ->  08 00  (confirmed working)
    # OFF: pdata = protobuf {field1: 2}  ->  08 02  (confirmed from cloud analysis)
    # NOTE: {field1: 1} = 0801 is WRONG - device firmware ignores it completely!
    pdata = _enc_varint(1, 0 if run_flag else 2)

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)          # pdata
    header += _enc_varint(2,  32)            # src  = app (32)
    header += _enc_varint(3,  2)             # dest = device
    header += _enc_varint(4,  1)             # d_src
    header += _enc_varint(5,  1)             # d_dest
    # field6 (enc_type=0) intentionally omitted - cloud omits default-zero fields
    header += _enc_varint(7,  3)             # field7 = 3 (cloud format)
    header += _enc_varint(8,  2)             # cmd_func = 2
    header += _enc_varint(9,  16)            # cmd_id = 16 (ON/OFF control)
    header += _enc_varint(10, len(pdata))    # data_len
    header += _enc_varint(11, 1)             # need_ack
    header += _enc_varint(14, seq)           # seq
    # NO field15 (product_id) - cloud omits it in cmd_id=16 packets
    header += _enc_varint(16, 3)             # version = 3 (cloud format)
    header += _enc_varint(17, 1)             # payload_ver
    header += _enc_str(25,    device_sn)     # device_sn

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD16 mode-switch encoders
# Confirmed 2026-06-21 from iOS app set-topic log analysis:
#   Smart Modus  → pdata field2=4  (device field25 → 8)
#   Manuell Modus → pdata field2=2 (device field25 → 6)
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16_set_smart(device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 {field2: 4} command to activate Smart Modus.

    Confirmed 2026-06-21: iOS app sends field2=4 when tapping "Smart Modus".
    Device responds with ACK {field1:8, field25:8} and subsequent full reports
    show field25=8 (Smart Modus aktiv).
    pdata=1004, example raw: 0a370a021004...

    Args:
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    pdata = _enc_varint(2, 4)  # field2=4 → Smart Modus

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  16)
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)

    return _enc_bytes(1, bytes(header))


def encode_powerglow_cmd16_set_manuell(device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 {field2: 2} command to activate Manueller Modus.

    Confirmed 2026-06-21: iOS app sends field2=2 when tapping "Manueller Modus"
    (same pdata as the stop-heating Variante B command, but in context of Smart→Manuell
    it exits the Smart schedule and returns to manual control).
    Device responds with ACK {field1:6, field25:6} and subsequent full reports
    show field25=6 (Manueller Modus aktiv).
    pdata=1002, example raw: 0a370a021002...

    Args:
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    pdata = _enc_varint(2, 2)  # field2=2 → Manueller Modus (exits Smart)

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  16)
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD17 request-all-parameters encoder
# ---------------------------------------------------------------------------

def encode_powerglow_cmd17_request_all(device_sn: str) -> bytes:
    """
    Build a cmd_id=17 "request all parameters" packet.

    The EcoFlow cloud sends this command every ~5 minutes (observed in MQTT logs).
    The device responds with a full cmd_id=1 HEATING_ROD_PARAM_REPORT containing
    all current values: temp, runFlag, mode, targetTemp, targetPower, heatingPower, etc.

    We send this immediately after MQTT connects so HA entities reflect the real
    device state on startup instead of showing an indeterminate/OFF state for up
    to 60 seconds until the next periodic report arrives.

    pdata confirmed from EcoFlow cloud log:
        08011001180120012801300138014001480170017801
    (fields 1-9, 14, 15 each set to 1 — acts as a "please report" bitmask)

    Args:
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish on the SET topic.
    """
    # Fixed pdata — same in every cloud-sent cmd_id=17 observed in logs.
    pdata = bytes.fromhex("08011001180120012801300138014001480170017801")

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  17)          # cmd_id = 17
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)

    return _enc_bytes(1, bytes(header))


# Alias so existing coordinator import (encode_powerglow_cmd17) keeps working.
encode_powerglow_cmd17 = encode_powerglow_cmd17_request_all

# ---------------------------------------------------------------------------
# CMD16 settemp encoder - set target temperature via cmd_id=16 field3
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16_settemp(temp_celsius: float, device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 command to set the target temperature.

    The EcoFlow cloud uses {field3: N} (where N = temp_celsius × 2) to set
    the device's targetTemp.  This works in BOTH Manueller Modus AND Smart Modus,
    unlike cmd_id=1 field6 which is silently ignored by the cloud in Smart Modus.

    Confirmed 2026-06-21 from set-topic log analysis:
      field3=68  → device echoes field6=68  → targetTemp=34°C  (68÷2)
      field3=92  → device echoes field6=92  → targetTemp=46°C  (92÷2)
      field3=120 → device echoes field6=120 → targetTemp=60°C  (120÷2)
    Device responds with a partial cmd_id=1 report containing only field6.

    Args:
        temp_celsius: Target temperature in °C (will be encoded as temp×2).
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    # field3 = targetTemp × 2 (half-degree resolution)
    pdata = _enc_varint(3, round(temp_celsius * 2))

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)           # pdata
    header += _enc_varint(2,  32)             # src  = app (32)
    header += _enc_varint(3,  2)              # dest = device
    header += _enc_varint(4,  1)              # d_src
    header += _enc_varint(5,  1)              # d_dest
    # field6 (enc_type=0) intentionally omitted - cloud omits default-zero fields
    header += _enc_varint(7,  3)              # field7 = 3 (cloud format)
    header += _enc_varint(8,  2)              # cmd_func = 2
    header += _enc_varint(9,  16)             # cmd_id = 16
    header += _enc_varint(10, len(pdata))     # data_len
    header += _enc_varint(11, 1)              # need_ack
    header += _enc_varint(14, seq)            # seq
    # NO field15 (product_id) - cloud omits it in cmd_id=16 packets
    header += _enc_varint(16, 3)              # version = 3 (cloud format)
    header += _enc_varint(17, 1)              # payload_ver
    header += _enc_str(25,    device_sn)      # device_sn

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD16 smart_settemp encoder - set SMART MODUS temperature via cmd_id=16 field5
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16_setpower(watts: float, device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 command to set the target power (Zielleistung).

    Uses {field4: N} where N = watts * 2 (same x2 encoding as all other cmd_id=16 fields).
    The device echoes the new value as field7 in the next cmd_id=1 HEATING_ROD_PARAM_REPORT.

    Confirmed 2026-06-21 from iOS app set-topic log:
      field4=7000 -> 7000/2 = 3500 W
      field4=3030 -> 3030/2 = 1515 W  (intermediate drag position)
      field4=0    -> 0 W

    Args:
        watts: Target power in watts (will be encoded as watts*2).
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    pdata = _enc_varint(4, round(watts * 2))

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)           # pdata
    header += _enc_varint(2,  32)             # src  = app (32)
    header += _enc_varint(3,  2)              # dest = device
    header += _enc_varint(4,  1)              # d_src
    header += _enc_varint(5,  1)              # d_dest
    header += _enc_varint(7,  3)              # field7 = 3
    header += _enc_varint(8,  2)              # cmd_func = 2
    header += _enc_varint(9,  16)             # cmd_id = 16
    header += _enc_varint(10, len(pdata))     # data_len
    header += _enc_varint(11, 1)              # need_ack
    header += _enc_varint(14, seq)            # seq
    header += _enc_varint(16, 3)              # version = 3
    header += _enc_varint(17, 1)              # payload_ver
    header += _enc_str(25,    device_sn)      # device_sn

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD16 Smart Modus temperature encoder - set via field5 (x2 encoding)
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16_smart_settemp(temp_celsius: float, device_sn: str) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=16 command to set the Smart Modus target temperature.

    Uses {field5: N} where N = temp_celsius * 2.
    Confirmed 2026-06-21: field5=60 -> 30 deg C, field5=120 -> 60 deg C.
    The device echoes the new value as field8 (smartTargetTemp) in the next cmd_id=1.

    Args:
        temp_celsius: Target temperature in degrees Celsius.
        device_sn: Device serial number string.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    # field5 = smartTargetTemp x 2 (same encoding as field3/targetTemp)
    pdata = _enc_varint(5, round(temp_celsius * 2))

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    header += _enc_bytes(1,  pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  16)
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# CMD97 keepalive encoder
# ---------------------------------------------------------------------------

def encode_powerglow_cmd97(device_sn: str, run_flag: int = 0) -> bytes:
    """
    Build a cmd_func=2 / cmd_id=97 keepalive packet.

    The EcoFlow cloud sends these every ~3 seconds.  field2 mirrors the
    current run-state (1 = heating, 0 = idle).

    Args:
        device_sn: Device serial number string.
        run_flag: 1 if device is currently heating, 0 otherwise.

    Returns:
        Serialised protobuf bytes ready to publish via MQTT.
    """
    pdata = _enc_varint(2, run_flag) if run_flag else b""

    seq = random.randint(0, 0x7FFFFFFF)

    header = bytearray()
    if pdata:
        header += _enc_bytes(1, pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  97)
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)

    return _enc_bytes(1, bytes(header))


# ---------------------------------------------------------------------------
# Smart Schedule slot encoders (confirmed 2026-06-21 from iOS set-topic log)
#
# The app sends individual cmd_id=16 packets for each slot parameter:
#   field5 = Slot 1 temperature x 2  (degC x 2 encoding)
#   field6 = Slot 1 time x 2         (minutes-from-midnight x 2)
#   field7 = Slot 2 temperature x 2  (degC x 2 encoding)
#   field8 = Slot 2 time x 2         (minutes-from-midnight x 2)
#
# Examples confirmed:
#   30 degC -> field5 = 60  (30 x 2)
#   60 degC -> field7 = 120 (60 x 2)
#   10:28   -> field6 = 1256 (628 min x 2)
#   20:28   -> field8 = 2456 (1228 min x 2)
# ---------------------------------------------------------------------------

def _build_cmd16_single_field(field_num: int, raw_value: int, device_sn: str) -> bytes:
    """Build a cmd_id=16 packet with a single varint pdata field."""
    pdata = _enc_varint(field_num, raw_value)
    seq   = random.randint(0, 0x7FFFFFFF)
    header = bytearray()
    header += _enc_bytes(1,  pdata)
    header += _enc_varint(2,  32)
    header += _enc_varint(3,  2)
    header += _enc_varint(4,  1)
    header += _enc_varint(5,  1)
    header += _enc_varint(7,  3)
    header += _enc_varint(8,  2)
    header += _enc_varint(9,  16)
    header += _enc_varint(10, len(pdata))
    header += _enc_varint(11, 1)
    header += _enc_varint(14, seq)
    header += _enc_varint(16, 3)
    header += _enc_varint(17, 1)
    header += _enc_str(25,    device_sn)
    return _enc_bytes(1, bytes(header))


def encode_powerglow_cmd16_schedule_slot1_temp(temp_c: float, device_sn: str) -> bytes:
    """
    Set Smart-Schedule Slot 1 target temperature.

    Sends cmd_id=16 with field5 = round(temp_c * 2).
    Confirmed 2026-06-21: field5=60 -> 30 degC, field5=100 -> 50 degC.

    Args:
        temp_c:     Target temperature in degC (20..75).
        device_sn:  Device serial number.
    """
    return _build_cmd16_single_field(5, round(temp_c * 2), device_sn)


def encode_powerglow_cmd16_schedule_slot1_time(hour: int, minute: int, device_sn: str) -> bytes:
    """
    Set Smart-Schedule Slot 1 switch-on time.

    Sends cmd_id=16 with field6 = (hour*60 + minute) * 2.
    Confirmed 2026-06-21: 10:28 -> field6=1256 (628 min x 2).

    Args:
        hour:       Hour 0-23.
        minute:     Minute 0-59.
        device_sn:  Device serial number.
    """
    mins = hour * 60 + minute
    return _build_cmd16_single_field(6, mins * 2, device_sn)


def encode_powerglow_cmd16_schedule_slot2_temp(temp_c: float, device_sn: str) -> bytes:
    """
    Set Smart-Schedule Slot 2 target temperature.

    Sends cmd_id=16 with field7 = round(temp_c * 2).
    Confirmed 2026-06-21: field7=120 -> 60 degC, field7=110 -> 55 degC.

    Args:
        temp_c:     Target temperature in degC (20..75).
        device_sn:  Device serial number.
    """
    return _build_cmd16_single_field(7, round(temp_c * 2), device_sn)


def encode_powerglow_cmd16_schedule_slot2_time(hour: int, minute: int, device_sn: str) -> bytes:
    """
    Set Smart-Schedule Slot 2 switch-on time.

    Sends cmd_id=16 with field8 = (hour*60 + minute) * 2.
    Confirmed 2026-06-21: 20:28 -> field8=2456 (1228 min x 2).

    Args:
        hour:       Hour 0-23.
        minute:     Minute 0-59.
        device_sn:  Device serial number.
    """
    mins = hour * 60 + minute
    return _build_cmd16_single_field(8, mins * 2, device_sn)


# ---------------------------------------------------------------------------
# Tank configuration encoders (confirmed 2026-06-21 from iOS set-topic log)
#
#   field9  = Tankvolumen × 2  (Liter × 2)
#             300 L → field9=600, 400 L → field9=800, 500 L → field9=1000
#             Custom z.B. 550 L → field9=1100
#
#   field14 = Tanktyp
#             0 = Puffertank (max 80°C)  ← KORRIGIERT 2026-06-22
#             2 = Wassertank (max 60°C)
# ---------------------------------------------------------------------------

def encode_powerglow_cmd16_tank_volume(liters: int, device_sn: str) -> bytes:
    """
    Set the water tank volume.

    Sends cmd_id=16 with field9 = liters * 2.
    Confirmed 2026-06-21: 300 L -> field9=600, 400 L -> field9=800,
                          500 L -> field9=1000, 550 L -> field9=1100.

    Args:
        liters:     Tank volume in litres (e.g. 300, 400, 500 or custom).
        device_sn:  Device serial number.
    """
    return _build_cmd16_single_field(9, liters * 2, device_sn)


def encode_powerglow_cmd16_tank_type(tank_type: int, device_sn: str) -> bytes:
    """
    Set the tank type (affects maximum temperature limit).

    Sends cmd_id=16 with field14 = tank_type.
    KORRIGIERT 2026-06-22 (user-bestätigt gegen EcoFlow App):
        0 = Puffertank  (Heizung,     max 80 degC)
        2 = Wassertank  (Trinkwasser, max 60 degC)

    Args:
        tank_type:  0 for Puffertank, 2 for Wassertank.
        device_sn:  Device serial number.
    """
    return _build_cmd16_single_field(14, tank_type, device_sn)

