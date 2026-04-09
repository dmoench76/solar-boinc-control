import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solar_boinc_control as ctrl

def fresh():
    """Default initial state — GPU off, all counters zero."""
    return dict(
        gpu_active=False, cpu_active=False, cur_gpu_limit=100,
        gpu_hits_up=0, gpu_hits_down=0,
        cpu_hits_up=0, cpu_hits_down=0,
    )

def has_action(actions, kind, type_):
    return any(a[0] == kind and a[1] == type_ for a in actions)

# ─────────────────────────────────────────────
# GPU START
# virtual = (-raw_pwr) + 0  (GPU off, add 0)
# ideal   = virtual + 50
# start zone: ideal >= 100  →  virtual >= 50  →  surplus >= 50W  →  raw_pwr <= -50W
# ─────────────────────────────────────────────

def test_gpu_stays_off_below_start_threshold():
    # surplus=20W → virtual=20 → ideal=70 < 100 → no start
    s, a = ctrl.control_step(-20, **fresh())
    assert not s['gpu_active']
    assert not has_action(a, 'gpu', 'start')

def test_gpu_stays_off_just_below_start_threshold():
    # surplus=49W → virtual=49 → ideal=99 < 100 → no start
    s, a = ctrl.control_step(-49, **fresh())
    assert not s['gpu_active']

def test_gpu_requires_two_confirmations_to_start():
    state = fresh()
    state, a = ctrl.control_step(-60, **state)   # surplus=60 → ideal=110 ≥ 100
    assert not state['gpu_active']               # first cycle: not yet
    assert state['gpu_hits_up'] == 1
    state, a = ctrl.control_step(-60, **state)   # second cycle
    assert state['gpu_active']
    assert has_action(a, 'gpu', 'start')

def test_gpu_starts_at_proportional_limit():
    # surplus=80 → virtual=80 → ideal=130 → starts at 130W
    state = fresh()
    state, _ = ctrl.control_step(-80, **state)
    state, a = ctrl.control_step(-80, **state)
    assert state['gpu_active']
    assert state['cur_gpu_limit'] == 130

def test_gpu_starts_at_max_when_surplus_very_high():
    # surplus=250 → virtual=250 → ideal=300 → capped at 180W
    state = fresh()
    state, _ = ctrl.control_step(-250, **state)
    state, a = ctrl.control_step(-250, **state)
    assert state['gpu_active']
    assert state['cur_gpu_limit'] == 180

# ─────────────────────────────────────────────
# GPU PROPORTIONAL TRACKING (GPU running)
# virtual = (-raw_pwr) + cur_gpu_limit
# ─────────────────────────────────────────────

def test_gpu_stable_with_200w_baseline_surplus():
    """Core stability test: GPU at 180W should not oscillate with 200W baseline."""
    state = fresh()
    # GPU starts after 2 cycles at 200W surplus
    state, _ = ctrl.control_step(-200, **state)
    state, _ = ctrl.control_step(-200, **state)
    assert state['gpu_active']
    assert state['cur_gpu_limit'] == 180

    # After GPU draws 180W, Tasmota drops to -20W (200-180=20W still exported)
    # virtual = 20 + 180 = 200 → ideal=250→180W → STABLE, no stop
    state, a = ctrl.control_step(-20, **state)
    assert state['gpu_active']
    assert state['cur_gpu_limit'] == 180
    assert not has_action(a, 'gpu', 'stop')

def test_gpu_limit_updates_proportionally():
    # GPU at 100W, raw_pwr=+10W (importing 10W, within 50W tolerance)
    # virtual = -10 + 100 = 90 → ideal=140 → GPU to 140W
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, a = ctrl.control_step(+10, **state)
    assert state['cur_gpu_limit'] == 140
    assert has_action(a, 'gpu', 'limit')

def test_gpu_limit_capped_at_max():
    # GPU at 180W, large export → stays at 180W, no action emitted
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(-100, **state)   # virtual=100+180=280 → ideal=330→180W
    assert state['cur_gpu_limit'] == 180
    assert not has_action(a, 'gpu', 'limit')

