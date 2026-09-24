# Galil Controller Suite — Project Notes

PyQt5 GUI for a Galil DMC-4133 3-axis stepper/servo controller (Light Injector
setup, axes A/B/C mapped to X/Y/Z). See `README.md` for install/run instructions;
this file is orientation for working in the codebase.

## Architecture

```
GUI panels (PyQt5)          STAGE terms: X/Y/Z, microns, +/- as the operator means
    ↕ signals/slots          <- utils/stage_axes.py converts here, and only here
PollingWorker (QThread, owns cmd_queue)
    · polls, then applies utils/soft_limits.py to the result
    ↕ blocking GCommand calls
GalilController  ←→ gclib ←→ DMC-4133 hardware
   or                        CONTROLLER terms: A/B/C, counts, raw _TP
MockGalilController (simulated motion, no hardware/gclib needed)
```

**The cycle is NOT 100 ms.** `msleep(POLL_MS)` sits at the END of the loop, so a
cycle is 100 ms plus the reads (~16 ms each) — about 165 ms today. That is what
sets how far a jog overshoots a limit, so it is a number worth knowing.

- `PollingWorker` (`ui/polling_worker.py`) is the **only** thread allowed to touch
  `gclib`. UI code queues controller calls via `worker.queue_command(fn, *args)`;
  the worker drains the queue then polls all axes once per cycle and emits
  `status_updated`.
- `GalilController` (`controller/galil_controller.py`) wraps `gclib`. It is a
  pure-Python class with no Qt imports.
- `MockGalilController` (`controller/mock_controller.py`) is a drop-in
  replacement with a trapezoidal-velocity simulation and a documented limit
  switch model — used for `--mock` and for the test suite.
- `controller/data_models.py` holds the Pydantic models shared across the app:
  `AxisStatus`, `ControllerInfo`, `AxisSettings`/`AppSettings`, `PositionSlot`.
- **Two direction conventions, converted at exactly one boundary.** The panel
  works in STAGE terms and the controller in CONTROLLER terms, and Y and Z are
  reversed between them. `utils/stage_axes.py` holds `AXIS_SIGN` and the
  converters; anything that mixes the two without going through it is a bug
  waiting to invert a limit or a jog.

## What changed on 2026-09-24

A long session. Each item has its own section below; this is the index.

| | |
|---|---|
| **Page Up/Down jog Z** | keyboard jogging is no longer arrows-only |
| **Y and Z reversed in software** | the stage's +Y/+Z were the controller's −B/−C. Done at the UI boundary because the absolute encoder's counting direction cannot be flipped to match |
| **Limit switches: `CN 1`, one per axis** | NC switches needed active-high; a trip means "at an end" and the direction that REACHED it says which. `LIMIT_INPUT` declares which inputs exist, because an empty input reads ACTIVE too |
| **Escape moves stopped stopping themselves** | the trip direction is latched on the transition, not re-read from `last_dir` — the bug that made Insert Z run 128 ms and quit |
| **Settings panel (new)** | soft travel limits per axis, in stage microns |
| **Retract Z / Insert Z** | blind moves past the end of the scale tape, with the state remembered across restarts |
| **Z micron origin → 6,991,849** | declared near the tape end; the encoder health work that went with it |
| **Jog overshoot quantified** | ~100 µm at 4000 counts/s, and it is the poll cycle, not the limit code |

Two lessons from it that generalise beyond their sections:

- **A mock override that short-circuits the logic under test is worse than no
  test.** `test_guard_does_not_halt_a_jog_escaping_a_limit` existed, passed, and
  did not catch the escape bug, because it forced the flags with
  `simulate_rev_limit()` instead of driving the axis to the trip point.
- **When a failure looks impossible, suspect the test runner.** pytest is not
  installed; the stand-in runner grew fixture caching, stacked `parametrize` and
  yield-fixtures during this session, each time because tests failed for its
  reasons rather than the code's.

## Directory map

```
run.py                     Main entry point (--mock / --address)
run_smoke_test.py          Minimal window: raw position + DMC command box
.gitignore                 __pycache__, galil_log*.txt, *.log (added 2026-09-23)
Galil_Controller_Suite_Manual.pdf   36-page operator manual - SEE THE CAVEAT BELOW
test_move_microns_hw.py    Hardware check of move_microns() accuracy/repeatability
test_small_step_tuning.py  KS/AC-DC/SP sweep + post-move relaxation profile
set_micron_origin.py       Declare the stage's true zero (micron display origin)
check_link_speed.py        Measure serial throughput and infer the baud rate
check_motor_current.py     Report AG/LC against the motor rating (read-only)
check_limits.py            Live view of every limit input (read-only)
controller/
  galil_controller.py      Real hardware interface
  mock_controller.py       Simulated controller
  data_models.py           Pydantic models (AxisStatus, AppSettings, ...)
ui/
  main_window.py           QMainWindow shell, sidebar nav, HALT/E-STOP bar
  polling_worker.py        100 ms poll loop, command queue
  panels/
    connection_panel.py    Wired into main_window
    jog_panel.py            Wired into main_window
    log_panel.py             Wired into main_window
    settings_panel.py        Soft travel limits (added 2026-09-24)
utils/
  stage_axes.py            AXIS_NAME/AXIS_SIGN and the stage<->controller converters
  soft_limits.py           Software travel bounds, merged into the poll result
  z_retract_state.py       Whether Z is parked off the tape, across restarts
  position_memory.py       Named position slots -> config/positions.json
  zero_offsets.py          Display origins: counts zero + micron origin per axis
  unit_converter.py        counts <-> um helpers
  logger_setup.py           loguru + Qt LogBridge for the log panel
config/
  settings.json             Persisted AppSettings (axis params + globals)
  positions.json             Named position memory slots (absolute _TP)
  zero_offsets.json          counts_zero and um_origin per axis
  last_direction.json        Per-axis last commanded direction, written on change
tests/                       pytest suite; test_move_microns.py also drives the
                             REAL controller through a fake gclib device
```

**Deleted 2026-09-23:** `ui/panels/settings_panel.py`, `encoder_error_measure.py`,
`long_encoder_measure.py`. All three are recoverable from git history.

## Known quirks / things to remember

- **`config/last_positions.json` is gone (2026-09-23)**, along with
  `dashboard_panel.py` and `lastPosSaveTest.py` — leftovers from an earlier
  version. `poll_all_axes()` no longer writes anything to disk, and the UI takes
  every position from the `AxisStatus` in the poll signal.
  - The one thing that file did carry was `last_direction`, which seeds the
    module-level `last_dir` — and that is what tells the software WHICH END of
    travel A or B is at, since those two share one switch across both DMC inputs.
    It is now in memory only.
  - It now lives in **`config/last_direction.json`**, written only when a move
    changes it rather than every poll cycle. `LAST_DIR_ON_START = +1` seeds it
    when that file does not exist — a one-time bootstrap so the app is movable
    on a fresh install, replaced by the truth as soon as each axis moves once.
- **`poll_all_axes()` queries `_TP{ax}` plus each axis's ONE limit switch, and
  only where one is wired** — reduced from 9 registers to 2 to keep the 100 ms
  cycle fast. Since 2026-09-24 it reads the switch per `LIMIT_INPUT` and
  resolves which END it means from `last_direction`, instead of reading `_LF`
  and copying it into both flags. Only C has a switch; A and B have none.
  Because of this, `AxisStatus.pos_ref`,
  `.vel`, `.err`, `.torque`, `.stop_code`, `.servo_on`, `.home_switch` stay at
  their Pydantic defaults for the **real** controller — only `pos`, `fwd_limit`,
  `rev_limit`, `last_direction` are live. `MockGalilController` still populates
  every field, so mock mode and dashboard-style displays will look "richer"
  than real hardware.
- **Two different counts-per-micron concepts exist, don't conflate them:**
  - `AppSettings.axis_a/b/c.counts_per_um` (`AxisSettings.counts_per_um`) is a
    *motor* scale factor, used for commanded moves (`MOTOR_COUNTS_TO_MICRONS_XY/Z`
    in `galil_controller.py`, and step-size-to-counts conversion in
    `jog_panel.py`).
  - `AppSettings.encoder_counts_per_um` (global, default `12.8`, added to
    `config/settings.json`) is the *encoder* scale factor, used purely for
    displaying encoder position (`_TP`) in microns — e.g. the "Current
    Positions" µm column and the Z-axis big readout in `jog_panel.py`. It is
    **not editable from any UI** — edit `config/settings.json` directly.
  - `galil_controller.py` also has its own hardcoded `ENCODER_COUNTS_TO_MICRONS_XY
    = 12.8` used in `move_exact`, independent of the new global setting — these
    have not been unified.
- **The encoders are NUMERIK JENA `LAK14-710A-RO`.** Decoded against the ordering
  key in §11.1 of `encoder manual.pdf`: BiSS C unidirectional (`4`), 20 µm
  grating, **78.125 nm measuring step** (`7`), **32-bit serial data word** (`1`),
  position encoded over **1200 mm** (`0`), M2.5 head, 0.3 m cable, 15-pin D-Sub.
  - `encoder_counts_per_um = 12.8` follows directly (1 / 78.125 nm) and is
    **confirmed**, not assumed. Cross-check: 20 µm grating ÷ 78.125 nm = 256,
    the interpolation factor.
  - All three axes carry the **same** head, `LAK14-710`, confirmed off the name
    plates. Same measuring step, same data word, same frame.
- **`BISS_CONFIG = "1,33,0,0<9"`: keep `n3=0`.** In Galil's `SS`, `n1` is the
  TOTAL bit count and `n3` how many are padding — but the LAK's extra 33rd bit is
  the BiSS C **CDS bit, which sits BEFORE the position data** (manual §6.6.2:
  Start, CDS, position MSB→LSB, E, W, CRC). It therefore lands at the MSB end and
  reads 0, so the 33-bit value already equals the 32-bit position and nothing
  needs shifting out. Matches the datasheet's "32 bits, no zero padding".
  - Galil's model is `[position][padding]` with padding **trailing after the
    LSB** — `n1` is "Number of Data bits, including zero padding bits" and `n3`
    is "Number of zero padding bits (Manufacturer specific)". So where padding
    genuinely exists, `n3=0` really would double the value. It just does not
    exist here.
  - **Proof that there is no trailing pad: LSB parity.** A trailing zero pad
    forces every reading even. Across all 38 `_TP` readings recorded at `n3=0`
    (all three axes, every log to 2026-09-17): **18 odd, 20 even** — a coin
    flip, so the LSB is real position data. `n3=1` would shift off a real bit.
  - **`n3` right-shifts the value.** Measured on stationary axes
    (`test_biss_padding.py`): every increment halves the reading, identically on
    all three axes.
  - **CRC cannot pick `n3` for you.** `_SSm0` stays clean at every `n3`, because
    CRC validates the frame, not the interpretation of its bits. Do not treat a
    clean status word as evidence about scale — use the parity check instead.
- **`_TP` is a PRC coordinate, not a stage coordinate — and not a tape
  coordinate either.** The absolute Pseudo Random Code spans the full 1200 mm
  encoding range, and a short tape is a *section cut from that code space*. Axis
  A reads ~3,226,000 counts = **252 mm on a 45 mm tape**, which is legal and
  expected, not an error. Consequences:
  - Nothing anchors `_TP` to the stage. Getting a stage coordinate needs this
    tape's own origin, measured by traversing the travel and recording min/max
    `_TP` per axis. **Nothing records that yet.**
  - Programming a logical zero-position would fix this at the encoder, but §7.6
    of the manual requires the ABSOFLEX software *and* the USB adapter, which we
    do not have. Measuring the origin in software is the available route.
- **Two different lengths per axis, both in `settings.json`:**
  `AxisSettings.travel_mm` (how far the stage may move) and
  `AxisSettings.encoder_length_mm` (physical scale tape, deliberately longer so
  the head never runs off it). A and B are 20 mm of travel on a 45 mm tape; C is
  the long axis, with a 200 mm tape. `travel_mm` is the load-bearing one:
  `enable_axes()` issues `LD=3`, which disables the hardware limit switches
  inside the controller, so software is the only protection against an end stop.
  - **`axis_c.travel_mm` is a placeholder at 20 mm, not C's real travel.** It is
    deliberately conservative — an under-permissive soft limit refuses a move,
    an over-permissive one drives into an end stop. Replace it with the measured
    figure before running C over any distance.
- **All three axes are 6.4 motor counts/µm.** `axis_c` was 12.8 and is now
  corrected. Three independent lines agree:
  1. **The Z stage spec** (`Boomerax MZ40CF40CF40-0200`, quote): Z travel 200 mm,
     **spindle pitch 2 mm**, **helical/screw gear unit 1:1** — the lateral mount
     redirects the drive without reducing it. With the 1.8°/step ST4118D3004-B
     (200 steps/rev) and the drive **fixed at 1/64 microstepping**:
     `200 × 64 × 1 / 2000 µm` = exactly **6.4**.
     - The old 12.8 is what that same drivetrain gives with a **1 mm** pitch. It
       was a pitch assumption, not a measurement.
     - **Microstepping is not configurable.** The controller's `-D4140` is the
       Galil SDM-44140, which the `YA` documentation states is *"always configured
       for 64th microstepping"* / *"Drive fixed at 1/64 step"*. `YA` does not set
       the drive; it only declares the resolution for Stepper Position
       Maintenance (SPM) mode.
     - **`_YA` reads 2 (the default) on all axes — it should be 64.** Harmless
       today because nothing enables `YS`/SPM, but SPM would compare against a
       32×-wrong drive resolution. Fix with `YA{ax}=64` in `enable_axes()`.
     - Running this backwards for X/Y: 6.4 with the same motor and drive implies
       a 2 mm pitch there too, so all three axes are genuinely uniform.
  2. **The encoder measurement** on axis C.
  3. **The A-vs-C invariant**, which depends on nothing else. Comparing **encoder
     counts per motor count** on the two axes, same encoder config on both:

  | axis | commanded | enc counts/step (steps 2–4) | enc per motor count |
  |---|---|---|---|
  | A | `PRA=640` | 3838 / 3 = 1279.3 | **1.999** |
  | C | `PRC=1280` | 7734 / 3 = 2578.0 | **2.014** |

     Identical to 0.75%. The ratio is independent of `n3` (it scales both axes
     equally) and independent of the encoder's nm/count (same head on both), so A
     and C move the same distance per motor count and cannot differ by 2x.
  - Note `Galil/manipulator Z.pdf` §8.1 describes the lateral mount as having a
    **2:1 gear unit**. The unit actually supplied has a **1:1** gear per the
    quote, so the manual's generic option table does not describe this stage —
    do not derive the scale from it.
