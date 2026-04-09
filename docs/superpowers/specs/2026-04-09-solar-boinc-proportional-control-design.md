# Solar BOINC Proportional Control — Design Spec
Date: 2026-04-09

## Goal
Maximize solar self-consumption by replacing the stepped GPU ramp-up logic with
real-time proportional control. GPU has absolute priority; CPU is a pure fill-in.

## System Context
- PV max output: 880W, max surplus: ~300W (house base load ~580W)
- GPU: RTX 3060, power limit range 100–180W (nvidia-smi hardware bounds)
- CPU: 5 cores, estimated draw ~70W when BOINC active
- Meter: Tasmota reads net grid point (negative = exporting, positive = importing)
- Allowed permanent grid draw: 50W

## Parameters

```python
GRID_TOLERANCE = 50   # W — allowed permanent grid draw budget
HYSTERESIS     = 20   # W — prevents oscillation at start/stop boundaries
GPU_MIN_W      = 100  # W — hardware minimum power limit (nvidia-smi floor)
GPU_MAX_W      = 180  # W — hardware maximum power limit
CPU_EST_W      = 70   # W — estimated CPU draw (5 BOINC cores)
CHECK_INTERVAL = 5    # s — reduced from 10s for tighter solar tracking
REQUIRED_CONFIRMATIONS = 2
```

## Derived Thresholds (calculated, not hardcoded)

| Boundary         | Formula                          | Value |
|------------------|----------------------------------|-------|
| GPU start        | GPU_MIN_W − GRID_TOLERANCE       | 50W   |
| GPU stop         | GPU start − HYSTERESIS           | 30W   |
| CPU start        | GPU_MAX_W + CPU_EST_W − GRID_TOLERANCE | 200W |
| CPU stop         | CPU start − HYSTERESIS           | 180W  |

"Surplus" in the table = `−raw_pwr` (positive number of watts being exported).

## Core Logic

### GPU (proportional)
```
ideal_limit = (−raw_pwr) + GRID_TOLERANCE
```

| State          | Condition                              | Action                              |
|----------------|----------------------------------------|-------------------------------------|
| GPU off        | ideal_limit >= GPU_MIN_W for 2 cycles  | Start GPU at clamped ideal_limit    |
| GPU running    | ideal_limit >= GPU_MIN_W               | Update limit immediately each cycle |
| GPU running    | ideal_limit < GPU_MIN_W for 2 cycles   | Stop GPU                            |
| Neither        | ideal_limit < GPU_MIN_W                | Reset counters                      |

GPU limit is always `clamp(ideal_limit, GPU_MIN_W, GPU_MAX_W)`.

### CPU (fill-in only)
CPU has no independent claim on runtime. Rules:

| Condition                                          | Action               |
|----------------------------------------------------|----------------------|
| GPU at MAX and surplus >= 200W for 2 cycles        | Start CPU            |
| GPU below MAX (any reason)                         | Stop CPU immediately |
| GPU at MAX but surplus < 180W for 2 cycles         | Stop CPU             |

### Counter Architecture
GPU and CPU use **separate** hit counters (`gpu_hits_up`, `gpu_hits_down`,
`cpu_hits_up`, `cpu_hits_down`). A GPU event never corrupts a CPU counter.

## Behavior Examples

| raw_pwr | surplus | ideal_limit | GPU state        | CPU state |
|---------|---------|-------------|------------------|-----------|
| −20W    | 20W     | 70W         | OFF (below min)  | OFF       |
| −60W    | 60W     | 110W        | ON at 110W       | OFF       |
| −130W   | 130W    | 180W        | ON at 180W (max) | OFF       |
| −210W   | 210W    | 260W→180W   | ON at 180W (max) | ON        |
| −40W    | 40W     | 90W         | stopping (< min) | OFF immediately |

## What Changes in Code
- `solar_boinc_control.py` only — dashboard unchanged
- Remove: `virtual_surplus`, stepped ramp logic, single shared hit counters
- Add: `ideal_limit` formula, separate GPU/CPU counters
- Tune: `CHECK_INTERVAL` 10→5, `GPU_ON_MIN`/`GPU_OFF_MAX` replaced by derived values
- Keep: FORCE_ON / FORCE_OFF manual modes, DB logging, cpupower profile sync
