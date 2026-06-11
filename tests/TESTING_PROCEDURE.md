# Galil Controller Suite — Testing Procedure

Use this document as a checklist before deploying code changes or connecting new hardware.  
Three tiers: **automated** (run any time, no hardware), **manual mock** (GUI smoke test, no hardware), **hardware** (physical DMC-4133 required).

---

## Tier 1 — Automated Tests (no hardware)

### Run the full suite

```bat
cd galil_controller_suite
pytest tests/ -v
```

### Expected output (48 tests)

```
tests/test_controller.py::test_connect                        PASSED
tests/test_controller.py::test_poll_returns_all_axes          PASSED
tests/test_controller.py::test_enable_disable                 PASSED
tests/test_controller.py::test_move_relative                  PASSED
tests/test_controller.py::test_move_absolute                  PASSED
tests/test_controller.py::test_define_zero                    PASSED
tests/test_controller.py::test_halt_does_not_raise            PASSED
tests/test_controller.py::test_halt_all_does_not_raise        PASSED
tests/test_controller.py::test_emergency_stop_disables_all    PASSED
tests/test_controller.py::test_fwd_limit_simulation           PASSED
tests/test_controller.py::test_move_all_absolute              PASSED
tests/test_controller.py::test_rev_limit_simulation           PASSED
tests/test_controller.py::test_estop_clears_servo_and_stop_code PASSED
tests/test_position_memory.py::* (6 tests)                    PASSED
tests/test_unit_converter.py::* (13 tests)                    PASSED
tests/test_mock_limits.py::* (18 tests)                       PASSED
```

Expected: **48 passed, 0 failed, 0 errors.**

### Specific test groups and what they validate

| Test file | What it validates |
|---|---|
| `test_controller.py` | Basic MockGalilController API, enable/disable, move, emergency stop, limit simulation |
| `test_position_memory.py` | Save / load / delete named position slots to `config/positions.json` |
| `test_unit_converter.py` | counts↔µm conversion accuracy and edge cases (zero, negative, round-trip) |
| `test_mock_limits.py` | Jog continuity, halt, XY dual-trip, Z single-trip, stop codes, last_direction, home switch |

### Run only a specific group

```bat
pytest tests/test_mock_limits.py -v          # limit and direction tests only
pytest tests/test_mock_limits.py -k "jog"   # only jog-related tests
pytest tests/ -v --tb=short                  # shorter traceback on failure
```

---

## Tier 2 — Manual GUI Smoke Tests (mock mode, no hardware)

```bat
python run.py --mock
```

Work through each checklist item. All should complete without errors or crashes.

### 2.1 Startup

- [ ] Application opens without errors
- [ ] Window title shows "Galil DMC-4133 Controller" or similar
- [ ] All five sidebar nav buttons are clickable (Connection, Dashboard, Jog, Settings, Log)
- [ ] Bottom bar shows **HALT** (amber) and **E-STOP** (red) buttons

### 2.2 Connection panel

- [ ] "Scan Network" does not crash
- [ ] "Connect" succeeds (status area shows "DMC-4133-MOCK")
- [ ] Disconnect / reconnect works

### 2.3 Dashboard panel

- [ ] Three axis cards (A, B, C) show updating position values
- [ ] Position history chart scrolls in real time
- [ ] No "---" / NaN values in readout

### 2.4 Jog panel — hold-to-jog

- [ ] Click and hold **X+**: position in the readout increases continuously while held
- [ ] Release **X+**: position stops advancing within one poll cycle (~100 ms)
- [ ] Repeat for X−, Y+, Y−, Z+, Z−

### 2.5 Jog panel — step-click

- [ ] Select step preset "10 µm"
- [ ] Click **+** (X step): Axis A position advances by 10 µm × counts_per_um (default 100 counts)
- [ ] Click **−** (X step): Axis A position decreases by the same amount
- [ ] Repeat for Y axis

### 2.6 Jog panel — XY limit simulation

