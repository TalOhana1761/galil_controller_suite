"""utils/app_paths.py - where config and logs live, in each of the two worlds.

The whole risk this module carries is that it behaves differently when frozen,
and the frozen branch is the one nobody runs during development. So the frozen
side is exercised here by faking ``sys.frozen``/``sys._MEIPASS`` rather than
being left to the installer to discover.

The other half is a promise: running from source must be COMPLETELY unchanged.
A regression there would move the whole project's config out from under the test
suite and the hardware scripts.
"""
import os
import sys

import pytest

from utils import app_paths


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Pretend we are a PyInstaller build, with both roots under tmp_path."""
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    local = tmp_path / "localappdata"
    local.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return bundle, local


# ── running from source: nothing may move ─────────────────────────────────────

def test_not_frozen_when_running_from_source():
    assert app_paths.is_frozen() is False


def test_config_dir_is_the_repo_config_folder():
    assert app_paths.config_dir() == os.path.join(REPO, "config")


def test_config_path_names_a_file_in_that_folder():
    assert app_paths.config_path("settings.json") == \
        os.path.join(REPO, "config", "settings.json")


def test_install_root_is_the_repo():
    assert app_paths.install_root() == REPO


def test_log_path_stays_relative_to_the_working_directory():
    # The historical behaviour, and what .gitignore's galil_log*.txt expects.
    assert app_paths.log_path("galil_log.txt") == os.path.abspath("galil_log.txt")


def test_seeding_is_a_no_op_from_source(tmp_path, monkeypatch):
    # Must never copy anything over the repo's own config.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    app_paths.seed_user_config()
    assert not (tmp_path / app_paths.APP_NAME).exists()


def test_add_bundled_dll_dir_does_not_touch_path_from_source(monkeypatch):
    before = os.environ.get("PATH", "")
    app_paths.add_bundled_dll_dir()
    assert os.environ.get("PATH", "") == before


# ── frozen: writable state moves out of the install directory ─────────────────

def test_frozen_config_dir_moves_to_localappdata(frozen):
    _, local = frozen
    assert app_paths.config_dir() == os.path.join(
        str(local), app_paths.APP_NAME, "config")


def test_frozen_install_root_is_the_bundle(frozen):
    bundle, _ = frozen
    assert app_paths.install_root() == str(bundle)


def test_frozen_install_root_and_config_dir_are_different_places(frozen):
    # The point of the whole module: the install dir is read-only under
    # Program Files, so config must not live inside it.
    assert app_paths.install_root() != app_paths.config_dir()


def test_frozen_log_goes_to_a_logs_folder(frozen):
    _, local = frozen
    assert app_paths.log_path("galil_log.txt") == os.path.join(
        str(local), app_paths.APP_NAME, "logs", "galil_log.txt")


def test_frozen_log_path_creates_its_directory(frozen):
    p = app_paths.log_path("galil_log.txt")
    assert os.path.isdir(os.path.dirname(p))


# ── seeding ───────────────────────────────────────────────────────────────────

def test_seed_copies_bundled_defaults_on_first_run(frozen):
    bundle, _ = frozen
    (bundle / "config" / "settings.json").write_text('{"a": 1}', encoding="utf-8")

    app_paths.seed_user_config()

    dst = os.path.join(app_paths.config_dir(), "settings.json")
    assert os.path.isfile(dst)
    with open(dst, encoding="utf-8") as f:
        assert f.read() == '{"a": 1}'


def test_seed_never_overwrites_the_rigs_own_calibration(frozen):
    """An upgrade must not reset zero_offsets.json to the shipped defaults."""
    bundle, _ = frozen
    (bundle / "config" / "zero_offsets.json").write_text("SHIPPED", encoding="utf-8")

    os.makedirs(app_paths.config_dir(), exist_ok=True)
    live = os.path.join(app_paths.config_dir(), "zero_offsets.json")
    with open(live, "w", encoding="utf-8") as f:
        f.write("MEASURED ON THIS RIG")

    app_paths.seed_user_config()

    with open(live, encoding="utf-8") as f:
        assert f.read() == "MEASURED ON THIS RIG"


def test_seed_delivers_a_config_file_added_by_a_later_version(frozen):
    """Per-file, not per-folder: an existing install must still get new files."""
    bundle, _ = frozen
    os.makedirs(app_paths.config_dir(), exist_ok=True)
    with open(os.path.join(app_paths.config_dir(), "settings.json"), "w") as f:
        f.write("existing")

    (bundle / "config" / "brand_new.json").write_text("NEW", encoding="utf-8")
    app_paths.seed_user_config()

    assert os.path.isfile(os.path.join(app_paths.config_dir(), "brand_new.json"))


def test_seed_survives_a_bundle_with_no_config_folder(frozen, monkeypatch):
    bundle, _ = frozen
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle / "nope"), raising=False)
    app_paths.seed_user_config()          # must not raise


# ── the bundled DLL directory ─────────────────────────────────────────────────

def test_frozen_dll_dir_is_prepended_to_path_when_the_dlls_are_there(frozen, monkeypatch):
    bundle, _ = frozen
    (bundle / "gclib.dll").write_bytes(b"not really a dll")
    monkeypatch.setenv("PATH", "C:/existing")

    app_paths.add_bundled_dll_dir()

    # Prepended, so it wins over any system-wide gclib install.
    assert os.environ["PATH"].startswith(os.path.abspath(str(bundle)))


def test_frozen_dll_dir_is_skipped_when_no_dlls_are_bundled(frozen, monkeypatch):
    monkeypatch.setenv("PATH", "C:/existing")
    app_paths.add_bundled_dll_dir()
    assert os.environ["PATH"] == "C:/existing"


def test_frozen_dll_dir_found_in_the_gclib_subfolder_too(frozen, monkeypatch):
    # The spec ships them at the root now, but the subfolder layout is still
    # supported so an older build directory keeps working.
    bundle, _ = frozen
    sub = bundle / "gclib"
    sub.mkdir()
    (sub / "gclibo.dll").write_bytes(b"not really a dll")
    monkeypatch.setenv("PATH", "C:/existing")

    app_paths.add_bundled_dll_dir()

    assert os.path.abspath(str(sub)) in os.environ["PATH"]
