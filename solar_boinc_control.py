import requests
import subprocess
import time
import json
import logging
import sqlite3
import os
import psutil

# Configuration
CHECK_INTERVAL = 5
DB_PATH = "/home/dmoench/solar_history.db"
CONFIG_PATH = "/home/dmoench/solar_config.json"
SETTINGS_PATH = "/home/dmoench/solar_settings.json"
BOINC_CMD = "/usr/bin/boinccmd"
RAPL_PATH = "/sys/class/powercap/intel-rapl:0/energy_uj"

def get_settings():
    with open(SETTINGS_PATH, 'r') as f:
        return json.load(f)

TASMOTA_URL      = get_settings()["tasmota_url"]
BOINC_RPC_PASSWD = get_settings().get("boinc_rpc_password", "")

# Hardware Limits
GPU_MIN_W = 100
GPU_MAX_W = 180
CPU_EST_W = 70   # Estimated draw of 5 BOINC cores

# Control parameters
GRID_TOLERANCE         = 50   # W — allowed permanent grid draw
HYSTERESIS             = 20   # W — CPU stop buffer
REQUIRED_CONFIRMATIONS = 3
EMERGENCY_STOP_W       = 300  # W — immediate stop without confirmation

# Derived thresholds (virtual surplus based)
# GPU starts when virtual surplus >= GPU_MIN_W - GRID_TOLERANCE = 50W
# GPU stops when virtual surplus <  50W  (ideal_limit < GPU_MIN_W)
# CPU starts when virtual surplus >= GPU_MAX_W + CPU_EST_W - GRID_TOLERANCE = 200W
# CPU stops when virtual surplus <  200W - HYSTERESIS = 180W
CPU_START_SURPLUS = GPU_MAX_W + CPU_EST_W - GRID_TOLERANCE   # = 200W
CPU_STOP_SURPLUS  = CPU_START_SURPLUS - HYSTERESIS            # = 180W

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_config():
    try:
        with open(CONFIG_PATH, 'r') as f: return json.load(f)
    except: return {"mode": "AUTO"}

def set_gpu_limit(watts):
    limit = max(GPU_MIN_W, min(watts, GPU_MAX_W))
    subprocess.run(["sudo", "nvidia-smi", "-pl", str(int(limit))], capture_output=True)
    return limit

def set_boinc(target, mode):
    duration = 0 if mode == 'always' else 9999999
    if target == 'gpu' and mode == 'always':
        subprocess.run(["sudo", "nvidia-smi", "-pm", "1"], capture_output=True)
    cmd = [BOINC_CMD]
    if BOINC_RPC_PASSWD:
        cmd += ["--passwd", BOINC_RPC_PASSWD]
    cmd += [f"--set_{target}_mode", mode, str(duration)]
    subprocess.run(cmd, capture_output=True)

def get_cpu_power():
    try:
        with open(RAPL_PATH) as f: e1 = int(f.read())
        time.sleep(0.2)
        with open(RAPL_PATH) as f: e2 = int(f.read())
        return (e2 - e1) / 200000.0
    except: return 0.0

def get_gpu_power():
    try:
        res = subprocess.run(["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"], capture_output=True, text=True)
        if res.returncode == 0: return float(res.stdout.strip())
    except: pass
    return 0.0