def test_gpu_limit_tracks_down_when_grid_draw_excessive():
    # GPU at 180W, importing 80W from grid (exceeds 50W tolerance)
    # virtual = -80 + 180 = 100 → ideal=150 → GPU drops to 150W
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(+80, **state)
    assert state['cur_gpu_limit'] == 150
    assert has_action(a, 'gpu', 'limit')

# ─────────────────────────────────────────────
# GPU STOP
# stop zone: ideal < GPU_MIN_W  →  virtual < 50
# ─────────────────────────────────────────────

def test_gpu_requires_two_cycles_to_stop():
    # GPU at 100W, importing 60W → virtual=-60+100=40 < 50 → stop zone
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, a = ctrl.control_step(+60, **state)    # first cycle
    assert state['gpu_active']
    assert not has_action(a, 'gpu', 'stop')
    state, a = ctrl.control_step(+60, **state)    # second cycle
    assert not state['gpu_active']
    assert has_action(a, 'gpu', 'stop')

def test_gpu_stop_resets_counters():
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, _ = ctrl.control_step(+60, **state)
    state, _ = ctrl.control_step(+60, **state)
    assert state['gpu_hits_down'] == 0

def test_gpu_does_not_stop_at_exact_tolerance():
    # GPU at 100W, importing exactly 50W → virtual=0+100=100 (wait: raw_pwr=+50)
    # virtual = -50 + 100 = 50 → ideal=100 = GPU_MIN_W → still in run zone (>=)
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 100
    state, a = ctrl.control_step(+50, **state)
    assert state['gpu_active']
    assert not has_action(a, 'gpu', 'stop')

# ─────────────────────────────────────────────
# CPU FILL-IN
# CPU uses same virtual surplus
# CPU_START_SURPLUS = 200W, CPU_STOP_SURPLUS = 180W
# When GPU at 180W: start when virtual≥200 → raw_pwr≤-20W
# ─────────────────────────────────────────────

def test_cpu_does_not_start_when_virtual_surplus_too_low():
    # GPU at 180W, importing 30W → virtual=-30+180=150 < 200 → no CPU start
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(+30, **state)
    assert not state['cpu_active']
    assert state['cpu_hits_up'] == 0
    assert not has_action(a, 'cpu', 'start')

def test_cpu_requires_two_confirmations_to_start():
    # GPU at 180W, exporting 30W → virtual=30+180=210 ≥ 200 → start zone
    state = fresh()
    state['gpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(-30, **state)    # first cycle
    assert not state['cpu_active']
    assert state['cpu_hits_up'] == 1
    state, a = ctrl.control_step(-30, **state)    # second cycle
    assert state['cpu_active']
    assert has_action(a, 'cpu', 'start')

def test_cpu_stops_immediately_if_gpu_drops_below_max():
    # GPU at 180W, importing 80W → GPU drops to 150W → CPU stops immediately
    state = fresh()
    state['gpu_active'] = True
    state['cpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(+80, **state)    # virtual=-80+180=100 → ideal=150
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')

def test_cpu_stops_after_two_cycles_when_virtual_drops():
    # GPU at 180W, CPU active, import 10W → virtual=-10+180=170 < CPU_STOP_SURPLUS=180
    state = fresh()
    state['gpu_active'] = True
    state['cpu_active'] = True
    state['cur_gpu_limit'] = 180
    state, a = ctrl.control_step(+10, **state)    # first cycle: virtual=170 < 180
    assert state['cpu_active']
    assert not has_action(a, 'cpu', 'stop')
    state, a = ctrl.control_step(+10, **state)    # second cycle
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')

def test_cpu_off_when_gpu_off():
    state = fresh()
    state['cpu_active'] = True                    # defensive: should never happen
    state, a = ctrl.control_step(-20, **state)
    assert not state['cpu_active']
    assert has_action(a, 'cpu', 'stop')
