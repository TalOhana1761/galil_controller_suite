# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller spec for the Galil Controller Suite.

Build with ``python packaging/build_installer.py``, not by hand - that script
regenerates the icon, runs this spec and then compiles the Inno Setup installer.

Notes that are easy to get wrong here:

* **One-folder, not one-file.** A one-file build unpacks the whole bundle to a
  temp directory on every launch, and this bundle carries 33 MB of gclib DLLs.
  The installer is what gives the user a single file to double-click, so onefile
  would buy nothing and cost seconds of startup.
* **The gclib DLLs must be declared explicitly.** ``gclib.py`` loads them at
  RUNTIME through ``ctypes``, so PyInstaller's import graph cannot see them and
  would ship a build that works in mock mode and fails the moment someone
  connects hardware. ``utils.app_paths.add_bundled_dll_dir()`` is the other half
  of this: it puts the bundled folder where ``find_library`` will look.
* **config/ ships as read-only defaults.** At runtime it is copied into
  ``%LOCALAPPDATA%`` and written there - see utils/app_paths.py for why.
"""
import os
import sys

HERE = os.path.abspath(os.path.dirname(SPECPATH))       # repo root
PKG = os.path.join(HERE, "packaging")

# gclib's two DLLs, installed system-wide by Galil's own installer. Resolved at
# build time so a machine without gclib produces a mock-only build loudly rather
# than a hardware build that silently cannot connect.
from ctypes.util import find_library

# Target "." (the bundle root), NOT a subfolder: PyInstaller follows the
# dependencies of any binary it is given and puts those at the root, and these
# two depend on each other - so a subfolder target shipped both copies, 33 MB
# wasted. utils.app_paths.add_bundled_dll_dir() looks in both places.
gclib_binaries = []
_missing = []
for _name in ("gclib.dll", "gclibo.dll"):
    _found = find_library(_name)
    if _found and os.path.isfile(_found):
        gclib_binaries.append((_found, "."))
    else:
        _missing.append(_name)
if _missing:
    print(f"*** WARNING: gclib DLL(s) not found: {', '.join(_missing)}. "
          f"The build will run in mock mode only.", file=sys.stderr)

datas = [
    (os.path.join(HERE, "ui", "dark_theme.qss"), "ui"),
    (os.path.join(HERE, "config"), "config"),
]

a = Analysis(
    [os.path.join(HERE, "run.py")],
    pathex=[HERE],
    binaries=gclib_binaries,
    datas=datas,
    # gclib is imported inside a branch of run.py, and pydantic/loguru pull
    # pieces in dynamically. Declared so the graph cannot miss them.
    hiddenimports=["gclib", "pydantic", "pydantic_core", "loguru"],
    hookspath=[],
    runtime_hooks=[],
    # The development venv is shared with unrelated work (jupyter, matplotlib).
    # Excluding them keeps the bundle from inheriting a scientific stack nothing
    # in this app imports.
    # NOTE: numpy is NOT excluded. galil_controller.py imports it for one
    # np.int64() cast on the _TP read path, so excluding it produces a build
    # that runs in --mock and dies with ModuleNotFoundError on real hardware.
    excludes=[
        "tkinter", "matplotlib", "scipy", "pandas", "PIL",
        "IPython", "jupyter", "notebook", "ipykernel",
        "pytest", "_pytest", "PyInstaller",
        "PyQt5.QtWebEngine", "PyQt5.QtWebEngineWidgets", "PyQt5.QtBluetooth",
        "PyQt5.QtQuick", "PyQt5.QtQml", "PyQt5.Qt3DCore", "PyQt5.QtMultimedia",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GalilControllerSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # GUI app - no console window
    disable_windowed_traceback=False,
    icon=os.path.join(PKG, "galil_suite.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GalilControllerSuite",
)
