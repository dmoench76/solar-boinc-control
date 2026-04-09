# Proportional GPU Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace stepped GPU ramp-up with real-time proportional control and give GPU absolute priority over CPU fill-in.

**Architecture:** Extract the entire AUTO decision logic from `main()` into a pure `control_step()` function that takes measurements + state, returns new state + action list. `main()` only fetches data, calls `control_step()`, and executes the returned actions. This makes the logic unit-testable without mocking hardware.

**Tech Stack:** Python 3, pytest, nvidia-smi, boinccmd, systemd

---

## File Map

| File | Change |
|------|--------|
| `solar_boinc_control.py` | Rewrite constants + AUTO logic + extract `control_step()` |
| `tests/test_control_step.py` | New — unit tests for `control_step()` |

---

### Task 1: Create test file with failing tests

**Files:**
- Create: `/home/dmoench/tests/test_control_step.py`

- [ ] **Step 1: Create tests directory and test file**

```bash
mkdir -p /home/dmoench/tests
```

```python
# /home/dmoench/tests/test_control_step.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solar_boinc_control as ctrl

def fresh():
    """Return default initial state dict."""
    return dict(
        gpu_active=False, cpu_active=False, cur_gpu_limit=100,
        gpu_hits_up=0, gpu_hits_down=0,
        cpu_hits_up=0, cpu_hits_down=0,
    )

def has_action(actions, kind, type_):
    return any(a[0] == kind and a[1] == type_ for a in actions)

# --- GPU start ---

def test_gpu_stays_off_below_start_threshold():
    # surplus=20W: ideal=70W < GPU_MIN_W=100 → no start
    s, a = ctrl.control_step(-20, **fresh())
    assert not s['gpu_active']
    assert not has_action(a, 'gpu', 'start')

def test_gpu_requires_two_confirmations_to_start():
    state = fresh()
    state, a = ctrl.control_step(-60, **state)   # surplus=60W, ideal=110W >= 100
    assert not state['gpu_active']               # first cycle: not yet
    assert state['gpu_hits_up'] == 1
    state, a = ctrl.control_step(-60, **state)   # second cycle
    assert state['gpu_active']
    assert has_action(a, 'gpu', 'start')

def test_gpu_starts_at_proportional_limit():
    # Two cycles at -80W (surplus=80, ideal=130) → GPU starts at 130W
    state = fresh()
    state, _ = ctrl.control_step(-80, **state)
    state, a = ctrl.control_step(-80, **state)
    assert state['gpu_active']
    assert state['cur_gpu_limit'] == 130

# --- GPU proportional tracking ---

def test_gpu_limit_updates_proportionally_each_cycle():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, a = ctrl.control_step(-100, **state)    # surplus=100, ideal=150
    assert state['cur_gpu_limit'] == 150
    assert has_action(a, 'gpu', 'limit')

def test_gpu_limit_capped_at_gpu_max():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(-250, **state)    # ideal=300 → capped at 180
    assert state['cur_gpu_limit'] == 180
    assert not has_action(a, 'gpu', 'limit')       # no change emitted

def test_gpu_limit_tracks_down_when_surplus_shrinks():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 170
    state, a = ctrl.control_step(-80, **state)     # surplus=80, ideal=130
    assert state['cur_gpu_limit'] == 130
    assert has_action(a, 'gpu', 'limit')

# --- GPU hysteresis zone (30W <= surplus < 50W) ---

def test_gpu_stays_on_in_hysteresis_zone():
    # surplus=40W: below start (50W) but above stop threshold (30W)
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 150
    state, a = ctrl.control_step(-40, **state)
    assert state['gpu_active']                     # should NOT stop
    assert state['cur_gpu_limit'] == 100           # clamped to GPU_MIN_W
    assert not has_action(a, 'gpu', 'stop')

def test_gpu_off_does_not_start_in_hysteresis_zone():
    # GPU is off and surplus is 40W → should not start (below 50W threshold)
    state = fresh()
    state, a = ctrl.control_step(-40, **state)
    assert not state['gpu_active']
    assert not has_action(a, 'gpu', 'start')

# --- GPU stop (below 30W) ---

def test_gpu_requires_two_cycles_to_stop():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, a = ctrl.control_step(-20, **state)     # surplus=20W < stop threshold 30W
    assert state['gpu_active']                     # first cycle: not yet
    assert not has_action(a, 'gpu', 'stop')
    state, a = ctrl.control_step(-20, **state)     # second cycle
    assert not state['gpu_active']
    assert has_action(a, 'gpu', 'stop')

def test_gpu_stop_resets_counters():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, _ = ctrl.control_step(-20, **state)
    state, _ = ctrl.control_step(-20, **state)
    assert state['gpu_hits_down'] == 0

# --- CPU fill-in ---

def test_cpu_does_not_start_when_gpu_below_max():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 150                  # GPU not at max (180W)
    state, a = ctrl.control_step(-210, **state)
    assert not state['cpu_active']
    assert not has_action(a, 'cpu', 'start')

def test_cpu_requires_two_confirmations_to_start():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180                  # GPU at max
    state, a = ctrl.control_step(-210, **state)   # surplus=210 >= CPU_START_SURPLUS=200
    assert not state['cpu_active']
    assert state['cpu_hits_up'] == 1
    state, a = ctrl.control_step(-210, **state)
    assert state['cpu_active']
    assert has_action(a, 'cpu', 'start')

def test_cpu_stops_immediately_if_gpu_drops_below_max():
    state = fresh()
    state['gpu_active'] = True
    state['cpu_active'] = True
    state['cur_gpu_limit'] = 180
    # surplus=120W → ideal=170W < GPU_MAX_W=180 → GPU runs at 170W, CPU must stop
    state, a = ctrl.control_step(-120, **state)
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')

def test_cpu_stops_after_two_cycles_when_surplus_insufficient():
    state = fresh()
    state['gpu_active'] = True
    state['cpu_active'] = True
    state['cur_gpu_limit'] = 180
    # surplus=175W < CPU_STOP_SURPLUS=180W
    state, a = ctrl.control_step(-175, **state)
    assert state['cpu_active']                    # first hit, not yet
    state, a = ctrl.control_step(-175, **state)
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')

def test_cpu_off_when_gpu_off():
    # Defensive: CPU should never run without GPU
    state = fresh()
    state['cpu_active'] = True
    state, a = ctrl.control_step(-20, **state)
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')
```

