"""
SmartCare - Combined Medical Monitoring App

Default mode runs the simulator, analyzer/database, and dashboard in one process.
No MQTT broker is required for the default integrated mode.
"""
import argparse
import json
import queue
import random
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# ============================================================
# SETTINGS
# ============================================================

BROKER_HOST = "localhost"
BROKER_PORT = 1883
DB_FILE = BASE_DIR / "smartcare.db"
DB_TIMEOUT_SECONDS = 10
MQTT_RETRY_SECONDS = 3
PUBLISH_INTERVAL_SECONDS = 3
CHART_LIMIT = 30

PATIENTS = [
    {"id": "P001", "name": "John Smith", "age": 45, "room": "101"},
    {"id": "P002", "name": "Mary Johnson", "age": 67, "room": "102"},
    {"id": "P003", "name": "David Brown", "age": 33, "room": "103"},
]

PATIENT_LOOKUP = {patient["id"]: patient for patient in PATIENTS}

METRICS_DISPLAY = {
    "heart_rate": {"label": "Heart Rate", "unit": "BPM", "color": "#e74c3c", "normal": (60, 100)},
    "systolic_bp": {"label": "Systolic BP", "unit": "mmHg", "color": "#e67e22", "normal": (100, 140)},
    "diastolic_bp": {"label": "Diastolic BP", "unit": "mmHg", "color": "#f39c12", "normal": (60, 90)},
    "temperature": {"label": "Temperature", "unit": "C", "color": "#9b59b6", "normal": (36.5, 37.5)},
    "glucose": {"label": "Glucose", "unit": "mmol/L", "color": "#1abc9c", "normal": (3.9, 7.8)},
    "oxygen_saturation": {"label": "SpO2", "unit": "%", "color": "#2980b9", "normal": (95, 100)},
}

METRIC_KEYS = tuple(METRICS_DISPLAY.keys())

THRESHOLDS = {
    "heart_rate": {"min": 50, "max": 120, "critical_min": 40, "critical_max": 150},
    "systolic_bp": {"min": 90, "max": 160, "critical_min": 80, "critical_max": 180},
    "diastolic_bp": {"min": 50, "max": 100, "critical_min": 40, "critical_max": 110},
    "temperature": {"min": 35.5, "max": 38.5, "critical_min": 35.0, "critical_max": 40.0},
    "glucose": {"min": 3.5, "max": 10.0, "critical_min": 3.0, "critical_max": 15.0},
    "oxygen_saturation": {"min": 92, "max": 100, "critical_min": 88, "critical_max": 100},
}

