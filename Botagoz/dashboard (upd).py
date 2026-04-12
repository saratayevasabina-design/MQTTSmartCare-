"""
SmartCare - Nurse Monitoring Dashboard
=======================================
This module provides the graphical user interface for the nurse
monitoring station. It subscribes to patient vital signs and alert
topics via MQTT, displays real-time data in an interactive Tkinter
interface, and provides a Comparison Chart showing patient condition
at the start versus end of treatment.

Author: Botagoz (Dashboard UI) / Fatima Nurlan (Comparison Chart)
Module: KZ4005CMD - Integrative Project
"""

# ---- IMPORTS ----
import tkinter as tk                                    # Tkinter for GUI components
from tkinter import ttk, messagebox                     # ttk for styled widgets, messagebox for popups
import paho.mqtt.client as mqtt                         # MQTT for receiving live data
import json                                             # JSON for parsing MQTT messages
import sqlite3                                          # SQLite for loading stored data
from datetime import datetime                           # DateTime for clock display
import matplotlib.pyplot as plt                         # Matplotlib for comparison charts
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # Embed matplotlib in Tkinter
from matplotlib.figure import Figure                    # Figure object for inline charts
import numpy as np                                      # NumPy for numerical calculations

# ---- SETTINGS ----
BROKER_HOST = "localhost"   # MQTT broker address
BROKER_PORT = 1883           # Default MQTT port
DB_FILE = "smartcare.db"     # SQLite database file

# ---- PATIENT REGISTRY ----
# Must match the patient list in sensor_simulator.py
PATIENTS = {
    "P001": {"name": "John Smith",   "room": "101"},
    "P002": {"name": "Mary Johnson", "room": "102"},
    "P003": {"name": "David Brown",  "room": "103"},
}

# ---- METRICS DISPLAY CONFIGURATION ----
# Defines how each vital sign is displayed in the dashboard
# Includes label, unit, colour, and normal range for status indicator
METRICS_DISPLAY = {
    "heart_rate":        {"label": "Heart Rate",   "unit": "BPM",    "color": "#e74c3c", "normal": (60, 100)},
    "systolic_bp":       {"label": "Systolic BP",  "unit": "mmHg",   "color": "#e67e22", "normal": (100, 140)},
    "diastolic_bp":      {"label": "Diastolic BP", "unit": "mmHg",   "color": "#f39c12", "normal": (60, 90)},
    "temperature":       {"label": "Temperature",  "unit": "C",      "color": "#9b59b6", "normal": (36.5, 37.5)},
    "glucose":           {"label": "Glucose",      "unit": "mmol/L", "color": "#1abc9c", "normal": (3.9, 7.8)},
    "oxygen_saturation": {"label": "SpO2",         "unit": "%",      "color": "#2980b9", "normal": (95, 100)},
}


# ============================================================
# DATABASE HELPER FUNCTIONS
# ============================================================