- [ ] **Step 2: Run tests — verify they all fail with AttributeError**

```bash
cd /home/dmoench && python3 -m pytest tests/test_control_step.py -v 2>&1 | head -20
```

Expected: `AttributeError: module 'solar_boinc_control' has no attribute 'control_step'`

---

### Task 2: Rewrite `solar_boinc_control.py`

**Files:**
- Modify: `/home/dmoench/solar_boinc_control.py`

- [ ] **Step 1: Replace constants block (lines 18–27)**

Replace from `# Hardware Limits` through `REQUIRED_CONFIRMATIONS = 2` with:

```python
# Hardware Limits
GPU_MIN_W = 100
GPU_MAX_W = 180
CPU_EST_W = 70   # Estimated draw of 5 BOINC cores

# Control parameters
GRID_TOLERANCE         = 50   # W — allowed permanent grid draw
HYSTERESIS             = 20   # W — prevents oscillation at start/stop boundaries
REQUIRED_CONFIRMATIONS = 2
CHECK_INTERVAL         = 5    # s — tighter solar tracking (was 10s)

# Derived thresholds (do not hardcode elsewhere)
# GPU starts when surplus >= GPU_MIN_W - GRID_TOLERANCE = 50W
# GPU stops when surplus <  GPU_MIN_W - GRID_TOLERANCE - HYSTERESIS = 30W
GPU_STOP_SURPLUS  = GPU_MIN_W - GRID_TOLERANCE - HYSTERESIS   # = 30W
CPU_START_SURPLUS = GPU_MAX_W + CPU_EST_W - GRID_TOLERANCE    # = 200W
CPU_STOP_SURPLUS  = CPU_START_SURPLUS - HYSTERESIS             # = 180W
```

- [ ] **Step 2: Add `control_step()` after `get_gpu_power()`, before `main()`**

