r"""Where the app reads bundled data and writes its own state.

Two very different situations, and the whole point of this module is that only
it knows which one is in force:

**Running from source** (``python run.py``, the test suite, every hardware
script) - nothing changes. ``config/`` is the folder next to the code, exactly
as it has always been, and the log lands in the working directory. This module
must stay invisible to that workflow.

**Running the installed build** (frozen by PyInstaller) - the program lives
under ``C:\Program Files\...``, which is READ-ONLY for a normal user. That
matters more here than in most apps, because this one writes config during
motion: ``last_direction.json`` is rewritten whenever an axis changes direction,
and per CLAUDE.md it is the only record of WHICH END of travel A and B are
sitting at. A silent failure there is a limit-logic failure later. So the
writable state moves to ``%LOCALAPPDATA%\GalilControllerSuite`` and the install
directory is treated as read-only.

The seeding step copies the bundled ``config/`` into that user folder on first
run, so a fresh install starts from the calibration that shipped with it and
then diverges per machine.

Every module that owns a config path keeps its module-level constant
(``SETTINGS_PATH``, ``LAST_DIR_PATH``, ``STATE_PATH``, ...) so the test suite can
still redirect it by monkeypatching - see the "Tests must not touch config/"
note in CLAUDE.md. Only the VALUE those constants are computed from changes.
"""
import os
import shutil
import sys

APP_NAME = "GalilControllerSuite"


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than from source."""
    return bool(getattr(sys, "frozen", False))


def install_root() -> str:
    """Read-only bundled data: the stylesheet, the default config, the DLLs.

    Frozen, this is PyInstaller's extraction dir (``sys._MEIPASS``); from source
    it is the repository root.
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return os.path.abspath(base)
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    """Writable root for everything the app persists.

    From source this is the repository root, so running from a checkout keeps
    using the checkout's own ``config/`` and nothing moves.
    """
    if not is_frozen():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def config_dir() -> str:
    """The folder holding settings.json, zero_offsets.json and friends."""
    return os.path.join(user_data_dir(), "config")


def config_path(name: str) -> str:
    """Absolute path of one config file, e.g. ``config_path('settings.json')``."""
    return os.path.join(config_dir(), name)


def log_path(filename: str = "galil_log.txt") -> str:
    """Where the rotating log sink writes.

    From source this stays relative to the working directory - the historical
    behaviour, and what ``.gitignore``'s ``galil_log*.txt`` rule expects. Frozen,
    it goes to a ``logs`` folder beside the config, because the working
    directory of a desktop shortcut is not something the app controls.
    """
    if not is_frozen():
        return os.path.abspath(filename)
    d = os.path.join(user_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def seed_user_config() -> None:
    """Copy the bundled default config into the user folder on first run.

    Per-file rather than per-folder on purpose: a build that adds a new config
    file should drop it into an existing install too, instead of only ever
    seeding a machine that has never run the app. Existing files are never
    overwritten - that would discard the rig's own calibration on every upgrade.
    """
    if not is_frozen():
        return
    src = os.path.join(install_root(), "config")
    dst = config_dir()
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copy2(s, d)
            except OSError:
                pass


def add_bundled_dll_dir() -> None:
    """Make the gclib DLLs shipped beside the app loadable.

    ``gclib.py`` resolves its two DLLs with ``ctypes.util.find_library``, which
    on Windows searches PATH - it does NOT look next to the executable. So a
    bundled copy is invisible unless PATH says otherwise. Both mechanisms are
    used: ``add_dll_directory`` for the loader's own search, and the PATH entry
    for ``find_library``'s text search.

    Silent when not frozen: from source, gclib is found the way it always was,
    via the system-wide install that Galil's own installer sets up.
    """
    if not is_frozen():
        return
    for sub in ("gclib", "."):
        d = os.path.abspath(os.path.join(install_root(), sub))
        if not os.path.isdir(d):
            continue
        if not any(os.path.isfile(os.path.join(d, n))
                   for n in ("gclib.dll", "gclibo.dll")):
            continue
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
