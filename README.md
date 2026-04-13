[README_final.md](https://github.com/user-attachments/files/26673806/README_final.md)
# SmartCare — Real-Time Patient Monitoring System using MQTT

## Overview

This project implements a Smart Healthcare Monitoring System using the MQTT protocol.

The system simulates a hospital ward where multiple patients are monitored simultaneously in real time. Medical sensor data (heart rate, temperature, blood pressure, glucose, SpO2) is continuously published to an MQTT broker, analysed against clinical thresholds, and displayed on a live nurse dashboard.

Data is stored persistently using SQLite. Alerts are automatically generated when vital signs exceed safe limits.

---

## System Architecture

The system consists of three main components:

### 1. Publisher (Sensor Simulator)

- Simulates bedside medical sensors for 3 patients
- Generates realistic vital sign readings using Gaussian noise
- Applies stress factor every 30 cycles to simulate patient deterioration
- Publishes data to MQTT broker every 3 seconds
- Uses QoS Level 1 for reliable telemetry delivery

### 2. Backend Service (Subscriber + Analyser)

- Subscribes to all patient vital sign topics
- Applies rule-based medical threshold analysis
- Determines WARNING or CRITICAL severity
- Publishes alerts with nurse recommendations using QoS Level 2
- Stores all readings and alerts in SQLite database

### 3. Dashboard (Subscriber + GUI)

- Subscribes to vitals and alerts topics
- Displays real-time patient data in a Tkinter interface
- Shows alert notifications with nurse recommendations
- Provides comparison chart of patient condition over time

---

## Project Files

- `sensor_simulator.py` — Publisher: generates and sends patient vital signs
- `backend_service.py` — Subscriber: stores data, analyses thresholds, publishes alerts
- `dashboard.py` — Subscriber: live nurse monitoring GUI
- `smartcare_combined.py` — All-in-one version, no broker required
- `smartcare.db` — SQLite database (auto-created on first run)

---

## Team

| Name | Role |
|------|------|
| Fatima Nurlan | Backend Service, Database, Comparison Chart |
| Botagoz Ainabek | Nurse Dashboard UI |
| Assem Medinamova | Blood Pressure & Glucose Simulation, Topic Hierarchy, QoS Design |
| Sabina Saratayeva | Sensor Simulator, Heart Rate / Temperature / SpO2, Publisher, Performance Testing |

---

## MQTT Topics

- `hospital/patient/P001/vitals` — QoS 1
- `hospital/patient/P002/vitals` — QoS 1
- `hospital/patient/P003/vitals` — QoS 1
- `hospital/patient/+/vitals` — wildcard subscription (backend)
- `hospital/patient/{id}/alerts` — QoS 2

---

## QoS Levels

- **QoS 1** — used for sensor telemetry (at-least-once delivery). No vital sign reading is ever silently lost. Duplicate readings are acceptable and filtered by timestamp.
- **QoS 2** — used for medical alerts (exactly-once delivery). A missed alert is a patient safety risk. A duplicate alert causes false panic. QoS 2 guarantees each alert arrives exactly once.

---

## Medical Thresholds

| Vital Sign | Warning Range | Critical Range |
|------------|--------------|----------------|
| Heart Rate | 70 – 82 BPM | < 40 or > 150 BPM |
| Systolic BP | 105 – 118 mmHg | < 80 or > 180 mmHg |
| Diastolic BP | 68 – 78 mmHg | < 40 or > 110 mmHg |
| Temperature | 36.6 – 37.0°C | < 35.0 or > 40.0°C |
| Glucose | 4.8 – 6.0 mmol/L | < 3.0 or > 15.0 mmol/L |
| SpO2 | 98 – 100% | < 88% |

---

## Technologies Used

- Python 3.8+
- MQTT (Eclipse Mosquitto — local broker)
- SQLite
- JSON
- paho-mqtt
- Tkinter
- Matplotlib
- NumPy

---

## Installation

Install dependencies:

```
pip install paho-mqtt matplotlib numpy
```

Install and start Mosquitto broker:

```
# macOS
brew install mosquitto
brew services start mosquitto

# Ubuntu / Debian
sudo apt-get install mosquitto mosquitto-clients
sudo systemctl start mosquitto

# Windows — download from https://mosquitto.org/download/
```

---

## Running the System

### Recommended — run all three services

Terminal 1:
```
python backend_service.py
```

Terminal 2:
```
python dashboard.py
```

Terminal 3:
```
python sensor_simulator.py
```

Start the backend and dashboard before the simulator so they are ready to receive data.

### Alternative — combined mode (no broker required)

```
python smartcare_combined.py
```

Runs everything in a single process. Good for quick testing.

---

## Database

The SQLite database file is created automatically:

```
smartcare.db
```

Tables:
- `vitals` — all incoming patient readings with timestamps
- `alerts` — all detected threshold violations with severity and recommendations

---

## Test Results

Performance test: 3 patients, 10 minutes, all threads simultaneous

| Metric | Result |
|--------|--------|
| Patients monitored simultaneously | 3 |
| Total messages sent | 1,800 |
| Messages lost | 0 |
| Publish interval per patient | 3 seconds |
| Message loss rate | 0% |

---

## Bugs Found & Fixed

| Bug | Fix |
|-----|-----|
| Broker connection conflicts when all 3 threads started simultaneously | Added `time.sleep(0.5)` stagger between thread launches |
| SpO2 occasionally exceeded 100% due to noise + stress factor | Wrapped calculation in `min(100, value)` |

---

## Future Improvements

- TLS/SSL encryption to protect patient data in transit
- MQTT authentication with username/password and access control lists
- PostgreSQL to replace SQLite for production multi-user access
- Real hospital network testing for latency and packet loss validation
- 20+ patient load test to validate scalability
- Gradual patient deterioration trajectories instead of random stress spikes

---

*SmartCare · Team 4 · KZ4005CMD Integrative Project · Coventry University Kazakhstan · April 2026*
