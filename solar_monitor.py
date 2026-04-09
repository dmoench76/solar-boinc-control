import subprocess
import requests
import time
import psutil
import os
import sqlite3
import json
import threading
import sys
import select
import xml.etree.ElementTree as ET
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.console import Console
from rich.layout import Layout
from rich import box
from rich.columns import Columns

# --- Configuration ---
BOINC_CMD = "/usr/bin/boinccmd"
DB_PATH = "/home/dmoench/solar_history.db"
CONFIG_PATH = "/home/dmoench/solar_config.json"
SETTINGS_PATH = "/home/dmoench/solar_settings.json"
RAPL_PATH = "/sys/class/powercap/intel-rapl:0/energy_uj"

with open(SETTINGS_PATH) as f:
    _s = json.load(f)
TASMOTA_URL     = _s["tasmota_url"]
JOB_LOG         = _s["job_log"]
USER_ID         = _s["einstein_user_id"]
EINSTEIN_API_URL = f"https://einstein.phys.uwm.edu/show_user.php?userid={USER_ID}&format=xml"

console = Console()

class DashboardState:
    """Thread-safe container for all dashboard data."""
    def __init__(self):
        self.lock = threading.Lock()
        self.tasmota = {"power": 0, "e_in": 0, "e_out": 0, "ok": False}
        self.system = {"cpu_usage": 0, "cpu_pwr": 0, "mem_usage": 0, "mem_total": 0, "gpu_usage": "0%", "gpu_pwr": 0, "gpu_pstate": "N/A", "net_up": 0, "net_down": 0}
        self.boinc = {"tasks": [], "mode": "Unknown", "disk": "N/A", "credits": "N/A"}
        self.history = {"avg_pwr": 0, "runtime": 0, "samples": 0, "energy_today": 0, "label": "INITIALIZING"}
        self.global_stats = {"total": "N/A", "rac": "N/A"}
        self.completions = {"cpu": 0, "gpu": 0}
        self.config = {"mode": "AUTO", "gpu_power_profile": "NORMAL"}
        self.last_update = "Never"

class ETATracker:
    def __init__(self, window_size=15):
        self.history = {}
        self.window_size = window_size
    def update_and_get_eta(self, name, fraction_done):
        now = time.time()
        if name not in self.history: self.history[name] = []
        self.history[name].append((now, fraction_done))
        if len(self.history[name]) > self.window_size: self.history[name].pop(0)
        if len(self.history[name]) < 2: return None
        s_t, s_f = self.history[name][0]; e_t, e_f = self.history[name][-1]
        t_d = e_t - s_t; f_d = e_f - s_f
        if f_d <= 0 or t_d <= 0: return None
        return (1.0 - e_f) / (f_d / t_d)

eta_tracker = ETATracker()

