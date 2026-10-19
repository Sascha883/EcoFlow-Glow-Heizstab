"""
Utility module for PowerOcean integration.

This module provides schema definitions, enumerations, and helper classes for
device detection, sensor metadata management, and reporting modes in the
PowerOcean Home Assistant integration.

Classes:
    BoxSchema: TypedDict for device schema definitions
    ReportMode: Enumeration for different report modes
    DeviceRole: Enumeration for device roles
    SensorMetaHelper: Helper class for sensor metadata (units, descriptions, icons)
"""

from collections.abc import Callable
from enum import Enum, StrEnum
from typing import TypedDict

DEVICE_SN_KEYS = (
    "evSn",
    "hrSn",
    "bpSn",
    "devSn",
)


class ReportMode(Enum):
    """Enumeration for different report modes in the PowerOcean integration."""

    DEFAULT = "data"
    BATTERY = "BP_STA_REPORT"
    WALLBOX = "EDEV_PARAM_REPORT"
    WALLBOX_SYS = "EDEV_SYS_REPORT"
    HEATING_ROD = "HEATING_ROD_PARAM_REPORT"
    HEATING_ROD_ENERGY = "HEATING_ROD_ENERGY_STREAM_REPORT"
    CHARGEBOX = "EVCHARGING_REPORT"
    EMS = "EMS_HEARTBEAT"
    PARALLEL = "PARALLEL_ENERGY_STREAM_REPORT"
    EMS_CHANGE = "EMS_CHANGE_REPORT"
    EMS_STATE_CHANGE = "EMS_STATE_CHANGE_REPORT"
    ENERGY_STREAM = "ENERGY_STREAM_REPORT"


