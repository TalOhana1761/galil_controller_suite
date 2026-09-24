# Building the Windows installer

Produces a single `GalilControllerSuite-Setup-<version>.exe`. That one file is
everything a peer needs — it carries the Python interpreter, PyQt5, numpy,
pydantic, loguru and the gclib DLLs, so **the target machine needs no Python, no
pip, no venv and no terminal.**

```bat
python packaging\build_installer.py
```

Output lands in `packaging\_output\`.

## What the peer gets

A normal Windows wizard: pick an install folder, optional desktop shortcut, then
a working app. Two Start Menu entries are created —

| entry | runs |
|---|---|
| Galil Controller Suite | `GalilControllerSuite.exe` (real hardware) |
| Galil Controller Suite (simulator) | `GalilControllerSuite.exe --mock` |

Plus an uninstaller in Add/Remove Programs.

## Prerequisites on the BUILD machine

Only here — not on the peer's machine.

| tool | install |
|---|---|
| PyInstaller | `pip install pyinstaller` |
| Inno Setup 6 | <https://jrsoftware.org/isdl.php> (a per-user install is fine) |
| Galil gclib | Galil's own installer, so its DLLs can be bundled |

If gclib is missing the build still succeeds but is **mock only**, and both the
spec and the build script say so on stderr rather than letting you find out when
a controller is plugged in.

## The files

| file | does |
|---|---|
| `build_installer.py` | runs all three stages; the only command you need |
| `make_icon.py` | draws `galil_suite.ico` (tracked, regenerated each build) |
| `galil_suite.spec` | PyInstaller: what to freeze, bundle and exclude |
| `installer.iss` | Inno Setup: the wizard, shortcuts and uninstaller |

`_dist/`, `_build/` and `_output/` are build artifacts and gitignored.

## Where the installed app keeps its state

**Not in the install directory.** Under `C:\Program Files\` that folder is
read-only for a normal user, and this app writes config *during motion* —
`last_direction.json` is rewritten whenever an axis changes direction, and per
`CLAUDE.md` it is the only record of which end of travel A and B are sitting at.

So `utils/app_paths.py` redirects, but **only when frozen**:

| | running from source | installed build |
|---|---|---|
| config | `<repo>\config\` | `%LOCALAPPDATA%\GalilControllerSuite\config\` |
| log | working directory | `%LOCALAPPDATA%\GalilControllerSuite\logs\` |
| read-only data | `<repo>\` | the bundle (`_internal\`) |

Running from a checkout is therefore **completely unchanged** — the test suite,
the hardware scripts and `python run.py` all behave exactly as before.

On first launch the bundled `config\` is copied into the user folder, per file.
Existing files are never overwritten, so upgrading keeps the rig's calibration
while still delivering any config file a new version adds.

## Two things that are easy to break

**The gclib DLLs are invisible to PyInstaller.** `gclib.py` loads them at runtime
through `ctypes.util.find_library`, which searches `PATH` and does *not* look
beside the executable. So they are declared explicitly in the spec, and
`app_paths.add_bundled_dll_dir()` puts their folder on `PATH` before
`galil_controller` is imported. Break either half and you get a build that runs
in `--mock` and fails the moment it talks to hardware.

**Declare the gclib DLLs at the bundle ROOT, not in a subfolder.** PyInstaller
follows the dependencies of any binary it is handed and writes those to the root
of `_internal`. Since `gclib.dll` and `gclibo.dll` depend on each other, giving
them a subfolder target shipped *both* copies - 33 MB of duplicate DLL. The spec
targets `"."` for exactly this reason.

**`numpy` is not optional.** `galil_controller.py` imports it for a single
`np.int64()` cast on the `_TP` read path. It was excluded from the spec during
development and produced exactly that failure. There is a comment in the spec
saying so; if the cast is ever replaced with a plain `int()`, numpy can be
dropped from both the spec and `requirements.txt`.

## Bundle size

Roughly 65 MB for the installer, ~190 MB installed. The large items are PyQt5
(~76 MB), the gclib DLLs (~33 MB) and numpy's bundled OpenBLAS (~21 MB).

Pillow is excluded in the spec: it gets pulled in transitively and nothing in
this app uses it, which is 13 MB saved.

The OpenBLAS is there only because of the single `np.int64()` cast mentioned
above - replacing it with `int()` would drop numpy entirely and take about
25 MB with it. Not done, because it is a change to the `_TP` read path on real
hardware and wants testing on the rig rather than a packaging-driven edit.

## Version

`--version` stamps the installer name and the Add/Remove Programs entry:

```bat
python packaging\build_installer.py --version 1.1.0
```

`AppId` in `installer.iss` is a fixed GUID and must **not** change between
releases — it is how Windows recognises an upgrade rather than a second parallel
install.

## Faster iteration

```bat
python packaging\build_installer.py --skip-app    :: only recompile the .iss
python packaging\build_installer.py --clean       :: wipe artifacts first
```
