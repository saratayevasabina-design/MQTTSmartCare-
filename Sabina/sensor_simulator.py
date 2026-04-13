"""
SmartCare - Medical Sensor Simulator (Publisher)
=================================================
This module simulates bedside medical sensors for multiple patients.
It generates realistic vital sign readings with physiological fluctuations
and publishes them to the MQTT broker using the publish-subscribe pattern.

Heart rate, temperature and oxygen saturation (SpO2) simulation:
    Author: Sabina Saratayeva
    
Blood pressure (systolic/diastolic) and glucose simulation:
    Author: Assem Medinamova
    
QoS level specification and MQTT topic hierarchy design:
    Author: Assem Medinamova
    
Module: KZ4005CMD - Integrative Project
"""

# ---- IMPORTS ----
import paho.mqtt.client as mqtt   # MQTT library for publishing sensor data
import json                        # JSON for serialising message payloads
import time                        # Time for controlling publishing interval
import random                      # Random for generating realistic fluctuations
import threading                   # Threading for running multiple patients simultaneously
from datetime import datetime      # DateTime for adding timestamps to readings

# ---- BROKER SETTINGS ----
BROKER_HOST = "localhost"   # MQTT broker address (Eclipse Mosquitto)
BROKER_PORT = 1883           # Default MQTT port

# ---- PATIENT REGISTRY ----
# List of patients currently monitored in the system
# Each patient gets an independent sensor thread
PATIENTS = [
    {"id": "P001", "name": "John Smith",   "age": 45, "room": "101"},
    {"id": "P002", "name": "Mary Johnson", "age": 67, "room": "102"},
    {"id": "P003", "name": "David Brown",  "age": 33, "room": "103"},
]

# ---- NORMAL MEDICAL RANGES ----
# Reference ranges for healthy adult vital signs
# Used for context - actual threshold analysis is done in backend_service.py
NORMAL_RANGES = {
    "heart_rate":        {"min": 60,   "max": 100,  "unit": "BPM"},
    "systolic_bp":       {"min": 100,  "max": 140,  "unit": "mmHg"},
    "diastolic_bp":      {"min": 60,   "max": 90,   "unit": "mmHg"},
    "temperature":       {"min": 36.5, "max": 37.5, "unit": "C"},
    "glucose":           {"min": 3.9,  "max": 7.8,  "unit": "mmol/L"},
    "oxygen_saturation": {"min": 95,   "max": 100,  "unit": "%"},
}


# ============================================================
# PATIENT SENSOR SIMULATOR CLASS
# Authors: Sabina Saratayeva (heart rate, temperature, SpO2)
#          Assem Medinamova (blood pressure, glucose)
# ============================================================

