# Galil Controller Suite

PyQt5 GUI for controlling the **Galil DMC-4133** 3-axis servo motor controller used in the Light Injector setup (Axes A, B, C → X, Y, Z).

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
| **Connection** | Enter IP / COM port, scan network, connect / disconnect |
| **Dashboard** | Live 3-axis position cards + live position history chart + limit alert banner |
| **Jog** | XY D-pad (hold-to-move), Z column, limit-switch table, named position memory |
| **Settings** | Per-axis Speed / Accel / Decel / Error-limit; save to `config/settings.json` |
| **Log** | Monospace TX/RX/Error log + raw DMC command input |

### Stop Buttons (always visible at the bottom)

| Button | DMC command | Effect |
|---|---|---|
| **HALT** | `ST` | Decelerated stop — servos stay energised |
| **E-STOP** | `AB` + `MO` | Immediate abort + servo off — use in emergencies |

---

## Prerequisites

- Python **3.11 or newer** (tested on 3.12.3)
- Windows (gclib ships as a Windows DLL; mock mode works on any OS)

---

## Installation

### Step 1 — Clone / copy the project

Place the `galil_controller_suite` folder anywhere on your machine.

### Step 2 — Install Python dependencies

```bat
cd galil_controller_suite
pip install -r requirements.txt
```

This installs: `PyQt5`, `pyqtgraph`, `loguru`, `pydantic`, `pytest`.

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
2. Open the **Settings** panel and verify Speed / Accel / Decel values, then click **Save**.  
3. Use the **Jog** panel to move axes and confirm motion.

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

All 30 tests should pass (no hardware required — tests use the mock controller).

---

## Project Structure

```
galil_controller_suite/
├── run.py                  ← Main entry point (full 5-panel GUI)
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
│       ├── dashboard_panel.py
│       ├── jog_panel.py
│       ├── settings_panel.py
│       └── log_panel.py
│
├── utils/
│   ├── position_memory.py  ← Named position slots (saved to JSON)
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
| Limit LED stays red | Axis is at a limit; jog in the opposite direction. |
| Settings not saved after restart | Click **Save** in the Settings panel before closing. |
| Tests fail | Run `pip install -r requirements.txt` to ensure all deps are present. |