- **`axis_c.travel_mm = 200.0`** — the real Z travel from the quote, replacing the
  earlier conservative placeholder. Consider whether a margin below the physical
  end stops is wanted, since `LD=3` leaves software as the only protection.
- **Physical orientation: X (A) and Z (C) are HORIZONTAL; Y (B) is VERTICAL.**
  This matters for backlash:
  - **B/Y is vertical**, so gravity constantly preloads the nut against one
    flank. Its lost motion is therefore small and *predictable* — measured ~3 µm,
    the tightest of the three, which is the expected consequence rather than a
    sign it is mechanically better.
  - **C/Z is horizontal**, so gravity does not explain its asymmetric take-up
    (8.9 µm out vs 16.9 µm back, 12.5 µm return error). A more likely cause on
    this stage is the bellows: the Boomerax is a bellows-sealed vacuum
    manipulator, so under vacuum atmospheric pressure applies a large constant
    axial load, preloading the drivetrain one way. Ferrovac sells spring-force
    supports (`MZS…`) specifically to counteract it. Unconfirmed.
  - For repeatable positioning on any axis, approach from a consistent direction.
    That converts lost motion into a fixed offset instead of a random one.
- `controller/galil_controller.py` docstring lists motion command it labels a
  "stepper-motor build," but methods mix closed-loop encoder-corrected moves
  (`move_relative_closed_loop`, `move_exact`, step-by-step correction via
  `determine_motion_step`/`determine_settle_speed`) with direct Galil motion
  commands (`move_relative`, `move_absolute`, `jog`).
- `MockGalilController` limit switch model (revised 2026-09-24): **every** axis
  has ONE switch, tripped at either end, and `last_direction` decides which flag
  rises — never both. C was previously modelled with two independent switches;
  it never had two.
- `galil_log.txt` and `__pycache__` are now in `.gitignore` and untracked
  (2026-09-23). 40 `.pyc` files had been committed and showed as modified on
  every run; the log had produced 10k-line diffs. Both are still on disk.

## Quick reference (state after 2026-09-22)

### Motion API — use these, not the older ones

| function | what it does | notes |
|---|---|---|
| `move_microns(axis, microns, ...)` | closed-loop RELATIVE move | snaps to the microstep grid by default |
| `move_to_counts(axis, target_counts, ...)` | closed-loop ABSOLUTE move to a `_TP` reading | no snapping — the target is a place |
| `move_all_absolute({axis: counts}, ...)` | several axes to absolute readings, sequentially | used by position-memory GO TO |
| `jog(axis, speed)` | continuous jog at counts/s until halted (`JG`+`BG`) | arrow keys and hold-to-jog |
| `is_jogging(axis)` | direction of an active jog, or 0 | how the poll loop knows to guard it |
| `halt_axis(axis)` | `ST`, and clears the jog flag | |
| `_wait_until_still(axis, window_ms)` | block until `_TP` stops changing | motion complete ≠ stopped; see below |
| `_wait_motion(axis, watch_limit)` | wait out one step, stopping on a limit trip | `LD=3` means nothing in hardware stops the axis |
| `_read_motion_state(axis)` | `(moving, limit_active)` | one operand per command, deliberately |
| `_read_float(cmd)` / `_parse_reply()` | one number, or a named desync error | catches a shifted link |

Older calls that still exist, unused by the UI: `move_relative()`,
`move_absolute()`, `define_zero()`, `home_axis()`.

**Deleted 2026-09-22**, all of it carrying the stale `*1.25` encoder factor:
`move_relative_closed_loop()`, `determine_motion_step()`,
`determine_settle_speed()`, `move_exact()`, `move_opposite_direction()` and
`move_if_at_limit()`, plus `forward_compensation`, `MOTOR_BACKLASH` and the
`MOTOR_COUNTS_TO_MICRONS_XY/_Z` pair (which read `axis_a`/`axis_c` and ignored
`axis_b`). `move_exact()` had no callers at all; the rest existed to serve
`move_relative_closed_loop()`.

That factor was 12.8/10.24 from an older motor scale, against a measured
encoder-per-motor-count ratio of 2.0 — it under-targeted by 1.6x, so a 10 µm
request moved 6.25 µm.

`encoder_error_measure.py` and `long_encoder_measure.py` called the deleted
methods and were **removed on 2026-09-23**. Both are recoverable from git
history if anything in them is worth salvaging.

### Movement parameters and where they live

| parameter | value | set in | why |
|---|---|---|---|
| `KS` (smoothing) | **2** | `KS_SMOOTHING`, applied by `enable_axes()` | was 16, the maximum; 16 left 1.41 µm arriving after motion complete, 2 leaves 0.156 µm |
| `SP` / `AC` / `DC` | from `settings.json` | `enable_axes()` | **nothing applied these before 2026-09-22** — the controller ran on power-on defaults (SP=25000, AC=DC=256000) |
| `FINE_SPEED` | 500 counts/s | `galil_controller.py` | final-approach speed; measured tightest spread and smallest tail |
| `FINE_BELOW_UM` | 5 µm | `galil_controller.py` | error below this switches to the fine speed |
| `gain` | 0.9 | `move_microns` default | undershoots on purpose so the approach never reverses into backlash |
| `tol_counts` | 1 encoder count | `move_microns` default | 0.078 µm |
| `settle_window_ms` | 200 default / **60 for jogging** | `JOG_SETTLE_WINDOW_MS` | speed/accuracy dial; a move costs 3–4 windows |
| `YA` | 64 | `enable_axes()` | declares the drive's fixed 1/64 microstepping for SPM |
| `LD` | 3 | `enable_axes()` | hardware limit switching OFF — software is the only protection |

### Config files

| file | holds | written by |
|---|---|---|
| `settings.json` | `AppSettings`: per-axis speed/accel/decel, `counts_per_um`, `travel_mm`, `encoder_length_mm`, `soft_limit_neg_um`/`soft_limit_pos_um`; globals `encoder_counts_per_um`, `serial_baud`, `last_address` | connection panel on a successful connect; `closeEvent`; the Settings panel's SAVE (soft limits ONLY). **Everything else is hand-edited** |
| `zero_offsets.json` | `counts_zero` and `um_origin` per axis | ZERO buttons / `set_micron_origin.py` |
| `positions.json` | named position slots, absolute `_TP` per axis | position memory SAVE |
| `last_direction.json` | per-axis last commanded direction | on change, not per poll |
| `z_retract_state.json` | whether the Z head is parked off the scale tape | `_set_rz_state()`, on every transition |

Absent on a fresh install; `LAST_DIR_ON_START = +1` seeds it until the first move
on each axis records the truth.

Deleted 2026-09-23: `last_positions.json`. The position half went with it; the
direction half moved to its own file, because it is the only thing that says
which end of travel an axis is on.

### Scale constants, and not conflating them

| constant | value | meaning |
|---|---|---|
| `MOTOR_COUNTS_PER_UM[axis]` | 6.4 | microsteps per µm — what `PR` is denominated in |
| `ENCODER_COUNTS_PER_UM` | 12.8 | encoder counts per µm — what `_TP` is denominated in |
| ratio | 2.0 | encoder counts per microstep; measured 1.9738 and 1.9567 |
| one microstep | 0.15625 µm | the position quantum — nothing lands between these |
| one encoder count | 0.078125 µm | the measurement resolution |

A 10 µm move is `PR=64` microsteps commanded and **128 counts** on the display.

## `move_microns()` — closed-loop micron moves (added 2026-09-17)

`GalilController.move_microns(axis, microns, ...)` in `controller/galil_controller.py`,
mirrored in `MockGalilController`. Move an axis a signed distance in microns,
closing the loop on the encoder.

```python
ctrl.move_microns("C", 50.0)             # 50 µm forward, returns µm actually travelled
ctrl.move_microns("A", -10.0, speed=2000)
```

- Target is set **once, in encoder counts**: `_TP + microns × ENCODER_COUNTS_PER_UM`.
  Then it steps: read `_TP`, convert the remaining error to motor counts with that
  axis's own `counts_per_um`, command `gain` (0.9) of it, wait, measure again.
  Nothing is precomputed, so dropped steps and rounding get corrected instead of
  accumulating.
- New per-axis tables at module level, built from `settings.json`:
  `MOTOR_COUNTS_PER_UM`, `AXIS_TRAVEL_MM`, `ENCODER_COUNTS_PER_UM`. Prefer these
  over the older `MOTOR_COUNTS_TO_MICRONS_XY/_Z` pair, which ignores `axis_b`.
- **`gain < 1` undershoots on purpose** so the approach stays unidirectional. On
  overshoot the loop **stops and logs it rather than reversing** — a reversal pays
  lost motion before the encoder moves at all, and it would leave the final
  position's backlash state unpredictable. Re-approaching from one side is
  `move_opposite_direction()`'s job.
- Four exits, all logged: inside `tol_counts` · next step rounds to 0 motor counts
  (resolution floor) · 3 steps with no progress (stiction) · `max_steps`. A limit
  tripping mid-move also breaks out.
- Guards before moving: unknown axis, a single move longer than `travel_mm`
  (`LD=3` means nothing in hardware would catch it), and driving further into an
  already-active limit.

### Resolution floor: one microstep = 2 encoder counts

Motor 6.4 counts/µm vs encoder 12.8 counts/µm, so the smallest step the
SDM-44140 can take moves `_TP` by 2 counts = **0.156 µm**. An error of 1 encoder
count is not correctable, only overshootable. `tol_counts=1` is the useful
default; judge results in absolute µm against 0.156 µm, never as a percentage.
A 1 µm command is 6.4 motor counts → rounds to 6 → lands at 0.9375 µm. Correct.

### Motion complete is NOT stopped — the biggest trap here

Found on the first hardware run (`test_move_microns_c.log`, 2026-09-17):

- **`GMotionComplete` / `AM` returns when the profiler finishes, not when the
  stage stops.** 27–56 encoder counts (2–4.4 µm) were still to arrive on 100 µm
  moves at `SP=2000`, and 376 counts (29 µm) on a 500 µm one. The deficit scales
  with the velocity carried when the profile ended: 0–94 ms of travel, not a
  fixed amount.
- **`WT` does not delay an immediate-mode command.** It is a program-flow
  command; from the host it queues a wait in the program thread and returns at
  once. Proof: `WT=20` averaged **15.2 ms** round trip, identical to `PR` (15.7)
  and `BG` (15.7); a real 20 ms wait would have read ~35 ms. Use `time.sleep()`
  in the host, or condition on the encoder.

Together these made the closed loop *worse than open loop*: every correction was
computed against a stale, too-short reading, so it commanded extra counts and the
rest of the motion landed on top. Every move overshot, up to +3.36 µm on a 100 µm
command.

`_wait_until_still()` is the fix: poll `_TP` until it stops changing, then
measure. **Stillness is judged over a window (`window_ms`, default 200 ms), not
between consecutive reads** — reads are ~16 ms apart, so "two reads within one
count" also passes for a steady crawl of 62 counts/s, and that is exactly what
slipped through the first attempt: every move still gained 3 counts (0.234 µm)
after the check returned. `window_ms` is the speed/accuracy knob; it costs one
window per measurement.

Anything else in this codebase that reads a position straight after commanding
motion has the same bug.

### Axis C results (2026-09-17, second run, before the window fix)

| commanded | mean error | spread (repeatability) |
|---|---|---|
| 1 µm | −0.66 µm | 1.17 µm |
| 10 µm | +0.42 µm | 0.078 µm |
| 50 µm | +0.31 µm | **0.000 µm** |
| 100 µm | +0.39 µm | **0.000 µm** |

- **Repeatability is excellent**: 50 and 100 µm moves repeated to the identical
  count three times running. That is the number that matters for positioning.
- The residual overshoot was **constant with distance** (4–6 counts), which is a
  settling signature, not scale — traced to the creep above and addressed by the
  windowed check. **Not yet re-verified on hardware.**
- **Axis C swallowed the first two 1 µm moves entirely**: 12 motor counts
  commanded, `_TP` did not move one count, then the third move worked. Take-up
  sets the smallest move C can honour from rest — **1 µm from rest is below it.**
- Return error over a 485 µm out-and-back was **0.47 µm**, far better than the
  8.9–16.9 µm recorded elsewhere in these notes. Those older figures may not be
  representative of this axis in this state.
- Drivetrain ratio `Δ_TP / Σ PR counts` read 1.9567 vs 2.0000 expected (−2.2%).
  Partly an artifact: the loop commands extra counts when it overshoots without
  reversing, inflating the denominator. Expect it to approach 2.0 once the
  overshoot is gone; if it does not, it deserves a proper look.

### Small-step tuning, axis C (2026-09-22)

`test_small_step_tuning.py C`, 5 repeats per config, raw `PR`/`BG` (no loop).
Controller was at its power-on defaults: **KS=16, SP=25000, AC=DC=256000** —
`enable_axes()` sets KS but never SP/AC/DC, so `settings.json`'s motion
parameters have never reached the hardware. Every earlier test ran at
AC=DC=256000.

KS sweep, at SP=25000 AC=DC=256000. `tail` is how much arrived AFTER motion
complete; `ratio` is delivered/expected:

| KS | 1 µm err | 1 µm spread | 10 µm err | 10 µm spread | 10 µm ratio | **10 µm tail** |
|---|---|---|---|---|---|---|
| 0.5 | −0.031 | 0.313 | −0.359 | 2.969 | 0.964 | **+0.031** |
| 1 | −0.297 | 0.703 | +0.203 | 3.828 | 1.020 | **+0.094** |
| 2 | −0.109 | 0.313 | −0.063 | 2.344 | 0.994 | **+0.156** |
| 4 | −0.047 | 0.313 | −0.328 | 2.344 | 0.967 | **+0.156** |
| 8 | −0.109 | 0.078 | −0.047 | 1.875 | 0.995 | **+0.281** |
| 16 | −0.234 | 0.313 | +0.016 | 2.500 | 1.002 | **+1.406** |

**The tail column is monotonic in KS and is the one clean signal in the run:
KS=16 leaves 1.41 µm still arriving after motion completes, KS=0.5 leaves
0.03 µm — a 45x difference.** That is the mechanism behind the overshoot
documented above, and it argues for KS **low** (2 is the Galil default), not
for the KS=8 the script's ranking picked.

Everything else was inside the noise at n=5:
- **AC/DC**: tail at 1 µm rises gently with AC (0.44 µm at 2000 → 0.72 at
  500000) but the 10 µm column is not monotonic. Weak effect.