```python
def control_step(raw_pwr, gpu_active, cpu_active, cur_gpu_limit,
                 gpu_hits_up, gpu_hits_down, cpu_hits_up, cpu_hits_down):
    """Pure decision function. No I/O. Returns (new_state_dict, actions_list).

    Action tuples:
      ('gpu', 'start', watts)  — start GPU at given power limit
      ('gpu', 'limit', watts)  — update running GPU power limit
      ('gpu', 'stop',  None)   — stop GPU
      ('cpu', 'start', None)   — start CPU
      ('cpu', 'stop',  None)   — stop CPU
    """
    surplus      = -raw_pwr
    ideal_limit  = surplus + GRID_TOLERANCE
    actual_limit = max(GPU_MIN_W, min(int(ideal_limit), GPU_MAX_W))
    actions      = []

    # --- GPU ---
    if ideal_limit >= GPU_MIN_W:
        # Sufficient surplus: start or track proportionally
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

    elif gpu_active and surplus < GPU_STOP_SURPLUS:
        # Below stop threshold (30W): count down to stop
        gpu_hits_down += 1; gpu_hits_up = 0
        if gpu_hits_down >= REQUIRED_CONFIRMATIONS:
            actions.append(('gpu', 'stop', None))
            gpu_active = False; gpu_hits_down = 0

    elif gpu_active:
        # Hysteresis zone (30W <= surplus < 50W): keep running at MIN, no stop
        if cur_gpu_limit != GPU_MIN_W:
            actions.append(('gpu', 'limit', GPU_MIN_W))
            cur_gpu_limit = GPU_MIN_W
        gpu_hits_up = 0; gpu_hits_down = 0

    else:
        # GPU off, surplus too low to start
        gpu_hits_up = 0; gpu_hits_down = 0

    # --- CPU fill-in (only when GPU is saturated at MAX) ---
    if gpu_active and cur_gpu_limit >= GPU_MAX_W:
        if surplus >= CPU_START_SURPLUS and not cpu_active:
            cpu_hits_up += 1; cpu_hits_down = 0
            if cpu_hits_up >= REQUIRED_CONFIRMATIONS:
                actions.append(('cpu', 'start', None))
                cpu_active = True; cpu_hits_up = 0
        elif surplus < CPU_STOP_SURPLUS and cpu_active:
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
```

- [ ] **Step 3: Replace `main()` through end of file**

```python
def main():
    logging.info("Starting Solar Engine v12 (Proportional Control)...")
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
```

---

### Task 3: Run tests — all must pass

**Files:**
- Test: `/home/dmoench/tests/test_control_step.py`

- [ ] **Step 1: Run full test suite**

```bash
cd /home/dmoench && python3 -m pytest tests/test_control_step.py -v
```

Expected (all green):
```
test_gpu_stays_off_below_start_threshold PASSED
test_gpu_requires_two_confirmations_to_start PASSED
test_gpu_starts_at_proportional_limit PASSED
test_gpu_limit_updates_proportionally_each_cycle PASSED
test_gpu_limit_capped_at_gpu_max PASSED
test_gpu_limit_tracks_down_when_surplus_shrinks PASSED
test_gpu_stays_on_in_hysteresis_zone PASSED
test_gpu_off_does_not_start_in_hysteresis_zone PASSED
test_gpu_requires_two_cycles_to_stop PASSED
test_gpu_stop_resets_counters PASSED
test_cpu_does_not_start_when_gpu_below_max PASSED
test_cpu_requires_two_confirmations_to_start PASSED
test_cpu_stops_immediately_if_gpu_drops_below_max PASSED
test_cpu_stops_after_two_cycles_when_surplus_insufficient PASSED
test_cpu_off_when_gpu_off PASSED

15 passed
```

- [ ] **Step 2: Fix any failures before continuing**

Read the assertion message. Fix the logic in `control_step()`. Do not skip or delete tests.

---

### Task 4: Deploy & verify

- [ ] **Step 1: Restart service**

```bash
sudo systemctl restart solar-boinc.service
```

- [ ] **Step 2: Verify running**

```bash
sudo systemctl status solar-boinc.service --no-pager
```

Expected: `Active: active (running)`

- [ ] **Step 3: Watch first log entries**

```bash
journalctl -u solar-boinc.service -f -n 20
```

Confirm: no `Logic Error` lines, version shows `v12`.

- [ ] **Step 4: Commit**

```bash
cd /home/dmoench
git add solar_boinc_control.py tests/test_control_step.py docs/
git commit -m "feat: proportional GPU control with CPU fill-in (v12)

- GPU limit tracks solar surplus in real-time: limit = surplus + 50W tolerance
- Hysteresis: GPU starts >= 50W surplus, stops < 30W surplus
- CPU fill-in only when GPU saturated at 180W and surplus >= 200W
- CPU stops immediately when GPU drops below max
- Separate hit counters for GPU and CPU
- CHECK_INTERVAL reduced 10s -> 5s for tighter tracking

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
