# Changelog

Alle wichtigen Änderungen an diesem Projekt werden hier dokumentiert.

---

## [2.1.0] – 2026-06-24

### Neu
- **PowerGlow Standalone-Unterstützung** (Modell-ID 88) – vollständige MQTT-Steuerung ohne PowerOcean-Wechselrichter
- **PV-Überschuss-Regelung** – automatische Leistungsanpassung anhand eines beliebigen HA-Sensors
- **Sanfte Leistungsrampe** – schrittweise Leistungsänderung (Standard 100 W / 10 s, konfigurierbar 50–500 W)
- **Temperaturabschaltung** – PV pausiert bei Abschalt-Temperatur, Wiedereinschalten mit 5 K Hysterese
- **PV-Proxy-Sensor** – stabile HA-Entität `sensor.*_pv_uberschuss` spiegelt den konfigurierten PV-Sensor
- **Tankvolumen-Auswahl** – 300 L / 400 L / 500 L oder individuell per Number-Entity
- **Tanktyp-Auswahl** – Wassertank (max 60 °C) oder Puffertank (max 80 °C)
- **Zeitplan (Smart Modus)** – 2 Slots mit individueller Startzeit und Zieltemperatur
- **Config-Flow Untermenüs** – getrennte Seiten für PV-Überschuss, Regellogik, Tank und Zeitplan
- **Lovelace-Dashboard** – fertiges 3-Tab-Dashboard (Übersicht / Steuerung / Diagnose)

### Geändert
- Betriebsmodus-Select auf 2 Optionen reduziert: Manuell / Smart (Solar-Protokoll unbekannt, entfernt)
- Config-Flow komplett neu strukturiert für PowerGlow-Standalone-Geräte
- Koordinator: `_pv_config` wird immer befüllt, unabhängig ob PV aktiv

### Behoben
- Null-Byte-Problem bei Datei-Schreiboperationen unter Windows (NTFS UTF-8-Truncation)
- Tanktyp-Mapping korrigiert: field14=0 → Puffertank, field14=2 → Wassertank
- Betriebsmodus-Erkennung korrigiert: field25 (nicht field37) ist der Modusindikator

---

## [2.0.0] – 2025-xx-xx

### Neu
- Initiale Unterstützung für PowerOcean Single Phase, DC Fit, Plus
- MQTT Push-Anbindung (paho-mqtt + protobuf)
- Automatische Entity-Registrierung via Sensor-Endpoints

*(Basiert auf [niltrip/powerocean](https://github.com/niltrip/powerocean))*