RECOMMENDATIONS = {
    "heart_rate_high": "Check patient, possible tachycardia. Consider ECG.",
    "heart_rate_low": "Bradycardia detected. Notify duty doctor immediately.",
    "systolic_bp_high": "High blood pressure. Monitor every 15 minutes.",
    "systolic_bp_low": "Hypotension detected. Check patient hydration.",
    "temperature_high": "Fever detected. Measure temperature manually, consider antipyretic.",
    "temperature_low": "Hypothermia detected. Cover patient, notify doctor urgently.",
    "glucose_high": "Hyperglycemia detected. Check insulin therapy.",
    "glucose_low": "Hypoglycemia! Administer glucose or sweet drink urgently.",
    "oxygen_saturation_low": "Low SpO2! Check airways, consider oxygen supplementation.",
}


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database():
    with closing(get_db_connection()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                patient_name TEXT,
                room TEXT,
                timestamp TEXT NOT NULL,
                heart_rate REAL,
                systolic_bp REAL,
                diastolic_bp REAL,
                temperature REAL,
                glucose REAL,
                oxygen_saturation REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                patient_name TEXT,
                room TEXT,
                timestamp TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                value REAL,
                threshold REAL,
                message TEXT,
                recommendation TEXT,
                acknowledged INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vitals_patient_timestamp
            ON vitals (patient_id, timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_patient_ack_timestamp
            ON alerts (patient_id, acknowledged, timestamp)
        """)
        conn.commit()
    print(f"[DB] Database ready: {DB_FILE}")


def save_vitals(data):
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("""
                INSERT INTO vitals
                (patient_id, patient_name, room, timestamp,
                 heart_rate, systolic_bp, diastolic_bp, temperature, glucose, oxygen_saturation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data["patient_id"], data["patient_name"], data["room"], data["timestamp"],
                data["heart_rate"], data["systolic_bp"], data["diastolic_bp"],
                data["temperature"], data["glucose"], data["oxygen_saturation"],
            ))


def save_alert(alert):
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("""
                INSERT INTO alerts
                (patient_id, patient_name, room, timestamp, alert_type,
                 severity, value, threshold, message, recommendation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert["patient_id"], alert["patient_name"], alert["room"],
                alert["timestamp"], alert["alert_type"], alert["severity"],
                alert["value"], alert["threshold"], alert["message"],
                alert["recommendation"],
            ))


def get_latest_vitals(patient_id, limit=CHART_LIMIT):
    with closing(get_db_connection()) as conn:
        rows = conn.execute("""
            SELECT * FROM vitals
            WHERE patient_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (patient_id, limit)).fetchall()
    return list(reversed(rows))


def get_first_and_last_vitals(patient_id):
    with closing(get_db_connection()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM vitals WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()[0]
        if total < 2:
            return None, None

        n = min(5, total // 2)
        first_rows = conn.execute("""
            SELECT * FROM vitals
            WHERE patient_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
        """, (patient_id, n)).fetchall()
        last_rows = conn.execute("""
            SELECT * FROM vitals
            WHERE patient_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (patient_id, n)).fetchall()

    last_rows = list(reversed(last_rows))

    def average(rows):
        result = {}
        for key in METRIC_KEYS:
            values = [row[key] for row in rows if row[key] is not None]
            result[key] = round(sum(values) / len(values), 2) if values else 0
        return result

    return average(first_rows), average(last_rows)


def get_unacknowledged_alerts(patient_id):
    with closing(get_db_connection()) as conn:
        return conn.execute("""
            SELECT * FROM alerts
            WHERE patient_id = ? AND acknowledged = 0
            ORDER BY timestamp DESC
            LIMIT 20
        """, (patient_id,)).fetchall()


def get_unacknowledged_alert_counts():
    with closing(get_db_connection()) as conn:
        rows = conn.execute("""
            SELECT patient_id, COUNT(*) AS total
            FROM alerts
            WHERE acknowledged = 0
            GROUP BY patient_id
        """).fetchall()
    return {row["patient_id"]: row["total"] for row in rows}


# ============================================================
# SENSOR + ANALYZER
# ============================================================

class PatientSensorSimulator:
    def __init__(self, patient_info):
        self.patient = patient_info
        self.pid = patient_info["id"]
        self.base = {
            "heart_rate": random.uniform(65, 90),
            "systolic_bp": random.uniform(110, 130),
            "diastolic_bp": random.uniform(65, 80),
            "temperature": random.uniform(36.6, 37.2),
            "glucose": random.uniform(4.5, 6.5),
            "oxygen_saturation": random.uniform(97, 99),
        }
        self.cycle_counter = 0

    def generate_reading(self):
        self.cycle_counter += 1
        stress_factor = random.uniform(0.8, 1.4) if self.cycle_counter % 30 == 0 else 1.0

        def fluctuate(value, noise=0.02, factor=1.0):
            return round(value * factor + random.gauss(0, value * noise), 2)

        oxygen = fluctuate(self.base["oxygen_saturation"], 0.02)
        return {
            "patient_id": self.pid,
            "patient_name": self.patient["name"],
            "room": self.patient["room"],
            "timestamp": datetime.now().isoformat(),
            "heart_rate": fluctuate(self.base["heart_rate"], 0.05, stress_factor),
            "systolic_bp": fluctuate(self.base["systolic_bp"], 0.03, stress_factor),
            "diastolic_bp": fluctuate(self.base["diastolic_bp"], 0.03, stress_factor),
            "temperature": fluctuate(self.base["temperature"], 0.01),
            "glucose": fluctuate(self.base["glucose"], 0.04),
            "oxygen_saturation": max(0, min(100, oxygen)),
        }


def validate_vitals(data):
    required = ("patient_id", "patient_name", "room", "timestamp", *METRIC_KEYS)
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    for key in METRIC_KEYS:
        data[key] = float(data[key])
    return data


def analyze_vitals(data):
    alerts = []
    for metric in METRIC_KEYS:
        value = data.get(metric)
        threshold = THRESHOLDS[metric]
        severity = None
        direction = None

        if value > threshold["critical_max"]:
            severity, direction = "CRITICAL", "high"
        elif value < threshold["critical_min"]:
            severity, direction = "CRITICAL", "low"
        elif value > threshold["max"]:
            severity, direction = "WARNING", "high"
        elif value < threshold["min"]:
            severity, direction = "WARNING", "low"

        if not severity:
            continue

        alert_type = f"{metric}_{direction}"
        threshold_value = threshold["critical_max"] if direction == "high" else threshold["critical_min"]
        if severity == "WARNING":
            threshold_value = threshold["max"] if direction == "high" else threshold["min"]

        alert = {
            "patient_id": data["patient_id"],
            "patient_name": data["patient_name"],
            "room": data["room"],
            "timestamp": data["timestamp"],
            "alert_type": alert_type,
            "severity": severity,
            "value": value,
            "threshold": threshold_value,
            "message": (
                f"{severity}: {metric.replace('_', ' ').title()} = {value} "
                f"({'>' if direction == 'high' else '<'} {threshold_value})"
            ),
            "recommendation": RECOMMENDATIONS.get(alert_type, "Please consult a doctor."),
        }
        save_alert(alert)
        alerts.append(alert)
        print(f"  [{severity}] {data['patient_name']} - {alert['message']}")

    return alerts


def run_local_sensor(patient_info, backend_queue, ui_queue, stop_event, interval=PUBLISH_INTERVAL_SECONDS):
    simulator = PatientSensorSimulator(patient_info)
    print(f"[SENSOR] Local sensor started for {patient_info['name']} ({patient_info['id']})")
    while not stop_event.is_set():
        reading = simulator.generate_reading()
        backend_queue.put(reading)
        ui_queue.put(("vitals", reading))
        stop_event.wait(interval)


def run_local_backend(backend_queue, ui_queue, stop_event):
    print("[BACKEND] Local analyzer started")
    while not stop_event.is_set():
        try:
            data = backend_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        try:
            data = validate_vitals(data)
            save_vitals(data)
            print(
                f"[DATA] {data['patient_name']} ({data['patient_id']}) | "
                f"HR:{data['heart_rate']} BP:{data['systolic_bp']}/{data['diastolic_bp']} "
                f"T:{data['temperature']}C SpO2:{data['oxygen_saturation']}%"
            )
            for alert in analyze_vitals(data):
                ui_queue.put(("alerts", alert))
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            print(f"[BACKEND ERROR] {exc}")


# ============================================================
# DASHBOARD
# ============================================================

class SmartCareDashboard:
    def __init__(self, root, message_queue=None, enable_mqtt=False):
        self.root = root
        self.message_queue = message_queue or queue.Queue()
        self.enable_mqtt = enable_mqtt
        self.mqtt_client = None

        self.root.title("SmartCare - Nurse Monitoring Dashboard")
        self.root.geometry("1280x800")
        self.root.configure(bg="#1a1a2e")

        self.current_data = {patient["id"]: {} for patient in PATIENTS}
        self.alert_counts = {patient["id"]: 0 for patient in PATIENTS}
        self.history_cache = {patient["id"]: deque(maxlen=CHART_LIMIT) for patient in PATIENTS}
        self.selected_patient = tk.StringVar(value=PATIENTS[0]["id"])

        init_database()
        self._build_ui()
        self._load_initial_data()
        self._select_patient(self.selected_patient.get())

        if self.enable_mqtt:
            self._connect_mqtt()

        self._schedule_update()

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#16213e", pady=10)
        header.pack(fill="x")
        tk.Label(
            header,
            text="SmartCare - Real-Time Patient Monitoring",
            font=("Helvetica", 16, "bold"),
            fg="#e0e0ff",
            bg="#16213e",
        ).pack(side="left", padx=20)
        self.time_label = tk.Label(header, text="", font=("Helvetica", 12), fg="#888", bg="#16213e")
        self.time_label.pack(side="right", padx=20)

        main = tk.Frame(self.root, bg="#1a1a2e")
        main.pack(fill="both", expand=True, padx=10, pady=10)

        left = tk.Frame(main, bg="#16213e", width=220)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        self._build_patient_list(left)

        center = tk.Frame(main, bg="#1a1a2e")
        center.pack(side="left", fill="both", expand=True)
        self._build_vitals_panel(center)

    def _build_patient_list(self, parent):
        tk.Label(parent, text="PATIENTS", font=("Helvetica", 11, "bold"), fg="#aaa", bg="#16213e").pack(
            pady=(15, 5)
        )

        self.patient_buttons = {}
        for patient in PATIENTS:
            pid = patient["id"]
            frame = tk.Frame(parent, bg="#16213e", pady=2)
            frame.pack(fill="x", padx=5)
            btn = tk.Button(
                frame,
                text=f"Room {patient['room']}\n{patient['name']}",
                font=("Helvetica", 10),
                fg="white",
                bg="#0f3460",
                relief="flat",
                cursor="hand2",
                wraplength=190,
                command=lambda p=pid: self._select_patient(p),
            )
            btn.pack(fill="x")
            self.patient_buttons[pid] = btn

            alert_label = tk.Label(frame, text="", font=("Helvetica", 9), fg="#e74c3c", bg="#16213e")
            alert_label.pack()
            self.patient_buttons[f"{pid}_alert"] = alert_label

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=15)
        tk.Button(
            parent,
            text="Comparison\nChart",
            font=("Helvetica", 10, "bold"),
            fg="white",
            bg="#533483",
            relief="flat",
            cursor="hand2",
            pady=8,
            command=self._show_comparison_chart,
        ).pack(fill="x", padx=5)
        tk.Button(
            parent,
            text="View Alerts",
            font=("Helvetica", 10, "bold"),
            fg="white",
            bg="#7d0a0a",
            relief="flat",
            cursor="hand2",
            pady=8,
            command=self._show_alerts_window,
        ).pack(fill="x", padx=5, pady=(5, 0))

    def _build_vitals_panel(self, parent):
        self.patient_name_label = tk.Label(
            parent,
            text="Select a patient",
            font=("Helvetica", 14, "bold"),
            fg="#e0e0ff",
            bg="#1a1a2e",
        )
        self.patient_name_label.pack(pady=(0, 10))

        cards_frame = tk.Frame(parent, bg="#1a1a2e")
        cards_frame.pack(fill="x")
        self.metric_cards = {}
        for i, (key, meta) in enumerate(METRICS_DISPLAY.items()):
            card = tk.Frame(cards_frame, bg="#16213e", padx=15, pady=12, relief="flat")
            card.grid(row=i // 3, column=i % 3, padx=8, pady=8, sticky="ew")
            cards_frame.columnconfigure(i % 3, weight=1)

            tk.Label(card, text=meta["label"], font=("Helvetica", 10), fg="#aaa", bg="#16213e").pack()
            value_label = tk.Label(card, text="--", font=("Helvetica", 22, "bold"), fg=meta["color"], bg="#16213e")
            value_label.pack()
            tk.Label(card, text=meta["unit"], font=("Helvetica", 9), fg="#666", bg="#16213e").pack()
            status_label = tk.Label(card, text="", font=("Helvetica", 9, "bold"), bg="#16213e")
            status_label.pack()
            self.metric_cards[key] = {"value": value_label, "status": status_label, "frame": card}

        chart_frame = tk.Frame(parent, bg="#16213e", pady=10)
        chart_frame.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(
            chart_frame,
            text="Live Vitals History (last 30 readings)",
            font=("Helvetica", 11, "bold"),
            fg="#aaa",
            bg="#16213e",
        ).pack(pady=5)

        self.chart_canvas = tk.Canvas(chart_frame, bg="#1a1a2e", highlightthickness=0, height=320)
        self.chart_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _select_patient(self, pid):
        self.selected_patient.set(pid)
        patient = PATIENT_LOOKUP[pid]
        self.patient_name_label.config(text=f"Room {patient['room']} - {patient['name']}")
        for key, button in self.patient_buttons.items():
            if isinstance(button, tk.Button):
                button.config(bg="#533483" if key == pid else "#0f3460")
        self._refresh_display()

    def _load_initial_data(self):
        for patient in PATIENTS:
            pid = patient["id"]
            rows = get_latest_vitals(pid, limit=CHART_LIMIT)
            self.history_cache[pid] = deque(rows, maxlen=CHART_LIMIT)
            if rows:
                self.current_data[pid] = dict(rows[-1])
        self.alert_counts.update(get_unacknowledged_alert_counts())

    def _refresh_display(self):
        pid = self.selected_patient.get()
        data = self.current_data.get(pid, {})
        if not data:
            return

        for key, meta in METRICS_DISPLAY.items():
            value = data.get(key)
            if value is None:
                continue
            card = self.metric_cards[key]
            card["value"].config(text=f"{value}")
            norm_min, norm_max = meta["normal"]
            if norm_min <= value <= norm_max:
                card["status"].config(text="Normal", fg="#2ecc71")
                card["frame"].config(bg="#16213e")
            elif abs(value - norm_min) / norm_min < 0.15 or abs(value - norm_max) / norm_max < 0.15:
                card["status"].config(text="Warning", fg="#f39c12")
                card["frame"].config(bg="#16213e")
            else:
                card["status"].config(text="CRITICAL", fg="#e74c3c")
                card["frame"].config(bg="#2d0a0a")

        self._update_chart(pid)

    def _update_chart(self, pid):
        rows = list(self.history_cache.get(pid, ()))
        if len(rows) < 2:
            rows = get_latest_vitals(pid, limit=CHART_LIMIT)
            self.history_cache[pid] = deque(rows, maxlen=CHART_LIMIT)
        if len(rows) < 2:
            return

        canvas = self.chart_canvas
        canvas.delete("all")
        canvas.update_idletasks()

        width = max(canvas.winfo_width(), 600)
        height = max(canvas.winfo_height(), 260)
        left, right, top, bottom = 55, 20, 30, 45
        plot_width = max(1, width - left - right)
        plot_height = max(1, height - top - bottom)
        chart_keys = list(METRICS_DISPLAY.keys())[:3]
        values = [row[key] for row in rows for key in chart_keys if row[key] is not None]
        if not values:
            return

        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1
        padding = (max_value - min_value) * 0.1
        min_value -= padding
        max_value += padding

        canvas.create_text(width / 2, 14, text="Heart Rate / Systolic BP / Diastolic BP", fill="#ccc",
                           font=("Helvetica", 10, "bold"))
        for i in range(5):
            y = top + plot_height * i / 4
            value = max_value - (max_value - min_value) * i / 4
            canvas.create_line(left, y, width - right, y, fill="#2a2a4a", dash=(3, 3))
            canvas.create_text(left - 8, y, text=f"{value:.0f}", fill="#aaa", anchor="e", font=("Helvetica", 8))

        canvas.create_line(left, top, left, height - bottom, fill="#444")
        canvas.create_line(left, height - bottom, width - right, height - bottom, fill="#444")

        def point(index, value):
            x = left + (plot_width * index / max(1, len(rows) - 1))
            y = top + (max_value - value) * plot_height / (max_value - min_value)
            return x, y

        for key in chart_keys:
            meta = METRICS_DISPLAY[key]
            coords = []
            for index, row in enumerate(rows):
                value = row[key]
                if value is None:
                    continue
                coords.extend(point(index, value))
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=meta["color"], width=2, smooth=True)
            for x, y in zip(coords[0::2], coords[1::2]):
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=meta["color"], outline=meta["color"])

        times = [row["timestamp"][11:19] for row in rows]
        tick_step = max(1, len(times) // 6)
        for index in range(0, len(times), tick_step):
            x, _ = point(index, rows[index][chart_keys[0]])
            canvas.create_text(x, height - bottom + 18, text=times[index], fill="#aaa", font=("Helvetica", 8))

        legend_x = width - right - 230
        for i, key in enumerate(chart_keys):
            meta = METRICS_DISPLAY[key]
            y = top + 10 + i * 18
            canvas.create_line(legend_x, y, legend_x + 22, y, fill=meta["color"], width=3)
            canvas.create_text(legend_x + 28, y, text=f"{meta['label']} ({meta['unit']})",
                               fill="white", anchor="w", font=("Helvetica", 8))

    def _show_comparison_chart(self):
        pid = self.selected_patient.get()
        first_avg, last_avg = get_first_and_last_vitals(pid)
        if not first_avg or not last_avg:
            messagebox.showinfo("Not enough data", "Not enough data for comparison yet.")
            return

        labels = [METRICS_DISPLAY[key]["label"] for key in METRIC_KEYS]
        first_values = [first_avg[key] for key in METRIC_KEYS]
        last_values = [last_avg[key] for key in METRIC_KEYS]
        patient = PATIENT_LOOKUP[pid]

        win = tk.Toplevel(self.root)
        win.title(f"Treatment Comparison: {patient['name']}")
        win.geometry("900x550")
        win.configure(bg="#1a1a2e")
        tk.Label(win, text=f"SmartCare - Patient Progress: {patient['name']}",
                 font=("Helvetica", 13, "bold"), fg="white", bg="#1a1a2e").pack(pady=10)

        canvas = tk.Canvas(win, bg="#1a1a2e", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        def draw():
            canvas.delete("all")
            canvas.update_idletasks()
            width = max(canvas.winfo_width(), 850)
            height = max(canvas.winfo_height(), 470)
            mid = width / 2

            canvas.create_text(mid / 2, 20, text="Vital Signs Comparison", fill="white",
                               font=("Helvetica", 12, "bold"))
            canvas.create_text(mid + mid / 2, 20, text="Change from Start (%)", fill="white",
                               font=("Helvetica", 12, "bold"))

            max_value = max(first_values + last_values + [1])
            chart_top, chart_bottom = 55, height - 95
            chart_height = chart_bottom - chart_top
            bar_width = max(14, (mid - 100) / (len(labels) * 3))
            group_gap = (mid - 120) / len(labels)

            canvas.create_line(45, chart_bottom, mid - 25, chart_bottom, fill="#444")
            canvas.create_line(45, chart_top, 45, chart_bottom, fill="#444")
            for i in range(5):
                y = chart_bottom - chart_height * i / 4
                value = max_value * i / 4
                canvas.create_line(45, y, mid - 25, y, fill="#333", dash=(3, 3))
                canvas.create_text(38, y, text=f"{value:.0f}", fill="#aaa", anchor="e", font=("Helvetica", 8))

            for i, label in enumerate(labels):
                x = 65 + i * group_gap
                first_h = first_values[i] / max_value * chart_height
                last_h = last_values[i] / max_value * chart_height
                canvas.create_rectangle(x, chart_bottom - first_h, x + bar_width, chart_bottom,
                                        fill="#3498db", outline="")
                canvas.create_rectangle(x + bar_width + 4, chart_bottom - last_h, x + bar_width * 2 + 4,
                                        chart_bottom, fill="#2ecc71", outline="")
                canvas.create_text(x + bar_width, chart_bottom - first_h - 8, text=f"{first_values[i]:.1f}",
                                   fill="#3498db", font=("Helvetica", 8))
                canvas.create_text(x + bar_width * 2 + 4, chart_bottom - last_h - 8, text=f"{last_values[i]:.1f}",
                                   fill="#2ecc71", font=("Helvetica", 8))
                canvas.create_text(x + bar_width, chart_bottom + 24, text=label, fill="#ccc",
                                   font=("Helvetica", 8), angle=35)

            canvas.create_rectangle(60, height - 38, 72, height - 26, fill="#3498db", outline="")
            canvas.create_text(80, height - 32, text="Start", fill="white", anchor="w", font=("Helvetica", 9))
            canvas.create_rectangle(135, height - 38, 147, height - 26, fill="#2ecc71", outline="")
            canvas.create_text(155, height - 32, text="Current", fill="white", anchor="w", font=("Helvetica", 9))

            changes = []
            for key in METRIC_KEYS:
                first = first_avg[key]
                last = last_avg[key]
                changes.append(round(((last - first) / first) * 100, 1) if first else 0)

            min_change = min(changes + [0])
            max_change = max(changes + [0])
            if min_change == max_change:
                min_change -= 1
                max_change += 1
            change_left, change_right = mid + 135, width - 55
            zero_x = change_left + (0 - min_change) * (change_right - change_left) / (max_change - min_change)
            canvas.create_line(zero_x, chart_top, zero_x, chart_bottom, fill="white")

            row_gap = chart_height / len(labels)
            for i, (label, change) in enumerate(zip(labels, changes)):
                y = chart_top + row_gap * i + row_gap / 2
                x = change_left + (change - min_change) * (change_right - change_left) / (max_change - min_change)
                color = "#2ecc71" if abs(change) < 10 else "#e74c3c" if change > 20 else "#f39c12"
                canvas.create_text(mid + 20, y, text=label, fill="#ccc", anchor="w", font=("Helvetica", 9))
                canvas.create_rectangle(min(zero_x, x), y - 8, max(zero_x, x), y + 8, fill=color, outline="")
                canvas.create_text(x + (8 if change >= 0 else -8), y, text=f"{change:+.1f}%",
                                   fill="white", anchor="w" if change >= 0 else "e", font=("Helvetica", 9))

        canvas.after(100, draw)

    def _show_alerts_window(self):
        pid = self.selected_patient.get()
        patient = PATIENT_LOOKUP[pid]
        alerts = get_unacknowledged_alerts(pid)

        win = tk.Toplevel(self.root)
        win.title(f"Alerts: {patient['name']}")
        win.geometry("750x400")
        win.configure(bg="#1a1a2e")
        tk.Label(
            win,
            text=f"Active Alerts - {patient['name']}",
            font=("Helvetica", 13, "bold"),
            fg="#e0e0ff",
            bg="#1a1a2e",
        ).pack(pady=10)

        columns = ("Time", "Type", "Severity", "Value", "Recommendation")
        tree = ttk.Treeview(win, columns=columns, show="headings", height=15)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=120 if column != "Recommendation" else 260)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#1a1a2e", foreground="white", fieldbackground="#16213e", rowheight=28)
        style.configure("Treeview.Heading", background="#0f3460", foreground="white")

        if not alerts:
            tree.insert("", "end", values=("-", "No active alerts", "-", "-", "-"))
        else:
            for alert in alerts:
                recommendation = alert["recommendation"]
                tree.insert(
                    "",
                    "end",
                    values=(
                        alert["timestamp"][11:19],
                        alert["alert_type"].replace("_", " ").title(),
                        alert["severity"],
                        f"{alert['value']}",
                        recommendation[:60] + "..." if len(recommendation) > 60 else recommendation,
                    ),
                    tags=("critical" if alert["severity"] == "CRITICAL" else "warning",),
                )

        tree.tag_configure("critical", foreground="#e74c3c")
        tree.tag_configure("warning", foreground="#f39c12")
        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="both", expand=True, padx=10)
        scroll.pack(side="right", fill="y")

    def _connect_mqtt(self):
        if mqtt is None:
            messagebox.showwarning("MQTT unavailable", "paho-mqtt is not installed. Dashboard will use database only.")
            return
        try:
            self.mqtt_client = create_mqtt_client("dashboard_ui")
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            self.mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            self.mqtt_client.loop_start()
        except OSError as exc:
            messagebox.showwarning("MQTT Connection", f"Could not connect to broker:\n{exc}\n\nUsing database only.")

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or str(reason_code).lower() == "success":
            client.subscribe("hospital/patient/+/vitals", qos=1)
            client.subscribe("hospital/patient/+/alerts", qos=1)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            kind = "vitals" if "/vitals" in msg.topic else "alerts"
            self.message_queue.put((kind, data))
        except json.JSONDecodeError as exc:
            print(f"[DASHBOARD] Bad MQTT payload: {exc}")

    def _drain_message_queue(self):
        while True:
            try:
                kind, data = self.message_queue.get_nowait()
            except queue.Empty:
                break

            pid = data.get("patient_id")
            if kind == "vitals" and pid in self.current_data:
                self.current_data[pid] = data
                self.history_cache[pid].append(data)
            elif kind == "alerts" and pid in self.alert_counts:
                self.alert_counts[pid] += 1

    def _schedule_update(self):
        self._drain_message_queue()
        self.time_label.config(text=datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))

        for patient in PATIENTS:
            pid = patient["id"]
            label = self.patient_buttons.get(f"{pid}_alert")
            if label:
                count = self.alert_counts.get(pid, 0)
                label.config(text=f"{count} alert(s)" if count > 0 else "")

        self._refresh_display()
        self.root.after(2000, self._schedule_update)

    def close(self):
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


# ============================================================
# OPTIONAL MQTT MODES
# ============================================================

def require_mqtt():
    if mqtt is None:
        raise RuntimeError("paho-mqtt is not installed, so MQTT modes cannot run.")


def create_mqtt_client(client_id=None):
    require_mqtt()
    try:
        if client_id is None:
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id) if client_id else mqtt.Client()


def connect_mqtt_with_retry(client, stop_event=None):
    while stop_event is None or not stop_event.is_set():
        try:
            print(f"[MQTT] Connecting to {BROKER_HOST}:{BROKER_PORT}...")
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            return True
        except OSError as exc:
            print(f"[MQTT] Broker unavailable ({exc}). Retrying in {MQTT_RETRY_SECONDS}s...")
            if stop_event:
                stop_event.wait(MQTT_RETRY_SECONDS)
            else:
                time.sleep(MQTT_RETRY_SECONDS)
    return False


def run_mqtt_backend():
    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[MQTT] Connected: {reason_code}")
        client.subscribe("hospital/patient/+/vitals", qos=1)

    def on_message(client, userdata, msg):
        try:
            data = validate_vitals(json.loads(msg.payload.decode("utf-8")))
            save_vitals(data)
            for alert in analyze_vitals(data):
                client.publish(
                    f"hospital/patient/{data['patient_id']}/alerts",
                    json.dumps(alert, separators=(",", ":")),
                    qos=1,
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            print(f"[BACKEND ERROR] {exc}")

    init_database()
    client = create_mqtt_client()
    client.on_connect = on_connect
    client.on_message = on_message
    connect_mqtt_with_retry(client)
    print("[INFO] MQTT backend active. Press Ctrl+C to stop.")
    client.loop_forever()


def run_mqtt_simulator():
    init_database()
    stop_event = threading.Event()
    threads = []

    def run_sensor(patient):
        simulator = PatientSensorSimulator(patient)
        client = create_mqtt_client(f"sensor_{patient['id']}")
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        if not connect_mqtt_with_retry(client, stop_event):
            return
        client.loop_start()
        print(f"[SENSOR] MQTT sensor started for {patient['name']} ({patient['id']})")
        try:
            while not stop_event.is_set():
                reading = simulator.generate_reading()
                client.publish(
                    f"hospital/patient/{patient['id']}/vitals",
                    json.dumps(reading, separators=(",", ":")),
                    qos=1,
                )
                stop_event.wait(PUBLISH_INTERVAL_SECONDS)
        finally:
            client.loop_stop()
            client.disconnect()

    for patient in PATIENTS:
        thread = threading.Thread(target=run_sensor, args=(patient,), daemon=True)
        thread.start()
        threads.append(thread)
        time.sleep(0.5)

    print("[INFO] MQTT simulator active. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2)


# ============================================================
# RUNNERS
# ============================================================

def run_integrated_app():
    init_database()
    backend_queue = queue.Queue()
    ui_queue = queue.Queue()
    stop_event = threading.Event()

    backend_thread = threading.Thread(target=run_local_backend, args=(backend_queue, ui_queue, stop_event), daemon=True)
    backend_thread.start()

    sensor_threads = []
    for patient in PATIENTS:
        thread = threading.Thread(
            target=run_local_sensor,
            args=(patient, backend_queue, ui_queue, stop_event),
            daemon=True,
        )
        thread.start()
        sensor_threads.append(thread)

    root = tk.Tk()
    dashboard = SmartCareDashboard(root, message_queue=ui_queue, enable_mqtt=False)

    def on_close():
        stop_event.set()
        dashboard.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def run_dashboard_only():
    root = tk.Tk()
    dashboard = SmartCareDashboard(root, enable_mqtt=True)
    root.protocol("WM_DELETE_WINDOW", lambda: (dashboard.close(), root.destroy()))
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="SmartCare combined app")
    parser.add_argument(
        "--mode",
        choices=("all", "dashboard", "backend", "simulator"),
        default="all",
        help="all = no MQTT broker required; other modes use MQTT",
    )
    args = parser.parse_args()

    if args.mode == "all":
        run_integrated_app()
    elif args.mode == "dashboard":
        run_dashboard_only()
    elif args.mode == "backend":
        run_mqtt_backend()
    elif args.mode == "simulator":
        run_mqtt_simulator()


if __name__ == "__main__":
    main()
