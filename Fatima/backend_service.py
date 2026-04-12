"""
SmartCare - Backend Service (Subscriber + Analyser + Database)
==============================================================
This module is the core backend of the SmartCare system.
It subscribes to all patient vital sign topics via MQTT,
validates and stores incoming data in a SQLite database,
applies rule-based medical threshold analysis to detect
abnormal values, and publishes alerts with nurse recommendations.

Author: Fatima Nurlan (Group Leader)
Module: KZ4005CMD - Integrative Project
"""

# ---- IMPORTS ----
import paho.mqtt.client as mqtt   # MQTT library for subscribe/publish
import json                        # JSON for parsing incoming messages
import sqlite3                     # SQLite for persistent data storage
from datetime import datetime      # DateTime for timestamps

# ---- BROKER SETTINGS ----
BROKER_HOST = "localhost"   # MQTT broker address
BROKER_PORT = 1883           # Default MQTT port
DB_FILE = "smartcare.db"     # SQLite database file name

# ---- MEDICAL THRESHOLDS ----
# Two-level threshold system: WARNING and CRITICAL
# WARNING = value is outside normal range but not immediately dangerous
# CRITICAL = value is dangerously abnormal, requires immediate action
THRESHOLDS = {
    "heart_rate":         {"min": 70,   "max": 82,   "critical_min": 40,   "critical_max": 150},
    "systolic_bp":        {"min": 105,  "max": 118,  "critical_min": 80,   "critical_max": 180},
    "diastolic_bp":       {"min": 68,   "max": 78,   "critical_min": 40,   "critical_max": 110},
    "temperature":        {"min": 36.6, "max": 37.0, "critical_min": 35.0, "critical_max": 40.0},
    "glucose":            {"min": 4.8,  "max": 6.0,  "critical_min": 3.0,  "critical_max": 15.0},
    "oxygen_saturation":  {"min": 98,   "max": 100,  "critical_min": 88,   "critical_max": 100},
}

# ---- NURSE RECOMMENDATIONS ----
# Pre-defined clinical recommendations for each type of alert
# These are displayed in the nurse dashboard when an alert is triggered
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


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def init_database():
    """
    Initialises the SQLite database by creating tables if they don't exist.
    
    Creates two tables:
    - vitals: stores every incoming patient reading with full timestamp
    - alerts: stores every detected anomaly with severity and recommendation
    
    Uses IF NOT EXISTS to safely run on every startup without data loss.
    """
    conn = sqlite3.connect(DB_FILE)   # Connect to (or create) the database file
    c = conn.cursor()                  # Create a cursor for executing SQL

    # Create vitals table - stores all incoming sensor readings
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

    # Create alerts table - stores all detected threshold violations
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

    conn.commit()   # Save changes to database
    conn.close()    # Close connection to free resources
    print("[DB] Database initialised: smartcare.db")


def save_vitals(data):
    """
    Saves one set of patient vital signs to the vitals table.
    
    Uses parameterised queries (?) to prevent SQL injection attacks.
    A new connection is created per call to ensure thread safety
    when multiple patients send data simultaneously.
    
    Args:
        data (dict): Patient reading containing all vital signs
    """
    conn = sqlite3.connect(DB_FILE)   # Open fresh connection
    c = conn.cursor()

    # Parameterised INSERT - ? placeholders prevent SQL injection
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

    conn.commit()   # Commit the transaction
    conn.close()    # Release the connection


