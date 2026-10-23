"""Durable state: processed API records, watched meetings and findings.

The state file guarantees that a document is never processed twice and
therefore that alerts are never repeated. Findings are additionally kept
in a JSONL file used to (re)generate the dashboard.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Labels shown on the dashboard and in e-mails, by finding kind.
KIND_LABELS = {
    "OJ": "agenda",
    "PV": "minutes",
    "PQ": "question",
    "PS": "plenary",
    "LX": "eur-lex",
    "FT": "funding",
}


@dataclass(frozen=True)
class Finding:
    """A space-relevant item flagged by one of the sources."""

    id: str
    committee: str
    kind: str  # OJ | PQ | PS (legacy: PV)
    date: str  # ISO meeting / tabling / sitting date
    doc_number: int
    url: str
    item_index: int
    title: str
    excerpt: str
    score: float
    matched: dict[str, float]  # term -> weight
    retrieved_at: str
    kind_label: str = field(default="")

    def __post_init__(self) -> None:
        if not self.kind_label:
            object.__setattr__(self, "kind_label", KIND_LABELS.get(self.kind, self.kind))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "committee": self.committee, "kind": self.kind,
            "date": self.date, "doc_number": self.doc_number, "url": self.url,
            "item_index": self.item_index, "title": self.title, "excerpt": self.excerpt,
            "score": self.score, "matched": self.matched, "retrieved_at": self.retrieved_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            id=data["id"], committee=data["committee"], kind=data["kind"],
            date=data["date"], doc_number=data["doc_number"], url=data["url"],
            item_index=data["item_index"], title=data["title"], excerpt=data["excerpt"],
            score=data["score"], matched=data["matched"], retrieved_at=data["retrieved_at"],
            kind_label=data.get("kind_label", ""),
        )


class StateStore:
    """Processed documents, pending minutes and findings, persisted as JSON.

    Layout:
      * ``state.json``      — processed doc ids, pending minutes, finding ids
      * ``findings.jsonl``  — one JSON object per finding (dashboard source)
    """

    def __init__(self, state_path: str | Path, findings_path: str | Path):
        self.state_path = Path(state_path)
        self.findings_path = Path(findings_path)
        self.processed: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}  # key: COMMITTEE|ISO-date
        self.meta: dict[str, Any] = {}  # e.g. API paging cursors
        self._finding_ids: set[str] = set()
        self._findings: list[Finding] = []
        self._load()

    # -- loading / saving -------------------------------------------------

    def _load(self) -> None:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.processed = raw.get("processed", {})
            self.pending = raw.get("pending", {})
            self.meta = raw.get("meta", {})
            self._finding_ids = set(raw.get("finding_ids", []))
        if self.findings_path.exists():
            for line in self.findings_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._findings.append(Finding.from_dict(json.loads(line)))

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "processed": self.processed,
            "pending": self.pending,
            "finding_ids": sorted(self._finding_ids),
            "meta": self.meta,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

        self.findings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.findings_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(f.to_dict(), ensure_ascii=False) for f in self._findings) + ("\n" if self._findings else ""),
            encoding="utf-8",
        )
        os.replace(tmp, self.findings_path)

    # -- processed documents ----------------------------------------------

    def is_processed(self, doc_id: str) -> bool:
        return doc_id in self.processed

    def mark_processed(self, doc_id: str, url: str) -> None:
        self.processed[doc_id] = {"url": url, "processed_at": _utcnow()}

    # -- pending meetings ---------------------------------------------------
    # Meetings seen in eMeeting whose agenda (OJ) has not been published
    # yet; retried on every run until the agenda shows up.

    @staticmethod
    def _pending_key(committee: str, day: date_cls | str) -> str:
        return f"{committee}|{day if isinstance(day, str) else day.isoformat()}"

    def add_pending(
        self,
        committee: str,
        day: date_cls | str,
        agenda_url: str = "",
        reference: str = "",
    ) -> bool:
        key = self._pending_key(committee, day)
        if key in self.pending:
            return False
        self.pending[key] = {
            "committee": committee,
            "date": day if isinstance(day, str) else day.isoformat(),
            "agenda_url": agenda_url,
            "reference": reference,
            "added_at": _utcnow(),
        }
        return True

    def has_pending(self, committee: str, day: date_cls | str) -> bool:
        return self._pending_key(committee, day) in self.pending

    def pending_items(self) -> list[dict[str, Any]]:
        return list(self.pending.values())

    def resolve_pending(self, committee: str, day: date_cls | str) -> None:
        self.pending.pop(self._pending_key(committee, day), None)

    def drop_stale_pending(self, max_age_days: int, today: date_cls) -> int:
        """Give up on meetings whose agenda never appeared; return count."""
        dropped = 0
        for key in list(self.pending):
            entry = self.pending[key]
            meeting = date_cls.fromisoformat(entry["date"])
            if (today - meeting).days > max_age_days:
                del self.pending[key]
                dropped += 1
        return dropped

    # -- findings -----------------------------------------------------------

    def add_finding(self, finding: Finding) -> bool:
        """Add a finding; return False when it was already known (no repeat alerts)."""
        if finding.id in self._finding_ids:
            return False
        self._finding_ids.add(finding.id)
        self._findings.append(finding)
        return True

    def findings(self) -> list[Finding]:
        return list(self._findings)
