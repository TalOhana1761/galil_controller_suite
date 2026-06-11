"""
Unit tests for the unit_converter helpers.
Run: pytest tests/test_unit_converter.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from utils.unit_converter import counts_to_um, um_to_counts, format_position


# ── counts_to_um ────────────────────────────────────────────────────────────

def test_counts_to_um_basic():
    assert counts_to_um(1000.0, 100.0) == pytest.approx(10.0)


def test_counts_to_um_fractional():
    assert counts_to_um(1500.0, 200.0) == pytest.approx(7.5)


def test_counts_to_um_zero_counts():
    assert counts_to_um(0.0, 100.0) == pytest.approx(0.0)


def test_counts_to_um_negative():
    assert counts_to_um(-500.0, 100.0) == pytest.approx(-5.0)


def test_counts_to_um_zero_cpm_returns_zero():
    # Zero counts_per_um — avoids division by zero
    assert counts_to_um(999.0, 0.0) == 0.0


# ── um_to_counts ─────────────────────────────────────────────────────────────

def test_um_to_counts_basic():
    assert um_to_counts(10.0, 100.0) == pytest.approx(1000.0)


def test_um_to_counts_fractional():
    assert um_to_counts(7.5, 200.0) == pytest.approx(1500.0)


def test_um_to_counts_zero():
    assert um_to_counts(0.0, 100.0) == pytest.approx(0.0)


def test_um_to_counts_negative():
    assert um_to_counts(-5.0, 100.0) == pytest.approx(-500.0)


# ── round-trip ───────────────────────────────────────────────────────────────

def test_round_trip():
    """counts → µm → counts should be lossless."""
    original = 12345.6
    cpm = 100.0
    assert um_to_counts(counts_to_um(original, cpm), cpm) == pytest.approx(original)


# ── format_position ───────────────────────────────────────────────────────────

def test_format_position_counts():
    result = format_position(1234.5, "counts", 100.0)
    assert "1234.5" in result
    assert "counts" in result


def test_format_position_um():
    result = format_position(1000.0, "um", 100.0)
    assert "10.000" in result
    assert "µm" in result


def test_format_position_negative_um():
    result = format_position(-500.0, "um", 100.0)
    assert "-5.000" in result