def save_alert(alert_data):
    """
    Saves a generated alert to the alerts table.
    
    Called whenever the threshold analysis engine detects an anomaly.
    Stores all alert details including severity, value, threshold breached,
    and the recommended nurse action.
    
    Args:
        alert_data (dict): Alert details including severity and recommendation
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Insert alert record with all details
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


# ============================================================
# THRESHOLD ANALYSIS ENGINE
# ============================================================

def analyze_vitals(data, mqtt_client):
    """
    Rule-based medical threshold analysis engine.
    
    Checks each vital sign reading against predefined thresholds.
    Uses a two-level severity system:
    - WARNING: value outside normal range, monitor closely
    - CRITICAL: value dangerously abnormal, immediate action required
    
    For each detected violation, the engine:
    1. Determines severity (WARNING or CRITICAL)
    2. Retrieves appropriate nurse recommendation
    3. Saves alert to database
    4. Publishes alert via MQTT with QoS 2 (exactly-once delivery)
    
    Args:
        data (dict): Patient vital sign reading
        mqtt_client: Active MQTT client for publishing alerts
    """
    pid = data["patient_id"]   # Extract patient ID for alert

    # Dictionary of metrics to check against thresholds
    checks = {
        "heart_rate":        data["heart_rate"],
        "systolic_bp":       data["systolic_bp"],
        "diastolic_bp":      data["diastolic_bp"],
        "temperature":       data["temperature"],
        "glucose":           data["glucose"],
        "oxygen_saturation": data["oxygen_saturation"],
    }

    # Iterate through each vital sign and check against thresholds
    for metric, value in checks.items():
        if metric not in THRESHOLDS:
            continue   # Skip if no threshold defined for this metric

        th = THRESHOLDS[metric]   # Get thresholds for this metric
        severity = None            # Reset severity for each check
        direction = None           # "high" or "low" direction of violation

        # Check CRITICAL thresholds first (higher priority)
        if "critical_max" in th and value > th["critical_max"]:
            severity = "CRITICAL"
            direction = "high"
        elif "critical_min" in th and value < th["critical_min"]:
            severity = "CRITICAL"
            direction = "low"
        # Then check WARNING thresholds
        elif value > th["max"]:
            severity = "WARNING"
            direction = "high"
        elif value < th["min"]:
            severity = "WARNING"
            direction = "low"

        # If a violation was detected - generate and publish alert
        if severity:
            key = f"{metric}_{direction}"   # e.g. "heart_rate_high"

            # Look up nurse recommendation for this alert type
            recommendation = RECOMMENDATIONS.get(key, "Please consult a doctor.")

            # Determine which threshold was breached
            threshold_val = th["critical_max"] if direction == "high" else th["critical_min"]
            if severity == "WARNING":
                threshold_val = th["max"] if direction == "high" else th["min"]

            # Build human-readable alert message
            message = (f"{severity}: {metric.replace('_', ' ').title()} = {value} "
                       f"({'>' if direction == 'high' else '<'} {threshold_val})")

            # Build complete alert dictionary
            alert = {
                "patient_id":     pid,
                "patient_name":   data["patient_name"],
                "room":           data["room"],
                "timestamp":      data["timestamp"],
                "alert_type":     key,
                "severity":       severity,
                "value":          value,
                "threshold":      threshold_val,
                "message":        message,
                "recommendation": recommendation,
            }

            save_alert(alert)   # Persist alert to database

            # Publish alert to MQTT broker
            # QoS 2 = "exactly once" - critical for medical alerts
            # Ensures alert is delivered exactly once, never lost or duplicated
            alert_topic = f"hospital/patient/{pid}/alerts"
            mqtt_client.publish(alert_topic, json.dumps(alert), qos=2)

            print(f"  [{severity}] {data['patient_name']} - {message}")


# ============================================================
# MQTT CALLBACK FUNCTIONS
# ============================================================

def on_connect(client, userdata, flags, reason_code, properties=None):
    """
    Callback triggered when the backend successfully connects to the broker.
    
    Subscribes to all patient vitals topics using the '+' wildcard,
    which matches any single topic level. This means the backend
    receives data from ALL patients without needing individual subscriptions.
    
    Args:
        client: MQTT client instance
        reason_code: Connection result code (Success = connected)
    """
    print(f"[MQTT] Connected! Code: {reason_code}")

    # Subscribe using '+' wildcard to receive all patient data
    # hospital/patient/+/vitals matches P001, P002, P003, etc.
    client.subscribe("hospital/patient/+/vitals", qos=1)
    print("[MQTT] Subscribed to: hospital/patient/+/vitals")


def on_message(client, userdata, msg):
    """
    Callback triggered when a new MQTT message is received.
    
    This is the main message processing function. It:
    1. Decodes the JSON payload
    2. Prints a summary to the console
    3. Saves the reading to the database
    4. Runs threshold analysis to check for anomalies
    
    Args:
        client: MQTT client instance
        userdata: User-defined data (unused)
        msg: MQTT message object containing topic and payload
    """
    try:
        # Decode JSON payload from bytes to Python dictionary
        data = json.loads(msg.payload.decode("utf-8"))

        pid = data.get("patient_id", "???")
        name = data.get("patient_name", "")

        # Print received data summary to console
        print(f"\n[DATA] {name} ({pid}) | "
              f"HR:{data['heart_rate']} "
              f"BP:{data['systolic_bp']}/{data['diastolic_bp']} "
              f"T:{data['temperature']}C "
              f"SpO2:{data['oxygen_saturation']}%")

        save_vitals(data)              # Step 1: Save to database
        analyze_vitals(data, client)   # Step 2: Check for anomalies

    except Exception as e:
        # Catch any errors to prevent the backend from crashing
        print(f"[ERROR] Failed to process message: {e}")


# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("   SmartCare - Backend Service Started")
    print("=" * 55)

    # Step 1: Initialise the database (creates tables if needed)
    init_database()

    # Step 2: Create MQTT client
    # Compatible with both paho-mqtt v1 and v2
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()

    # Step 3: Register callback functions
    client.on_connect = on_connect   # Called when connected to broker
    client.on_message = on_message   # Called when message is received

    # Step 4: Connect to broker
    print(f"[MQTT] Connecting to {BROKER_HOST}:{BROKER_PORT}...")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)

    print("[INFO] Backend active. Press Ctrl+C to stop.\n")

    # Step 5: Start blocking network loop
    # loop_forever() handles reconnections automatically
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Backend stopped.")