def get_db_connection():
    """
    Creates and returns a new SQLite database connection.
    Sets row_factory to sqlite3.Row for dictionary-style access.
    
    Returns:
        sqlite3.Connection: Active database connection
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row   # Allows column access by name
    return conn


def get_latest_vitals(patient_id, limit=30):
    """
    Retrieves the most recent vital sign readings for a patient.
    
    Queries the vitals table ordered by timestamp descending,
    then reverses the result for chronological display on charts.
    
    Args:
        patient_id (str): The patient ID (e.g. "P001")
        limit (int): Maximum number of records to retrieve (default: 30)
    
    Returns:
        list: List of Row objects in chronological order
    """
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT * FROM vitals
        WHERE patient_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (patient_id, limit)).fetchall()
    conn.close()
    return list(reversed(rows))   # Reverse to get chronological order


def get_first_and_last_vitals(patient_id):
    """
    Calculates average vital signs for the first and last 5 readings.
    
    Used by the Comparison Chart to show patient condition at the
    start of monitoring versus the current state. Averaging 5 readings
    reduces the impact of momentary fluctuations.
    
    Args:
        patient_id (str): The patient ID
    
    Returns:
        tuple: (first_avg, last_avg) - Two dictionaries of averaged values,
               or (None, None) if insufficient data
    """
    conn = get_db_connection()
    # Get all readings in chronological order
    all_rows = conn.execute("""
        SELECT * FROM vitals WHERE patient_id = ? ORDER BY timestamp ASC
    """, (patient_id,)).fetchall()
    conn.close()

    # Need at least 2 readings to make a comparison
    if len(all_rows) < 2:
        return None, None

    # Take up to 5 readings from start and end
    n = min(5, len(all_rows) // 2)
    first_rows = all_rows[:n]    # First n readings (start of treatment)
    last_rows = all_rows[-n:]    # Last n readings (current state)

    def avg_row(rows):
        """Calculates average value for each metric across a set of rows."""
        metrics = ["heart_rate", "systolic_bp", "diastolic_bp",
                   "temperature", "glucose", "oxygen_saturation"]
        result = {}
        for m in metrics:
            vals = [r[m] for r in rows if r[m] is not None]
            result[m] = round(sum(vals) / len(vals), 2) if vals else 0
        return result

    return avg_row(first_rows), avg_row(last_rows)


def get_unacknowledged_alerts(patient_id):
    """
    Retrieves all unacknowledged alerts for a specific patient.
    
    Returns only alerts where acknowledged = 0 (not yet reviewed),
    ordered by most recent first.
    
    Args:
        patient_id (str): The patient ID
    
    Returns:
        list: List of unacknowledged alert records
    """
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT * FROM alerts
        WHERE patient_id = ? AND acknowledged = 0
        ORDER BY timestamp DESC
        LIMIT 20
    """, (patient_id,)).fetchall()
    conn.close()
    return rows


# ============================================================
# MAIN DASHBOARD CLASS
# ============================================================