- **SP** (10 µm only — a 6-count move never reaches SP): SP=500 gave the
  tightest spread (0.70 µm vs 1.0–3.7) and the smallest tail. Plausible, one
  sample.

**Open-loop repeatability is poor at every setting**: spread on a 10 µm raw step
was 1.9–3.8 µm regardless of parameters. The closed loop is what produced the
0.000 µm repeatability recorded on 2026-09-17, so `move_microns()` is not
optional for positioning — the underlying mechanics are that noisy.

Response thresholds: **stiction 1–2 microsteps (0.16–0.31 µm)** to break free
even when already loaded in that direction, and the encoder then jumps only
1 count (0.078 µm) — half a microstep.

**Take-up on a direction reversal is 10.78 µm** (69 microsteps), measured
2026-09-22 once the detector was direction-checked. The earlier 0.625 µm figure
was the `abs()` bug firing on creep in the wrong direction. This **confirms the
8.9–16.9 µm** in the older notes rather than contradicting it.

The 0.47 µm return error measured on 2026-09-17 is not in conflict: that run
returned through `move_microns()`, and a loop that closes on an absolute encoder
target absorbs take-up by commanding extra counts until the encoder arrives. So
backlash costs time and counts, never final position — as long as the closed
loop is used. Open-loop moves pay it in full.

Closed-loop check at KS=8/SP=500/AC=20000, through `move_microns()`:

| commanded | mean moved | error | spread | vs floor |
|---|---|---|---|---|
| 1 µm | +1.094 | +0.094 | 0.156 | 0.6 microsteps above |
| 2 µm | +2.313 | +0.313 | 0.391 | 2.0 microsteps above |
| 5 µm | +5.094 | +0.094 | 0.234 | 0.6 microsteps above |
| 10 µm | +10.172 | +0.172 | 0.156 | 1.1 microsteps above |

Everything overshoots by about one microstep, consistently. With
`tol_counts=1` and `gain=0.9` the loop should undershoot, so the extra counts
arrive after its final measurement: **a slow post-move relaxation that the
settle criterion cannot see.** One count per 200 ms window passes as "still",
which is 0.4 µm/s — and roughly 1 s elapses between the loop's last check and
the confirming read. The encoder is quiet to 0 counts over 1 s when nothing has
been commanded, so this is relaxation after motion, not drift. The bellows on
this vacuum manipulator are the obvious suspect.

**Re-run 2026-09-22 16:07, corrected scoring, at the live KS=2 / AC=DC=19456
baseline.** This was the first sweep ever taken at the configured accel rather
than the 256000 power-on default, and with the step-loss penalty no longer
firing on 6-count moves.

- **AC/DC: no change needed.** The 1 µm tail is minimised at **19456–20000**,
  which is where `settings.json` already puts it — 0.156 µm there against
  0.34–0.41 µm at the extremes. The sweep picked 19456, the value in use.
- **KS: still monotonic in the tail**, now over a wider range —
  0.22 / 0.25 / 0.27 / 0.63 / 1.14 / **3.23** µm for KS 0.5 / 1 / 2 / 4 / 8 / 16
  on a 10 µm step. It picked KS=1, but 1 vs 2 is 0.250 vs 0.266 µm, inside the
  noise. Left at 2.
- **SP: inconclusive.** Open-loop spreads of 1.5–5 µm on a 10 µm step swamp any
  speed effect.
- Take-up on a reversal read **6.875 µm** this time against 10.78 µm earlier —
  variable, but both inside the 7–11 µm band the older notes record. Stiction
  1–2 microsteps.

**Scoring caveat on that run.** The ranking's picks (KS=8, AC/DC=20000, SP=500)
are weakly supported. The step-loss penalty fired whenever `ratio < 0.97`, but
on a 6-count step stiction alone puts the ratio at 0.68–0.97, so nearly every
config was penalised and AC=20000 "won" only by over-delivering (ratio 1.133)
while having the **worst** spread in its sweep (1.25 µm). The penalty now only
applies where ≥50 counts were commanded, and the tail is scored directly.

### Motion parameter changes (2026-09-22, confirmed in effect on hardware)

- **`KS` 16 -> 2** (`KS_SMOOTHING` in `galil_controller.py`). 16 was the maximum
  smoothing Galil allows and left 1.41 um still arriving after motion complete;
  2 is Galil's default and leaves 0.156 um, one microstep, with no step loss.
- **`enable_axes()` now applies `SP`/`AC`/`DC` from `settings.json`**
  (`AXIS_SPEED`/`AXIS_ACCEL`/`AXIS_DECEL`). It never did before, so the
  controller ran on power-on defaults and every measurement before this date was
  taken at AC=DC=256000, not the 20000 in the config file.
- **`move_microns()` stages its speed**: the configured axis speed for the bulk
  of the move, dropping to `FINE_SPEED` (500 counts/s) once the remaining error
  is inside `FINE_BELOW_UM` (5 um). SP=500 measured best for both spread and
  tail, but it cannot be the global speed - 200 mm of C travel at 500 counts/s
  is 43 minutes. Costs one extra `SP` command per move.

All three were live for the 2026-09-22 16:07 tuning run, which read
`KS=2.0000 SP=4000.0000 AC=19456.0000 DC=19456.0000` straight back off the
controller — the first run ever at the configured accel rather than the 256000
power-on default.

Sub-micron was already achieved before them: residuals were 0.094-0.313 um. What
remained was the microstep floor at 0.078-0.156 um, and the obstacle turned out
to be the post-move relaxation - see below.

### GUI wiring (2026-09-22)

Every motion the jog panel can start is closed loop. Microns go straight through
— no `um_to_counts` at the call site — plus `settle_window_ms` and, for the
D-pad, the `button_state_callback` that greys the button out for the duration.

| control | calls | sign |
|---|---|---|
| Retract / Insert Z | `move_to_counts(C, um_origin)` then `jog(C, ±500)` | flipped; see its own section |
| D-pad click (X/Y/Z, ±) | `move_microns(axis, ±step_um)` | flipped on B/C since 2026-09-24 |
| D-pad hold | `jog(axis, ±speed)`, release → `halt_axis` | flipped on B/C |
| HOME X/Y/Z | `move_to_counts(axis, counts_zero)` | none — a place, not a direction |
| position memory GO TO | `move_all_absolute({axis: saved _TP})` | none — a place |
| ZERO X/Y/Z | nothing — display offset only | none — stores raw `_TP` |

This replaced `move_relative_closed_loop()` at every site, **fixing the 1.6x
under-travel** its stale `*1.25` factor caused on every jog step. Nothing in the
UI calls `move_relative()`, `move_absolute()` or `move_relative_closed_loop()`
any more — see the jog panel cleanup section for what was removed to get there.

`JOG_SETTLE_WINDOW_MS = 60` (against the 200 ms default) is the speed/accuracy
dial: measured on the fake device, a 10 µm move costs **1065 ms at a 200 ms
window and 363 ms at 60 ms**, in the same three steps. Interactive moves should
not pay for a long settle, because waiting does not fix the error anyway — the
relaxation runs for seconds.

The panel imports that constant from `galil_controller` inside a `try/except`:
`run.py` imports that module only in the non-mock branch and it imports `gclib`
at the top, so an unguarded import would break `--mock` where gclib is absent.

**Caveat:** `move_microns()` blocks, and the jog panel queues onto
`PollingWorker` — the same thread that polls. The position readout freezes for
the duration of a move (~0.3-0.4 s per jog click). Fixing that properly means
letting the worker poll between loop steps, which is a change to the threading
model.

### Motor current and heating — settled, no action (2026-09-23)

The motors run hot to the touch when idle. Investigated, and **nothing is
wrong**:

| setting | value | meaning |
|---|---|---|
| `AG` | 1 | **1.0 A/phase** |
| motor rating | — | 1.2 A/phase (ST4118D3004-B) |
| ratio | 0.83× | within rating, with margin |
| `LC` | 0 | 25% holding after a move → **0.25 A at rest** |

**Hot-at-standstill is normal stepper behaviour.** A stepper holds position by
pushing continuous current through its windings with no back-EMF and no
airflow, so standstill is its worst thermal case, not its best. Datasheets
typically permit 80 °C case temperature, which is far too hot to keep a hand on.

Nothing in the system can reassure you about this: per manual A10, the
SDM-44140 monitors **over-current and under-voltage only**. There is no motor
temperature sensing anywhere, so a clean status says nothing about heat.

`check_motor_current.py` reports all of the above. Read-only, nothing moves.

**AG is amps per phase, not a resolution gain** — 0.5 / 1.0 / 2.0 / 3.0 A for
n=0..3 on the D4140. The idea of sweeping all four to see whether it changes
resolution does not work on two counts: microstepping is fixed at 1/64 so the
0.15625 µm quantum is untouchable, and **AG=2 and AG=3 would overdrive a 1.2 A
motor by 1.7× and 2.5×** — 2.8× and 6.3× the rated heating. Only n=0 and n=1 are
usable on this rig, and n=1 is already the better of the two.

`LC = -1` (always 100% holding current) was considered as a way to attack the
post-move relaxation, on the theory that the relaxation is the drivetrain
creeping once holding torque drops to 25%. **Rejected**: it would quadruple idle
heating on motors that are already at temperature, to chase 0.5 µm. The theory
remains untested and is probably not worth the heat.

`enable_axes()` does not set `AG` or `LC`, so both are whatever the controller's
EEPROM holds — the same unmanaged-parameter situation that had `SP`/`AC`/`DC`
sitting at factory defaults. Here the values happen to be right, so nothing was
changed, but they could drift if the controller is reset or swapped.

### Y and Z are reversed in software (2026-09-24)