*(Manually trigger via the mock controller's `set_trip_points`; or use the smoke-test script below)*

Open a Python shell with the running app OR run the dedicated limit smoke test:

```bat
python -c "
import sys; sys.path.insert(0, '.')
from controller.mock_controller import MockGalilController
c = MockGalilController()
c.enable_axes('ABC')
c.set_trip_points('A', fwd=1000.0, rev=-1000.0)
c.jog('A', 500)
for _ in range(30): c.poll_all_axes()
s = c.poll_all_axes()
print('A fwd_limit:', s['A'].fwd_limit, '| rev_limit:', s['A'].rev_limit)
print('last_direction:', s['A'].last_direction)
"
```

Expected output: `A fwd_limit: True | rev_limit: True`, `last_direction: 1`

In the GUI:
- [ ] When A hits a limit, the **FWD** and **REV** columns in the limit table both show **HIT ●**
- [ ] The alert banner shows "Axis A (X) LIMIT — jog REV (−) to escape"
- [ ] The **X+** jog button is disabled (greyed out)
- [ ] The **X−** jog button remains enabled
- [ ] Releasing and clicking X− moves the axis back into bounds and clears the alert

### 2.7 Jog panel — Z limit simulation

```bat
python -c "
import sys; sys.path.insert(0, '.')
from controller.mock_controller import MockGalilController
c = MockGalilController()
c.enable_axes('C')
c.set_trip_points('C', fwd=1000.0, rev=-500.0)
c.jog('C', 500)
for _ in range(30): c.poll_all_axes()
s = c.poll_all_axes()
print('C fwd_limit:', s['C'].fwd_limit, '| rev_limit:', s['C'].rev_limit)
"
```

Expected: `C fwd_limit: True | rev_limit: False`

In the GUI (after reaching fwd trip):
- [ ] Only **FWD** column for Axis C shows **HIT ●**; **REV** stays **CLR ●**
- [ ] Alert: "Axis C (Z) FWD limit — jog REV (↓) to escape"
- [ ] **Z+** button disabled, **Z−** enabled

### 2.8 Settings panel

- [ ] Change "Counts per µm" for Axis A from 100 to 50, click Apply
- [ ] Open Jog panel → select 10 µm step → click X+ step → position advances ~500 counts (10 µm × 50 cpm)
- [ ] Restore counts_per_um to 100 and save

### 2.9 HALT and E-STOP buttons

- [ ] While jogging X+, press **HALT**: motion decelerates and stops; servo stays ON (step-clicks still work)
- [ ] While jogging X+, press **E-STOP**: motion stops immediately; servo is OFF; enable is required before next move

### 2.10 Log panel

- [ ] Log panel shows timestamped TX/RX lines for each move command
- [ ] After a move completes, a line like `[A] Motion complete → 1000.0 counts` appears in the log file (`galil_log.txt`)
- [ ] Filter buttons (TX, RX, ERR) work correctly
- [ ] Raw DMC command box: type `ST` and press Enter — log shows `>> ST` and `<< :`

### 2.11 Position memory

- [ ] Move axes to a known position (e.g. A=1000, B=0, C=0)
- [ ] Open Jog panel → Position Memory → "SAVE" → enter name "test_pos"
- [ ] Move axes to a different position
- [ ] Select "test_pos" and click "GO TO" → axes return to A=1000

### 2.12 Crash / stability check

- [ ] Open app, leave running for ~60 seconds (600 poll cycles)
- [ ] No memory errors, no frozen UI, no log spam

---

## Tier 3 — Hardware Tests (DMC-4133 required)

> Run these only after all Tier 1 and Tier 2 checks pass.

### 3.0 Prerequisites

- gclib installed (see README)
- DMC-4133 powered, connected via Ethernet or serial
- All axes are in a safe, mid-travel position

### 3.1 Connection

```bat
python run.py --address 192.168.0.40   # or your controller's IP/COM
```

- [ ] Connection panel shows correct model and serial
- [ ] Latency displayed < 5 ms (Ethernet) or < 20 ms (Serial)

### 3.2 Servo enable

- [ ] Click **Enable All** in Connection panel (or Settings)
- [ ] Dashboard shows `servo_on = True` for all axes
- [ ] No position error fault (`stop_code != 3`)

### 3.3 Move relative

- [ ] From Jog panel, set step = 100 µm, click X+ step
- [ ] Axis A moves 100 µm × counts_per_um in the expected physical direction
- [ ] Motion complete message logged in `galil_log.txt`
- [ ] Repeat for Y (B) and Z (C)

### 3.4 Move to absolute position

- [ ] Enter a known safe target in "Move to Absolute Position" and click MOVE
- [ ] Axis arrives at target within `error_limit` counts
- [ ] Dashboard chart shows the motion profile

### 3.5 Jog and HALT

- [ ] Hold X+ jog button — axis moves forward
- [ ] Release — axis decelerates to a smooth stop (servo ON, no fault)
- [ ] Press **HALT**: same result, no fault

### 3.6 E-STOP

- [ ] Start a slow jog, press **E-STOP** immediately
- [ ] Axis stops with no deceleration ramp
- [ ] All servo indicators show OFF
- [ ] Hardware must be re-enabled before any further motion

### 3.7 Forward limit switch (Z axis)

- [ ] Manually drive Z toward the forward (top) limit — via slow jog
- [ ] When physical switch activates: only `fwd_limit` light shows HIT for C
- [ ] `rev_limit` remains CLR (two-switch Z wiring confirmed)
- [ ] Alert banner: "Axis C (Z) FWD limit — jog REV (↓) to escape"
- [ ] Z+ button disabled, Z− enabled
- [ ] Jog Z− clears the limit and re-enables Z+

### 3.8 Reverse limit switch (Z axis)

- [ ] Drive Z toward the reverse (bottom / home) limit
- [ ] Only `rev_limit` shows HIT; `fwd_limit` stays CLR
- [ ] Alert: "Axis C (Z) REV limit — jog FWD (↑) to escape"

### 3.9 XY limit switch (one switch, both inputs wired)

- [ ] Drive X or Y to either physical travel boundary
- [ ] Both `fwd_limit` AND `rev_limit` show HIT simultaneously for that axis
- [ ] Alert shows the correct escape direction based on last movement direction
- [ ] The escape-direction button is enabled; the opposite button is disabled

### 3.10 Home axis

- [ ] Press **HOME A** — axis moves to home switch
- [ ] `home_switch` LED shows HIT when switch activates
- [ ] Position resets to 0 after homing completes

### 3.11 Settings persistence

- [ ] Change Speed to 2000 in Settings panel, click Apply
- [ ] Close and reopen app; Settings panel shows Speed = 2000 for that axis

### 3.12 Log file verification

After the hardware session, open `galil_log.txt` and confirm:
- [ ] Every move command appears as a `>> PA...` / `>> PR...` / `>> JG...` line
- [ ] Every completed move has a `[X] Motion complete → NNNN.N counts` entry
- [ ] Every limit trip has a `[X] FWD/REV limit active → NNNN.N counts` warning

---

## Regression Quick-Check (run after any code change)

1. `pytest tests/ -v` → 0 failures
2. `python run.py --mock` → app launches without errors
3. Hold X+ jog for 2 s → position advances; release → stops
4. Press HALT → servo stays ON; press E-STOP → servo goes OFF
5. Check `galil_log.txt` for motion-complete entries