class SmartCareDashboard:
    """
    Main dashboard class for the SmartCare nurse monitoring interface.
    
    Uses OOP to encapsulate all UI components, MQTT subscription,
    and data display logic in a single, organised class. The dashboard
    auto-refreshes every 2 seconds using Tkinter's after() scheduler.
    """

    def __init__(self, root):
        """
        Constructor - initialises the dashboard window and all components.
        
        Args:
            root (tk.Tk): The main Tkinter window
        """
        self.root = root
        self.root.title("SmartCare - Nurse Monitoring Dashboard")
        self.root.geometry("1280x800")
        self.root.configure(bg="#1a1a2e")   # Dark blue background

        # Storage for latest MQTT data per patient
        self.current_data = {pid: {} for pid in PATIENTS}

        # Alert counter per patient (increments when alert received via MQTT)
        self.alert_counts = {pid: 0 for pid in PATIENTS}

        # Currently selected patient ID
        self.selected_patient = tk.StringVar(value="P001")

        self._build_ui()       # Build all UI components
        self._connect_mqtt()   # Connect to MQTT broker
        self._schedule_update() # Start auto-refresh loop

    # ---- UI CONSTRUCTION METHODS ----

    def _build_ui(self):
        """Builds the complete dashboard UI layout."""

        # === Header bar ===
        header = tk.Frame(self.root, bg="#16213e", pady=10)
        header.pack(fill="x")
        tk.Label(header, text="SmartCare  -  Real-Time Patient Monitoring",
                 font=("Helvetica", 16, "bold"), fg="#e0e0ff", bg="#16213e").pack(side="left", padx=20)

        # Clock label - updated every 2 seconds
        self.time_label = tk.Label(header, text="", font=("Helvetica", 12),
                                   fg="#888", bg="#16213e")
        self.time_label.pack(side="right", padx=20)

        # === Main layout ===
        main = tk.Frame(self.root, bg="#1a1a2e")
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # Left panel - patient selection list
        left = tk.Frame(main, bg="#16213e", width=220)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)   # Prevent frame from shrinking
        self._build_patient_list(left)

        # Centre panel - vitals cards and charts
        center = tk.Frame(main, bg="#1a1a2e")
        center.pack(side="left", fill="both", expand=True)
        self._build_vitals_panel(center)

    def _build_patient_list(self, parent):
        """
        Builds the patient selection panel on the left side.
        Creates one button per patient plus action buttons.
        
        Args:
            parent (tk.Frame): Parent frame to build into
        """
        tk.Label(parent, text="PATIENTS", font=("Helvetica", 11, "bold"),
                 fg="#aaa", bg="#16213e").pack(pady=(15, 5))

        self.patient_buttons = {}   # Store references to buttons for styling

        # Create a button for each patient
        for pid, info in PATIENTS.items():
            frame = tk.Frame(parent, bg="#16213e", pady=2)
            frame.pack(fill="x", padx=5)

            btn = tk.Button(
                frame,
                text=f"Room {info['room']}\n{info['name']}",
                font=("Helvetica", 10), fg="white", bg="#0f3460",
                relief="flat", cursor="hand2", wraplength=190,
                command=lambda p=pid: self._select_patient(p)   # Lambda captures pid
            )
            btn.pack(fill="x")
            self.patient_buttons[pid] = btn

            # Alert count label below each patient button
            alert_lbl = tk.Label(frame, text="", font=("Helvetica", 9),
                                  fg="#e74c3c", bg="#16213e")
            alert_lbl.pack()
            self.patient_buttons[f"{pid}_alert"] = alert_lbl

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=15)

        # Button to open Comparison Chart window
        tk.Button(
            parent, text="Comparison\nChart",
            font=("Helvetica", 10, "bold"), fg="white", bg="#533483",
            relief="flat", cursor="hand2", pady=8,
            command=self._show_comparison_chart
        ).pack(fill="x", padx=5)

        # Button to open Alerts window
        tk.Button(
            parent, text="View Alerts",
            font=("Helvetica", 10, "bold"), fg="white", bg="#7d0a0a",
            relief="flat", cursor="hand2", pady=8,
            command=self._show_alerts_window
        ).pack(fill="x", padx=5, pady=(5, 0))

    def _build_vitals_panel(self, parent):
        """
        Builds the centre panel with vital sign cards and live chart.
        
        Args:
            parent (tk.Frame): Parent frame to build into
        """
        # Patient name display at top
        self.patient_name_label = tk.Label(
            parent, text="Select a patient",
            font=("Helvetica", 14, "bold"), fg="#e0e0ff", bg="#1a1a2e"
        )
        self.patient_name_label.pack(pady=(0, 10))

        # Grid of 6 vital sign cards (2 rows x 3 columns)
        cards_frame = tk.Frame(parent, bg="#1a1a2e")
        cards_frame.pack(fill="x")

        self.metric_cards = {}   # Store references for updating values

        for i, (key, meta) in enumerate(METRICS_DISPLAY.items()):
            # Each card shows: label, value, unit, and normal/warning/critical status
            card = tk.Frame(cards_frame, bg="#16213e", padx=15, pady=12)
            card.grid(row=i // 3, column=i % 3, padx=8, pady=8, sticky="ew")
            cards_frame.columnconfigure(i % 3, weight=1)

            tk.Label(card, text=meta["label"], font=("Helvetica", 10),
                     fg="#aaa", bg="#16213e").pack()

            # Large value display in metric colour
            val_lbl = tk.Label(card, text="--", font=("Helvetica", 22, "bold"),
                                fg=meta["color"], bg="#16213e")
            val_lbl.pack()

            tk.Label(card, text=meta["unit"], font=("Helvetica", 9),
                     fg="#666", bg="#16213e").pack()

            # Status indicator: Normal / Warning / CRITICAL
            status_lbl = tk.Label(card, text="", font=("Helvetica", 9, "bold"),
                                   bg="#16213e")
            status_lbl.pack()

            self.metric_cards[key] = {"value": val_lbl, "status": status_lbl, "frame": card}

        # Real-time line chart at bottom
        chart_frame = tk.Frame(parent, bg="#16213e", pady=10)
        chart_frame.pack(fill="both", expand=True, pady=(10, 0))

        tk.Label(chart_frame, text="Live Vitals History (last 30 readings)",
                 font=("Helvetica", 11, "bold"), fg="#aaa", bg="#16213e").pack(pady=5)

        # Create matplotlib figure embedded in Tkinter
        self.fig = Figure(figsize=(9, 3.5), facecolor="#16213e")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1a1a2e")

        # Embed figure in Tkinter canvas widget
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ---- DISPLAY UPDATE METHODS ----

    def _select_patient(self, pid):
        """
        Handles patient selection from the left panel.
        Updates the display to show the selected patient's data.
        
        Args:
            pid (str): Patient ID of the selected patient
        """
        self.selected_patient.set(pid)
        name = PATIENTS[pid]["name"]
        room = PATIENTS[pid]["room"]
        self.patient_name_label.config(text=f"Room {room}  -  {name}")

        # Highlight selected patient button in purple
        for p, btn in self.patient_buttons.items():
            if isinstance(btn, tk.Button):
                btn.config(bg="#0f3460" if p != pid else "#533483")

        self._refresh_display()   # Immediately refresh data for new patient

    def _refresh_display(self):
        """
        Updates all vital sign cards with the latest data for selected patient.
        
        Changes card colour based on value status:
        - Green: Normal range
        - Orange: Warning (close to threshold)
        - Red: Critical (outside threshold)
        """
        pid = self.selected_patient.get()
        if not pid or pid not in PATIENTS:
            return

        data = self.current_data.get(pid, {})
        if not data:
            return   # No data yet, skip update

        for key, meta in METRICS_DISPLAY.items():
            val = data.get(key)
            card = self.metric_cards[key]
            if val is None:
                continue

            # Update displayed value
            card["value"].config(text=f"{val}")

            # Determine status based on normal range
            norm_min, norm_max = meta["normal"]
            if norm_min <= val <= norm_max:
                card["status"].config(text="Normal", fg="#2ecc71")  # Green
                card["frame"].config(bg="#16213e")
            elif abs(val - norm_min) / norm_min < 0.15 or abs(val - norm_max) / norm_max < 0.15:
                card["status"].config(text="Warning", fg="#f39c12")  # Orange
            else:
                card["status"].config(text="CRITICAL", fg="#e74c3c")  # Red
                card["frame"].config(bg="#2d0a0a")   # Red background for critical

        self._update_chart(pid)   # Update the live chart

    def _update_chart(self, pid):
        """
        Updates the real-time line chart with latest readings from database.
        
        Plots Heart Rate, Systolic BP, and Diastolic BP over the last
        30 readings retrieved from the SQLite database.
        
        Args:
            pid (str): Patient ID to plot data for
        """
        rows = get_latest_vitals(pid, limit=30)
        if len(rows) < 2:
            return   # Not enough data to plot

        self.ax.clear()
        self.ax.set_facecolor("#1a1a2e")

        # Extract time labels (HH:MM:SS format)
        times = [r["timestamp"][11:19] for r in rows]
        x = range(len(times))

        # Plot first 3 metrics (HR, Systolic BP, Diastolic BP)
        for key, meta in list(METRICS_DISPLAY.items())[:3]:
            vals = [r[key] for r in rows]
            self.ax.plot(x, vals, color=meta["color"], linewidth=2,
                         label=f"{meta['label']} ({meta['unit']})",
                         marker="o", markersize=3)

        # Style the chart
        self.ax.set_xticks(list(x)[::5])
        self.ax.set_xticklabels(times[::5], color="#aaa", fontsize=8, rotation=30)
        self.ax.tick_params(axis="y", colors="#aaa", labelsize=9)
        self.ax.spines[:].set_color("#333")
        self.ax.set_title("Heart Rate / Systolic BP / Diastolic BP", color="#ccc", fontsize=10)
        self.ax.legend(loc="upper right", fontsize=8,
                       facecolor="#16213e", labelcolor="white", edgecolor="#333")
        self.ax.grid(True, color="#2a2a4a", linestyle="--", alpha=0.5)
        self.fig.tight_layout()
        self.canvas.draw()   # Redraw the embedded canvas

    # ---- COMPARISON CHART ----

    def _show_comparison_chart(self):
        """
        Opens a window showing two comparison charts:
        1. Grouped bar chart: start of treatment vs current values
        2. Horizontal bar chart: percentage change per vital sign
        
        Data is retrieved from the SQLite database and averaged
        over the first and last 5 readings to reduce noise.
        """
        pid = self.selected_patient.get()
        if not pid:
            messagebox.showinfo("Error", "Please select a patient first.")
            return

        # Retrieve averaged first and last readings from database
        first_avg, last_avg = get_first_and_last_vitals(pid)
        if not first_avg or not last_avg:
            messagebox.showinfo("Not enough data",
                                "Not enough data for comparison.\n"
                                "Wait a few minutes for data to accumulate.")
            return

        labels = [METRICS_DISPLAY[m]["label"] for m in METRICS_DISPLAY]
        keys = list(METRICS_DISPLAY.keys())
        first_vals = [first_avg[k] for k in keys]   # Start of treatment values
        last_vals  = [last_avg[k]  for k in keys]   # Current values

        # Create new popup window for charts
        win = tk.Toplevel(self.root)
        win.title(f"Treatment Comparison: {PATIENTS[pid]['name']}")
        win.geometry("900x550")
        win.configure(bg="#1a1a2e")

        # Create side-by-side matplotlib figure
        fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
        fig.patch.set_facecolor("#1a1a2e")

        # --- Chart 1: Grouped bar chart ---
        ax1 = axes[0]
        ax1.set_facecolor("#16213e")
        x = np.arange(len(labels))
        width = 0.35   # Width of each bar group

        # Blue bars = start of treatment
        bars1 = ax1.bar(x - width/2, first_vals, width,
                        label="Start of Treatment", color="#3498db", alpha=0.85, edgecolor="white")
        # Green bars = current status
        bars2 = ax1.bar(x + width/2, last_vals, width,
                        label="Current Status", color="#2ecc71", alpha=0.85, edgecolor="white")

        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, color="#ccc", fontsize=9, rotation=25)
        ax1.tick_params(axis="y", colors="#ccc")
        ax1.spines[:].set_color("#333")
        ax1.set_title("Vital Signs Comparison", color="white", fontsize=12, fontweight="bold")
        ax1.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=9)
        ax1.grid(True, axis="y", color="#333", linestyle="--")

        # Add value labels above each bar
        for bar in bars1:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width()/2, h),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", va="bottom", color="#3498db", fontsize=8)
        for bar in bars2:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width()/2, h),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", va="bottom", color="#2ecc71", fontsize=8)

        # --- Chart 2: Percentage change chart ---
        ax2 = axes[1]
        ax2.set_facecolor("#16213e")
        changes = []
        change_labels = []
        change_colors = []

        # Calculate percentage change for each metric
        for k, lbl in zip(keys, labels):
            f = first_avg[k]
            l = last_avg[k]
            if f and f != 0:
                pct = round(((l - f) / f) * 100, 1)   # Percentage change formula
                changes.append(pct)
                change_labels.append(lbl)
                # Colour coding: green = stable, orange = moderate, red = large change
                change_colors.append(
                    "#2ecc71" if abs(pct) < 10 else
                    "#e74c3c" if pct > 20 else "#f39c12"
                )

        bars = ax2.barh(change_labels, changes, color=change_colors, alpha=0.85, edgecolor="white")
        ax2.axvline(0, color="white", linewidth=1)   # Zero reference line
        ax2.tick_params(colors="#ccc")
        ax2.spines[:].set_color("#333")
        ax2.set_title("Change from Start of Treatment (%)", color="white",
                      fontsize=12, fontweight="bold")
        ax2.set_xlabel("Percentage change (%)", color="#aaa")
        ax2.grid(True, axis="x", color="#333", linestyle="--")

        # Add percentage labels on each bar
        for bar, val in zip(bars, changes):
            ax2.annotate(f"{val:+.1f}%",
                         xy=(val, bar.get_y() + bar.get_height()/2),
                         xytext=(5 if val >= 0 else -5, 0),
                         textcoords="offset points",
                         ha="left" if val >= 0 else "right",
                         va="center", color="white", fontsize=9)

        fig.suptitle(f"SmartCare - Patient Progress: {PATIENTS[pid]['name']}",
                     color="white", fontsize=13, fontweight="bold")
        fig.tight_layout(rect=[0, 0, 1, 0.95])

        # Embed matplotlib figure in Tkinter window
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

    # ---- ALERTS WINDOW ----

    def _show_alerts_window(self):
        """
        Opens a window displaying all unacknowledged alerts for selected patient.
        
        Retrieves alerts from the SQLite database and displays them
        in a styled table with colour coding by severity level.
        """
        pid = self.selected_patient.get()
        if not pid:
            messagebox.showinfo("Error", "Please select a patient first.")
            return

        alerts = get_unacknowledged_alerts(pid)

        # Create popup window
        win = tk.Toplevel(self.root)
        win.title(f"Alerts: {PATIENTS[pid]['name']}")
        win.geometry("750x400")
        win.configure(bg="#1a1a2e")

        tk.Label(win, text=f"Active Alerts - {PATIENTS[pid]['name']}",
                 font=("Helvetica", 13, "bold"), fg="#e0e0ff", bg="#1a1a2e").pack(pady=10)

        # Table columns
        cols = ("Time", "Type", "Severity", "Value", "Recommendation")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=15)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=120 if col != "Recommendation" else 260)

        # Style the table with dark theme
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#1a1a2e", foreground="white",
                        fieldbackground="#16213e", rowheight=28, font=("Helvetica", 9))
        style.configure("Treeview.Heading", background="#0f3460", foreground="white")

        if not alerts:
            tree.insert("", "end", values=("-", "No active alerts", "-", "-", "-"))
        else:
            for a in alerts:
                # Tag rows by severity for colour coding
                tag = "critical" if a["severity"] == "CRITICAL" else "warning"
                rec = a["recommendation"]
                tree.insert("", "end", values=(
                    a["timestamp"][11:19],
                    a["alert_type"].replace("_", " ").title(),
                    a["severity"],
                    f"{a['value']}",
                    rec[:60] + "..." if len(rec) > 60 else rec
                ), tags=(tag,))

        # Apply colour tags: red for critical, orange for warning
        tree.tag_configure("critical", foreground="#e74c3c")
        tree.tag_configure("warning", foreground="#f39c12")

        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="both", expand=True, padx=10)
        scroll.pack(side="right", fill="y")

    # ---- MQTT CONNECTION ----

    def _connect_mqtt(self):
        """
        Creates and connects an MQTT client for the dashboard.
        
        Subscribes to both vitals and alerts topics so the dashboard
        receives live data and alert notifications simultaneously.
        If connection fails, falls back to database-only mode.
        """
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="dashboard_ui")
        except AttributeError:
            client = mqtt.Client(client_id="dashboard_ui")

        self.mqtt_client = client
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message

        try:
            self.mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            self.mqtt_client.loop_start()   # Background thread for MQTT
        except Exception as e:
            print(f"[DASHBOARD] Could not connect to MQTT: {e}")
            messagebox.showwarning("MQTT", f"Could not connect to broker:\n{e}\n\nLoading from database.")

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        """
        Callback when dashboard connects to MQTT broker.
        Subscribes to vitals and alerts for all patients.
        """
        if reason_code == 0 or str(reason_code) == "Success":
            # Subscribe to all patient vitals (wildcard +)
            client.subscribe("hospital/patient/+/vitals", qos=1)
            # Subscribe to all patient alerts
            client.subscribe("hospital/patient/+/alerts", qos=1)

    def _on_mqtt_message(self, client, userdata, msg):
        """
        Callback when a new MQTT message arrives at the dashboard.
        
        Updates in-memory data store for live display.
        Alert messages increment the alert counter for the patient button.
        
        Args:
            msg: MQTT message with topic and payload
        """
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            if "/vitals" in msg.topic:
                # Update latest vitals for this patient
                pid = data.get("patient_id")
                if pid in self.current_data:
                    self.current_data[pid] = data
            elif "/alerts" in msg.topic:
                # Increment alert counter for patient button indicator
                pid = data.get("patient_id")
                if pid in self.alert_counts:
                    self.alert_counts[pid] += 1
        except Exception:
            pass   # Silently ignore malformed messages

    # ---- AUTO-REFRESH LOOP ----

    def _schedule_update(self):
        """
        Schedules periodic UI refresh every 2000 milliseconds (2 seconds).
        
        Updates the clock display, patient alert counters,
        and refreshes the vital signs display for the selected patient.
        Uses Tkinter's after() method for non-blocking scheduling.
        """
        # Update clock in header
        self.time_label.config(text=datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))

        # Update alert count labels on patient buttons
        for pid in PATIENTS:
            alert_key = f"{pid}_alert"
            if alert_key in self.patient_buttons:
                cnt = self.alert_counts.get(pid, 0)
                self.patient_buttons[alert_key].config(
                    text=f"{cnt} alert(s)" if cnt > 0 else ""
                )

        # Refresh vitals display for currently selected patient
        if self.selected_patient.get() in self.current_data:
            self._refresh_display()

        # Schedule next update in 2 seconds (non-blocking)
        self.root.after(2000, self._schedule_update)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()              # Create main Tkinter window
    app = SmartCareDashboard(root)  # Instantiate dashboard
    root.mainloop()             # Start Tkinter event loop 