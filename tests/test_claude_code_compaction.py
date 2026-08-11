from titanium.agents.installed.claude_code import _is_compaction_boundary


def test_top_level_compact_boundary_is_detected():
    # Real Claude Code session artifacts emit the marker as a top-level subtype.
    event = {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": "auto"},
    }
    assert _is_compaction_boundary(event) is True


def test_non_compaction_system_events_are_ignored():
    assert _is_compaction_boundary({"type": "system", "subtype": "init"}) is False
    assert _is_compaction_boundary({"type": "system"}) is False
    # Wrong event type, even with a matching subtype, is not a boundary.
    assert (
        _is_compaction_boundary({"type": "user", "subtype": "compact_boundary"})
        is False
    )
