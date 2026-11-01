# Stork Controller (Galil Controller Suite)

PyQt5 GUI for controlling the **Galil DMC-4133** 3-axis servo motor controller used in the Light Injector setup (Axes A, B, C → X, Y, Z).

The manipulator it drives is called **Stork**, so that is the name the operator
sees: window title, Start menu, installer and icon all say **Stork Controller**.
Inside the code it is still the Galil Controller Suite.

---

## How It Works

The application has three layers:

```
GUI (PyQt5 panels)
    ↕  signals / slots
PollingWorker (QThread, 100 ms cycle)
    ↕  command queue
GalilController  ←→  gclib  ←→  DMC-4133 hardware
   or
MockGalilController  (simulated motion, no hardware needed)
```

- **Polling Worker** runs in a background thread and queries all three axes every 100 ms (position, velocity, error, limits, servo state).  
- **GalilController** wraps the `gclib` C library. Every public method maps directly to Galil DMC commands (`JG`, `PA`, `PR`, `ST`, `AB`, `MO`, `SP`, `AC`, `DC`, …).  
- **MockGalilController** is a drop-in replacement that simulates encoder drift and limit switches — no hardware or `gclib` needed.

### Panels

| Panel | Purpose |
|---|---|
| **Connection** | Enter IP / COM port, scan for ports, connect / disconnect; remembers the last port that connected |
| **Settings** | Soft travel limits per axis, in microns from the display zero |
| **Jog** | XY D-pad (click to step, hold to jog), Z column with Retract/Insert Z, keyboard jogging (arrows for X/Y, Page Up/Down for Z), Y/Z direction reversed in software to match the stage, live position readout, limit-switch table, HOME / ZERO, named position memory |
| **Log** | Monospace TX/RX/Error log + raw DMC command input |

The **Dashboard** panel was removed (2026-09-23), and so was the original
Settings panel. A new Settings panel was added on 2026-09-24, but it edits
**only the soft travel limits**. Every other motion parameter (speed, accel,
decel, `counts_per_um`, `travel_mm`) lives in `config/settings.json`, is applied
by **Enable Motors**, and must be edited in that file by hand. See `CLAUDE.md`
for why.

### Stop Buttons (always visible at the bottom)

| Button | DMC command | Effect |
|---|---|---|
| **HALT** | `ST` | Decelerated stop — servos stay energised |
| **E-STOP** | `AB` + `MO` | Immediate abort + servo off — use in emergencies |

---

## Install on Windows (no Python needed)

**This is the easy way, and what most people want.**

1. Get `StorkController-Setup-<version>.exe` (`GalilControllerSuite-Setup-…` before 1.2.0).
2. Run it. Pick an install folder; tick the desktop shortcut.
3. Double-click the desktop icon.

That is all. The installer carries its own Python interpreter, PyQt5, numpy,
pydantic, loguru and the Galil gclib DLLs — **no Python, no pip, no venv and no
terminal on the target machine.** It installs per-user by default, so
administrator rights are not required.

Two Start Menu entries are created:

| entry | what it does |
|---|---|
| **Stork Controller** | connects to real hardware |
| **Stork Controller (simulator)** | `--mock`, no hardware needed |

Installing 1.2.0 over a 1.1.x install upgrades it in place and replaces the old
"Galil Controller Suite" shortcuts.

