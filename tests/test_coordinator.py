"""Unit tests for the pure data-shaping helpers in coordinator.py.

These don't touch the network or a running HA instance — they test the
math/logic that's easy to get subtly wrong: sign handling, reset-window
detection, and the derived Mains Import/Export split.
"""

from datetime import UTC, datetime, timedelta

from custom_components.emporia_vue.coordinator import (
    add_minute_mains_split,
    apply_api_update_debounce,
    carry_forward_mains_split,
    determine_reset_datetime,
    fix_usage_sign,
    is_in_reset_debounce_window,
)


def test_fix_usage_sign_solar_inverted():
    """Solar channels flip sign when invert_solar is True."""
    assert fix_usage_sign("4", 5.0, False, True, True) == -5.0


def test_fix_usage_sign_solar_not_inverted():
    """Solar channels keep their sign when invert_solar is False."""
    assert fix_usage_sign("4", 5.0, False, True, False) == 5.0


def test_fix_usage_sign_branch_circuit_forced_positive():
    """Non-mains, non-bidirectional branch circuits are always positive."""
    assert fix_usage_sign("4", -5.0, False, False, True) == 5.0


def test_fix_usage_sign_mains_keeps_sign():
    """The combined mains channel keeps its sign (needed for import/export math)."""
    assert fix_usage_sign("1,2,3", -5.0, False, False, True) == -5.0


def test_fix_usage_sign_bidirectional_keeps_sign():
    """Bidirectional channels (e.g. a battery) keep their sign."""
    assert fix_usage_sign("5", -5.0, True, False, True) == -5.0


def test_determine_reset_datetime_day():
    """Day reset is always local midnight."""
    local_time = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
    reset = determine_reset_datetime(local_time, monthly_cycle_start=1, is_month=False)
    assert reset == datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def test_determine_reset_datetime_month_before_cycle_start():
    """Before this month's cycle-start day, the reset is last month's cycle-start."""
    local_time = datetime(2026, 8, 5, tzinfo=UTC)
    reset = determine_reset_datetime(local_time, monthly_cycle_start=15, is_month=True)
    assert reset == datetime(2026, 7, 15, tzinfo=UTC)


def test_determine_reset_datetime_month_after_cycle_start():
    """After this month's cycle-start day, the reset is this month's cycle-start."""
    local_time = datetime(2026, 8, 20, tzinfo=UTC)
    reset = determine_reset_datetime(local_time, monthly_cycle_start=15, is_month=True)
    assert reset == datetime(2026, 8, 15, tzinfo=UTC)


def test_determine_reset_datetime_month_clamps_short_month():
    """A cycle-start day past the end of a short month clamps to the last day."""
    local_time = datetime(2026, 2, 27, tzinfo=UTC)
    reset = determine_reset_datetime(local_time, monthly_cycle_start=31, is_month=True)
    assert reset == datetime(2026, 2, 28, tzinfo=UTC)


def test_is_in_reset_debounce_window_true_just_after_reset():
    """A timestamp a few minutes after reset is inside the debounce window."""
    reset = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    local_time = reset + timedelta(minutes=5)
    assert is_in_reset_debounce_window(local_time, reset, "day") is True


def test_is_in_reset_debounce_window_false_well_after_reset():
    """A timestamp well after reset is outside the debounce window."""
    reset = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    local_time = reset + timedelta(hours=2)
    assert is_in_reset_debounce_window(local_time, reset, "day") is False


def test_apply_api_update_debounce_bounds_inflated_total():
    """During the debounce window, a lower existing value wins over an inflated new total."""
    reset = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    timestamp = reset + timedelta(minutes=5)
    updated = {"a": {"usage": 10.0, "reset": reset, "timestamp": timestamp}}
    existing = {"a": {"usage": 2.0}}
    apply_api_update_debounce(updated, existing, "day")
    assert updated["a"]["usage"] == 2.0


def test_apply_api_update_debounce_leaves_data_outside_window():
    """Outside the debounce window, the updated value is left untouched."""
    reset = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    timestamp = reset + timedelta(hours=2)
    updated = {"a": {"usage": 10.0, "reset": reset, "timestamp": timestamp}}
    existing = {"a": {"usage": 2.0}}
    apply_api_update_debounce(updated, existing, "day")
    assert updated["a"]["usage"] == 10.0


def test_add_minute_mains_split_import():
    """Positive combined-mains usage becomes an Import entry, zero Export."""
    data = {
        "123-1,2,3-1MIN": {
            "device_gid": "123",
            "channel_num": "1,2,3",
            "usage": 500.0,
            "scale": "1MIN",
        }
    }
    add_minute_mains_split(data)
    assert data["123-MainsImport-1MIN"]["usage"] == 500.0
    assert data["123-MainsExport-1MIN"]["usage"] == 0.0


def test_add_minute_mains_split_export():
    """Negative combined-mains usage becomes an Export entry, zero Import."""
    data = {
        "123-1,2,3-1MIN": {
            "device_gid": "123",
            "channel_num": "1,2,3",
            "usage": -300.0,
            "scale": "1MIN",
        }
    }
    add_minute_mains_split(data)
    assert data["123-MainsImport-1MIN"]["usage"] == 0.0
    assert data["123-MainsExport-1MIN"]["usage"] == 300.0


def test_carry_forward_mains_split_preserves_running_totals():
    """Derived Import/Export totals survive a fresh API response that omits them."""
    old_data = {
        "123-MainsImport-1D": {"usage": 12.5},
        "123-MainsExport-1D": {"usage": 3.0},
        "123-1,2,3-1D": {"usage": 9.5},
    }
    new_data = {"123-1,2,3-1D": {"usage": 10.0}}
    carry_forward_mains_split(old_data, new_data)
    assert new_data["123-MainsImport-1D"]["usage"] == 12.5
    assert new_data["123-MainsExport-1D"]["usage"] == 3.0
    # A key already present in new_data (a real API channel) is never overwritten.
    assert new_data["123-1,2,3-1D"]["usage"] == 10.0
