r"""Build the Windows installer, end to end.

    python packaging/build_installer.py
    python packaging/build_installer.py --version 1.1.0
    python packaging/build_installer.py --skip-app     # only recompile the .iss

Three stages:

1. ``make_icon.py``    - regenerate stork.ico
2. ``PyInstaller``     - freeze the app into packaging/_dist/StorkController
3. ``ISCC.exe``        - wrap that folder into a single setup .exe

The result lands in ``packaging/_output``. That file is the ONLY thing a peer
needs: it carries the Python interpreter, PyQt5, numpy, pydantic, loguru and the
gclib DLLs, so the target machine needs no Python, no pip and no venv.

Prerequisites on the BUILD machine (not on the peer's):
  * pip install pyinstaller
  * Inno Setup 6 - https://jrsoftware.org/isdl.php
  * Galil gclib installed, so its DLLs can be bundled. Without it the build
    still succeeds but is mock-only, and says so loudly.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(HERE, "_dist")
WORK = os.path.join(HERE, "_build")
OUT = os.path.join(HERE, "_output")

DEFAULT_VERSION = "1.0.0"

# The PyInstaller folder/exe name and the setup file's prefix. Must match
# name= in galil_suite.spec and MyAppExeName in installer.iss.
APP_DIR = "StorkController"

# Inno Setup installs per-user or per-machine depending on how it was run, so
# look in both. The per-user path is what a non-admin install produces.
ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def find_iscc() -> str:
    for c in ISCC_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    if found:
        return found
    raise SystemExit(
        "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php\n"
        "Looked in:\n  " + "\n  ".join(c for c in ISCC_CANDIDATES if c))


def ensure_dist_writable() -> None:
    """Clear the previous build, with a useful message when Windows says no.

    PyInstaller wipes its output directory before writing, and on Windows that
    fails if ANYTHING holds the folder - the app still running from a previous
    test, an Explorer window sitting in it, or a shell whose working directory
    is inside it. The raw error is a bare WinError 32 several frames deep in
    shutil, which does not hint at any of that.
    """
    app_dir = os.path.join(DIST, APP_DIR)
    if not os.path.isdir(app_dir):
        return
    try:
        shutil.rmtree(app_dir)
    except PermissionError as e:
        raise SystemExit("\n".join([
            "Cannot clear the previous build:",
            f"  {app_dir}",
            "",
            f"  {e}",
            "",
            "Something is holding that folder. Usually one of:",
            f"  * {APP_DIR}.exe is still running - close it",
            "  * an Explorer window or terminal is sitting inside that folder",
            "  * an antivirus scan is still reading the files",
        ]))


def run(cmd, cwd=None, what=""):
    print(f"\n=== {what} ===\n$ {' '.join(cmd)}\n", flush=True)
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise SystemExit(f"{what} FAILED (exit {r.returncode})")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Stork Controller installer")
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help=f"version stamped into the installer (default {DEFAULT_VERSION})")
    ap.add_argument("--skip-app", action="store_true",
                    help="reuse the existing PyInstaller output; only recompile the installer")
    ap.add_argument("--clean", action="store_true",
                    help="delete previous build artifacts first")
    args = ap.parse_args()

    py = sys.executable
    iscc = find_iscc()

    if args.clean:
        for d in (DIST, WORK, OUT):
            shutil.rmtree(d, ignore_errors=True)
        print("cleaned previous artifacts")

    if not args.skip_app:
        ensure_dist_writable()
        run([py, os.path.join(HERE, "make_icon.py")], what="1/3  icon")
        run([py, "-m", "PyInstaller", os.path.join(HERE, "galil_suite.spec"),
             "--noconfirm", "--distpath", DIST, "--workpath", WORK, "--log-level", "WARN"],
            cwd=ROOT, what="2/3  freeze the app (PyInstaller)")
    else:
        print("skipping icon + PyInstaller (--skip-app)")

    app_dir = os.path.join(DIST, APP_DIR)
    if not os.path.isdir(app_dir):
        raise SystemExit(f"PyInstaller output missing: {app_dir}")

    # A build without the gclib DLLs runs in mock mode only. Say so here rather
    # than letting someone discover it when they plug in a controller.
    # Look where add_bundled_dll_dir() looks at runtime: the spec puts them at
    # the top of _internal ("."), older builds used a gclib/ subfolder. Checking
    # only gclib/ printed MOCK ONLY on builds that were fine (1.1.2).
    internal = os.path.join(app_dir, "_internal")
    if not any(os.path.isfile(os.path.join(internal, sub, n))
               for sub in ("gclib", ".") for n in ("gclib.dll", "gclibo.dll")):
        print("\n*** WARNING: no gclib DLLs bundled - this installer is MOCK ONLY. ***\n",
              file=sys.stderr)

    os.makedirs(OUT, exist_ok=True)
    run([iscc, f"/DMyAppVersion={args.version}", os.path.join(HERE, "installer.iss")],
        cwd=HERE, what="3/3  build the installer (Inno Setup)")

    setup = os.path.join(OUT, f"{APP_DIR}-Setup-{args.version}.exe")
    if not os.path.isfile(setup):
        raise SystemExit(f"expected installer not produced: {setup}")

    print("\n" + "=" * 68)
    print("  INSTALLER READY")
    print("  " + setup)
    print(f"  {human(os.path.getsize(setup))} - send this one file to your peers.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