def data_fetcher_thread(state):
    """Background thread to perform all blocking IO operations."""
    last_global_fetch = 0
    while True:
        # 1. Tasmota
        try:
            r = requests.get(TASMOTA_URL, timeout=2).json()
            sns = r.get("StatusSNS", {}).get("MT631", {})
            t_data = {"power": sns.get("Power", 0), "e_in": sns.get("E_in", 0), "e_out": sns.get("E_out", 0), "ok": True}
        except: t_data = {"power": 0, "e_in": 0, "e_out": 0, "ok": False}

        # 2. System Stats (RAPL + SMI + psutil)
        try:
            with open(RAPL_PATH, 'r') as f: e1 = int(f.read())
            time.sleep(0.4) # Still blocking but in background!
            with open(RAPL_PATH, 'r') as f: e2 = int(f.read())
            cpu_p = (e2 - e1) / 400000.0
        except: cpu_p = 0.0
        
        mem = psutil.virtual_memory(); net = psutil.net_io_counters()
        s_data = {"cpu_usage": psutil.cpu_percent(), "cpu_pwr": cpu_p, "mem_usage": mem.used / (1024**3), "mem_total": mem.total / (1024**3), "net_up": net.bytes_sent / (1024**2), "net_down": net.bytes_recv / (1024**2)}
        
        try:
            res = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,pstate", "--format=csv,noheader,nounits"], capture_output=True, text=True)
            if res.returncode == 0:
                p = res.stdout.strip().split(',')
                s_data.update({"gpu_usage": f"{p[0].strip()}%", "gpu_pwr": float(p[1].strip()), "gpu_pstate": p[2].strip()})
        except: pass

        # 3. BOINC Stats
        try:
            res = subprocess.run([BOINC_CMD, "--get_tasks"], capture_output=True, text=True)
            tasks, cur_task = [], {}
            for line in res.stdout.splitlines():
                line = line.strip()
                if "-----------" in line or line.startswith("=="):
                    if cur_task and 'name' in cur_task: tasks.append(cur_task)
                    cur_task = {}
                elif ":" in line: k, v = line.split(":", 1); cur_task[k.strip()] = v.strip()
            if cur_task and 'name' in cur_task: tasks.append(cur_task)
            
            mode_res = subprocess.run([BOINC_CMD, "--get_cc_status"], capture_output=True, text=True)
            b_mode = "Unknown"
            for line in mode_res.stdout.splitlines():
                if "current mode:" in line: b_mode = line.split(":")[1].strip(); break
            
            disk_res = subprocess.run([BOINC_CMD, "--get_disk_usage"], capture_output=True, text=True)
            proj_res = subprocess.run([BOINC_CMD, "--get_project_status"], capture_output=True, text=True)
            
            disk_usage = "N/A"; credits_str = "N/A"
            for line in disk_res.stdout.splitlines():
                if "total disk usage:" in line: disk_usage = line.split(":")[1].strip()
            for line in proj_res.stdout.splitlines():
                if "user_total_credit:" in line: credits_str = line.split(":")[1].strip()
            
            b_data = {"tasks": tasks, "mode": b_mode, "disk": disk_usage, "credits": credits_str}
        except: b_data = {"tasks": [], "mode": "Error", "disk": "N/A", "credits": "N/A"}

        # 4. History & DB
        try:
            conn = sqlite3.connect(DB_PATH); c = conn.cursor()
            c.execute("SELECT AVG(power), SUM(boinc_active)*10.0/3600.0 FROM solar_log WHERE timestamp > datetime('now', '-24 hours')")
            avg_p, run_h = c.fetchone()
            c.execute("SELECT SUM(cpu_power + gpu_power) * 10.0 / 3600.0 FROM solar_log WHERE timestamp > datetime('now', 'start of day')")
            en_wh = c.fetchone()[0]
            c.execute("SELECT status_label FROM solar_log ORDER BY timestamp DESC LIMIT 1")
            lbl = c.fetchone()
            conn.close()
            h_data = {"avg_pwr": avg_p or 0, "runtime": run_h or 0, "energy_today": en_wh or 0, "label": lbl[0] if lbl else "UNKNOWN"}
        except: h_data = {"avg_pwr": 0, "runtime": 0, "energy_today": 0, "label": "DB ERROR"}

        # 5. Job Log
        try:
            ts_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            res = subprocess.run(["sudo", "cat", JOB_LOG], capture_output=True, text=True)
            c_cpu = 0; c_gpu = 0
            for line in res.stdout.splitlines():
                p = line.split()
                if p and int(p[0]) >= ts_today and "nm" in p:
                    n = p[p.index("nm")+1].lower()
                    if "o4as" in n: c_gpu += 1
                    else: c_cpu += 1
            comp_data = {"cpu": c_cpu, "gpu": c_gpu}
        except: comp_data = {"cpu": 0, "gpu": 0}

        # 6. Global stats (only every 15m)
        if time.time() - last_global_fetch > 900:
            try:
                r = requests.get(EINSTEIN_API_URL, timeout=5)
                root = ET.fromstring(r.text)
                state.global_stats = {"total": f"{float(root.find('total_credit').text):,.0f}", "rac": f"{float(root.find('expavg_credit').text):,.2f}"}
                last_global_fetch = time.time()
            except: pass

        # 7. Config
        try:
            with open(CONFIG_PATH, 'r') as f: cfg = json.load(f)
        except: cfg = {"mode": "AUTO"}

        # Atomic Update
        with state.lock:
            state.tasmota = t_data
            state.system = s_data
            state.boinc = b_data
            state.history = h_data
            state.completions = comp_data
            state.config = cfg
            state.last_update = datetime.now().strftime("%H:%M:%S")
        
        time.sleep(1) # Frequency of background updates