REPORT_DATAPOINTS: dict[str, set[str]] = {
    ReportMode.DEFAULT.value: {
        "sysLoadPwr",
        "sysGridPwr",
        "mpptPwr",
        "bpPwr",
        "online",
        "dcdcPwr",
        "todayElectricityGeneration",
        "monthElectricityGeneration",
        "yearElectricityGeneration",
        "totalElectricityGeneration",
        "systemName",
    },
    ReportMode.ENERGY_STREAM.value: {
        "pv1Pwr",
        "pv2Pwr",
        "pv3Pwr",
        "pvInvPwr",
        "otherPvPwr",
        "gridPwr",
        "loadPwr",
        "heatingPower",
        "dcdcPwr",
    },
    ReportMode.EMS_CHANGE.value: {
        "bpTotalChgEnergy",
        "bpTotalDsgEnergy",
        "bpSoc",
        "bpOnlineSum",
        "emsCtrlLedBright",
        "mppt1FaultCode",
        "mppt1WarningCode",
        "mppt2FaultCode",
        "mppt2WarningCode",
        "mppt3FaultCode",
        "mppt3WarningCode",
    },
    ReportMode.BATTERY.value: {
        "bpSn",
        "bpPwr",
        "bpSoc",
        "bpSoh",
        "bpVol",
        "bpAmp",
        "bpCycles",
        "bpSysState",
        "bmsChgDsgSta",
        "bpBalanceState",
        "bpRemainWatth",
        "bmsRunSta",
        "bpEnvTemp",
        "bpMinCellTemp",
        "bpMaxCellTemp",
        "moduleAplSwVer",
        "bpAccuChgEnergy",
        "bpAccuDsgEnergy",
    },
    ReportMode.EMS.value: {
        "bpRemainWatth",
        "emsBpAliveNum",
        "emsBpPower",
        "emsBpSelfcheckState",
        "emsMpptSelfcheckState",
        "emsMpptRunState",
        "pcsActPwr",
        "pcsMeterPower",
        "emsSystemState",
        "mpptRatePower",
        "gridOutDayEnergy",
        "meterACurrent",
        "meterBCurrent",
        "bpInDayEnergy",
        "meterPowerFactor",
        "isPvToInvDirectly",
        "bp1SocCoefficient",
        "sysRunMeterFeedPower",
        "inverterBand",
        "loadDayEnergy",
        "startDischargePower",
        "bpDichargeAbilityAdjustValueLv2",
        "bpDichargeAbilityAdjustValueLv1",
        "meterAddress",
        "gridFeedRate",
        "currentNetif",
        "batterySocLowerLimit",
        "bpChgDsgSta",
        "autoDetectStartPowerEn",
        "meterPhasePower",
        "sysErrCode",
        "meterTotalPower",
        "bpBusVoltageBaseCoefficient",
        "bpSaveSocStopDelay",
        "bpOutDayEnergy",
        "bpBusVoltCoefficient",
        "pvInvWiringMode",
        "underVoltageProtectPoint",
        "batterySocUpperLimit",
        "bpRunDelay",
        "meterBVoltage",
        "bpStopDelay",
        "errCode",
        "gridInDayEnergy",
        "sysRunMeterTakePower",
        "bpSoc",
        "bp3SocCoefficient",
        "bpErrorCodeMask",
        "workingMode",
        "appCtrlState",
        "bpSocBaseCoefficient",
        "emsErrorCodeMask",
        "meterRatio",
        "sysWorkSta",
        "pv2ErrorCodeMask",
        "pv1ErrorCodeMask",
        "manualSetStartChargePower",
        "pvWiringType",
        "emsErrCode",
        "bp2SocCoefficient",
        "mpptWithstandVoltage",
        "meterCVoltage",
        "pvaErrorCodeMask",
        "meterAVoltage",
        "invWiringType",
        "disOrChargeAbilityStepValue",
        "reportCycleTime",
        "pvInDayEnergy",
        "meterType",
        "meterCCurrent",
        "mpptVoltageMinimum",
        "mpptVoltageMaximum",
        "gridFeedPowerMinimum",
        "epoSwitchState",
    },
    ReportMode.CHARGEBOX.value: {
        "evSn",
        "workMode",
        "useGridFirst",
        "evOnoffSet",
        "orderStartTimestamp",
        "onlineBits",
        "errorCode",
        "evUserManual",
        "evChargingEnergy",
        "evCurrSet",
        "chargeVehicleId",
        "chargingStatus",
        "evPwr",
    },
    ReportMode.HEATING_ROD.value: {
        "hrSn",
        "selfcheckPercent",
        "temp",
        "keepWarmTemp",      # field 4 float: Komfort-/Haltewärme-Setpoint (Zweck TBD)
        "targetTemp",        # field 6 varint÷2: Zieltemperatur Manueller Modus (bestätigt 27°C)
        "smartTargetTemp",   # field 38 varint: Zieltemperatur Smart Modus (bestätigt 60°C)
        "scheduleTime",      # field 8 varint (legacy): alte Einschaltzeit (encoding TBD)
        # Smart Schedule (2 Slots) – bestätigt 2026-06-21 aus iOS set-topic log:
        #   SET:  field5=temp1×2, field6=time1_min×2, field7=temp2×2, field8=time2_min×2
        #   REPORT: scheduleTime1/2 werden als Minuten ab Mitternacht zurückgemeldet
        "scheduleTime1",     # Slot 1 Einschaltzeit (Minuten ab Mitternacht)
        "scheduleTime2",     # Slot 2 Einschaltzeit (Minuten ab Mitternacht)
        "scheduleSlot1Temp", # Slot 1 Zieltemperatur (°C, aus REPORT wenn verfügbar)
        "scheduleSlot2Temp", # Slot 2 Zieltemperatur (°C, aus REPORT wenn verfügbar)
        # "onlineBits",
        "errorCode",
        "runFlag",
        "mode",
        "heatingPower",
        "waterTankVolume",  # field22 in cmd_id=1 REPORT (L×2): Tankvolumen in Litern
        "tankType",         # field14 in cmd_id=16 SET → 0=Wassertank(60°C), 2=Puffertank(80°C)
                            # REPORT-Feld für tankType noch unbekannt – State zeigt None bis Gerät antwortet
        "runStat",
        "targetPower",
    },
    ReportMode.HEATING_ROD_ENERGY.value: {
        "hrPwr",
        "fromPv",
        "fromBat",
        "fromGrid",
    },
    ReportMode.WALLBOX.value: {
        "devSn",
        "workMode",
        "chargeTarget",
        "switchBits",
        "chargeVehicleId",
        "chargingPwr",
        "timeToUseCar",
        "currentOuputMax",
        "userCurrentSet",
        "solarCurrentMin",
        "phaseSpecified",
        "chargingStatus",
    },
    ReportMode.WALLBOX_SYS.value: {
        "devSn",
        "allocatedPower",
        "realPowerLock",
        "refPower",
        "feedPwrCap",
        "startState",
        "errorCode",
        "warnCode",
        "socCur",
        "pclPwrBase",
    },
}


