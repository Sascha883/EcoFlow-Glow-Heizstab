# Changelog

Alle wichtigen Änderungen an diesem Projekt werden hier dokumentiert.

---

## [2.1.3] – 2026-10-19

### Behoben
- Release 2.1.2 enthielt eine fehlerhafte `__init__.py` – Setup schlug fehl mit „No setup or config entry setup function defined“. 2.1.3 enthält den korrekten, aufgeräumten Stand (Code identisch mit 2.1.2).

## [2.1.2] – 2026-10-19

### Neu
- **Sensor-Typ für PV-Regelung** – Überschuss / Netz-Einspeisung / Netz-Bezug; bei Netzsensoren wird die eigene Heizleistung zurückgerechnet
- **Mindestlaufzeit und Mindestpause** (Standard je 60 s)

### Behoben
- PV-Regelung schaltete bei Netzsensoren (z. B. GoodWe active_power) alle 10–20 s ein/aus
- Leistung wird auf Rampenschritt abgerundet, bei Netzbezug sofort abgesenkt
- Log-Spam: Zielleistungs-Befehle und Start-Probes nur noch auf DEBUG

## [2.1.1] – 2026-10-18

### Behoben
- Heizleistung wurde ohne geöffnete EcoFlow-App nicht aktualisiert – Integration sendet jetzt selbst den App-Keepalive (alle 10 s) und fragt den Status alle 60 s ab
- Schalter- und Modus-Abgleich las Werte aus der falschen Ebene der Nachricht
- Hintergrund-Task stoppte bei einem Fehler unbemerkt

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
