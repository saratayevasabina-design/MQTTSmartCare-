"""
SmartCare - Nurse Monitoring Dashboard
Displays real-time patient data + comparison charts (start vs end of treatment)
"""
import tkinter as tk
from tkinter import ttk, messagebox
import paho.mqtt.client as mqtt
import json
import sqlite3
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import numpy as np

# ---- SETTINGS ----
BROKER_HOST = "localhost"
BROKER_PORT = 1883
DB_FILE = "smartcare.db"

PATIENTS = {
    "P001": {"name": "John Smith",   "room": "101"},
    "P002": {"name": "Mary Johnson", "room": "102"},
    "P003": {"name": "David Brown",  "room": "103"},
}

METRICS_DISPLAY = {
    "heart_rate":        {"label": "Heart Rate",   "unit": "BPM",    "color": "#e74c3c", "normal": (60, 100)},
    "systolic_bp":       {"label": "Systolic BP",  "unit": "mmHg",   "color": "#e67e22", "normal": (100, 140)},
    "diastolic_bp":      {"label": "Diastolic BP", "unit": "mmHg",   "color": "#f39c12", "normal": (60, 90)},
    "temperature":       {"label": "Temperature",  "unit": "C",      "color": "#9b59b6", "normal": (36.5, 37.5)},
    "glucose":           {"label": "Glucose",      "unit": "mmol/L", "color": "#1abc9c", "normal": (3.9, 7.8)},
    "oxygen_saturation": {"label": "SpO2",         "unit": "%",      "color": "#2980b9", "normal": (95, 100)},
}


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_latest_vitals(patient_id, limit=30):
    """Gets the last N readings from database"""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT * FROM vitals
        WHERE patient_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (patient_id, limit)).fetchall()
    conn.close()
    return list(reversed(rows))  # chronological order