def control_step(raw_pwr, gpu_active, cpu_active, cur_gpu_limit,
                 gpu_hits_up, gpu_hits_down, cpu_hits_up, cpu_hits_down):
    """Pure decision function. No I/O. Returns (new_state_dict, actions_list).

    Virtual surplus = what solar produces above house load, independent of BOINC.
    Formula: virtual = (-raw_pwr) + (cur_gpu_limit if gpu_active else 0)

    Action tuples:
      ('gpu', 'start', watts)  — start GPU at given power limit
      ('gpu', 'limit', watts)  — update running GPU power limit
      ('gpu', 'stop',  None)   — stop GPU
      ('cpu', 'start', None)   — start CPU
      ('cpu', 'stop',  None)   — stop CPU
    """
    virtual      = (-raw_pwr) + (cur_gpu_limit if gpu_active else 0)
    ideal_limit  = virtual + GRID_TOLERANCE
    actual_limit = max(GPU_MIN_W, min(int(ideal_limit), GPU_MAX_W))
    actions      = []

    # --- Emergency stop (raw grid draw exceeds threshold) ---
    if raw_pwr > EMERGENCY_STOP_W:
        if cpu_active:
            actions.append(('cpu', 'stop', None))
            cpu_active = False
        if gpu_active:
            actions.append(('gpu', 'stop', None))
            gpu_active = False
        return {
            'gpu_active': gpu_active, 'cpu_active': cpu_active,
            'cur_gpu_limit': cur_gpu_limit,
            'gpu_hits_up': 0, 'gpu_hits_down': 0,
            'cpu_hits_up': 0, 'cpu_hits_down': 0,
        }, actions

    # --- GPU ---
    if ideal_limit >= GPU_MIN_W:
        # Sufficient virtual surplus: start or track proportionally
        if not gpu_active:
            gpu_hits_up += 1; gpu_hits_down = 0
            if gpu_hits_up >= REQUIRED_CONFIRMATIONS:
                actions.append(('gpu', 'start', actual_limit))
                gpu_active = True; cur_gpu_limit = actual_limit; gpu_hits_up = 0
        else:
            if actual_limit != cur_gpu_limit:
                actions.append(('gpu', 'limit', actual_limit))
            cur_gpu_limit = actual_limit
            gpu_hits_up = 0; gpu_hits_down = 0
    else:
        # Virtual surplus insufficient for GPU_MIN_W
        if gpu_active:
            if cpu_active:
                # CPU läuft noch — zuerst CPU stoppen, GPU-Countdown zurücksetzen.
                # GPU bekommt 2 frische Messungen ohne CPU-Last.
                actions.append(('cpu', 'stop', None))
                cpu_active = False
                cpu_hits_up = 0; cpu_hits_down = 0
                gpu_hits_down = 0
            else:
                # CPU bereits aus — jetzt GPU-Countdown
                gpu_hits_down += 1; gpu_hits_up = 0
                if gpu_hits_down >= REQUIRED_CONFIRMATIONS:
                    actions.append(('gpu', 'stop', None))
                    gpu_active = False; gpu_hits_down = 0
        else:
            gpu_hits_up = 0; gpu_hits_down = 0

    # --- CPU fill-in (only when GPU is saturated at MAX) ---
    if gpu_active and cur_gpu_limit >= GPU_MAX_W:
        if virtual >= CPU_START_SURPLUS and not cpu_active:
            cpu_hits_up += 1; cpu_hits_down = 0
            if cpu_hits_up >= REQUIRED_CONFIRMATIONS:
                actions.append(('cpu', 'start', None))
                cpu_active = True; cpu_hits_up = 0
        elif virtual < CPU_STOP_SURPLUS and cpu_active:
            cpu_hits_down += 1; cpu_hits_up = 0
            if cpu_hits_down >= REQUIRED_CONFIRMATIONS:
                actions.append(('cpu', 'stop', None))
                cpu_active = False; cpu_hits_down = 0
        else:
            cpu_hits_up = 0; cpu_hits_down = 0
    else:
        if cpu_active:
            actions.append(('cpu', 'stop', None))
            cpu_active = False
        cpu_hits_up = 0; cpu_hits_down = 0

    return {
        'gpu_active': gpu_active, 'cpu_active': cpu_active,
        'cur_gpu_limit': cur_gpu_limit,
        'gpu_hits_up': gpu_hits_up, 'gpu_hits_down': gpu_hits_down,
        'cpu_hits_up': cpu_hits_up, 'cpu_hits_down': cpu_hits_down,
    }, actions

def main():
    logging.info("Starting Solar Engine v12 (Proportional Control — Virtual Surplus)...")
    state = dict(
        gpu_active=False, cpu_active=False, cur_gpu_limit=GPU_MIN_W,
        gpu_hits_up=0, gpu_hits_down=0, cpu_hits_up=0, cpu_hits_down=0,
    )

    while True:
        try:
            mode    = get_config().get("mode", "AUTO")
            raw_pwr = requests.get(TASMOTA_URL, timeout=5).json() \
                              .get("StatusSNS", {}).get("MT631", {}).get("Power", 0)
            cur_gpu = get_gpu_power()
            cur_cpu = get_cpu_power()

            if mode == "FORCE_ON":
                if not state['gpu_active']: set_boinc('gpu', 'always'); state['gpu_active'] = True
                if not state['cpu_active']: set_boinc('run', 'always'); state['cpu_active'] = True
                state['cur_gpu_limit'] = set_gpu_limit(GPU_MAX_W)
                label = "MANUAL: MAX"

            elif mode == "FORCE_OFF":
                if state['gpu_active']: set_boinc('gpu', 'never'); state['gpu_active'] = False
                if state['cpu_active']: set_boinc('run', 'never'); state['cpu_active'] = False
                label = "MANUAL: OFF"

            else:
                new_state, actions = control_step(raw_pwr, **state)
                state = new_state

                for kind, action, value in actions:
                    if kind == 'gpu':
                        if action == 'start':
                            set_boinc('gpu', 'always')
                            state['cur_gpu_limit'] = set_gpu_limit(value)
                            logging.info(f"GPU START @ {value}W")
                        elif action == 'limit':
                            state['cur_gpu_limit'] = set_gpu_limit(value)
                            logging.info(f"GPU -> {value}W")
                        elif action == 'stop':
                            set_boinc('gpu', 'never')
                            logging.info("GPU STOP")
                    elif kind == 'cpu':
                        if action == 'start':
                            set_boinc('run', 'always')
                            logging.info("CPU START")
                        elif action == 'stop':
                            set_boinc('run', 'never')
                            logging.info("CPU STOP")

                label = "IDLE"
                if state['gpu_active']: label = f"GPU {state['cur_gpu_limit']}W"
                if state['cpu_active']: label = "FULL POWER"
                if state['gpu_hits_up']   > 0: label += f" (+{state['gpu_hits_up']})"
                if state['gpu_hits_down'] > 0: label += f" (-{state['gpu_hits_down']})"

            gov = "performance" if (state['gpu_active'] or state['cpu_active']) else "powersave"
            subprocess.run(["sudo", "cpupower", "frequency-set", "-g", gov], capture_output=True)

            try:
                conn = sqlite3.connect(DB_PATH); c = conn.cursor()
                c.execute(
                    "INSERT INTO solar_log "
                    "(power, boinc_active, cpu_usage, gpu_power, status_label, cpu_power) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (raw_pwr,
                     (1 if state['cpu_active'] else 0) + (1 if state['gpu_active'] else 0),
                     psutil.cpu_percent(), cur_gpu, label, cur_cpu)
                )
                conn.commit(); conn.close()
            except: pass

        except Exception as e:
            logging.error(f"Logic Error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