Settings, calibration and logs are kept in
`%LOCALAPPDATA%\GalilControllerSuite\` (the old name, kept so upgrades keep
your calibration), **not** in the install folder — so the
app works correctly when installed under `C:\Program Files\`, and an uninstall
offers to keep your calibration for a reinstall.

To build that installer yourself, see [`packaging/README.md`](packaging/README.md).

> **Note on the simulator.** The shipped calibration is for this rig, where the
> encoders read in the millions of counts, while the mock controller starts every
> axis at 0. Mock mode therefore opens with all three axes reported at a soft
> travel limit. That is expected — it is the real zero offsets meeting a
> simulated stage, not a fault.

---

## Prerequisites

- Python **3.11 or newer** (tested on 3.12.3)
- Windows (gclib ships as a Windows DLL; mock mode works on any OS)

---

## Running from source (developers)

### Step 1 — Clone the project

```bat
git clone https://github.com/TalOhana1761/galil_controller_suite.git
cd galil_controller_suite
```

> The Galil and Numerik Jena vendor manuals and installers are **not** in this
> repository (they are large and vendor copyrighted). Download gclib and the GDK
> from <https://www.galil.com/sw/pub/galil/> if you need them.

### Step 2 — Install Python dependencies

```bat
cd galil_controller_suite
pip install -r requirements.txt
```

This installs: `PyQt5`, `loguru`, `pydantic`, `pytest`.

---

## Running WITHOUT a Controller (Mock Mode)

Use this on any computer where the Galil hardware is not connected.  
The GUI looks and behaves identically — axes move in simulation.

```bat
python run.py --mock
```

The mock controller simulates:
- Smooth encoder motion toward commanded positions
- Limit switch activation near ±travel bounds
- Servo enable / disable state

---

## Running WITH a Controller

### Step 1 — Install gclib

Download and install the Galil C library from:  
<https://www.galil.com/sw/pub/all/regusr/galil-tools.html>

Then install the Python wheel:

```bat
pip install https://www.galil.com/sw/pub/python/gclib-1.0.1-py3-none-any.whl
```

> If the URL above is outdated, download the `.whl` from the Galil website and run  
> `pip install path\to\gclib-x.x.x-py3-none-any.whl`

### Step 2 — Connect the controller

**Ethernet (recommended)**

```bat
python run.py --address 192.168.0.40
```

Replace `192.168.0.40` with the IP address assigned to your DMC-4133.  
You can find the IP using the **Connection** panel's "Scan Network" button.

**Serial / USB**

```bat
python run.py --address COM3
```

Replace `COM3` with the actual COM port shown in Device Manager.

### Step 3 — First-time setup inside the GUI

1. Open the **Connection** panel and click **Connect**.
2. Click **Enable Motors** in the bottom bar. This is what applies the
   `SP`/`AC`/`DC` values from `config/settings.json` to the controller, along
   with the encoder and limit configuration.
3. Park each axis where you want its working zero and press **ZERO X/Y/Z**.
   Without a zero, **HOME** has nowhere to go and will refuse.
4. Use the **Jog** panel to move axes and confirm motion.

---

## Running the Smoke Test (minimal validation window)

`run_smoke_test.py` is a lightweight window that shows raw position readback and a DMC command box — useful for quickly verifying connectivity without the full GUI.

```bat
# Without hardware
python run_smoke_test.py --mock

# With hardware
python run_smoke_test.py --address 192.168.0.40
```

---

## Running Tests

```bat
pytest tests/ -v
```

All **261 tests across 13 files** should pass (no hardware required — the suite
runs against the mock controller and a fake gclib device).

---

## Project Structure

```
galil_controller_suite/
├── run.py                  ← Main entry point (3-panel GUI)
├── run_smoke_test.py       ← Minimal validation window
├── requirements.txt
│
├── controller/
│   ├── galil_controller.py ← Real hardware interface (gclib wrapper)
│   ├── mock_controller.py  ← Simulated controller (no hardware needed)
│   └── data_models.py      ← Pydantic models (AxisStatus, AppSettings, …)
│
├── ui/
│   ├── main_window.py      ← QMainWindow shell + sidebar navigation
│   ├── polling_worker.py   ← QThread 100 ms polling loop
│   ├── dark_theme.qss      ← Dark stylesheet
│   └── panels/
│       ├── connection_panel.py
│       ├── jog_panel.py
│       └── log_panel.py
│
├── utils/
│   ├── position_memory.py  ← Named position slots (saved to JSON)
│   ├── zero_offsets.py     ← Display origins: counts zero + micron origin
│   ├── unit_converter.py   ← counts ↔ µm conversion helpers
│   └── logger_setup.py     ← Loguru configuration
│
├── config/
│   ├── settings.json       ← Persisted axis parameters
│   └── positions.json      ← Named position memory
│
└── tests/
    ├── test_controller.py
    ├── test_position_memory.py
    └── test_unit_converter.py
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: gclib` | Install gclib (see above). Use `--mock` if no hardware. |
| GUI opens but axes show `---` | Controller not connected — check IP/COM and click Connect. |
| Limit cell stays red | Axis is at a limit; the alert under the table names the direction that escapes. The button driving further in is disabled. |
| `HOME` says "Not zeroed" | That axis has no counts zero yet. Press **ZERO** for it first. |
| Motion parameters seem wrong | Edit `config/settings.json`, then press **Enable Motors** — that is what sends `SP`/`AC`/`DC`. A restart is needed for scale constants. |
| Tests fail | Run `pip install -r requirements.txt` to ensure all deps are present. |
