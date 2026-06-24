# EcoFlow PowerGlow – Home Assistant Integration

[![GitHub release](https://img.shields.io/github/release/Sascha883/EcoFlow-PowerGlow?include_prereleases=&sort=semver&color=blue)](https://github.com/Sascha883/EcoFlow-PowerGlow/releases/)
[![GitHub issues](https://img.shields.io/github/issues/Sascha883/EcoFlow-PowerGlow)](https://github.com/Sascha883/EcoFlow-PowerGlow/issues)
[![Code size](https://img.shields.io/github/languages/code-size/Sascha883/EcoFlow-PowerGlow?color=red)](https://github.com/Sascha883/EcoFlow-PowerGlow)
[![Last commit](https://img.shields.io/github/last-commit/Sascha883/EcoFlow-PowerGlow?style=flat-square)](https://github.com/Sascha883/EcoFlow-PowerGlow/commits/main)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Home Assistant](https://home-assistant.io/) Custom Integration fuer den **EcoFlow PowerGlow Heizstab** (3,5 kW / 6 kW / 9 kW) mit vollstaendiger PV-Ueberschuss-Regelung, MQTT-Push-Anbindung und fertigem Lovelace-Dashboard.

> Basiert auf [niltrip/powerocean](https://github.com/niltrip/powerocean) – erweitert um vollstaendige PowerGlow-Standalone-Unterstuetzung und intelligente PV-Ueberschuss-Regelung.

---

## Features

| Funktion | Beschreibung |
|---|---|
| **Echtzeit-MQTT** | Push-Daten ohne Polling – sofortige Statusaktualisierung |
| **3 Betriebsmodi** | Manuell, Smart (Zeitplan), PV-Ueberschuss |
| **PV-Ueberschuss-Regelung** | Automatische Leistungsanpassung anhand eines beliebigen HA-Sensors |
| **Sanfte Leistungsrampe** | Schrittweise Anpassung konfigurierbar (Standard: 100 W / 10 s) |
| **Temperaturabschaltung** | PV-Betrieb stoppt bei Solltemperatur, Wiedereinschalten mit 5 K Hysterese |
| **Smart Modus / Zeitplan** | 2 Zeitslots mit individueller Zieltemperatur |
| **Tank-Einstellungen** | Wassertank (max 60 C) oder Puffertank (max 80 C), 300-500 L oder individuell |
| **PV-Proxy-Sensor** | Stabile HA-Entitaet spiegelt den konfigurierten PV-Sensor |
| **Lovelace-Dashboard** | Fertiges 3-Tab-Dashboard (Uebersicht, Steuerung, Diagnose) |

---

## Voraussetzungen

- Home Assistant **2024.1** oder neuer
- EcoFlow Konto (E-Mail + Passwort)
- EcoFlow PowerGlow Heizstab (Modell-ID **88**)
- Seriennummer des Geraets (EcoFlow App > Geraet > Info)
- Internetzugang des HA-Servers (Port 8883 fuer MQTT TLS)
- *(Optional)* HA-Sensor der den PV-Ueberschuss in Watt liefert

---

## Installation

### Via HACS (empfohlen)

1. HACS > Drei-Punkte-Menu > **Benutzerdefinierte Repositories**
2. URL eingeben: `https://github.com/Sascha883/EcoFlow-PowerGlow`
3. Kategorie: **Integration** > Hinzufuegen
4. **PowerOcean** in HACS suchen und installieren
5. Home Assistant neu starten

### Manuell

1. ZIP herunterladen und entpacken
2. Ordner `custom_components/powerocean/` nach `config/custom_components/powerocean/` kopieren
3. Home Assistant neu starten

---

## Einrichtung

### Geraet hinzufuegen

*Einstellungen > Geraete & Dienste > Integration hinzufuegen > PowerOcean*

| Feld | Beschreibung |
|---|---|
| Seriennummer | Z. B. `HF31ZE1AZHAG0125` |
| E-Mail | EcoFlow-Konto E-Mail |
| Passwort | EcoFlow-Konto Passwort |
| Modell | **PowerGlow (Eigenstaendig)** |
| Geraetename | Anzeigename in HA, z. B. `Heizung PowerGlow` |

![Setup Schritt 1](documentation/setup_step_01.png)

### PV-Ueberschuss konfigurieren

*Einstellungen > Geraete & Dienste > PowerOcean > Konfigurieren > PV-Ueberschuss*

| Einstellung | Standard |
|---|---|
| PV-Ueberschuss aktivieren | Aus |
| PV-Ueberschuss-Sensor | HA-Entitaet mit Typ `power` (W) |
| Minimaler Ueberschuss | 200 W |
| Maximale Heizleistung | 3500 W |
| Abschalt-Temperatur | 60 C |

### Regellogik

*Einstellungen > Geraete & Dienste > PowerOcean > Konfigurieren > Regellogik*

| Einstellung | Bereich | Standard |
|---|---|---|
| Leistungsaenderung pro Schritt (Rampe) | 50-500 W | 100 W |
| Rampen-Intervall | 5-600 s | 10 s |
| Anlaufverzoegerung | 0-300 s | 30 s |

---

## Betriebsmodi

### Manueller Modus
Direktsteuerung mit fester Zieltemperatur und Heizleistung.

### Smart Modus
Zeitplan-basiertes Heizen: 2 Slots mit individueller Startzeit und Zieltemperatur.

### PV-Ueberschuss-Regelung
Aktivierung ueber die Integrationsoptionen. Die Heizleistung wird per Rampe sanft an den verfuegbaren Solarstrom angepasst:

```
PV-Ueberschuss 1800 W, Start bei 0 W:
  0 s:    0 W ->  100 W
 10 s:  100 W ->  200 W
...
180 s: 1700 W -> 1800 W  Ziel erreicht

PV faellt auf 400 W:
190 s: 1800 W -> 1700 W
...
310 s:  500 W ->  400 W  neues Ziel
```

**Temperatursperre:** PV pausiert wenn Wasser-Abschalt-Temperatur erreicht ist.  
Wiedereinschalten erst bei `Abschalt-Temp - 5 K` (Hysterese).

---

## Entitaeten

### Sensoren

| Entitaet | Beschreibung |
|---|---|
| `sensor.*_temp` | Wassertemperatur (C) |
| `sensor.*_heatingpower` | Aktuelle Heizleistung (W) |
| `sensor.*_targetpower` | Gesetzte Zielleistung (W) |
| `sensor.*_mode` | Betriebsmodus |
| `sensor.*_runflag` | Heizstab aktiv (0/1) |
| `sensor.*_frompv` | Leistung aus PV (W) |
| `sensor.*_fromgrid` | Leistung aus Netz (W) |
| `sensor.*_frombat` | Leistung aus Batterie (W) |
| `sensor.*_pv_uberschuss` | PV-Ueberschuss Proxy |

### Steuerentitaeten

| Entitaet | Beschreibung |
|---|---|
| `switch.*_ein_aus` | Heizstab Ein/Aus |
| `select.*_betriebsmodus` | Betriebsmodus: Manuell / Smart |
| `number.*_zieltemperatur` | Zieltemperatur Manuell (C) |
| `number.*_zielleistung` | Heizleistung Manuell (W) |
| `time.*_zeitplan_timer_1/2` | Startzeit Zeitplan-Slot 1/2 |
| `number.*_temperatur_timer_1/2` | Temperatur Zeitplan-Slot 1/2 (C) |
| `select.*_tankvolumen` | Tankvolumen (300/400/500 L / Anpassen) |
| `select.*_typ_wassertank` | Tanktyp (Wassertank / Puffertank) |

---

## Dashboard

Das fertige Lovelace-Dashboard liegt im Ordner `PowerGlow-Dashboard/`.

**Import:** HA > Dashboard > YAML-Modus > Inhalt von `powerglow_dashboard.yaml` einfuegen

Tabs: **Uebersicht** (Temperatur, Leistung, PV) | **Steuerung** (Modi, Timer) | **Diagnose** (Rohdaten)

---

## Fehlerbehebung

**Debug-Logging:**

```yaml
logger:
  default: warn
  logs:
    custom_components.powerocean: debug
```

| Problem | Loesung |
|---|---|
| Integration verbindet nicht | Zugangsdaten pruefen. Port 8883 muss erreichbar sein. |
| Entitaeten unavailable | PowerGlow mit WLAN verbunden? Nach HA-Start 30-60 s warten. |
| PV-Regelung startet nicht | PV-Sensor in Integrationsoptionen konfigurieren (Typ `power`). |
| Entity-IDs weichen ab | Developer Tools > Zustaende: aktuelle IDs pruefen und in Dashboard-YAML anpassen. |

---

## Lizenz

[MIT](LICENSE)

Basiert auf [niltrip/powerocean](https://github.com/niltrip/powerocean).  
Inspiriert von [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud).