The manipulator's physical **+Y and +Z are the controller's −B and −C**: pressing
Y+ or Z+ drove the stage the way the operator calls *down*/*back*. X (A) is
correct and is not touched.

`AXIS_SIGN = {"A": +1, "B": -1, "C": -1}` in **`utils/stage_axes.py`** (it was in
`ui/panels/jog_panel.py` until 2026-09-24), with
`to_controller(axis, v)` and `to_stage(axis, v)` around it — the same multiply
both ways, two names so the call site says which direction the conversion goes.

**The line is drawn at the panel.** Everything the operator sees or presses is in
STAGE terms; everything handed to the controller is converted at the boundary:

| stays CONTROLLER (a place, no sign to flip) | converted to STAGE (a direction) |
|---|---|
| `_TP`, and every log line | the D-pad step clicks (`move_microns`) |
| `zero_offsets.json` — both origins | the D-pad hold jogs and the key jogs (`jog`) |
| `positions.json` — absolute `_TP` per slot | the enc-counts and µm columns |
| `move_to_counts` / `move_all_absolute` — HOME and GO TO **needed no change** | the saved-position list |
| `_LF`, `last_direction`, `_guard_jog_limits()` | `_direction_blocked()`, converted once at its top |

Two sites are easy to miss, and both are covered by tests:

- **`_on_jog_button_state_changed()` arrives in CONTROLLER terms.**
  `move_microns()` derives the `"+"`/`"-"` it reports from the sign of the
  microns it was *given*, so a stage +Y click reports `-`. Unconverted, it greys
  out the Y− button while Y+ is the one that is busy.
- **The limit alert names a button.** It used to print the controller's FWD/REV,
  which on the reversed axes is now the *opposite* button to the one that
  escapes. It is built from `_direction_blocked()` instead — the same rule that
  greys the buttons out — so the hint cannot point at a disabled button.

**Why not in hardware, or with `MT=2.5`.** Both reverse the motor and leave the
encoder alone, and the loop is closed on the encoder: `_move_microns()` computes
the error in encoder counts and commands `PR = error / 2.0 × gain`, which assumes
positive `PR` raises `_TP`. Flip only the motor and every correction drives
*away* from the target — positive feedback, on axes where `LD=3` makes software
the only limit protection. It would run until the no-progress or `max_steps`
guard fired.

And the encoder cannot be flipped back to match:

- **`CE`** reverses quadrature and pulse-and-direction encoders. Position here
  arrives as a BiSS serial frame via `SS`.
- **`SS` has no direction argument at all** — its parameters are port, bit count,
  padding and clock divider; the sign of `n1` selects absolute vs continuous
  rollover mode, not direction. Checked against `Galil/gdk_commands.txt`.
- Reprogramming the LAK's own counting direction needs ABSOFLEX plus the USB
  adapter, which this rig does not have (encoder manual §7.6).

So a hardware reversal would have forced a software change anyway, and a deeper
one — inside the closed loop rather than at the UI boundary.

**Consequence when reading logs against the screen.** The GUI's Y and Z columns
are now the *negative* of raw `_TP`, on top of already being offsets from a zero
rather than absolute. `test_move_microns_hw.py`, `test_small_step_tuning.py` and
`set_micron_origin.py` talk to the controller directly and stay in controller
terms. Both column headers carry a tooltip saying so.

### Keyboard jogging (arrows 2026-09-23, Page Up/Down for Z 2026-09-24)

Hold a key, the axis runs; let go, it stops. **Right/Left drive X (A), Up/Down
drive Y (B), Page Up/Page Down drive Z (C)**, at the **Jog Speed slider's**
setting. The slider governs only this — the step buttons keep the configured
axis speed (`AXIS_SPEED`, 4000 counts/s, dropping to `FINE_SPEED` for the final
approach).

Z is on the page keys because the arrows are a two-axis pad and Z is the one
that moves out of that plane. The map is `JogPanel.JOG_KEYS` (renamed from
`ARROW_AXIS`, which stopped being true) and `JOG_KEY_NAME` names the same six
bindings in the jog-button tooltips, so the panel cannot advertise a key that
drives nothing. Nothing else changed: **Z jogging goes through exactly the same
`_start_key_jog`/`_stop_key_jog` path**, so it inherits the limit refusal, the
auto-repeat filtering, the stop-on-deactivate and the `clear_queue()` before the
halt.

Implemented as an **application-wide event filter** on `JogPanel`, not
`keyPressEvent`, because the panel rarely holds focus — the user has just
clicked a button or a list. The filter is deliberately narrow:

- it does nothing unless the panel is visible and its window active;
- it stands aside when focus is in a widget that genuinely needs these keys
  (`QAbstractSpinBox`, `QAbstractItemView`, `QLineEdit`), otherwise it would
  hijack the step spin box and the saved-position list. One rule covers both
  sets, because a spin box takes Page Up/Page Down (a 10x step) and a list view
  pages with them just as they take the arrows;
- **`QSlider` is deliberately NOT in that list.** The speed slider is
  `Qt.NoFocus` so the arrows stay with jogging. Both together, because either
  alone reproduces the bug reported on 2026-09-23: adjust the speed, press an
  arrow, and the *slider* moved instead of the stage — it had kept keyboard
  focus after being dragged, and the filter was standing aside for sliders. Any
  future slider given focus here will swallow the arrows again;
- the step spin box hands the arrows back on `editingFinished`, rather than
  holding them for as long as it stays focused;
- it drops **auto-repeat** events, which otherwise fire press/release
  continuously while a key is held and would restart the jog dozens of times;
- it consumes the key, so it does not also move focus between buttons or
  scroll the panel;
- it stops every key jog on window deactivation — otherwise alt-tabbing away
  with a key held leaves the axis running with no key left to lift.

Releasing a key calls `clear_queue()` before queueing the halt, so the stop does
not wait behind moves already queued.

**`jog()` was rewritten to do what its name says.** It now issues Galil `JG` +
`BG` — constant speed, no target, stopped by `ST`. It previously took MICRONS
and did a `PR` move, while the jog panel had always passed it the speed slider's
value. So holding a jog button asked for a move of that many microns: **4 mm at
the default 4000, up to 50 mm at the slider's maximum, on a 20 mm axis**, on a
path with no travel guard. It also set `KS=16`, undoing the KS=2 tuning, and
switched on Stepper Position Maintenance (`YS=1`) that nothing switched off. The
mock had always used the speed interpretation, so the two disagreed and only the
mock was right.

**The jog safety guard matters more than the keys.** A jog has no target and
`LD=3` disables the controller's own limit action, so
`PollingWorker._guard_jog_limits()` is the only thing between a held jog key and
an end stop. It halts any axis whose limit is active in the jog direction, one
rule for all three. It carried an A/B special case until 2026-09-24 that forced
a halt whenever either flag showed; with one switch per axis and the end already
resolved by `last_direction`, that would now halt an ESCAPE jog.

Reaction time is one poll cycle, and **the cycle is longer than `POLL_MS`**:
`msleep(100)` sits at the END of the loop, so a cycle is 100 ms plus the reads
(3 × `_TP` + one per wired limit input, ~16 ms each) — about **165 ms** today.

**Measured ~100 µm of overtravel at 4000 counts/s** (user, 2026-09-24), which is
what that cycle predicts: `JG` is in motor steps/s, so 4000 / 6.4 = 625 µm/s,
× 0.165 s ≈ 102 µm. An earlier figure of "60 µm" in these notes assumed the
cycle WAS 100 ms and ignored the read time — it understated the real overshoot by
about 1.6×. At the slider's 50000 maximum it is ~1.3 mm, not 780 µm.

That is an argument for a modest jog speed, not for removing the guard. See
"Jog overshoot" under the soft limits section for why this is not fixable by
tuning and what the real options are. `jog()` also refuses to start into
an already-active limit.

### One limit switch per axis, and which end it means (2026-09-24)

**Every axis has exactly ONE switch**, tripped mechanically at EITHER end of
travel. The signal says "this axis is at an end" and nothing more — there is no
second switch, and no per-end input to read. **The last commanded direction is
what resolves it**: drive − into the switch and you are at the reverse end,
drive + into it and you are at the forward end.

This was always true of A and B (documented as "one switch across both DMC
inputs"); C turned out to be the same, and the logic is now uniform across all
three instead of a special case.

```python
LIMIT_INPUT = {
    "B": "FWD",          # NC switch on B's forward DMC input, wired 2026-09-24
    "C": "FWD",          # NC switch on C's forward DMC input, wired 2026-09-24
}
```

`LIMIT_INPUT` maps an axis to the DMC input its one switch happens to be wired
to. That is a **wiring detail only** — it says WHERE TO READ the signal, never
which end of travel it means. An axis absent from the map has no switch: it is
reported clear without being read, which under `CN=1` is the only way to tell an
empty input from a tripped one, since both read ACTIVE.

> **Add an entry the moment a switch is physically connected.** An unlisted axis
> blocks nothing, and `LD=3` means the controller takes no limit action of its
> own either — so it has NO end-stop protection beyond the `travel_mm` guard in
> `_move_microns()`. As of 2026-09-24 **X is in exactly that state.**

Both declared switches sit at the **stage's minus end** (−Y and −Z), because B
and C are reversed in software: controller +B/+C is stage −Y/−Z.

**B was declared FWD to match C rather than measured.** The controller was
powered off when it was added (COM4 did not enumerate) and the user confirmed the
same wiring. If that is wrong, Y reads permanently at a limit — run
`check_limits.py`, which prints a MISMATCH row naming the correct terminal.

#### Polarity: `CN 1`, because the switches are normally closed

`enable_axes()` sends `CN 1` — limit inputs active HIGH. Nothing ever set `CN`
before, so it sat at the controller's `CN -1` default and every NC reading came
out inverted: the newly wired Z switch read ACTIVE while unpressed and went
clear when pressed. That is what started this.

Active high is what NC needs. At rest an NC switch grounds its input and reads
clear; pressing it opens the circuit and reads ACTIVE — **and so does a broken
wire.** Failing safe on a broken wire is the entire point of NC, and it only
works at this polarity.

**`CN` is GLOBAL** — one setting for all six inputs on all three axes, no
per-axis form. Measured on the rig before committing to it:

| input | `CN -1` (was) | `CN 1` (now) |
|---|---|---|
| A fwd / rev | clear, clear | **ACTIVE, ACTIVE** |
| B fwd / rev | clear, clear | **ACTIVE, ACTIVE** |
| C fwd | **ACTIVE** (wrong) | clear ✓ |
| C rev | clear | **ACTIVE** |

Which is precisely why `LIMIT_INPUT` has to exist: flipping `CN` alone would
have pinned five of the six inputs at a permanent limit and made X and Y
unmovable.

#### Two functions, and why they are not the same question

| | |
|---|---|
| `limit_tripped(axis)` | is the switch pressed? The RAW signal, says nothing about which end. An axis with no switch answers False unread |
| `limit_active(axis, direction)` | is the end stop at `direction` active? `limit_tripped()` **and** `direction ==` the LATCHED trip direction. Since one switch means "at an end", this is also exactly "would moving that way drive further in" |

`limit_active()` is THE rule: the poll and all four motion guards
(`jog()`, `_move_microns()`'s pre-move guard, `move_relative()`,
`move_absolute()`) ask it. The opposite direction always answers False, which is
what lets the axis drive back off the switch.

`limit_blocks()` existed briefly on 2026-09-24 and is gone — once every axis has
one switch it computed exactly what `limit_active()` does.

#### The trip direction is LATCHED, not re-read (fixed 2026-09-24)

`limit_tripped()` records which direction was being travelled **on the
clear→pressed transition**, and holds it until the switch opens again
(`_limit_trip_dir`). `limit_active()` compares against that, not against
`_last_dir`.

**Why: `_last_dir` is the LAST COMMANDED direction, and a move that drives OFF
the switch sets it the moment it starts.** Reading it afterwards re-labels the
still-pressed switch as belonging to the direction now being travelled, so
`_guard_jog_limits()` halts the escape on the very switch it is escaping.

Observed on the rig, Insert Z after a retract:

```
15:32:30  [C] FWD limit active -> 8581970 counts     after the retract
15:39:40  Jog C at -500 counts/s                     insert starts
15:39:40  [C] limit reached while jogging - halting   128 ms later
15:39:40  [C] REV limit active -> 8581970 counts      SAME reading, now REV
```

This affected **every** escape jog, not just Insert Z; the retract only made it
easy to hit, because it is the one operation that parks the axis on a switch and
then immediately drives off it.

**The test suite missed it**, and that is the more useful lesson.
`test_guard_does_not_halt_a_jog_escaping_a_limit` existed and passed against the
broken code, because it reached the limit with `simulate_rev_limit()` — an
override that sets the flags directly and so never runs the rule that decides
WHICH end a switch is. It now drives the axis to the trip point instead, and
fails against the old behaviour. **A mock override that short-circuits the logic
under test is worse than no test.**

**The mid-move watch deliberately uses the RAW state.** `_read_motion_state()`
and `_wait_motion()` call `limit_tripped()`, not `limit_active()`, and
`_wait_motion` arms only on a clear→pressed transition. This is not an
oversight, it is the trap:

> `_move_microns()` rewrites `last_dir` as it steps. An escape move starts with
> `direction != last_dir` (so the pre-move guard lets it through), but the first
> loop iteration sets `last_dir = direction` — at which point a
> `limit_active()`-based watch would match, fire `ST`, and abort the move on the
> very switch it was driving off. `test_an_escape_move_is_not_aborted_by_its_own_switch`
> is the regression guard.

#### One rule, in one place

`poll_all_axes()` hands over **at most one** of `fwd_limit`/`rev_limit`, already
resolved. The jog panel's `_direction_blocked()` therefore reads the same way for
all three axes:

```python
return (s.fwd_limit and sign > 0) or (s.rev_limit and sign < 0)
```

It used to carry a separate A/B branch doing the `last_direction` arithmetic
itself, so the rule lived in two places and could drift. It no longer does.

**`_LR` is read now** where a switch is wired to that terminal. `poll_all_axes()`
used to do `lr = lf`, a literal copy, which said nothing about which end.

#### Cost and tooling

Only axes with a switch are read, so the poll is **3×`_TP` + 1 limit read = 4
reads**, fewer than the 6 before. All three wired it becomes 6 reads, still
inside the cycle — the earlier worry about 9 reads was against the abandoned
per-end model, which needed two reads per axis.

`PollingWorker._guard_jog_limits()` lost its own A/B special case the same day,
for the same reason — it forced a halt whenever either flag showed, which with
resolved flags would stop an escape jog dead on the switch it is driving off.

**`check_limits.py`** is the live view: one row per axis showing both raw
inputs, what `LIMIT_INPUT` declares, what the wiring **looks like**, the last
direction, and which stage end the software concludes it is at. Read-only, and
it closes with `GClose` rather than `disconnect()`.

It can work out where a switch is wired from the same asymmetry that makes
`LIMIT_INPUT` necessary: at rest the input reading CLEAR is the one with the NC
switch on it, while the open one reads ACTIVE. So it names the terminal to
declare for an undeclared axis, and prints **MISMATCH** when a declaration
disagrees with the wiring — which matters, because naming the wrong terminal
makes an axis read permanently limited at a position it never reached. It also
flags `_CN0 != 1`, which inverts every reading including that detection.

**The mock models the same thing** — one switch per axis, flag chosen by
`last_direction`, never both. `simulate_fwd_limit()`/`simulate_rev_limit()` force
the switch pressed AND declare the end, because with one switch a caller cannot
say "forward limit" without also saying which way the axis was travelling.

### Limit handling (reworked 2026-09-22)

**`move_if_at_limit()` is gone.** It was the open-loop escape path that
`move_relative_closed_loop()` needed; `move_microns()` handles both cases itself.

**Stopping mid-move.** `LD=3` disables the controller's own limit action, so
nothing in hardware stops the axis, and `GMotionComplete` would block until a
bulk step of several hundred microns had finished. `move_microns()` therefore
polls `_BG` and `_LF` **while the axis is moving** (`_wait_motion`) and issues
`ST` the moment the switch trips. Reaction distance is one poll, ~16 ms: about
2.5 µm at the 500 counts/s fine speed, ~20 µm at 4000.

`_read_motion_state()` fetches both operands in ONE round trip
(`MG _BGx, _LFx`), because during a move every round trip is travel. It falls
back to two reads if the controller rejects the combined form.

**Escape moves.** The watch arms only on a clear->active *transition*. A move
that starts with the switch already active is an escape move — the level stays
active until the axis drives off it, so aborting on the level would make
escaping impossible and strand the axis on the switch. The pre-move guard has
already established that such a move runs away from the limit.

**In the jog panel**, `_direction_blocked()` is the single rule, used both by
the status update and by the post-move re-enable so they cannot disagree:

- Every axis has ONE switch, so an active limit means "at an end" and
  `last_direction` says which. Since 2026-09-24 the CONTROLLER does that
  resolution and hands over at most one flag, so the panel just reads them: the
  direction that got you there is blocked, the opposite stays live to escape.
  The panel used to do the `last_direction` arithmetic itself for A/B, which put
  the rule in two places.

`_set_direction_enabled()` blocks **both** the jog button and the step-move
button for that direction — previously the step grid stayed live at a limit and
could drive into a switch the jog button was blocked from.

The post-move callback re-enables a button only if that direction is not
blocked. A move that ended *because* the switch tripped would otherwise leave
the button live until the next poll, which is exactly when it must not be.

`limit_flags` (a module-level dict in the panel) was deleted: with the fallback
branches gone it was write-only, and the button enable state is now the flag.

### Zeroing: two display origins, neither of them DP (2026-09-22)

`utils/zero_offsets.py` holds **two independent origins per axis**, in encoder
counts, persisted to `config/zero_offsets.json`:

| | set by | drives | when it changes |
|---|---|---|---|
| `counts_zero` | the ZERO X/Y/Z buttons | the **counts** column | whenever convenient |
| `um_origin` | `set_micron_origin.py` | the **micron** column | once, at the rig's zero |

Keeping them apart is the point. The ZERO buttons are a working zero, pressed
mid-job to measure a short move. If one offset drove both columns, pressing one
would silently move the micron origin too, and the reading that is supposed to
answer "where is the stage" would stop meaning anything fixed.

So: **ZERO X zeroes the counts column and leaves microns alone.** Microns are
always measured from the true zero.

The micron origin is deliberately **not** on a button, since moving it by
accident changes the meaning of every position on screen. `set_micron_origin.py`
sets it: park the axes at the rig's zero and run it. `--from-file` reads
`config/last_positions.json` instead of the controller, for when the GUI is
running and holding COM4; `--show` prints both origins.

The file migrated from an older flat format `{"A": 3226664.0, ...}` that drove
the counts column alone. Those values had been recorded at the rig's zero, so
they seeded **both** origins - which is exactly the one-time micron zero, with no
need to park the stage anywhere to migrate.

Neither offset touches the controller and neither moves anything. `_TP` is left
alone: it is fed from the BiSS frame and IS an absolute coordinate in the
encoder's 1200 mm PRC code space (axis A around 3,226,657 counts), so every log,
test and hardware script stays comparable with the encoder's own numbers. Note
the consequence - **the GUI shows positions relative to a zero while the logs and
test scripts report absolute `_TP`**, so the two will not match when comparing a
log against the screen.

Why not the controller's `DP`, which the old "ZERO ALL (DP=0)" buttons used:

1. **It would not persist.** `DP` lives in controller RAM, does not survive a
   power cycle, and `enable_axes()` does not restore it — so a zero needs a file
   regardless, and then there are two mechanisms to hold in agreement.
2. **It shifts `_TP` itself**, so it cannot give two columns two different
   origins, and it would break the correspondence with the logs.

`GalilController.define_zero()` still exists — it is a legitimate Galil command —
but **nothing in the UI calls it any more**. There had been two DP-based buttons:
the jog panel's "ZERO ALL (DP=0)" and a second "ZERO ALL" in `main_window.py`'s
bottom bar next to Enable/Disable Motors. Both are gone.

Buttons in the Homing group are HOME A / HOME B / HOME C and ZERO X / ZERO Y /
ZERO Z — six in total. "HOME ALL AXES" and "ZERO ALL" are gone, `_home_all()`
with them, and the Z column's duplicate ZERO Z was removed.

**The position display reads `AxisStatus` from the poll signal**, not
`config/last_positions.json`. That file is rewritten by `poll_all_axes()` every
cycle while the panel reads it, so the read raced the writer and could return a
half-written file (seen: `JSONDecodeError` straight out of
`on_status_updated`). It was also a cycle behind, and in mock mode it showed
whatever the last hardware session had left in the file rather than the
simulated stage — which is why `--mock` appeared to have a frozen readout.

`_raw_counts` caches what the counts column was built from so ZERO zeroes that
exact value. Getting this wrong is easy: zeroing against `AxisStatus.pos` while
the display came from the file left the reading at 3,229,723 instead of 0.

Those were the last readers: `_go_home_xy()` and `_go_to_target_xy()` were
removed later the same day, and the position-memory recall now passes absolute
`_TP` straight through. The panel no longer imports `json`, `os` or
`POSITION_PATH` at all.

**The counts column is ENCODER counts (`_TP`, 12.8 counts/µm), not motor
microsteps (6.4)** — and since 2026-09-24 it is signed in STAGE terms, so on Y
and Z it reads the opposite sign to raw `_TP`; see the reversal section. A 10 µm move is `PR=64` microsteps commanded and **128
counts** on the display — the factor of 2 is the encoder/motor ratio, the same
one the tuning runs measured as 1.9738 and 1.9567. Both columns were labelled
just "counts", which is exactly how the two scales get conflated (it caught the
user on 2026-09-22), so they now read **"enc counts"** with a tooltip spelling
out both scales.

### Encoder health, and the Z micron origin (2026-09-24)

**The Z micron origin is now `6991849` counts**, up from `6329144`. It was
declared deliberately near the **end of C's tape**, +51.80 mm from the old rig
zero, so the micron column measures Z from close to the start of travel. A, B
and every `counts_zero` were not touched.

**`_SY` is NOT the encoder status — `_SSm0` is.** `_SY{ax}` only echoes the
warn/error POLARITY that `enable_axes()` wrote (`SY{ax}=0`), so it reads 0
whether or not anything is wrong. Checking `MG _SYC` to see whether the encoder
is happy always answers "fine". The status word is `MG _SS{ax}0`:

| bit | meaning |
|---|---|
| 0 | timeout |
| 1 | CRC invalid |
| 2 | warning (signal degraded, data still valid) |
| 3 | error (data not valid) |

Galil is explicit: **`_SSm0 = 0` means the encoder is functioning properly**, and
`#SERERR` fires on any of the four.

**Current state, measured 2026-09-24 with `SY=0` confirmed in force:**

| axis | `_SSm0` | |
|---|---|---|
| A | **0** | clean |
| B | **4** | warning, steady. **Not new**: `_SSB0 = 4` was recorded on 2026-09-17 as "system margin below 12.5%", and the encoder re-mount the user began on 2026-09-16 was never confirmed. Still outstanding |
| C | **4** | warning, steady, at the chosen origin — **but C read `_SSC0 = 0` on 2026-09-17**, the cleanest of the three |

**Past the tape end, C's position data is corrupted, not merely flagged.** At the
first spot tried, `_SSC0` flickered 4 → 12 (warning always, error on 9 of 75
reads) and `_TPC` came back with **bit 23 set**: 8,388,608 counts = 655 mm high,
on an axis with 200 mm of travel. It was identified because `_TPC − 2²³` fell
within 276 counts (21.6 µm) of the previously recorded origin, while A and B sat
+41 and −143 counts from theirs — so the head was physically still at the rig
zero and one bit was wrong. A genuine PRC misdecode would have landed anywhere in
the 1200 mm code space, not within 22 µm of `true + 2²³`.

- It was **stable**, set on all 75 samples, so repeated reads do not catch it.
- **The CRC stayed clean throughout**, which is the same trap as the doubled
  reading in the `BISS_CONFIG` notes: a valid CRC says the frame arrived intact,
  never that it was decoded right. The warning and error bits are what spoke up
  here.
- The hazard is not the origin value, it is the closed loop: `move_microns()` and
  `move_to_counts()` compute their correction from `_TP`, and a bit-23 flip
  mid-move is a 655 mm error. The pre-move travel guard checks the REQUESTED
  distance, not the per-step correction, and `LD=3` leaves no hardware limit.

**C's warning looks position-dependent, not a degraded encoder.** It read a clean
`_SSC0 = 0` on 2026-09-17 near the rig zero, warns at the new origin 51.8 mm
along, and errors outright further out — which is the signal margin falling as
the head approaches the end of the tape. That also means a clean status word
near the middle of travel says nothing about the ends.

At the origin finally chosen, `_TPC` read **6991849 stable to 0 counts** over 43
samples with no bit-23 artifact, which is why it was accepted with the warning
bit still on. Only the ABSOFLEX adapter would show the actual margin percentage;
over BiSS only this single below-12.5% bit is visible.

**`set_micron_origin.py` closes via `disconnect()`, which sends `ST` + `MO`** — so
it drops holding current immediately after declaring a zero. It also does not
check `_SSm0` before recording. Both are worth fixing before it is used again;
the 2026-09-24 origin was set through a scratch script that checked the status
word and closed with `GClose`.

### Jog panel cleanup (2026-09-22)

Removed, all redundant once the D-pad went closed loop:

- **"Step Move (µm, closed loop)" grid.** It queued exactly what a D-pad click
  queues — same `move_microns`, same step size, same settle window — so it was a
  second way to do one thing.
- **HALT XY and HALT Z.** The bottom bar's HALT issues `ST` on every axis, and
  E-STOP sits next to it.
- **The Z column's Step / +1 Step / −1 Step / Target Z / GO TO Z.** These were
  the last open-loop motion in the panel: `move_relative` and `move_absolute` in
  raw counts, bypassing the closed loop the Z D-pad above them already used.
  Their spinboxes were labelled "counts" while everything around them is µm.

`_halt_xy()` and `_step_move_btns` went with them, and
`_set_direction_enabled()` is back to handling just the D-pad.

Motion still reachable from the panel: the D-pad (`move_microns`, or `jog` on a
hold), HOME (`move_to_counts`), position-memory GO TO (`move_all_absolute`), and
`halt_axis` on a jog release.

Also removed: the D-pad's centre ⌂ button (`_go_home_xy`) and the "Move to
Absolute Position" panel with its MOVE TO TARGET XY button (`_go_to_target_xy`).
Both called `move_relative_closed_loop()` — the stale `*1.25` factor — and fed it
`_TP` PRC coordinates as relative moves; `_go_to_target_xy` also hardcoded `*6.4`
while mixing motor and encoder counts. HOME A / HOME B cover what ⌂ was for,
per axis and closed loop.

**Nothing in the UI calls `move_relative_closed_loop()`, `move_relative()` or
`move_absolute()` any more.** Every motion the panel can start is closed loop:
`move_microns` (D-pad click), `jog` (D-pad hold), `move_to_counts` (HOME),
`move_all_absolute` (position-memory GO TO), `halt_axis` (jog release).

With those two handlers gone the panel no longer opens
`config/last_positions.json` at all, so `POSITION_PATH` and the `json`/`os`
imports went with them.

### Post-move relaxation and the verify pass (2026-09-22)

**Measured on axis C**, watching `_TP` for 10 s after a move, stage untouched:

| step | drift at 10 s | as % of step |
|---|---|---|
| 1 µm | +0.443 µm | 44 % |
| 10 µm | +0.651 µm | 6.5 % |
| 100 µm | +0.495 µm | 0.5 % |

Across a **100× range in distance the drift varies 1.5×**, so it is a CONSTANT
per move, not proportional — fitting both models, constant wins by ~7× on
spread/mean. Physically sensible: elastic wind-up depends on the force in the
drivetrain (friction, bellows preload), which does not change with distance. It
does settle; by 10 s the rate is under one encoder count per second. The earlier
"still climbing at 5 s" reading was simply too short a window.

**It always runs WITH the direction of travel**, which is why every closed-loop
move overshoots, and why waiting longer makes the overshoot bigger: a 10 µm move
drifts +0.26 µm by 0.2 s, +0.44 by 1 s, +0.65 by 10 s.

Fraction arrived by the time the loop measures:

| window | 1 µm | 10 µm | 100 µm |
|---|---|---|---|
| 0.2 s | 12 % | 40 % | 53 % |
| 1 s | 53 % | 68 % | 74 % |

At a 200 ms window a 10 µm move has seen +0.260 µm of it — and the measured
closed-loop error was **+0.219 µm**. The loop is not inaccurate; it is measuring
a stage that has not finished moving.

**This rules out correcting after the fact.** The stage ends PAST the target, and
reversing costs 7–11 µm of take-up to recover 0.2 µm. The only workable order is
to aim SHORT and let the relaxation carry the stage in.

`post_move_verify(axis, target_counts, ...)` does the second half:

- waits `RELAX_SETTLE_S` (1 s, ~70 % arrived), settles, re-measures;
- corrects only in the **approach direction** — an overshoot is reported and left
  alone, never reversed;
- **will not correct a shortfall smaller than `RELAX_UNDERSHOOT_UM`**, because a
  correction is itself a move and starts its own ~0.45 µm of creep. Simulation
  showed that guard mattering: without it, correcting a small shortfall turned a
  +0.22 µm residual into +0.32 µm.
- stops early if a pass fails to improve, and caps at `VERIFY_MAX_PASSES`.

`move_to_counts(..., verify=True)` aims `RELAX_UNDERSHOOT_UM` short (capped at
half the move, so a short hop is not reduced below the 0.31 µm stiction
threshold) and then runs the verify. **HOME and position-memory GO TO use it**;
jogging does not, because it costs ~1 s per pass.

Simulated against the measured behaviour (constant creep, ~1 s time constant),
sweeping the real creep against the assumed 0.45 µm:

| real creep | no verify | with verify |
|---|---|---|
| 0.25 µm | +0.289 | −0.199 |
| 0.35 | +0.221 | −0.092 |
| 0.45 | +0.329 | +0.016 |
| 0.65 | +0.544 | +0.282 |
| 0.90 | +0.814 | +0.345 |

Better in every case, 1.4× to 20×.

**Confirmed working on hardware, 2026-09-22.** The user exercised HOME and
position-memory GO TO on the rig and reported both behaving correctly. Note what
that does and does not establish: the mechanism works end to end and does not
misbehave — it was NOT a measurement of the residual error, so
`RELAX_UNDERSHOOT_UM = 0.45` is still a first estimate from three relaxation
rounds on one axis (measured range 0.44–0.65). It sits at the low end
deliberately, because an undershoot can be corrected and an overshoot cannot.
Tightening it further needs a run of `test_move_microns_hw.py` with verify
enabled, which that script does not yet expose.

### Homing (2026-09-22)

HOME A / HOME B / HOME C drive the axis back to **where its counts display reads
0** — the `counts_zero` offset — via `move_to_counts()`, so it lands within a
microstep like any other closed-loop move.

Two things it is deliberately NOT:

- **Not Galil's `HM` routine**, which the buttons used to call. `HM` seeks a home
  switch; this rig runs `LD=3` and has no reverse switch at all.
- **Not raw `_TP = 0`.** `_TP` is an absolute PRC coordinate, so axis A's zero
  sits near 3,226,664 counts and a literal 0 would ask for 252 mm of travel on a
  20 mm axis. The travel guard would refuse it, but it is not what "home" means
  here.

An axis whose `counts_zero` is still 0 has never been zeroed — an absolute
encoder on this rig reads millions, never 0 — so homing it is refused with a
dialog telling the user to zero it first, rather than attempting a move across
the travel.

HOME targets the **counts** zero, not the micron origin, and that is
deliberate — confirmed by the user 2026-09-22. Pressing ZERO anywhere makes that
spot the new home target immediately, because HOME reads the same value ZERO
writes. The micron origin does not move with it, so the micron column keeps
measuring from the rig's true zero no matter how often the working zero is
re-set. Demonstrated end to end: zero at A, jog away, HOME returns; re-zero
1000 µm along, jog away, HOME returns to the NEW spot.

`GalilController.home_axis()` (the `HM`/`BG` wrapper) still exists but nothing in
the UI calls it.

### Position memory (2026-09-22)

Slots store **absolute `_TP`** — the encoder's own reading — so a slot identifies
a physical place rather than a distance from wherever the stage happened to be.
`_save_position()` already did this; everything downstream of it changed.

**Display.** The saved list shows **microns from the micron origin**
(`X:+100.00  Y:+0.00  Z:+0.00 µm`), not the stored counts, which are PRC
coordinates in the millions and unreadable. The raw `_TP` values and the save
timestamp are in the item's tooltip.

**GO TO** now hands `move_all_absolute()` the stored readings unchanged, and that
calls `move_to_counts()` per axis — the same step/measure/correct loop as
`move_microns()`, aimed at a fixed encoder position instead of a distance.

Two bugs were in that path:

- **The current position was subtracted twice.** `_go_to_saved()` computed
  `slot.pos_a - current` and handed the difference to `move_all_absolute()`,
  which subtracted the current position again — so the stage moved
  `(target - current) - current`.
- It then drove `move_relative_closed_loop()`, with its stale `*1.25` encoder
  factor.

`move_to_counts(axis, target_counts)` is the new entry point. It shares the loop
via `_move_microns(..., absolute_target=...)`, which pins the encoder target
rather than deriving it from a distance — so creep between the caller's reading
and the loop's own cannot be added to the destination. Snapping is off: the
target is a place, not a distance, so rounding it to the microstep grid would be
rounding the destination.

It returns 0 and commands nothing when already inside tolerance, so recalling the
position you are standing on does not nudge the stage. Verified against the fake
device: a three-axis recall lands on every target to 0 counts, in both
directions, and repeating it moves nothing.

`move_all_absolute()` drives the axes **sequentially** — each closes its own
loop, and interleaving would put three correction loops on one serial link. Its
`wait` argument is accepted and ignored: a closed loop is blocking by nature.

### Relaxation is reproducible; heating is ruled out (2026-09-23)

Two FULL profiles a day apart — one after a long session of motion, one on a
cold rig. Drift at 10 s, µm:

| run | 1 µm | 10 µm | 100 µm | mean |
|---|---|---|---|---|
| 2026-09-22 16:07 (warm) | +0.443 | +0.651 | +0.495 | +0.530 |
| 2026-09-23 11:34 (cold) | +0.495 | +0.521 | +0.417 | +0.478 |
| difference | 0.7 counts | 1.7 counts | 1.0 counts | |

All six measurements fall in **0.417–0.651 µm, mean 0.504**, and the constant
model beats the proportional one by 12× on the second run (spread/mean 0.22
against 2.67 — a 100× range in distance produces a 1.2× range in drift).

**Motor and drive heating does not measurably change it**, so that hypothesis is
dead: cold and warm agree to within about one encoder count, which is the
measurement resolution.

`RELAX_UNDERSHOOT_UM = 0.45` sits **0.7 encoder counts** below the measured mean
of 0.504. That is inside the resolution of the measurement, so the constant is
well chosen and changing it to 0.50 would not be a measurable improvement.
**Left at 0.45.**

One caveat on comparing the two profiles: the 2026-09-23 run used the new
adaptive sampling (fast for 0.6 s, then 4 Hz), so marks after 0.6 s carry up to
250 ms of staleness. At the observed decay rates that is ~0.01 µm — an eighth of
an encoder count, well below anything being concluded here.

This **corrects an earlier claim in these notes**: "the relaxation varies
between sessions (0.44–0.65 characterised, 0.09–0.29 a day later)". That was
inferred from the plain-move overshoot in the verify runs, which is not the same
quantity — overshoot mixes the move's own error in with the relaxation. Measured
DIRECTLY it is stable to about an encoder count, and the 0.33 µm undershoot that
the noisy run suggested was the fitting-noise answer.

It also relocates the repeatability problem: whatever made the 10:56 moves
scatter 4× is NOT the relaxation. Still unexplained.

**The 1 µm step had not finished at 10 s** (mean rate +0.025 µm/s, driven by one
round still moving at +0.076); the 10 and 100 µm steps had. So the tail is
occasionally longer than 10 s. `--relax-seconds 60` would find where it really
stops, if the last ~0.1 µm ever matters.

**The link recovery was confirmed on hardware** by this run — it timed out
partway through and the log shows `reconnecting to resynchronise` followed by
`[OK  ] reconnected`, which is the first real-world exercise of that path.

Two script fixes came out of it, **both confirmed by the clean 2026-09-23 11:34
re-run**, which completed all nine measurements with no timeout:

- **One timeout no longer discards the phase.** The run died two rounds into
  nine. `_relax_round()` is now a separate function so a round that fails can be
  retried after a reconnect, and a round that fails twice is skipped rather than
  abandoning the rest.
- **The sampling is adaptive.** 10 s at ~28 Hz is ~280 commands per round, and
  that continuous traffic is what timed the link out. It now samples fast for
  the first 0.6 s, where the marks are dense, then at 4 Hz — roughly an eighth
  of the load, with the early curve intact.

### Rig repeatability is now the limiting factor (2026-09-23)

Two `--verify` runs 34 minutes apart, same code, same settings. Comparing only
the PLAIN moves — an identical code path in both, so any difference is the rig:

| step | 10:22 error | 10:56 error | 10:22 spread | 10:56 spread |
|---|---|---|---|---|
| 1 µm | +0.094 | +0.068 | 0.156 | **0.547** |
| 10 µm | +0.287 | +0.859 | 0.078 | **0.781** |
| 50 µm | +0.287 | +0.365 | 0.156 | 0.313 |
| 100 µm | +0.104 | +0.182 | 0.078 | 0.234 |
| **mean spread** | | | **0.117** | **0.469** |

**Repeatability got 4× worse in half an hour, with nothing changed in software.**
For scale, the 2026-09-17 baseline had 50 µm and 100 µm repeating to **0.000 µm**
— the identical encoder count three times running.

That makes parameter tuning pointless for now: the scatter exceeds the effect
being tuned. The evidence is in the recommendation itself, which oscillates
between runs — 0.33 µm from the 10:22 data, 0.50 µm from 10:56. That is fitting
noise.

**What to look at instead.** The first hypothesis was motor heating changing the
relaxation — **tested and ruled out**, see the section above: cold and warm
profiles agree to within a fifth of an encoder count. So the scatter comes from
something else. Remaining candidates, none tested: environmental disturbance
during that particular run, where the stage sits in its travel, the vacuum and
bellows state, or variability in the stiction break-free (which was 1–2
microsteps and is the one part of a move that is inherently irregular).

### Verify pass measured on hardware (2026-09-23) — not yet a win

`test_move_microns_hw.py C --verify`, undershoot 0.45 µm, both variants measured
2 s after the move ends:

| step | error, plain | error, verified | spread plain | spread verified | time |
|---|---|---|---|---|---|
| 1 µm | 0.094 | 0.271 | 0.156 | 0.078 | 1.6× |
| 10 µm | 0.287 | 0.182 | 0.078 | 0.078 | 2.9× |
| 50 µm | 0.287 | **0.078** | 0.156 | **0.859** | 1.9× |
| 100 µm | 0.104 | 0.104 | 0.078 | 0.313 | 1.8× |
| **mean** | **0.193** | **0.159** | **0.117** | **0.332** | ~2× |

**The mean error improves slightly; repeatability gets ~3× worse.** For
positioning that is the wrong trade — a consistent offset can be calibrated out,
a random one cannot. As tuned, the verify pass is not earning its cost.

Two things explain it:

1. **The undershoot is too large for this session.** Verified moves land SHORT
   by 0.12 µm on average. Correct setting from this data: **0.33 µm**.
2. **The relaxation is not stable between sessions.** Inferred from the plain
   overshoot here (0.09–0.29 µm at 2 s), it is roughly half what the
   characterisation run measured (0.44–0.65 µm). A fixed feed-forward constant
   cannot track something that varies run to run, which is also why the verified
   spread blows out — the correction fires on some repeats and not others.

**Re-run at `--undershoot 0.33` (10:56):** the mean improved on **4 of 4**
distances (+0.068→+0.042, +0.859→+0.469, +0.365→+0.313, +0.182→−0.130), and the
spread improved at 1 µm and 100 µm but got worse at 10 µm and 50 µm. Verdict
still mixed — but see the section above: the rig's own repeatability degraded 4×
between the two runs, so neither run is a clean measurement of the constant.

Leave `RELAX_UNDERSHOOT_UM` at 0.45 until the rig is repeatable again. Tuning it
against this scatter is fitting noise.

**A reporting bug was found and fixed doing this.** The script's recommendation
line had both branches inverted: `error = final - target`, so positive is PAST
the target and negative is SHORT, and it read them the other way round. It told
the user to *enlarge* an undershoot that was already too big (`try --undershoot
0.57` when the answer was 0.33). The sign convention is now asserted against the
known physics — plain moves must overshoot, because relaxation runs with the
direction of travel.

### Remembering the last connected port (2026-09-23)

The connection panel presents the address of the **last connection that actually
succeeded** — `AppSettings.last_address`, written by `_remember_address()` from
`ControllerInfo.address` in `_on_connected`, i.e. only on success. A port that
failed to open is never recorded.

- **Written immediately, not at shutdown.** `closeEvent` does not run if the app
  is killed or crashes, and a port that connected is the one thing worth keeping
  out of such a session.
- **`last_address` defaults to `""`, not `"COM4"`.** Empty means nothing has ever
  connected on this install, and the panel then presents *nothing* — the address
  field stays empty and the COM combo sits at index −1 with a placeholder, rather
  than offering a port that was never tested. `_build_address()` returns `""` and
  CONNECT already answers "Enter an address first".
- `_restore_last_address()` picks the mode from the stored value: `COM\d+` switches
  the panel to Serial and selects the port (adding it if it is outside the COM1–8
  list), anything else fills the Ethernet field. `_split_address()` strips gclib
  options — `--baud N` is restored into the baud combo, subscribe flags are
  dropped because that is a per-session choice, not part of identifying the port.

**`AppSettings.save_to_file()` did not exist**, though `main_window.closeEvent`
called it inside a bare `try`. The `AttributeError` was silently skipping the
three lines below it, so the app exited without `halt_all()`, `disable_axes()` or
`disconnect()` — the link was never closed cleanly. The method now exists (one
canonical writer), and the save and the
controller teardown sit in separate `try` blocks so neither can gate the other.
Note this makes `MO` fire on window close; the DISCONNECT button already did that,
so it is not a new state for the rig.

### Retract Z / Insert Z — the blind moves past the tape (2026-09-24)

One button at the bottom of the Z column, toggling between **Retract Z** (red)
and **Insert Z** (green). It is one control because the two are a toggle:
whichever it offers is the only one that makes sense from where the head is.

**Retract**: closed loop to 0 µm, then **open loop** from there — a plain jog at
`BLIND_JOG_SPEED` (1000 counts/s) until the limit switch stops it.
**Insert**: jog back at the same speed until the encoder calls its own data valid
again, i.e. the head is back on the tape at about 0 µm.

Why open loop at all: **the Z limit switch at the stage's minus end sits BEYOND
the scale tape.** Once the head runs off, `_TP` is not a valid reading, so no
closed-loop move can be aimed at the switch and nothing can measure how far it
went. The closed-loop half gets the axis to 0 µm, where the encoder is still
good, and the blind half covers the rest.

#### Speed, and why it is the only lever

Nothing stops a jog except the poll loop noticing the switch, one cycle
(~165 ms) later. That overshoot is speed × cycle, and because this stretch is
open loop and past the tape it **cannot be measured or corrected afterwards,
only avoided**. See "Jog overshoot" for the same arithmetic and the options that
would fix it properly.

| `BLIND_JOG_SPEED` | µm/s | overshoot | 7.4 mm takes |
|---|---|---|---|
| 500 (until 2026-09-24) | 78 | ~13 µm | 95 s |
| **1000 (now)** | **156** | **~26 µm** | **47 s** |
| 4000 (the jog default) | 625 | ~100 µm | 12 s |

Raised to 1000 because 95 s is a long time to watch; the doubled overshoot was
accepted deliberately for it. The test ceiling is 1000, so going further is a
decision that has to change a test as well.

#### Z's own controls are locked out for the whole round trip

From the moment **Retract Z** is pressed until **Insert Z** has completed, the
Z+/Z− D-pad buttons are greyed and **Page Up/Page Down are refused**. X and Y are
untouched.

Between those two points the head is off the scale tape, or on its way there or
back: `_TP` is not a position, so a step move has nothing to close its loop on
and a jog would run blind with the soft limits suspended. The retract/insert
button is the only Z motion that makes sense in between.

`_z_manual_locked(axis)` is the state test; `_direction_allowed(axis, sign)`
combines it with `_direction_blocked()` and is what every enable decision now
asks — the poll loop, the post-move re-enable, and `_start_key_jog()`.

**The lock is deliberately NOT folded into `_direction_blocked()`.** That rule
means "at an end", and it also drives the limit table and the escape hint;
merging them would make the table read HIT for an axis that is merely busy, and
the hint name a button for a limit that was never reached.

`_apply_z_lock()` pushes it to the buttons at press time rather than a poll
cycle later, and any Page Up/Down jog already running is stopped **before** the
retract is queued — `_stop_key_jog()` calls `clear_queue()`, which would
otherwise throw the retract command away.

#### Both buttons ask first

| button | prompt |
|---|---|
| Retract Z | "Are you sure to retract Z?" |
| Insert Z | "Is the Vacuum valve opened?" |

Yes proceeds, No aborts with nothing queued and no state changed. **Both default
to No**, so Enter or Escape on a dialog nobody read means don't — these guard a
move that cannot be undone by looking at the encoder afterwards, because once
the head is off the tape there is no reading to say where it went.

`_confirm(title, question)` is a method rather than an inline `QMessageBox` so
tests can replace it: a modal blocks an offscreen run outright, which is the
trap `CLAUDE.md` already records for SAVE and OPEN FILE.

#### Poll-driven, not a blocking command

Each press queues ONE short command and the state advances in
`on_status_updated()` as the flags come back. A blocking implementation would
freeze the very poll loop that has to stop the jog, and would swallow HALT for
the whole retraction — on the one move that is open loop and past the end of the
scale. States: `on_tape → retracting → off_tape → inserting → on_tape`.

The retract's single queued command does the closed-loop move AND starts the jog,
so the blind phase cannot begin before the axis has arrived.

#### Four things that are easy to get wrong here

- **Direction.** Stage −Z is controller **+C**, so the retract jogs `+500`. A
  sign slip drives the head further onto the tape, or off the far end. Derived
  through `to_controller()`, never hardcoded.
- **Soft limits are suspended for both blind phases**, and stay suspended while
  parked off the tape. They are computed from `_TP`, so once it is garbage a
  bound would fire on nonsense and halt the very move that put the head there.
  `SoftLimits.suspend()`/`resume()`.
- **The insert does not stop on ONE clean encoder read.** It waits for
  `_ENC_OK_POLLS` (3) consecutive polls with the BiSS **error** bit clear,
  because that bit was measured INTERMITTENT near the tape end — 9 of 75 reads
  on 2026-09-24. It keys off bit 3 only, not the warning bit: B and C sit at
  warning at rest, so a clean status word would never arrive.
- **`enc_status` is not polled by default.** One more read per axis is ~16 ms,
  and the cycle length is what sets the jog overshoot. `PollingWorker.watch_encoder`
  is the set of axes to read `_SS{ax}0` for; the insert adds "C" and drops it on
  completion. `AxisStatus.enc_status` is `None` when nobody asked — which is not
  a value to act on.

#### Bounds and failure

- **The bound is `BLIND_MAX_TRAVEL_UM` = 10.5 mm, a DISTANCE.** The switch sits
  about **7.4 mm past 0 µm** — the first retract reported 100 s, but that timer
  starts at the button press and so includes the closed-loop approach to 0 µm;
  the blind part was 94 s at 78 µm/s. The check itself is a timeout — open loop,
  elapsed time IS distance — but the seconds are DERIVED
  (`_blind_timeout_s()`), so **doubling the speed halved the timeout to 67 s
  while leaving the distance bound exactly where it was**. That is the whole
  reason it is written as a distance: a hardcoded number of seconds would have
  silently doubled how far a runaway could travel.
  Until the switch trips this is the ONLY thing bounding the move: a broken wire
  reads ACTIVE under `CN=1` and would stop it, but an unplugged one would not.
- An aborted RETRACT lands in `off_tape`, not `on_tape`: the head may be
  anywhere, possibly off the tape, and `off_tape` is the state whose button
  offers the move that brings it back.
- A queued command that RAISES aborts the sequence immediately, via
  `worker.error_occurred`. Without that the button would sit dead until the
  timeout — over two minutes, silently.
- Retract refuses with a dialog if C has no micron origin: an absolute encoder
  never reads 0 on this rig, so `um_origin == 0` means it has never been set.
#### The state is remembered across restarts

`config/z_retract_state.json`, via `utils/z_retract_state.py`. Nothing on the rig
can be asked whether the head is off the tape — `_TP` is not a valid reading
while it is — so the software has to remember. Before 2026-09-24 every launch
assumed `on_tape` and offered Retract Z, which after a retract is the one move
that makes no sense.

**Only the two stable states are stored, and the mapping is deliberately
asymmetric:**

| written | when |
|---|---|
| `on_tape` | ONLY when an insert completes — the one moment the encoder itself says its data is valid again |
| `off_tape` | the moment a retract **starts**, and held through retracting / parked / inserting |

So a crash or a kill at any point in the round trip leaves `off_tape` on disk.
That is the safe way round: `off_tape` offers Insert Z, the move that recovers,
and it costs only a short blind jog if the head was on the tape after all —
the insert stops as soon as the encoder reads valid.

The reverse mistake is the expensive one. A stale `on_tape` offers Retract Z,
whose first act is a closed-loop move to 0 µm; against an off-tape `_TP` that
target is hundreds of mm away and the `travel_mm` guard in `_move_microns()`
refuses it, so it fails loudly rather than driving anywhere — but it is still a
state nobody wants.

`_set_rz_state()` is the ONLY place `_rz_state` changes, and it does the button,
the Z lock and the disk write together, so persisting cannot be forgotten in one
of the four transitions. The restore in `__init__` assigns directly and does NOT
write back — loading is not a change.

**Tests must redirect `z_retract_state.STATE_PATH`**, the same way the
`move_microns` fixture redirects `LAST_DIR_PATH`. A fabricated `off_tape` in the
real file is worse than no file: the panel would offer Insert Z on a stage that
was never retracted.

### Soft travel limits, and the new Settings panel (2026-09-24)

**The sidebar is Connection / Jog Control / Log / Settings**, stack indices
0/1/2/3.

The Settings panel's first feature is a 3×2 table of **soft travel limits**: a
negative and a positive bound per axis, in **STAGE MICRONS from the micron
origin** — the same number the position display shows, so what is typed is what
is read. Reaching one raises the matching limit flag exactly as a physical
switch would, so every existing consumer reacts without knowing the difference:
the jog and step buttons for that direction grey out, the limit table lights up,
the escape hint names the way out, and `_guard_jog_limits()` halts a jog that is
still running.

Measured end to end on the mock, X bounded at −1000 µm:

| X position | fwd | rev | X+ | X− |
|---|---|---|---|---|
| −999.9 µm | False | False | on | on |
| **−1000.0 µm** | False | **True** | on | **OFF** |
| −1200.0 µm | False | True | on | OFF |
| −999.0 µm | False | False | on | on |

The flag clears by itself on the way back in — it is re-evaluated every poll,
so there is nothing to reset.

#### Jog overshoot: ~100 µm at 4000 counts/s, and it is NOT the soft limits

**Open, deferred by the user 2026-09-24.** Jogging into a soft bound overshoots
it by about **100 µm at 4000 counts/s** before the flag rises and the axis halts.

This is the poll cycle, not the limit code, and it applies identically to the
physical switches — `_guard_jog_limits()` is one cycle behind whatever tripped.
The cycle is `msleep(100)` PLUS the reads, ~165 ms in total, and `JG` is in motor
steps/s: 4000 / 6.4 = 625 µm/s × 0.165 s ≈ 102 µm. It scales with jog speed, so
the slider's 50000 maximum is over a millimetre.

**Note it does not affect step moves.** `_wait_motion()` polls every ~16 ms
during a move, so a `move_microns()` reaction is ~2.5 µm at the fine speed and
~20 µm at 4000 — an order of magnitude better. This is a jogging problem only.

Options, none implemented:

1. **Poll faster while a jog is running.** The cheapest change and it attacks the
   dominant term directly, but it adds link traffic exactly when the loop is
   already busy — and continuous traffic is what timed the link out during the
   relaxation profiling on 2026-09-23.
2. **Feed-forward the stopping distance.** Speed and `DC` are both known, so the
   distance to standstill is computable; trip the flag that far early. Predictive
   rather than reactive, costs nothing per cycle, but it bounds the axis short of
   where the operator asked.
3. **Issue a bounded move instead of an open-ended jog.** A jog toward a known
   soft bound could be a `PA` to that position rather than `JG`, letting the
   profiler decelerate INTO the limit rather than being stopped after it. The
   most accurate, and the largest change — it would mean the jog panel knowing
   which bound it is heading for.

Option 2 or 3 is the real fix; 1 only moves the number.

#### Where it is evaluated, and why not in the controller

`utils/soft_limits.py`, applied by `PollingWorker` to the poll result **before it
is emitted or guarded**. That is the one place every consumer already reads, so
nothing downstream needed changing.

It is deliberately NOT in `GalilController`: that class is pure Python and knows
nothing about micron origins (`zero_offsets.json`) or the stage/controller
reversal, and a soft limit needs both. Pulling those in would drag UI concepts
into the hardware layer.

Consequences worth knowing:

- **A direct script call is not bounded.** `move_microns()` from a hardware
  script goes straight to the controller and never sees these. `travel_mm` still
  guards the length of a single move; that is a different guard and neither
  replaces the other.
- **Flags are OR'd, never replaced.** A real switch must not be masked by a soft
  bound that has not been reached.
- **`neg >= pos` is refused at SAVE**, because it leaves no legal position and
  would block both directions at once with no way out.
- `AppSettings.axis_*.soft_limit_neg_um` / `soft_limit_pos_um`, `None` = no bound
  that way. A spin box cannot hold `None`, so its **minimum** reads "no limit" —
  `setSpecialValueText` only ever applies to the minimum, which is why it is the
  bottom of the range in BOTH columns and not the top of the positive one.

#### The sign trap this had to get right

The bounds and the position are STAGE microns; `fwd_limit`/`rev_limit` are
CONTROLLER flags. **Y and Z are reversed, so the stage's negative end is the
controller's FORWARD limit on those two.** Getting it backwards would block the
wrong button and leave the axis free to keep driving past the bound it was
supposed to guard. `test_soft_limits.py` pins all three axes in both directions,
plus the specific mirror-position failure a sign slip produces.

#### AXIS_SIGN moved out of the UI

`AXIS_NAME`, `AXIS_SIGN`, `to_controller()`, `to_stage()` and `stage_end()` now
live in **`utils/stage_axes.py`**. They were in `ui/panels/jog_panel.py` until a
third caller needed them (the soft limits, and `check_limits.py`, which had been
importing from the jog panel behind a `try/except` because that module pulls in
PyQt5). The alternative was a second copy of the sign map, and a sign map that
disagreed with itself would silently invert a limit or a jog. The jog panel
imports them under the same names, so its call sites are unchanged.

**One `ZeroOffsets` for the whole app**, owned by `GalilMainWindow` and passed to
both the jog panel and the soft limits — two instances would drift the moment
either wrote. `JogPanel` takes `zeros=` and still builds its own if none is
given, which is what the tests do.

#### This is not the panel that was deleted

The 2026-09-23 panel was removed because it was broken AND carried a live
hazard: `_collect_settings()` built ONE `AxisSettings` from six widgets and
assigned it to all three axes, so a working SAVE would have silently reset
`axis_c.travel_mm` from 200 to 20 and every `encoder_length_mm` to its default.
Only an unrelated `AttributeError` stopped it.

The new panel **mutates the two soft-limit fields on each axis's own settings
object and touches nothing else.** It never constructs an `AxisSettings`, and
never assigns one axis's settings to another. Everything else in
`settings.json` is still hand-edited.

### The settings panel is gone (2026-09-23) — and a new one exists (2026-09-24)

**Superseded in part**: a NEW `ui/panels/settings_panel.py` was written on
2026-09-24 for the soft travel limits; see the section above for why it does not
repeat this one's mistakes. The sidebar is **Connection / Jog Control / Log /
Settings**, indices 0/1/2/3. Everything below is about the ORIGINAL panel and
still explains why the parameters it edited are not in the new one.

The original `ui/panels/settings_panel.py` was deleted and unwired from
`main_window`, leaving the sidebar at **Connection / Jog Control / Log**,
indices 0/1/2.

Why: everything it could edit is either hardcoded in `galil_controller.py` (KS,
`FINE_SPEED`, the settle windows, the `RELAX_*` constants) or lives in
`settings.json`, where SP/AC/DC have been measured and tuned. `enable_axes()` is
what applies them, so a spin box could not have changed them mid-session anyway.

It was also broken and carried a live hazard:

- `_AxisTab.get_values()` read `self._cpm_spin`, whose creation was commented
  out, so **APPLY and SAVE both raised `AttributeError`** and neither had worked
  for some time. `ER` therefore never reached the controller at all.
- Worse, `_collect_settings()` assigned one `AxisSettings` to all three axes,
  built from only the six widget fields — so `travel_mm` and `encoder_length_mm`
  fell back to class defaults. A *working* SAVE would have silently rewritten
  `axis_c.travel_mm` from **200 to 20** and `encoder_length_mm` from 200 to 45,
  capping every Z move at 20 mm. The `AttributeError` was the only thing
  preventing it.

### Dead-code pass (2026-09-23)

An audit of everything persisted-but-unread, done the same day. Removed:

| Removed | Was |
|---|---|
| `AxisStatus.pos_ref` / `.err` / `.torque` | set only by the mock, read by nothing, no tests |
| `AxisSettings.error_limit` | `ER` reached the controller through exactly one path — the settings panel's broken APPLY — so it has never been applied |
| `AxisSettings.fwd_/rev_/home_limit_type` | NO/NC wiring, read by nothing; `_LF` polarity is hardcoded |
| `AppSettings.poll_interval_ms` / `position_history_depth` / `log_file` / `display_units` / `auto_reconnect` | written by the settings panel, read by nobody |
| `AppSettings.travel_motor_counts` / `travel_encoder_counts` | zero callers |
| `GalilController.home_axis()` + mock's | the last `HM` wrapper; nothing called it |
| `PollingWorker.log_line` | signal declared, never emitted (`LogBridge` does this) |
| `ConnectionPanel.settings_changed` | emitted, connected by nobody |
| Connection panel Timeout spin, Auto-reconnect checkbox, Latency row + `update_latency()` | none was ever read; Latency permanently showed `—` |
| `main_window` `POSITION_PATH`, the commented-out `last_positions` block, `import sys`, top-level `import json` | pointed at a file deleted on 2026-09-23 |
| the `um_to_counts` import in `jog_panel` | unused import; the function itself stays, it has tests |

**Kept, despite looking dead:** `move_relative`, `move_absolute`, `define_zero`,
`set_params` (the mock calls `move_relative` internally, hardware scripts use
them, ~30 tests cover them); `AxisStatus.vel`/`.stop_code`/`.servo_on` (asserted
in tests); `unit_converter.format_position`/`um_to_counts` and
`ZeroOffsets.clear()` (all three have their own tests).

`config/settings.json` lost the 17 corresponding keys and **round-trips exactly**
through `AppSettings`, so `save_to_file()` cannot reintroduce them.

Still outstanding: **`enable_biss()` is documented as a deprecated alias for
`enable_axes()`, but it is what the Enable Motors button actually calls.** Point
the button at `enable_axes` before deleting the alias.

### The limit table now uses the same rule as the buttons (2026-09-23)

The FWD/REV cells were computed as `s.fwd_limit and s.last_direction >= 1` /
`<= -1`. Two bugs:

- with `last_direction == 0` neither branch fired, so **an active switch
  displayed as CLR in both columns** while the jog button for that direction was
  correctly disabled — the table and the buttons disagreed;
- `s.rev_limit` was never read, so an independent reverse trip on C (which has
  separate switches) showed nothing at all.

Both cells now call `_direction_blocked(ax, ±1)`, the same single rule the button
enable state uses, so the two cannot diverge. A cell means "this direction is
blocked", which with one switch per axis is exactly what the hardware can tell
you.

**The HOME column was removed from the limit table** the same day. `_HM` is
never polled (`poll_all_axes` reads only `_TP` and `_LF`), so it could only ever
read CLR on real hardware, and there is no home switch on this rig — homing is a
closed-loop move to the counts zero.

### Serial link speed (settled 2026-09-23)

**115200 baud, fixed at the controller in hardware**, confirmed by the user with
`check_link_speed.py`. So the ~16 ms per command is **fixed overhead**, not
bandwidth — driver, serial latency timer and controller parse, none of which a
baud change touches. `serial_baud` in `settings.json` can pin it explicitly but
there is nothing to gain by changing it.

Ethernet is not an option on this rig at present. It remains the only route to
materially faster command turnaround, since it removes the per-command overhead
rather than the transfer time — worth revisiting only if the software feels
slow.

### Link recovery in the app (2026-09-23)

`GalilController.reconnect()` closes and reopens the connection, then re-runs
`enable_axes()` — the axis configuration (MT/SY/SS/YA/LD/KS/SP/AC/DC) lives on
the old connection and a fresh `GOpen` does not carry it.

`PollingWorker` calls it after **two consecutive link faults**, capped at
**three attempts** so it cannot loop against dead hardware. `_is_link_fault()`
separates the two kinds of error carefully: a `RuntimeError` from a limit guard
is a normal refusal and must NOT trigger a reconnect; a timeout or a
desynchronised reply is the link. Verified against the mock — five limit
refusals cause zero reconnects, two timeouts cause one.

**Is there a hardware fix?** Raising the gclib timeout is not one — it only
waits longer before giving up, and the damage is the late reply arriving
afterwards and shifting every subsequent read.

**The link speed was never recorded.** The app opened a bare `GOpen("COM4")`,
so the rate was whatever gclib defaults to. `check_link_speed.py` measures it:
it times `MG` commands with response lengths from 1 to 64 bytes and fits time
against bytes, which separates the two things that matter —

- the **slope** is the transfer rate, from which the baud follows (8N1 = 10 bits
  per byte);
- the **intercept** is fixed per-command overhead (driver, serial latency timer,
  controller parse) that no baud change removes.

If the intercept dominates a 20-byte exchange, raising the baud buys almost
nothing and only Ethernet would help. The inference was checked against
synthetic links of known rate and recovers them exactly, including when the
overhead is large. Run `python check_link_speed.py`; nothing moves.

`AppSettings.serial_baud` (default `0` = say nothing, gclib's default, the
historical behaviour) appends `--baud N` to the address when set, so the rate
can be pinned once measured. Both ends must agree — the controller's serial rate
is a controller-side setting, so opening at one it is not using fails loudly.

### Poll resilience and link desync (2026-09-22)

Two faults from one real session, visible in the log as
`cannot access local variable 'last_direction'` and
`could not convert string to float: '3230950.0000

: 1.0000'`.

**`poll_all_axes()` could not survive a bad axis.** The write back to
`last_positions.json` sat OUTSIDE the try/except but referenced `pos` and
`last_direction`, which are only bound inside it:

- first axis fails -> both names unbound -> the whole poll dies with
  `cannot access local variable 'last_direction'`;
- a LATER axis fails -> both still hold the PREVIOUS axis's values, so one axis
  silently records another's position. That one is worse, because nothing
  reports it.

Fixed: readings are collected per axis, and only axes that actually read are
persisted, in a single write per cycle instead of one read+write per axis
(60 file operations a second before).

**`last_direction` is NOT a leftover.** `AxisStatus.last_direction` is what the
jog panel's `_direction_blocked()` uses to decide which end of travel A or B is
sitting at, since those two share one switch across both DMC inputs. Remove it
and the panel can no longer tell which jog button to block. It is set by
`move_microns()`, carried in the poll, and seeded at import from
`last_positions.json`.

**The merged reply is a desynchronised link**: `'3230950.0000

: 1.0000'` is a
position reading and a limit reading in one response, i.e. every read is
returning the PREVIOUS command's answer. `_parse_reply()` now names that
explicitly instead of surfacing it as a float conversion error.

The combined-operand read (`MG _BGx, _LFx`, added the same day to save a 16 ms
round trip during a move) has been **withdrawn**. It is the only place that ever
assumed a multi-value reply, and whether or not it caused the shift, 16 ms is not
worth the risk of scrambling every subsequent reading. `_read_motion_state()`
now issues one operand per command.

Recovery after a desync still requires a reconnect - there is no flush in gclib.
`test_small_step_tuning.py` does this with `resync()`; the app does not yet.

### Tests must not touch `config/`

`tests/test_move_microns.py` drives the **real** `GalilController` class, so any
side effect it has on disk is a real side effect. `move_microns()` persists the
direction whenever it changes, and on 2026-09-23 the suite wrote a fake test's
directions into `config/last_direction.json` — worse than having no file, since
the limit logic would then trust fabricated data about which end of travel an
axis is on.

The `hw` fixture now redirects `LAST_DIR_PATH` to a temp file and restores it
afterwards. `test_poll_resilience.py` did the same for `POSITION_PATH` while that
existed. **Anything else added to the real controller that writes to disk needs
the same treatment** — check `config/` is unchanged after a suite run.

### Testing

`pytest` is **not installed** in `Software/venv` or on the system python, so
`pytest tests/ -v` does not currently run. **261 tests across 13 files**, all
passing as of 2026-09-24, executed with a minimal stand-in runner. The files are
ordinary pytest and will run under it once installed.

**The stand-in runner lives in the session scratchpad, not the repo**, so it is
gone once that session ends — the same situation as the manual build script. It
grew as the suite needed it, and rebuilding it is the tax on not having pytest.
By the end of 2026-09-24 it supported: fixtures, fixtures that depend on other
fixtures, `tmp_path`, yield-fixtures with teardown, `pytest.approx`,
`pytest.raises(match=...)`, stacked `parametrize` decorators as a cross product,
and per-test fixture caching.

**Three of those were added because a test FAILED for the runner's reasons, not
the code's** — most instructively the fixture caching: without it `soft` and
`settings` each got their OWN `AppSettings`, so configuring a limit in the test
had no effect on the object under test and 7 tests failed misleadingly.
**Installing pytest would retire all of this**; until then, suspect the runner
before the code when a failure looks impossible.

| file | tests | covers |
|---|---|---|
| `test_move_microns.py` | 48 | the closed loop end to end |
| `test_zero_offsets.py` | 18 | the two display origins, migration, home target |
| `test_mock_limits.py` | 19 | mock limit-switch model |
| `test_logging.py` | 24 | every action emits its log line |
| `test_controller.py` | 13 | mock controller basics |
| `test_unit_converter.py` | 13 | counts ↔ µm |
| `test_poll_resilience.py` | 8 | a bad axis must not poison the poll |
| `test_arrow_jog.py` | 19 | jog API agreement, the limit guard incl. escaping, jog-key ownership, Page Up/Down on Z |
| `test_limit_wiring.py` | 24 | `CN 1` polarity, one switch per axis, which end a trip means, and that starting an escape does not re-label it |
| `test_soft_limits.py` | 12 | software travel bounds, and the stage→controller sign on the reversed axes |
| `test_z_retract.py` | 34 | the blind retract/insert: direction, suspended bounds, the encoder stop condition, the Z lockout, both prompts and the persisted state |
| `test_axis_direction.py` | 23 | the Y/Z software reversal at every boundary it crosses |
| `test_position_memory.py` | 6 | slot storage |

`test_move_microns.py` drives the **real** `GalilController` through a fake gclib
device (`FakeGalil`) rather than only the mock. It models the 2:1
encoder-to-motor ratio, a drivetrain that under-delivers, a stage that keeps
moving after motion completes (`lag_reads`), a limit that trips mid-step
(`poll_steps` + `trip_at`), and a desynchronised reply. Those are the regression
guards for the faults found on hardware — without the settle, a 100 µm move lands
177 counts (13.8 µm) past target.

GUI work was checked by building the panel and the full window offscreen
(`QT_QPA_PLATFORM=offscreen`), clicking buttons and asserting what they queue.
Careful: clicking SAVE or OPEN FILE opens a modal dialog and blocks the script.

**A µm error column can never validate encoder scale.** The loop targets
`_TP + µm × 12.8` and any check measuring `Δ_TP / 12.8` uses the same constant on
both sides, so it cancels: if 12.8 were wrong every error would still read zero
while the stage moved the wrong distance. Validating 12.8 needs an external
length reference. What *is* independent is encoder counts per **motor** count,
since `_TP` and the `PR` counts come from different constants.

### The operator manual, and how it was built (2026-09-23)

`Galil_Controller_Suite_Manual.pdf` in the project root is a 36-page A4 manual
written FOR THE OPERATOR, not for whoever maintains the code. It was produced by
reading the source rather than the README, and it documents the things that
actually bite: the 6.4-vs-12.8 counts/um trap, `LD=3` meaning software is the only
limit protection, "motion complete is not stopped", the two display origins, the
four limit guards, and the post-move relaxation.

**It is BADLY STALE.** It was already out of date the day after it was written,
and 2026-09-24 moved most of what it describes. It has no idea that:

- Y and Z are reversed in software, so **every direction in it is wrong for two
  of the three axes**;
- there is a Settings panel again (it describes the deleted one), and the sidebar
  has four items;
- the limit switches are normally closed on `CN 1`, there is one per axis, and
  which end a trip means is decided by direction;
- soft travel limits, Page Up/Down Z jogging and Retract/Insert Z exist at all;
- the Z micron origin moved to 6,991,849 counts.

**Do not hand it to an operator as it stands** — the direction reversal alone
would have them driving the wrong way. Regenerate it; the HTML and build script
are in a long-gone session scratchpad, so that means writing it again.

How it was built, since there is no PDF library installed:

- The source is HTML, rendered by `QTextDocument` + `QPrinter` from PyQt5, which
  is already a dependency. Manual pagination draws each page and paints a footer,
  because `QTextDocument` will not add page numbers itself.
- **Render with `QT_QPA_PLATFORM=windows`, never `offscreen`.** Under the
  offscreen plugin the fonts do not resolve: glyphs come out blank and the layout
  runs to 47 pages instead of 36, with a 919 KB file of nothing. This is silent -
  the PDF is produced, it is just empty of text.
- `QTextDocument` supports only a subset of HTML/CSS. Class selectors, table
  borders via `td { border: ... }`, background colours and
  `page-break-before: always` all work; anything more modern does not.
- The build script and HTML live in the session scratchpad, not the repo. If the
  manual is to be maintained, move them in.

### Still open

**Opened or changed on 2026-09-24:**

- **X has no limit switch.** B and C are wired and declared in `LIMIT_INPUT`; A
  is not. With `LD=3` that leaves X with NO end-stop protection beyond its soft
  travel bounds and the `travel_mm` guard, and the jog guard can never fire for
  it. Wire it, then add `"A": "<terminal>"` — `check_limits.py` names the
  terminal for you.
- **B's terminal was assumed, not measured.** It was declared `FWD` to match C
  while the controller was powered off. If it is wrong, Y reads permanently at a
  limit; `check_limits.py` prints a MISMATCH row naming the correct terminal.
  **Confirm it on the next run with the rig on.**
- **Jogging overshoots a limit by ~100 µm at 4000 counts/s** — it is the poll
  cycle, not the limit code, and it applies to the hardware switches as well.
  See its own section for the three candidate fixes; the user deferred it
  deliberately. The blind retract works around it by running at 1000 counts/s
  (~26 µm) rather than by fixing it.
- **The retract distance is measured but the timer includes the approach.**
  `_rz_finish()` times from the button press, so the reported blind travel is
  overstated by however long the closed-loop move to 0 µm takes — the first run
  reported 100 s / 7.9 mm for a blind phase that was really 94 s / 7.4 mm.
  Stamping the clock inside `_retract_sequence()` just before the jog would fix
  both the figure and the bound. Two lines; not done.
- **`BLIND_MAX_TRAVEL_UM` (10.5 mm) has not been re-checked since the speed
  doubled.** The real travel is ~7.4 mm, so the margin is fine, but the bound
  was chosen against a 10 mm estimate that turned out high.
- **B and C both sit at the BiSS warning bit at rest**, and B's has been open
  since 2026-09-16 pending a re-mount that was never confirmed. Only A reads a
  clean `_SS`. C's warning is positional (clean near 0 µm, warns further along,
  errors past the tape); B's is not.
- **The operator manual is badly stale** and would now actively mislead — every
  direction in it is wrong for Y and Z. See its section.

**Longer standing:**

- **The verify pass is not yet a win** — see its own section. Two runs gave
  mixed results, and the second improved the mean on 4/4 distances while the
  spread went both ways. Blocked on the repeatability problem below, not on more
  tuning.
- **Rig repeatability degraded 4× within one session** (2026-09-23), in the
  plain code path, and is unexplained. Heating has been ruled out. This is the
  limiting factor and blocks further parameter work — the scatter exceeds the
  effects being tuned.
- **~16 ms per command is fixed overhead and cannot be tuned away.** The baud is
  115200, confirmed and fixed in controller hardware. Only Ethernet would change
  it, and that is deferred.
- **No tape origin is MEASURED for any axis.** `_TP` is a PRC coordinate in the
  encoder's 1200 mm code space, not anchored to the stage.
  - **This is a deliberate choice, not an oversight** (user, 2026-09-23): the
    micron origin in `zero_offsets.json` is declared to BE the start of travel,
    whatever the tape's real start is. The usable travel is bounded by the limit
    switches and is less than the 45 mm tape anyway, so the tape's physical ends
    are ignored.
  - **Done for C on 2026-09-24**: the user parked it near the tape end and
    declared `um_origin = 6991849`, choosing a spot where the encoder warns but
    does not error. A and B still sit at origins migrated from the old flat
    file.
  - What that costs: nothing for positioning, which is all relative to the
    declared zero. It does mean position alone cannot say which end of travel an
    axis is at — hence the assumed start direction above.
- **`enable_biss()` is called by the UI although it is documented as a
  deprecated alias** for `enable_axes()`. The Enable Motors button in
  `main_window.py` queues `controller.enable_biss`, so the "deprecated" path is
  the live one. Point the button at `enable_axes` first, then the alias can go.
- **Six measurement files disappeared from the working tree during the
  2026-09-23 session** — `encoder_measure`, its four dated rotations,
  `encoder_measurements.txt` and `encoder_average_backlash.txt`. They were
  present at the start of that session and gone by the end, and no command run
  during it targeted them; the cause is not established. All are intact in
  `HEAD` and restore with
  `git checkout HEAD -- encoder_measure encoder_measurements.txt encoder_average_backlash.txt`.
  They are deliberately NOT in `.gitignore`: they look generated but they are
  tracked measurement records, and ignoring them would hide a re-created one.
- **`requierements.txt` (sic) is not a duplicate of `requirements.txt`** and was
  kept for that reason. It is a UTF-16 `pip freeze` of the whole venv — jupyter,
  matplotlib and all — and is the only record of the pinned gclib wheel URL
  (`gclib @ https://www.galil.com/sw/pub/python/gclib-1.0.1-py3-none-any.whl`).
  Rename it to `requirements-freeze.txt` or retire it once that URL is recorded
  somewhere better. `requirements.txt` itself lost `pyqtgraph` on 2026-09-23 —
  nothing has imported it since the dashboard panel was deleted.
- **The last direction starts as an assumption on a fresh install** — see
  `LAST_DIR_ON_START`. It is persisted in `config/last_direction.json` and
  corrected by the first move on each axis, so the assumption only applies until
  then. Solvable properly by measuring the tape origin, which would let position
  alone say which end an axis is on.

## Running

```bat
python run.py --mock              # no hardware required
python run.py --address COM4      # or an IP address

pytest tests/ -v                  # NOTE: pytest is not installed in Software/venv

python test_move_microns_hw.py C          # accuracy/repeatability of move_microns
python test_move_microns_hw.py C --verify # with vs without the verify pass
python test_small_step_tuning.py C --relax-only   # post-move relaxation profile
python test_small_step_tuning.py C        # full KS / AC-DC / SP sweep (~4 min)
python check_link_speed.py                # measure the serial rate (nothing moves)
python check_motor_current.py            # amplifier current vs motor rating (read-only)
python check_limits.py                    # live limit-input view; press a switch and watch
python check_limits.py --once             # one snapshot and exit
python set_micron_origin.py --show        # print both display origins
python set_micron_origin.py               # set the true zero (close the GUI first)
```