class PatientSensorSimulator:
    """
    Simulates all bedside medical sensors for a single patient.
    
    Each patient instance has unique baseline vital sign values,
    representing individual physiological differences between patients.
    Gaussian noise is applied to simulate realistic sensor fluctuations.
    
    Every 30 reading cycles, a stress factor is applied to simulate
    sudden patient deterioration events, which may trigger alerts
    in the backend threshold analysis engine.
    """

    def __init__(self, patient_info):
        """
        Initialises the sensor simulator for one patient.
        
        Sets individual baseline values using random ranges to ensure
        each patient has different normal values, reflecting real-world
        physiological variation between individuals.
        
        Args:
            patient_info (dict): Patient metadata including id, name, age, room
        """
        self.patient = patient_info
        self.pid = patient_info["id"]   # Unique patient identifier (e.g. P001)

        # Individual baseline vital signs — randomised per patient
        # Heart rate and temperature baselines: Sabina Saratayeva
        # Blood pressure and glucose baselines: Assem Medinamova
        self.base = {
            "heart_rate":        random.uniform(65, 90),    # BPM — Sabina
            "systolic_bp":       random.uniform(110, 130),  # mmHg — Assem
            "diastolic_bp":      random.uniform(65, 80),    # mmHg — Assem
            "temperature":       random.uniform(36.6, 37.2),# Celsius — Sabina
            "glucose":           random.uniform(4.5, 6.5),  # mmol/L — Assem
            "oxygen_saturation": random.uniform(97, 99),    # % SpO2 — Sabina
        }

        self.cycle_counter = 0   # Tracks number of readings for stress simulation

    def generate_reading(self):
        """
        Generates one complete set of vital sign readings with fluctuations.
        
        Applies Gaussian noise to each baseline value to simulate realistic
        sensor variation. Every 30 cycles, a random stress factor is applied
        to all values to simulate patient deterioration events.
        
        Heart rate, temperature, SpO2 logic: Sabina Saratayeva
        Blood pressure, glucose logic: Assem Medinamova
        
        Returns:
            dict: Complete patient reading with all vital signs and metadata,
                  formatted as a JSON-serialisable dictionary for MQTT publishing
        """
        self.cycle_counter += 1

        # Every 30 cycles — simulate possible patient deterioration
        # stress_factor < 1.0 = values drop (e.g. SpO2, temperature drop)
        # stress_factor > 1.0 = values rise (e.g. heart rate, BP spike)
        stress_factor = 1.0
        if self.cycle_counter % 30 == 0:
            stress_factor = random.uniform(0.8, 1.4)

        def fluctuate(value, noise=0.02, factor=1.0):
            """
            Applies Gaussian noise and stress factor to a baseline value.
            
            Args:
                value: The baseline vital sign value
                noise: Standard deviation as fraction of value (default 2%)
                factor: Stress multiplier (1.0 = normal, >1.0 = elevated)
            Returns:
                float: Fluctuated value rounded to 2 decimal places
            """
            return round(value * factor + random.gauss(0, value * noise), 2)

        # Build and return the complete reading dictionary
        # This structure matches the JSON schema designed by Assem Medinamova
        # Topic: hospital/patient/{id}/vitals — designed by Assem Medinamova
        return {
            "patient_id":          self.pid,
            "patient_name":        self.patient["name"],
            "room":                self.patient["room"],
            "timestamp":           datetime.now().isoformat(),

            # Heart rate — Sabina Saratayeva
            # Stress factor applied: simulates tachycardia/bradycardia events
            "heart_rate":          fluctuate(self.base["heart_rate"], 0.05, stress_factor),

            # Blood pressure (systolic + diastolic) — Assem Medinamova
            # Stress factor applied: simulates hypertension/hypotension events
            "systolic_bp":         fluctuate(self.base["systolic_bp"], 0.03, stress_factor),
            "diastolic_bp":        fluctuate(self.base["diastolic_bp"], 0.03, stress_factor),

            # Body temperature — Sabina Saratayeva
            # No stress factor: temperature changes are slower physiologically
            "temperature":         fluctuate(self.base["temperature"], 0.01),

            # Blood glucose — Assem Medinamova
            # No stress factor: glucose changes are slower than HR/BP
            "glucose":             fluctuate(self.base["glucose"], 0.04),

            # Oxygen saturation (SpO2) — Sabina Saratayeva
            # Capped at 100% using min() — SpO2 cannot exceed 100%
            "oxygen_saturation":   min(100, fluctuate(self.base["oxygen_saturation"], 0.02)),
        }


# ============================================================
# MQTT PUBLISHER FUNCTION
# Author: Sabina Saratayeva
# ============================================================

def run_sensor(patient_info, interval=3):
    """
    Starts continuous MQTT publishing for a single patient.
    
    Creates an independent MQTT client per patient to ensure
    simultaneous publishing from multiple patients without interference.
    Uses QoS Level 1 for telemetry data (at-least-once delivery).
    
    QoS 1 was selected for telemetry because:
    - Occasional duplicate readings are acceptable (can be identified by timestamp)
    - More efficient than QoS 2 for high-frequency sensor data
    - Ensures no readings are silently dropped during network issues
    
    Author: Sabina Saratayeva
    QoS level specification: Assem Medinamova
    
    Args:
        patient_info (dict): Patient metadata dictionary
        interval (int): Publishing interval in seconds (default: 3)
    """
    simulator = PatientSensorSimulator(patient_info)   # Create simulator for this patient

    # Create unique MQTT client per patient to avoid connection conflicts
    client = mqtt.Client(client_id=f"sensor_{patient_info['id']}")
    client.connect(BROKER_HOST, BROKER_PORT)
    client.loop_start()   # Start non-blocking network loop

    print(f"[SENSOR] Started sensor for patient {patient_info['name']} (ID: {patient_info['id']})")

    # Continuous publishing loop — runs until process is terminated
    while True:
        reading = simulator.generate_reading()   # Generate new vital sign reading

        # MQTT topic hierarchy — designed by Assem Medinamova
        # Format: hospital/patient/{patient_id}/vitals
        # The '+' wildcard in the backend subscriber matches all patient IDs
        topic = f"hospital/patient/{patient_info['id']}/vitals"

        payload = json.dumps(reading)   # Serialise dictionary to JSON string

        # Publish with QoS 1 — at-least-once delivery for telemetry
        # QoS specification designed by Assem Medinamova
        client.publish(topic, payload, qos=1)

        time.sleep(interval)   # Wait before next reading


# ============================================================
# MAIN ENTRY POINT
# Author: Sabina Saratayeva
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("   SmartCare - Sensor Simulator Started")
    print("=" * 50)

    # Launch one thread per patient for simultaneous monitoring
    # threading.Thread with daemon=True ensures threads stop when main process stops
    threads = []
    for patient in PATIENTS:
        t = threading.Thread(target=run_sensor, args=(patient,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.5)   # Small delay between thread starts to avoid connection race

    print("\n[INFO] All sensors active. Press Ctrl+C to stop.\n")

    # Keep main thread alive while sensor threads run
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[INFO] Simulator stopped.")