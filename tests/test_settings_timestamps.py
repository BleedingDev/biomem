"""Regression coverage for settings-state UTC timestamps."""

import json
import warnings
from datetime import datetime, timezone

from memory_module.settings_manager import SettingsManager


def test_default_timestamps_are_aware_utc_without_deprecation_warnings():
    before = datetime.now(timezone.utc)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        state = SettingsManager._default_state()
    after = datetime.now(timezone.utc)

    for field in ("created_at", "updated_at"):
        parsed = datetime.fromisoformat(state[field])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert before <= parsed <= after


def test_default_timestamps_remain_json_and_iso_8601_compatible():
    state = json.loads(json.dumps(SettingsManager._default_state()))

    created_at = datetime.fromisoformat(state["created_at"])
    updated_at = datetime.fromisoformat(state["updated_at"])

    assert created_at <= updated_at