class BoxSchema(TypedDict):
    """Schema for a detected device (box) in PowerOcean responses."""

    detect: Callable[[dict], bool]
    mode: str  # "boxed" | "single"
    sn_path: list[str]
    model: str
    name_prefix: str
    paths: dict[str, list[str]] | None
    sensors: set[str]  # 🔑 jetzt als Set für schnelle Lookup


BOX_SCHEMAS: dict[str, BoxSchema] = {
    "battery": {
        "mode": "boxed",
        "detect": lambda p: "bpSn" in p,
        "sn_path": ["bpSn"],
        "model": "PowerOcean Battery",
        "name_prefix": "Battery",
        "paths": None,
        "sensors": REPORT_DATAPOINTS[ReportMode.BATTERY.value],
    },
    "wallbox": {
        "mode": "boxed",
        "detect": lambda p: "pileChargingParamReport" in p,
        "sn_path": ["devInfo", "devSn"],
        "model": "PowerOcean PowerPulse",
        "name_prefix": "PowerPulse",
        "paths": {
            "devSn": ["devInfo", "devSn"],
            "workMode": ["pileChargingParamReport", "paramSet", "workMode"],
            "timeToUseCar": [
                "pileChargingParamReport",
                "paramSet",
                "smartMode",
                "timeToUseCar",
            ],
            "chargeTarget": [
                "pileChargingParamReport",
                "paramSet",
                "smartMode",
                "chargeTarget",
            ],
            "chargingStatus": ["pileChargingParamReport", "chargingStatus"],
            "chargingPwr": ["pileChargingParamReport", "chargingPwr"],
            "currentVehicleComsumption": ["vehicleInfo", "currentVehicleComsumption"],
            "orderChargingEnergy": [
                "orderRealStatus",
                "orderChargingEnergy",
            ],
        },
        "sensors": REPORT_DATAPOINTS[ReportMode.WALLBOX.value],
    },
    "charge": {
        "mode": "single",
        "detect": lambda p: "evPlugAndPlay" in p,
        "sn_path": ["evSn"],
        "model": "PowerOcean PowerPulse",
        "name_prefix": "PowerPulse",
        "paths": None,
        "sensors": REPORT_DATAPOINTS[ReportMode.CHARGEBOX.value],
    },
    "heating_rod": {
        "mode": "single",
        "detect": lambda p: "hrSn" in p,
        "sn_path": ["hrSn"],
        "model": "PowerOcean PowerGlow",
        "name_prefix": "PowerGlow",
        "paths": None,
        "sensors": REPORT_DATAPOINTS[ReportMode.HEATING_ROD.value],
    },
}


class DeviceRole(StrEnum):
    """Enumeration for device roles in the PowerOcean integration."""

    MASTER = "_master"
    SLAVE = "_slave"
    ALL = "_all"
    EMPTY = ""  # Used when no specific role is assigned


def _join_id(*parts: str) -> str:
    return "_".join(p for p in parts if p)


def clean_zero(v: float, eps: float = 0.05) -> float:
    """
    Setzt kleine Werte auf exakt 0, um Floating-Point Artefakte zu vermeiden.

    Args:
        v (float): Der zu prüfende Wert.
        eps (float, optional): Die Schwelle unterhalb derer der Wert auf 0 gesetzt wird.

    Returns:
        float: 0.0, wenn abs(v) < eps, sonst der Originalwert v.

    Beispiel:
        >>> clean_zero(0.03)
        0.0
        >>> clean_zero(-0.02)
        0.0
        >>> clean_zero(0.1)
        0.1

    """
    if abs(v) < eps:
        return 0.0
    return v


def decode_version(value: int) -> str:
    """
    Decode a 32-bit integer into a dotted version string.

    The integer is interpreted as:
        - bits 24-31: major
        - bits 16-23: minor
        - bits 8-15: patch
        - bits 0-7: build

    Args:
        value: Encoded 32-bit version integer.

    Returns:
        Version string in the format "major.minor.patch.build".

    """
    major = (value >> 24) & 0xFF
    minor = (value >> 16) & 0xFF
    patch = (value >> 8) & 0xFF
    build = value & 0xFF
    return f"{major}.{minor}.{patch}.{build}"


def decode_product_info(value: int) -> dict[str, int]:
    """
    Decode a 16-bit product information value.

    The integer is interpreted as:
        - bits 8-15: product ID
        - bits 0-7: hardware revision

    Args:
        value: Encoded product information integer.

    Returns:
        A dict with keys "product_id" and "hardware_revision".
    """
    return {
        "product_id": (value >> 8) & 0xFF,
        "hardware_revision": value & 0xFF,
    }
