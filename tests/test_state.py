"""State store tests: dedupe, pending minutes lifecycle, persistence."""

import json
from datetime import date

from ep_space_monitor.state import Finding, StateStore

MEETING_DAY = date(2026, 10, 21)


def make_finding(fid="ITRE-OJ-2026-10-21-1#item6", **overrides):
    defaults = dict(
        id=fid, committee="ITRE", kind="OJ", date="2026-10-21",
        doc_number=1, url="https://www.europarl.europa.eu/x.html",
        item_index=6, title="Exchange of views on the EU Space Programme",
        excerpt="…EU Space Programme…", score=6.0,
        matched={"eu space programme": 3.5}, retrieved_at="2026-10-22T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_processed_documents(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "findings.jsonl")
    assert not store.is_processed("ITRE-OJ-2026-10-21-1")
    store.mark_processed("ITRE-OJ-2026-10-21-1", "https://example")
    assert store.is_processed("ITRE-OJ-2026-10-21-1")


def test_finding_dedupe_guards_against_repeat_alerts(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "findings.jsonl")
    assert store.add_finding(make_finding()) is True
    assert store.add_finding(make_finding()) is False  # no repeat alert
    assert len(store.findings()) == 1


def test_pending_minutes_lifecycle(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "findings.jsonl")
    assert store.add_pending("ITRE", MEETING_DAY, "https://agenda") is True
    assert store.add_pending("ITRE", MEETING_DAY, "https://agenda") is False
    assert store.has_pending("ITRE", MEETING_DAY)
    assert len(store.pending_items()) == 1

    store.resolve_pending("ITRE", MEETING_DAY)
    assert not store.has_pending("ITRE", MEETING_DAY)


def test_drop_stale_pending(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "findings.jsonl")
    store.add_pending("ITRE", date(2025, 1, 1))
    dropped = store.drop_stale_pending(max_age_days=270, today=date(2026, 10, 22))
    assert dropped == 1
    assert store.pending_items() == []


def test_persistence_roundtrip(tmp_path):
    state_path = tmp_path / "state.json"
    findings_path = tmp_path / "findings.jsonl"
    store = StateStore(state_path, findings_path)
    store.mark_processed("ITRE-PV-2026-10-21-1", "https://x")
    store.add_finding(make_finding())
    store.add_pending("SEDE", "2026-10-20")
    store.save()

    reloaded = StateStore(state_path, findings_path)
    assert reloaded.is_processed("ITRE-PV-2026-10-21-1")
    assert len(reloaded.findings()) == 1
    assert reloaded.findings()[0].title.startswith("Exchange of views")
    assert reloaded.has_pending("SEDE", "2026-10-20")

    lines = findings_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "ITRE-OJ-2026-10-21-1#item6"
