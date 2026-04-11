"""
SmartCare - Medical Sensor Simulator (Publisher)
Generates patient vital signs and publishes them via MQTT
"""
import paho.mqtt.client as mqtt
import json
import time
import random
import threading
from datetime import datetime

# ---- SETTINGS ----
BROKER_HOST = "localhost"
BROKER_PORT = 1883

# Patients in the system
PATIENTS = [
    {"id": "P001", "name": "John Smith",   "age": 45, "room": "101"},
    {"id": "P002", "name": "Mary Johnson", "age": 67, "room": "102"},
    {"id": "P003", "name": "David Brown",  "age": 33, "room": "103"},
]

# Normal medical ranges
NORMAL_RANGES = {
    "heart_rate":        {"min": 60,   "max": 100,  "unit": "BPM"},
    "systolic_bp":       {"min": 100,  "max": 140,  "unit": "mmHg"},
    "diastolic_bp":      {"min": 60,   "max": 90,   "unit": "mmHg"},
    "temperature":       {"min": 36.5, "max": 37.5, "unit": "C"},
    "glucose":           {"min": 3.9,  "max": 7.8,  "unit": "mmol/L"},
    "oxygen_saturation": {"min": 95,   "max": 100,  "unit": "%"},
}


class PatientSensorSimulator:
    """Simulates medical sensors for one patient"""

    def __init__(self, patient_info):
        self.patient = patient_info
        self.pid = patient_info["id"]
        # Base values (individual for each patient)
        self.base = {
            "heart_rate":        random.uniform(65, 90),
            "systolic_bp":       random.uniform(110, 130),
            "diastolic_bp":      random.uniform(65, 80),
            "temperature":       random.uniform(36.6, 37.2),
            "glucose":           random.uniform(4.5, 6.5),
            "oxygen_saturation": random.uniform(97, 99),
        }
        self.cycle_counter = 0  # Counter to simulate condition changes

    def generate_reading(self):
        """Generates sensor readings with small fluctuations"""
        self.cycle_counter += 1

        # Every 30 cycles - simulate possible deterioration
        stress_factor = 1.0
        if self.cycle_counter % 30 == 0:
            stress_factor = random.uniform(0.8, 1.4)

        def fluctuate(value, noise=0.02, factor=1.0):
            return round(value * factor + random.gauss(0, value * noise), 2)

        return {
            "patient_id":          self.pid,
            "patient_name":        self.patient["name"],
            "room":                self.patient["room"],
            "timestamp":           datetime.now().isoformat(),
            "heart_rate":          fluctuate(self.base["heart_rate"], 0.05, stress_factor),
            "systolic_bp":         fluctuate(self.base["systolic_bp"], 0.03, stress_factor),
            "diastolic_bp":        fluctuate(self.base["diastolic_bp"], 0.03, stress_factor),
            "temperature":         fluctuate(self.base["temperature"], 0.01),
            "glucose":             fluctuate(self.base["glucose"], 0.04),
            "oxygen_saturation":   min(100, fluctuate(self.base["oxygen_saturation"], 0.02)),
        }


def run_sensor(patient_info, interval=3):
    """Starts continuous data publishing for one patient"""
    simulator = PatientSensorSimulator(patient_info)
    client = mqtt.Client(client_id=f"sensor_{patient_info['id']}")
    client.connect(BROKER_HOST, BROKER_PORT)
    client.loop_start()

    print(f"[SENSOR] Started sensor for patient {patient_info['name']} (ID: {patient_info['id']})")

    while True:
        reading = simulator.generate_reading()
        topic = f"hospital/patient/{patient_info['id']}/vitals"
        payload = json.dumps(reading)
        client.publish(topic, payload, qos=1)  # QoS 1 - reliable delivery
        time.sleep(interval)


if __name__ == "__main__":
    print("=" * 50)
    print("   SmartCare - Sensor Simulator Started")
    print("=" * 50)

    threads = []
    for patient in PATIENTS:
        t = threading.Thread(target=run_sensor, args=(patient,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.5)

    print("\n[INFO] All sensors active. Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[INFO] Simulator stopped.")