def get_first_and_last_vitals(patient_id):
    """
    Returns averages for first 5 and last 5 records.
    Used to compare 'start of treatment vs now'.
    """
    conn = get_db_connection()
    all_rows = conn.execute("""
        SELECT * FROM vitals WHERE patient_id = ? ORDER BY timestamp ASC
    """, (patient_id,)).fetchall()
    conn.close()

    if len(all_rows) < 2:
        return None, None

    n = min(5, len(all_rows) // 2)
    first_rows = all_rows[:n]
    last_rows = all_rows[-n:]

    def avg_row(rows):
        metrics = ["heart_rate", "systolic_bp", "diastolic_bp",
                   "temperature", "glucose", "oxygen_saturation"]
        result = {}
        for m in metrics:
            vals = [r[m] for r in rows if r[m] is not None]
            result[m] = round(sum(vals) / len(vals), 2) if vals else 0
        return result

    return avg_row(first_rows), avg_row(last_rows)

def get_unacknowledged_alerts(patient_id):
    """Gets unacknowledged alerts for a patient"""
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
# MAIN DASHBOARD WINDOW
# ============================================================

class SmartCareDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("SmartCare - Nurse Monitoring Dashboard")
        self.root.geometry("1280x800")
        self.root.configure(bg="#1a1a2e")

        # Storage for latest data per patient
        self.current_data = {pid: {} for pid in PATIENTS}
        self.alert_counts = {pid: 0 for pid in PATIENTS}
        self.selected_patient = tk.StringVar(value="P001")

        self._build_ui()
        self._connect_mqtt()
        self._schedule_update()

    # ---- BUILD UI ----

    def _build_ui(self):
        # === Header ===
        header = tk.Frame(self.root, bg="#16213e", pady=10)
        header.pack(fill="x")
        tk.Label(header, text="SmartCare  -  Real-Time Patient Monitoring",
                 font=("Helvetica", 16, "bold"), fg="#e0e0ff", bg="#16213e").pack(side="left", padx=20)
        self.time_label = tk.Label(header, text="", font=("Helvetica", 12),
                                   fg="#888", bg="#16213e")
        self.time_label.pack(side="right", padx=20)

        # === Main container ===
        main = tk.Frame(self.root, bg="#1a1a2e")
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # Left panel - patient list
        left = tk.Frame(main, bg="#16213e", width=220)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        self._build_patient_list(left)

        # Center - vitals cards + charts
        center = tk.Frame(main, bg="#1a1a2e")
        center.pack(side="left", fill="both", expand=True)
        self._build_vitals_panel(center)

    def _build_patient_list(self, parent):
        tk.Label(parent, text="PATIENTS", font=("Helvetica", 11, "bold"),
                 fg="#aaa", bg="#16213e").pack(pady=(15, 5))

        self.patient_buttons = {}
        for pid, info in PATIENTS.items():
            frame = tk.Frame(parent, bg="#16213e", pady=2)
            frame.pack(fill="x", padx=5)

            btn = tk.Button(
                frame, text=f"Room {info['room']}\n{info['name']}",
                font=("Helvetica", 10), fg="white", bg="#0f3460",
                relief="flat", cursor="hand2", wraplength=190,
                command=lambda p=pid: self._select_patient(p)
            )
            btn.pack(fill="x")
            self.patient_buttons[pid] = btn

            alert_lbl = tk.Label(frame, text="", font=("Helvetica", 9),
                                  fg="#e74c3c", bg="#16213e")
            alert_lbl.pack()
            self.patient_buttons[f"{pid}_alert"] = alert_lbl

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=15)

        # Comparison chart button
        tk.Button(
            parent, text="Comparison\nChart",
            font=("Helvetica", 10, "bold"), fg="white", bg="#533483",
            relief="flat", cursor="hand2", pady=8,
            command=self._show_comparison_chart
        ).pack(fill="x", padx=5)

        # Alerts history button
        tk.Button(
            parent, text="View Alerts",
            font=("Helvetica", 10, "bold"), fg="white", bg="#7d0a0a",
            relief="flat", cursor="hand2", pady=8,
            command=self._show_alerts_window
        ).pack(fill="x", padx=5, pady=(5, 0))

    def _build_vitals_panel(self, parent):
        self.patient_name_label = tk.Label(
            parent, text="Select a patient",
            font=("Helvetica", 14, "bold"), fg="#e0e0ff", bg="#1a1a2e"
        )
        self.patient_name_label.pack(pady=(0, 10))

        # Vitals cards
        cards_frame = tk.Frame(parent, bg="#1a1a2e")
        cards_frame.pack(fill="x")

        self.metric_cards = {}
        for i, (key, meta) in enumerate(METRICS_DISPLAY.items()):
            card = tk.Frame(cards_frame, bg="#16213e", padx=15, pady=12, relief="flat")
            card.grid(row=i // 3, column=i % 3, padx=8, pady=8, sticky="ew")
            cards_frame.columnconfigure(i % 3, weight=1)

            tk.Label(card, text=meta["label"], font=("Helvetica", 10),
                     fg="#aaa", bg="#16213e").pack()
            val_lbl = tk.Label(card, text="--", font=("Helvetica", 22, "bold"),
                                fg=meta["color"], bg="#16213e")
            val_lbl.pack()
            tk.Label(card, text=meta["unit"], font=("Helvetica", 9),
                     fg="#666", bg="#16213e").pack()
            status_lbl = tk.Label(card, text="", font=("Helvetica", 9, "bold"),
                                   bg="#16213e")
            status_lbl.pack()

            self.metric_cards[key] = {"value": val_lbl, "status": status_lbl, "frame": card}

        # Real-time chart
        chart_frame = tk.Frame(parent, bg="#16213e", pady=10)
        chart_frame.pack(fill="both", expand=True, pady=(10, 0))

        tk.Label(chart_frame, text="Live Vitals History (last 30 readings)",
                 font=("Helvetica", 11, "bold"), fg="#aaa", bg="#16213e").pack(pady=5)

        self.fig = Figure(figsize=(9, 3.5), facecolor="#16213e")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1a1a2e")

        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ---- LOGIC ----

    def _select_patient(self, pid):
        self.selected_patient.set(pid)
        name = PATIENTS[pid]["name"]
        room = PATIENTS[pid]["room"]
        self.patient_name_label.config(text=f"Room {room}  -  {name}")

        for p, btn in self.patient_buttons.items():
            if isinstance(btn, tk.Button):
                btn.config(bg="#0f3460" if p != pid else "#533483")

        self._refresh_display()

    def _refresh_display(self):
        pid = self.selected_patient.get()
        if not pid or pid not in PATIENTS:
            return

        data = self.current_data.get(pid, {})
        if not data:
            return

        for key, meta in METRICS_DISPLAY.items():
            val = data.get(key)
            card = self.metric_cards[key]
            if val is None:
                continue
            card["value"].config(text=f"{val}")
            norm_min, norm_max = meta["normal"]
            if norm_min <= val <= norm_max:
                card["status"].config(text="Normal", fg="#2ecc71")
                card["frame"].config(bg="#16213e")
            elif abs(val - norm_min) / norm_min < 0.15 or abs(val - norm_max) / norm_max < 0.15:
                card["status"].config(text="Warning", fg="#f39c12")
            else:
                card["status"].config(text="CRITICAL", fg="#e74c3c")
                card["frame"].config(bg="#2d0a0a")

        self._update_chart(pid)

    def _update_chart(self, pid):
        rows = get_latest_vitals(pid, limit=30)
        if len(rows) < 2:
            return

        self.ax.clear()
        self.ax.set_facecolor("#1a1a2e")

        times = [r["timestamp"][11:19] for r in rows]
        x = range(len(times))

        for key, meta in list(METRICS_DISPLAY.items())[:3]:
            vals = [r[key] for r in rows]
            self.ax.plot(x, vals, color=meta["color"], linewidth=2,
                         label=f"{meta['label']} ({meta['unit']})",
                         marker="o", markersize=3)

        self.ax.set_xticks(list(x)[::5])
        self.ax.set_xticklabels(times[::5], color="#aaa", fontsize=8, rotation=30)
        self.ax.tick_params(axis="y", colors="#aaa", labelsize=9)
        self.ax.spines[:].set_color("#333")
        self.ax.set_title("Heart Rate / Systolic BP / Diastolic BP",
                          color="#ccc", fontsize=10)
        self.ax.legend(loc="upper right", fontsize=8,
                       facecolor="#16213e", labelcolor="white", edgecolor="#333")
        self.ax.grid(True, color="#2a2a4a", linestyle="--", alpha=0.5)
        self.fig.tight_layout()
        self.canvas.draw()

    # ---- COMPARISON CHART ----

    def _show_comparison_chart(self):
        """Window comparing start of treatment vs current condition"""
        pid = self.selected_patient.get()
        if not pid:
            messagebox.showinfo("Error", "Please select a patient first.")
            return

        first_avg, last_avg = get_first_and_last_vitals(pid)
        if not first_avg or not last_avg:
            messagebox.showinfo("Not enough data",
                                "Not enough data for comparison.\n"
                                "Run the system and wait a few minutes.")
            return

        labels = [METRICS_DISPLAY[m]["label"] for m in METRICS_DISPLAY]
        keys = list(METRICS_DISPLAY.keys())
        first_vals = [first_avg[k] for k in keys]
        last_vals  = [last_avg[k]  for k in keys]

        win = tk.Toplevel(self.root)
        win.title(f"Treatment Comparison: {PATIENTS[pid]['name']}")
        win.geometry("900x550")
        win.configure(bg="#1a1a2e")

        fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
        fig.patch.set_facecolor("#1a1a2e")

        # --- Chart 1: Bar chart comparison ---
        ax1 = axes[0]
        ax1.set_facecolor("#16213e")
        x = np.arange(len(labels))
        width = 0.35

        bars1 = ax1.bar(x - width/2, first_vals, width,
                        label="Start of Treatment", color="#3498db",
                        alpha=0.85, edgecolor="white")
        bars2 = ax1.bar(x + width/2, last_vals, width,
                        label="Current Status", color="#2ecc71",
                        alpha=0.85, edgecolor="white")

        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, color="#ccc", fontsize=9, rotation=25)
        ax1.tick_params(axis="y", colors="#ccc")
        ax1.spines[:].set_color("#333")
        ax1.set_title("Vital Signs Comparison", color="white",
                      fontsize=12, fontweight="bold")
        ax1.legend(facecolor="#1a1a2e", labelcolor="white", fontsize=9)
        ax1.grid(True, axis="y", color="#333", linestyle="--")

        for bar in bars1:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}",
                         xy=(bar.get_x() + bar.get_width()/2, h),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", va="bottom", color="#3498db", fontsize=8)
        for bar in bars2:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}",
                         xy=(bar.get_x() + bar.get_width()/2, h),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", va="bottom", color="#2ecc71", fontsize=8)

        # --- Chart 2: Percentage change ---
        ax2 = axes[1]
        ax2.set_facecolor("#16213e")
        changes = []
        change_labels = []
        change_colors = []

        for k, lbl in zip(keys, labels):
            f = first_avg[k]
            l = last_avg[k]
            if f and f != 0:
                pct = round(((l - f) / f) * 100, 1)
                changes.append(pct)
                change_labels.append(lbl)
                change_colors.append(
                    "#2ecc71" if abs(pct) < 10 else
                    "#e74c3c" if pct > 20 else "#f39c12"
                )

        bars = ax2.barh(change_labels, changes, color=change_colors,
                        alpha=0.85, edgecolor="white")
        ax2.axvline(0, color="white", linewidth=1)
        ax2.tick_params(colors="#ccc")
        ax2.spines[:].set_color("#333")
        ax2.set_title("Change from Start of Treatment (%)",
                      color="white", fontsize=12, fontweight="bold")
        ax2.set_xlabel("Percentage change (%)", color="#aaa")
        ax2.grid(True, axis="x", color="#333", linestyle="--")

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

        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

    # ---- ALERTS WINDOW ----

    def _show_alerts_window(self):
        pid = self.selected_patient.get()
        if not pid:
            messagebox.showinfo("Error", "Please select a patient first.")
            return

        alerts = get_unacknowledged_alerts(pid)
        win = tk.Toplevel(self.root)
        win.title(f"Alerts: {PATIENTS[pid]['name']}")
        win.geometry("750x400")
        win.configure(bg="#1a1a2e")

        tk.Label(win, text=f"Active Alerts - {PATIENTS[pid]['name']}",
                 font=("Helvetica", 13, "bold"), fg="#e0e0ff", bg="#1a1a2e").pack(pady=10)

        cols = ("Time", "Type", "Severity", "Value", "Recommendation")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=15)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=120 if col != "Recommendation" else 260)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#1a1a2e", foreground="white",
                        fieldbackground="#16213e", rowheight=28,
                        font=("Helvetica", 9))
        style.configure("Treeview.Heading", background="#0f3460", foreground="white")

        if not alerts:
            tree.insert("", "end", values=("-", "No active alerts", "-", "-", "-"))
        else:
            for a in alerts:
                tag = "critical" if a["severity"] == "CRITICAL" else "warning"
                rec = a["recommendation"]
                tree.insert("", "end", values=(
                    a["timestamp"][11:19],
                    a["alert_type"].replace("_", " ").title(),
                    a["severity"],
                    f"{a['value']}",
                    rec[:60] + "..." if len(rec) > 60 else rec
                ), tags=(tag,))

        tree.tag_configure("critical", foreground="#e74c3c")
        tree.tag_configure("warning", foreground="#f39c12")

        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="both", expand=True, padx=10)
        scroll.pack(side="right", fill="y")

    # ---- MQTT ----

    def _connect_mqtt(self):
        self.mqtt_client = mqtt.Client(client_id="dashboard_ui")
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        try:
            self.mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            self.mqtt_client.loop_start()
        except Exception as e:
            print(f"[DASHBOARD] Could not connect to MQTT broker: {e}")
            messagebox.showwarning(
                "MQTT Connection",
                f"Could not connect to broker:\n{e}\n\n"
                "Data will be loaded from the database."
            )

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("hospital/patient/+/vitals", qos=1)
            client.subscribe("hospital/patient/+/alerts", qos=1)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            if "/vitals" in msg.topic:
                pid = data.get("patient_id")
                if pid in self.current_data:
                    self.current_data[pid] = data
            elif "/alerts" in msg.topic:
                pid = data.get("patient_id")
                if pid in self.alert_counts:
                    self.alert_counts[pid] += 1
        except Exception:
            pass

    # ---- AUTO UPDATE ----

    def _schedule_update(self):
        self.time_label.config(text=datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))

        for pid in PATIENTS:
            alert_key = f"{pid}_alert"
            if alert_key in self.patient_buttons:
                cnt = self.alert_counts.get(pid, 0)
                self.patient_buttons[alert_key].config(
                    text=f"{cnt} alert(s)" if cnt > 0 else ""
                )

        if self.selected_patient.get() in self.current_data:
            self._refresh_display()

        self.root.after(2000, self._schedule_update)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = SmartCareDashboard(root)
    root.mainloop()