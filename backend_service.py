"""
SmartCare - Backend Service (Subscriber + Analyzer + Database)
Receives sensor data, analyzes it, and stores it in SQLite
"""
import paho.mqtt.client as mqtt
import json
import sqlite3
from datetime import datetime

# ---- SETTINGS ----
BROKER_HOST = "localhost"
BROKER_PORT = 1883
DB_FILE = "smartcare.db"

THRESHOLDS = {
    "heart_rate":         {"min": 50,   "max": 120,  "critical_min": 40,   "critical_max": 150},
    "systolic_bp":        {"min": 90,   "max": 160,  "critical_min": 80,   "critical_max": 180},
    "diastolic_bp":       {"min": 50,   "max": 100,  "critical_min": 40,   "critical_max": 110},
    "temperature":        {"min": 35.5, "max": 38.5, "critical_min": 35.0, "critical_max": 40.0},
    "glucose":            {"min": 3.5,  "max": 10.0, "critical_min": 3.0,  "critical_max": 15.0},
    "oxygen_saturation":  {"min": 92,   "max": 100,  "critical_min": 88,   "critical_max": 100},
}

RECOMMENDATIONS = {
    "heart_rate_high":         "Check patient, possible tachycardia. Consider ECG.",
    "heart_rate_low":          "Bradycardia detected. Notify duty doctor immediately.",
    "systolic_bp_high":        "High blood pressure. Monitor every 15 minutes.",
    "systolic_bp_low":         "Hypotension detected. Check patient hydration.",
    "temperature_high":        "Fever detected. Measure temperature manually, consider antipyretic.",
    "temperature_low":         "Hypothermia detected. Cover patient, notify doctor urgently.",
    "glucose_high":            "Hyperglycemia detected. Check insulin therapy.",
    "glucose_low":             "Hypoglycemia! Administer glucose or sweet drink urgently.",
    "oxygen_saturation_low":   "Low SpO2! Check airways, consider oxygen supplementation.",
}

def init_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS vitals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id          TEXT NOT NULL,
            patient_name        TEXT,
            room                TEXT,
            timestamp           TEXT NOT NULL,
            heart_rate          REAL,
            systolic_bp         REAL,
            diastolic_bp        REAL,
            temperature         REAL,
            glucose             REAL,
            oxygen_saturation   REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id      TEXT NOT NULL,
            patient_name    TEXT,
            room            TEXT,
            timestamp       TEXT NOT NULL,
            alert_type      TEXT NOT NULL,
            severity        TEXT NOT NULL,
            value           REAL,
            threshold       REAL,
            message         TEXT,
            recommendation  TEXT,
            acknowledged    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] Database initialized: smartcare.db")

def save_vitals(data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO vitals
        (patient_id, patient_name, room, timestamp,
         heart_rate, systolic_bp, diastolic_bp, temperature, glucose, oxygen_saturation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["patient_id"], data["patient_name"], data["room"], data["timestamp"],
        data["heart_rate"], data["systolic_bp"], data["diastolic_bp"],
        data["temperature"], data["glucose"], data["oxygen_saturation"]
    ))
    conn.commit()
    conn.close()

def save_alert(alert_data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO alerts
        (patient_id, patient_name, room, timestamp, alert_type,
         severity, value, threshold, message, recommendation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        alert_data["patient_id"], alert_data["patient_name"], alert_data["room"],
        alert_data["timestamp"], alert_data["alert_type"], alert_data["severity"],
        alert_data["value"], alert_data["threshold"], alert_data["message"],
        alert_data["recommendation"]
    ))
    conn.commit()
    conn.close()

def analyze_vitals(data, mqtt_client):
    pid = data["patient_id"]
    checks = {
        "heart_rate":        data["heart_rate"],
        "systolic_bp":       data["systolic_bp"],
        "diastolic_bp":      data["diastolic_bp"],
        "temperature":       data["temperature"],
        "glucose":           data["glucose"],
        "oxygen_saturation": data["oxygen_saturation"],
    }
    for metric, value in checks.items():
        if metric not in THRESHOLDS:
            continue
        th = THRESHOLDS[metric]
        severity = None
        direction = None
        if "critical_max" in th and value > th["critical_max"]:
            severity = "CRITICAL"; direction = "high"
        elif "critical_min" in th and value < th["critical_min"]:
            severity = "CRITICAL"; direction = "low"
        elif value > th["max"]:
            severity = "WARNING"; direction = "high"
        elif value < th["min"]:
            severity = "WARNING"; direction = "low"
        if severity:
            key = f"{metric}_{direction}"
            recommendation = RECOMMENDATIONS.get(key, "Please consult a doctor.")
            threshold_val = th["critical_max"] if direction == "high" else th["critical_min"]
            if severity == "WARNING":
                threshold_val = th["max"] if direction == "high" else th["min"]
            message = (f"{severity}: {metric.replace('_',' ').title()} = {value} "
                       f"({'>' if direction=='high' else '<'} {threshold_val})")
            alert = {
                "patient_id": pid, "patient_name": data["patient_name"],
                "room": data["room"], "timestamp": data["timestamp"],
                "alert_type": key, "severity": severity, "value": value,
                "threshold": threshold_val, "message": message,
                "recommendation": recommendation,
            }
            save_alert(alert)
            mqtt_client.publish(f"hospital/patient/{pid}/alerts", json.dumps(alert), qos=2)
            print(f"  [{severity}] {data['patient_name']} - {message}")

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT] Connected! Code: {reason_code}")
    client.subscribe("hospital/patient/+/vitals", qos=1)
    print("[MQTT] Subscribed to: hospital/patient/+/vitals")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        pid = data.get("patient_id", "???")
        name = data.get("patient_name", "")
        print(f"\n[DATA] {name} ({pid}) | HR:{data['heart_rate']} "
              f"BP:{data['systolic_bp']}/{data['diastolic_bp']} "
              f"T:{data['temperature']}C SpO2:{data['oxygen_saturation']}%")
        save_vitals(data)
        analyze_vitals(data, client)
    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    print("=" * 55)
    print("   SmartCare - Backend Service Started")
    print("=" * 55)
    init_database()
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    print(f"[MQTT] Connecting to {BROKER_HOST}:{BROKER_PORT}...")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    print("[INFO] Backend active. Press Ctrl+C to stop.\n")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Backend stopped.")