def format_eta(seconds):
    if seconds is None: return "[dim]...[/]"
    if seconds <= 0: return "Done"
    h = int(seconds // 3600); m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"

def generate_dashboard(state):
    with state.lock:
        s = state # Shortcut
        layout = Layout()
        layout.split_column(Layout(name="header", size=3), Layout(name="main"), Layout(name="footer", size=5))
        
        # Header
        m = s.config.get("mode", "AUTO")
        m_c = "cyan" if m=="AUTO" else ("green" if m=="FORCE_ON" else "red")
        layout["header"].update(Panel(f"[bold white]SOLAR & BOINC 4K DASHBOARD[/] | MODE: [bold {m_c}]{m}[/] | [cyan]{datetime.now().strftime('%H:%M:%S')}[/] (Ref: {s.last_update})", box=box.ROUNDED, style="blue"))
        
        # Left Panel
        p_c = "green" if s.tasmota['power'] < 0 else "red"
        p_str = f"Solar: [bold {p_c}]{s.tasmota['power']} W[/] | [bold white]{s.history['label']}[/]\n"
        p_str += f"Draw:  [bold magenta]{s.system['cpu_pwr']+s.system['gpu_pwr']:.1f} W[/] (Total)\n"
        p_str += f"[dim]CPU: {s.system['cpu_pwr']:.1f}W | GPU: {s.system['gpu_pwr']:.1f}W[/]\n\n"
        
        pst_c = "green" if s.system['gpu_pstate'] in ['P0','P1','P2'] else "red"
        sys_str = f"CPU: [bold cyan]{s.system['cpu_usage']}%[/] | RAM: [bold cyan]{s.system['mem_usage']:.1f}G[/]\nGPU: [bold cyan]{s.system['gpu_usage']}[/] (State: [bold {pst_c}]{s.system['gpu_pstate']}[/])\nNet: [green]↑ {s.system['net_up']:.1f}M[/] [blue]↓ {s.system['net_down']:.1f}M[/]"
        
        # Middle Panel (Workloads)
        gpu_t = [t for t in s.boinc['tasks'] if "GPU" in t.get('resources','') or "NVIDIA" in t.get('resources','')]
        cpu_t = [t for t in s.boinc['tasks'] if t not in gpu_t and t.get('active_task_state') in ['EXECUTING','SUSPENDED']]
        
        def make_table(tasks, title, style):
            t = Table(expand=True, box=box.SIMPLE, header_style=f"bold {style}")
            t.add_column("Progress", justify="right", width=10); t.add_column("ETA", justify="right", width=10); t.add_column("Status", justify="center", width=8); t.add_column("Name")
            for x in tasks:
                f = float(x.get('fraction done', 0)); eta = eta_tracker.update_and_get_eta(x.get('name'), f)
                st = "[green]RUN[/]" if x.get('active_task_state')=="EXECUTING" else "[yellow]WAIT[/]"
                t.add_row(f"{f*100:5.1f}%", format_eta(eta), st, x.get('name','')[:50])
            return t

        task_layout = Layout()
        task_layout.split_column(Layout(Panel(make_table(gpu_t, "GPU", "magenta"), title="[bold magenta]GPU[/]")), Layout(Panel(make_table(cpu_t, "CPU", "cyan"), title="[bold cyan]CPU[/]")))

        # Right Panel
        hist_str = f"Avg (24h): [bold white]{s.history['avg_pwr']:.1f} W[/]\nRuntime:   [bold green]{s.history['runtime']:.1f} h[/]\n\n"
        hist_str += f"[bold white]Einstein@Home Global[/]\nTotal: [bold green]{s.global_stats['total']}[/]\nRAC:   [bold green]{s.global_stats['rac']}[/]\n\nDisk:  [bold blue]{s.boinc['disk']}[/]"

        main_layout = Layout()
        main_layout.split_row(Layout(Panel(p_str + sys_str, title="[bold yellow]Energy[/]"), ratio=1), Layout(task_layout, ratio=2), Layout(Panel(hist_str, title="[bold blue]Stats[/]"), ratio=1))
        layout["main"].update(main_layout)
        
        # Footer
        stats = Columns([Panel(f"CPU Done: {s.completions['cpu']}"), Panel(f"GPU Done: {s.completions['gpu']}"), Panel(f"Keys: [cyan]A[/], [green]O[/], [red]F[/] | [bold green]{s.history['energy_today']/1000.0:.3f} kWh[/]")], expand=True)
        layout["footer"].update(Panel(stats, title="[bold white]Daily Overview[/]"))
        return layout

if __name__ == "__main__":
    import termios
    import tty
    state = DashboardState()
    threading.Thread(target=data_fetcher_thread, args=(state,), daemon=True).start()

    def get_key():
        fd = sys.stdin.fileno()
        if not os.isatty(fd): return None
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            if select.select([sys.stdin], [], [], 0.1)[0]: return sys.stdin.read(1)
        finally: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return None

    with Live(generate_dashboard(state), screen=True, auto_refresh=False) as live:
        while True:
            key = get_key()
            if key:
                k = key.lower()
                if k == 'a':
                    with open(CONFIG_PATH, 'w') as f: json.dump({"mode":"AUTO"}, f)
                elif k == 'o':
                    with open(CONFIG_PATH, 'w') as f: json.dump({"mode":"FORCE_ON"}, f)
                elif k == 'f':
                    with open(CONFIG_PATH, 'w') as f: json.dump({"mode":"FORCE_OFF"}, f)
                elif k == 'q': break
            live.update(generate_dashboard(state))
            live.refresh()
            time.sleep(0.4)
