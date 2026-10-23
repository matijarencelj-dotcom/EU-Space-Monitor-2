"""Monitor orchestration: discover, detect, persist, dashboard, alert.

Daily run (all sources are official JSON APIs — see ep_api.py):
  1. Committee agendas (eMeeting): every meeting of the committee months
     covered by the lookback/lookahead window is checked; published
     agendas (OJ) are scored item by item — future meetings give the
     early heads-up, meetings without a published agenda are watched
     (pending) and retried every run.
  2. Parliamentary questions (Open Data API): questions tabled in the
     lookback window are fetched (offset cursor; new questions are
     appended at the end of the year list) and scored on subject line.
  3. Plenary (Open Data API): server-side full-text search over plenary
     speeches for the configured queries; the debates found are scored
     on their titles.
  4. EUR-Lex / Council documents (CELLAR SPARQL): one query per run
     over the Publications Office knowledge graph; Council
     working-party documents surface here too.
  5. Funding & Tenders Portal (SEDIA search API): published calls and
     tenders — Forthcoming/Open, English — matched on the configured
     terms; the publication-date window is filtered locally because the
     endpoint ignores range filters.
  6. New findings are persisted (deduplicated), the dashboard is
     regenerated and the alert e-mail is sent.

First run: when no state file exists, an agenda backfill over the
configured horizon (default 6 months) runs automatically, using the
eMeeting year archive (every published OJ of the period) before the
daily pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .config import Config
from .detection import excerpt_around, score_text
from .dashboard import render_dashboard, write_dashboard
from .ep_api import (
    KIND_AGENDA,
    KIND_EURLEX,
    KIND_FUNDING,
    KIND_QUESTION,
    KIND_SPEECH,
    ApiClient,
    ApiError,
    HttpUrlGetter,
    archive_url,
    cellar_url,
    events_url,
    ft_multipart_body,
    ft_query,
    ft_search_url,
    oj_url,
    parse_archive,
    parse_cellar,
    parse_events,
    parse_ft,
    parse_oj,
    parse_question,
    parse_speeches,
    pq_detail_url,
    pq_list_url,
    speech_records,
    speeches_url,
)
from .emailer import Mailer
from .state import Finding, StateStore

BACKFILL_OJ_CAP = 400  # safety cap on OJ fetches during a backfill
PLENARY_MAX_PAGES = 5  # safety cap on /speeches pages per query
# Consecutive old questions required before the first pass accepts the
# history-window boundary (tolerates numbering inversions).
PQ_OLD_STREAK = 25
# Raised per-run cap for the one-off re-scan of a year poisoned by the
# pre-2.2.1 catch-up, so the repair finishes in a single daily run.
PQ_RESCAN_MAX_NEW = 2500


@dataclass
class RunResult:
    run_date: date
    agendas_found: int = 0
    questions_checked: int = 0
    speeches_checked: int = 0
    lex_checked: int = 0
    ft_checked: int = 0
    pending_after: int = 0
    new_findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Run of {self.run_date}: {self.agendas_found} agenda(s), "
            f"{self.questions_checked} question(s), {self.speeches_checked} plenary hit(s), "
            f"{self.lex_checked} EUR-Lex/Council document(s) scanned, "
            f"{self.ft_checked} funding call(s)/tender(s) scanned — "
            f"{len(self.new_findings)} new finding(s), "
            f"{self.pending_after} upcoming meeting(s) being watched."
        ]
        lines += [f"  ! {e}" for e in self.errors]
        return "\n".join(lines)


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) pairs covering start..end."""
    out: list[tuple[int, int]] = []
    current = start.replace(day=1)
    end_month = end.replace(day=1)
    while current <= end_month:
        out.append((current.year, current.month))
        current = (current + timedelta(days=32)).replace(day=1)
    return out


class EPCommitteeMonitor:
    def __init__(
        self,
        config: Config,
        client: ApiClient | None = None,
        state: StateStore | None = None,
        mailer: Mailer | None = None,
        clock: Callable[[], date] = date.today,
    ):
        self.config = config
        self.state = state or StateStore(config.state_path, config.findings_path)
        self.mailer = mailer
        self._clock = clock
        if client is None:
            getter = HttpUrlGetter(
                user_agent=config.network.user_agent,
                allowlist=config.network.allowlist,
                retries=config.monitor.max_retries,
            )
            client = ApiClient(getter, delay=config.monitor.request_delay_seconds)
        self.client = client
        # Years whose state was written by the pre-2.2.1 catch-up (which
        # wrongly trusted the Open Data list ordering) and must be
        # re-scanned with a raised per-run cap so the catch-up finishes
        # in a single run.
        self._pq_rescan_years: set[int] = set()

    # -- scoring helpers ----------------------------------------------------

    def _finding(
        self,
        *,
        id: str,
        committee: str,
        kind: str,
        day: str,
        doc_number: int,
        item_index: int,
        title: str,
        text: str,
        url: str,
    ) -> Finding | None:
        """Score a text against the keywords; return a Finding or None."""
        match = score_text(text, self.config.keywords)
        if match is None:
            return None
        return Finding(
            id=id,
            committee=committee,
            kind=kind,
            date=day,
            doc_number=doc_number,
            url=url,
            item_index=item_index,
            title=title,
            excerpt=excerpt_around(text, match.first_pos, self.config.keywords.excerpt_chars),
            score=match.score,
            matched={term: weight for term, _lang, weight in match.matched},
            retrieved_at=_timestamp(),
        )

    def _add_finding(self, finding: Finding, result: RunResult) -> None:
        if self.state.add_finding(finding):
            result.new_findings.append(finding)

    # -- committee agendas (eMeeting) -----------------------------------------

    def _fetch_agenda(self, reference: str) -> object | None:
        """Fetch one OJ; None when the agenda is not published (yet)."""
        url = oj_url(reference, self.config.monitor.language)
        return self.client.get_json(
            url,
            timeout=self.config.monitor.request_timeout_seconds,
            allow_empty=True,
        )

    def _process_agenda(self, data: object, result: RunResult) -> None:
        agenda = parse_oj(data)
        if agenda is None:
            return
        doc_id = f"OJ:{agenda.reference}"
        for item in agenda.items:
            finding = self._finding(
                id=f"{doc_id}#item{item.number}",
                committee=agenda.committee,
                kind=KIND_AGENDA,
                day=agenda.day,
                doc_number=item.number,
                item_index=item.number,
                title=item.title,
                text=item.text,
                url=agenda.source_url,
            )
            if finding:
                self._add_finding(finding, result)
        self.state.mark_processed(doc_id, agenda.source_url)
        result.agendas_found += 1
        if agenda.day:
            self.state.resolve_pending(agenda.committee, agenda.day)

    def _scan_agendas(self, today: date, result: RunResult) -> None:
        """Check every meeting of the window months for each committee."""
        mon = self.config.monitor
        window_start = today - timedelta(days=mon.agenda_lookback_days)
        window_end = today + timedelta(days=mon.agenda_lookahead_days)

        references: list[tuple[str, str, str]] = []  # (reference, day, committee)
        for committee in mon.committees:
            for year, month in _months_between(window_start, window_end):
                try:
                    data = self.client.get_json(
                        events_url(committee, year, month, mon.language),
                        timeout=mon.request_timeout_seconds,
                    )
                except ApiError as exc:
                    result.errors.append(f"{committee} events {year}-{month:02d}: {exc}")
                    continue
                for reference, day in parse_events(data):
                    references.append((reference, day, committee))

        for reference, day, committee in references:
            if self.state.is_processed(f"OJ:{reference}"):
                continue
            try:
                data = self._fetch_agenda(reference)
            except ApiError as exc:
                result.errors.append(f"{committee} agenda {reference}: {exc}")
                continue
            if data is None:
                # Agenda not published yet: watch the meeting and retry
                # on every future run until it shows up (or it goes stale).
                meeting_day = date.fromisoformat(day)
                if meeting_day >= today:
                    self.state.add_pending(committee, meeting_day, reference=reference)
                continue
            try:
                self._process_agenda(data, result)
            except Exception as exc:  # noqa: BLE001 — keep the run alive
                result.errors.append(f"{committee} agenda {reference}: {exc}")

        # Retry watched meetings whose agenda may have appeared later.
        for entry in self.state.pending_items():
            reference = entry.get("reference") or ""
            if not reference or self.state.is_processed(f"OJ:{reference}"):
                continue
            try:
                data = self._fetch_agenda(reference)
            except ApiError as exc:
                result.errors.append(f"{entry.get('committee')} agenda {reference}: {exc}")
                continue
            if data is not None:
                try:
                    self._process_agenda(data, result)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{entry.get('committee')} agenda {reference}: {exc}")

        # Drop meetings that are long past without a published agenda
        # (cancelled meetings, workgroup sessions without OJ, ...).
        dropped = self.state.drop_stale_pending(mon.pending_max_age_days, today)
        if dropped:
            result.errors.append(
                f"{dropped} watched meeting(s) dropped: no agenda published within "
                f"{mon.pending_max_age_days} days"
            )
        result.pending_after = len(self.state.pending_items())

    def _backfill_agendas(self, start: date, end: date, result: RunResult) -> None:
        """Backfill agendas via the eMeeting year archive (published OJs)."""
        mon = self.config.monitor
        fetched = 0
        for committee in mon.committees:
            for year in range(start.year, end.year + 1):
                try:
                    data = self.client.get_json(
                        archive_url(committee, year, mon.language),
                        timeout=mon.request_timeout_seconds,
                    )
                except ApiError as exc:
                    result.errors.append(f"{committee} archive {year}: {exc}")
                    continue
                for reference, day in parse_archive(data):
                    meeting_day = date.fromisoformat(day)
                    if meeting_day < start or meeting_day > end:
                        continue
                    if self.state.is_processed(f"OJ:{reference}"):
                        continue
                    if fetched >= BACKFILL_OJ_CAP:
                        result.errors.append(
                            f"backfill cap of {BACKFILL_OJ_CAP} agendas reached; "
                            "the daily window will pick up the rest"
                        )
                        return
                    try:
                        oj = self._fetch_agenda(reference)
                    except ApiError as exc:
                        result.errors.append(f"{committee} agenda {reference}: {exc}")
                        continue
                    if oj is None:
                        continue
                    fetched += 1
                    try:
                        self._process_agenda(oj, result)
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{committee} agenda {reference}: {exc}")

    # -- parliamentary questions (Open Data API) ------------------------------

    def _scan_questions(self, today: date, result: RunResult) -> None:
        """Check questions tabled within the lookback window.

        The Open Data year list is NOT reliably ordered (live-verified:
        pages come back shuffled and the order differs between calls), so
        list order must never be trusted for recency. Question numbers,
        however, are allocated at tabling time and are chronological, so
        walking identifiers by descending number is a reliable
        newest-first walk. Two modes keep this cheap:

        * steady state (cursor + v2 marker exist): walk forward from the
          cursor to the end of the list — usually one or two requests;
        * first pass (no cursor yet): fetch the whole year with a
          doubling page size (100, 200, 400, ... — ~6 requests for a
          full year), then walk identifiers newest-first (by number)
          until the first-run history window or the per-run cap. The
          history boundary is only accepted after a long run of
          consecutive old questions.

        A cursor written by the pre-2.2.1 catch-up (no ``pq_scan_v2``
        marker) is not trustworthy — the old logic stopped at the first
        old question it happened to meet and marked the rest of the
        year seen. Such years are cleared and re-scanned automatically,
        with a raised cap so the re-scan finishes in one run.
        """
        mon = self.config.monitor
        window_start = today - timedelta(days=mon.questions_lookback_days)
        first_run_start = today - timedelta(days=mon.questions_first_run_days)
        years = sorted({window_start.year, today.year})

        for year in years:
            cursor_key = f"pq_offset_{year}"
            cursor = int(self.state.meta.get(cursor_key, 0))
            if cursor > 0 and not self.state.meta.get(f"pq_scan_v2_{year}"):
                self._reset_question_year(year)
                cursor = 0
                self._pq_rescan_years.add(year)
            if cursor > 0:
                self._scan_questions_from(cursor, year, window_start, today, result)
            else:
                self._scan_questions_first_pass(year, first_run_start, today, result)

    def _reset_question_year(self, year: int) -> None:
        """Forget everything a legacy (pre-2.2.1) catch-up marked for one year."""
        year_str = str(year)
        doomed = [
            key for key in self.state.processed
            if key.startswith("PQ:") and key.split("-")[2:3] == [year_str]
        ]
        for key in doomed:
            del self.state.processed[key]
        self.state.meta.pop(f"pq_offset_{year}", None)

    def _scan_questions_from(
        self, cursor: int, year: int, window_start: date, today: date, result: RunResult
    ) -> None:
        """Steady state: walk forward from the cursor to the end of the
        list. The start is rewound by one page as a safety margin: the
        list order is unstable, so positions can drift between runs.
        Re-seeing processed identifiers is free (state dedupe)."""
        mon = self.config.monitor
        checked = 0
        offset = max(0, cursor - mon.questions_page_size)
        pages = 0
        stop = False
        while pages < mon.questions_max_pages and not stop:
            try:
                page = self.client.get_json(
                    pq_list_url(year, offset, mon.questions_page_size),
                    timeout=mon.request_timeout_seconds,
                )
            except ApiError as exc:
                result.errors.append(f"questions {year} list@{offset}: {exc}")
                break
            ids = _pq_ids(page)
            if not ids:
                break
            pages += 1
            for ident in ids:
                position = _position_after(offset, ids, ident)
                if self.state.is_processed(f"PQ:{ident}"):
                    if position:
                        cursor = max(cursor, position)
                    continue
                if checked >= mon.questions_max_new:
                    stop = True
                    break
                if self._check_question(ident, window_start, result):
                    checked += 1
                    if position:
                        cursor = max(cursor, position)
            offset += len(ids)
        self.state.meta[f"pq_offset_{year}"] = cursor

    def _scan_questions_first_pass(
        self, year: int, first_run_start: date, today: date, result: RunResult
    ) -> None:
        """First pass: fetch the year list once, then walk identifiers
        newest-first by question number (not by list order — the list
        endpoint's ordering is unstable).

        The history-window boundary is only accepted after
        ``PQ_OLD_STREAK`` consecutive old questions; a single old
        question no longer ends the catch-up. Only once the boundary is
        accepted are identifiers *below the oldest scanned number*
        marked seen without fetching — numbers are chronological, so
        those are older still. A capped walk marks nothing extra and
        simply continues on the next daily run.
        """
        mon = self.config.monitor
        offset = 0
        limit = mon.questions_page_size
        all_ids: list[str] = []
        pages = 0
        while pages < mon.questions_max_pages:
            try:
                page = self.client.get_json(
                    pq_list_url(year, offset, limit),
                    timeout=mon.request_timeout_seconds,
                )
            except ApiError as exc:
                result.errors.append(f"questions {year} list@{offset}: {exc}")
                return
            ids = _pq_ids(page)
            if not ids:
                break
            pages += 1
            all_ids.extend(ids)
            offset += len(ids)
            limit *= 2

        ordered = sorted(set(all_ids), key=_pq_number, reverse=True)
        cursor_key = f"pq_offset_{year}"
        max_new = mon.questions_max_new
        if year in self._pq_rescan_years:
            # One-off repair run: the raised cap lets the re-scan of a
            # legacy-poisoned year finish in a single run.
            max_new = max(max_new, PQ_RESCAN_MAX_NEW)
        checked = 0
        old_streak = 0
        floor: int | None = None
        for ident in ordered:
            if self.state.is_processed(f"PQ:{ident}"):
                continue
            if checked >= max_new:
                break  # continue the catch-up on the next daily run
            try:
                detail = self.client.get_json(
                    pq_detail_url(ident),
                    timeout=mon.request_timeout_seconds,
                )
            except ApiError as exc:
                result.errors.append(f"question {ident}: {exc}")
                continue
            checked += 1
            number = _pq_number(ident)
            floor = number if floor is None else min(floor, number)
            question = parse_question(detail, ident)
            if question is None:
                self.state.mark_processed(f"PQ:{ident}", pq_detail_url(ident))
                continue
            self.state.mark_processed(f"PQ:{ident}", question.url)
            if question.day and question.day < first_run_start.isoformat():
                old_streak += 1
                if old_streak >= PQ_OLD_STREAK:
                    break
                continue
            old_streak = 0
            self._score_question(question, result)
            if checked % 250 == 0:
                # A re-scan can fetch thousands of details: checkpoint
                # so an interrupted run does not lose progress.
                self.state.save()
        if old_streak >= PQ_OLD_STREAK and floor is not None:
            # Below the oldest scanned number everything is older still
            # (numbers are chronological): mark it seen without fetching.
            for ident in ordered:
                if _pq_number(ident) < floor and not self.state.is_processed(f"PQ:{ident}"):
                    self.state.mark_processed(f"PQ:{ident}", pq_detail_url(ident))
        if not any(not self.state.is_processed(f"PQ:{ident}") for ident in ordered):
            # Whole year consumed: switch to steady state.
            self.state.meta[cursor_key] = len(all_ids)
            self.state.meta[f"pq_scan_v2_{year}"] = True
        # else: cursor stays 0 — the next run continues the catch-up.
        result.questions_checked += checked

    def _check_question(self, ident: str, window_start: date, result: RunResult) -> bool:
        """Fetch one question; return True when a detail was fetched."""
        try:
            detail = self.client.get_json(
                pq_detail_url(ident),
                timeout=self.config.monitor.request_timeout_seconds,
            )
        except ApiError as exc:
            result.errors.append(f"question {ident}: {exc}")
            return False
        result.questions_checked += 1
        question = parse_question(detail, ident)
        if question is None:
            self.state.mark_processed(f"PQ:{ident}", pq_detail_url(ident))
            return True
        self.state.mark_processed(f"PQ:{ident}", question.url)
        if question.day and question.day < window_start.isoformat():
            return True  # outside the daily window: seen, never scanned again
        self._score_question(question, result)
        return True

    def _score_question(self, question, result: RunResult) -> None:
        text = f"{question.title}\n{question.alt_title}"
        finding = self._finding(
            id=f"PQ:{question.identifier}",
            committee="EP",
            kind=KIND_QUESTION,
            day=question.day,
            doc_number=0,
            item_index=0,
            title=question.title,
            text=text,
            url=question.url,
        )
        if finding:
            self._add_finding(finding, result)


    # -- plenary speeches (Open Data API) --------------------------------------

    def _scan_plenary(self, today: date, result: RunResult) -> None:
        """Debates whose title matches a configured query (Open Data API).

        Live-verified (October 2026): the /speeches endpoint's
        sitting-date filters and sort parameter are broken (HTTP 500 or
        silently ignored), so the scan searches by title only, pages
        with limit/offset and filters the sitting-date window locally.
        """
        mon = self.config.monitor
        window_from = today - timedelta(days=mon.plenary_lookback_days)
        for query in mon.plenary_queries:
            for page in range(PLENARY_MAX_PAGES):
                offset = page * mon.plenary_limit_per_query
                try:
                    data = self.client.get_json(
                        speeches_url(query, mon.plenary_limit_per_query, offset, mon.language),
                        timeout=mon.request_timeout_seconds,
                    )
                except ApiError as exc:
                    result.errors.append(f"plenary search {query!r}: {exc}")
                    break
                records = speech_records(data)
                for hit in parse_speeches(data):
                    try:
                        sitting = date.fromisoformat(hit.day)
                    except ValueError:
                        continue
                    if sitting < window_from or sitting > today:
                        continue
                    if self.state.is_processed(f"PS:{hit.activity_id}"):
                        continue
                    self.state.mark_processed(f"PS:{hit.activity_id}", hit.url)
                    result.speeches_checked += 1
                    finding = self._finding(
                        id=f"PS:{hit.activity_id}",
                        committee="EP",
                        kind=KIND_SPEECH,
                        day=hit.day,
                        doc_number=0,
                        item_index=0,
                        title=hit.title,
                        text=hit.title,
                        url=hit.url,
                    )
                    if finding:
                        self._add_finding(finding, result)
                if len(records) < mon.plenary_limit_per_query:
                    break  # last page reached

    def _scan_eurlex(self, today: date, result: RunResult) -> None:
        """EUR-Lex / CELLAR documents (Publications Office SPARQL).

        One SPARQL query per run covers all configured terms; the date
        window is filtered server-side (string comparison on the ISO
        date literal). Council working-party documents surface here as
        well (consil:/immc: ids) — the register itself has no API.
        """
        mon = self.config.monitor
        if not mon.eurlex_queries:
            return
        date_from = (today - timedelta(days=mon.eurlex_lookback_days)).isoformat()
        try:
            data = self.client.get_json(
                cellar_url(list(mon.eurlex_queries), date_from, mon.eurlex_limit),
                timeout=mon.request_timeout_seconds,
            )
        except ApiError as exc:
            result.errors.append(f"eur-lex cellar search: {exc}")
            return
        for doc in parse_cellar(data):
            try:
                doc_day = date.fromisoformat(doc.day)
            except ValueError:
                continue
            if doc_day > today:
                continue
            key = f"LX:{doc.doc_id}"
            if self.state.is_processed(key):
                continue
            self.state.mark_processed(key, doc.url)
            result.lex_checked += 1
            finding = self._finding(
                id=key,
                committee="EU",
                kind=KIND_EURLEX,
                day=doc.day,
                doc_number=0,
                item_index=0,
                title=doc.title,
                text=doc.title,
                url=doc.url,
            )
            if finding:
                self._add_finding(finding, result)

    def _scan_funding(self, today: date, result: RunResult) -> None:
        """Funding & Tenders Portal — published calls and tenders (SEDIA
        search API; keyless — the portal's own public search backend).

        One POST per configured term; the response is limited server-side
        to calls/tenders (type 0) in Forthcoming/Open status, English
        language. The server ignores range filters and sorting, so the
        publication-date window (ft_lookback_days) is applied locally;
        titles+descriptions are scored against the keyword list.
        """
        mon = self.config.monitor
        if not mon.ft_enabled or not mon.ft_queries:
            return
        date_from = today - timedelta(days=mon.ft_lookback_days)
        body = ft_multipart_body(ft_query())
        for term in mon.ft_queries:
            try:
                data = self.client.post_json(
                    ft_search_url(term, mon.ft_page_size),
                    body,
                    timeout=mon.request_timeout_seconds,
                    headers={"Content-Type": "multipart/form-data; boundary=----epsm-ft"},
                )
            except ApiError as exc:
                result.errors.append(f"funding & tenders search ({term}): {exc}")
                continue
            for call in parse_ft(data):
                try:
                    call_day = date.fromisoformat(call.day)
                except ValueError:
                    continue
                if call_day < date_from or call_day > today:
                    continue
                if self.state.is_processed(call.key):
                    continue
                self.state.mark_processed(call.key, call.url)
                result.ft_checked += 1
                finding = self._finding(
                    id=call.key,
                    committee="EU",
                    kind=KIND_FUNDING,
                    day=call.day,
                    doc_number=0,
                    item_index=0,
                    title=call.title,
                    text=call.text,
                    url=call.url,
                )
                if finding:
                    self._add_finding(finding, result)

    # -- public API ------------------------------------------------------------

    def run(self, dry_run: bool = False, backfill_months: int | None = None) -> RunResult:
        """One monitoring pass. Optionally backfills first."""
        today = self._clock()
        result = RunResult(run_date=today)

        first_run = not self.config.state_path.exists() and not self.state.processed
        if backfill_months is None and first_run and self.config.monitor.first_run_backfill:
            backfill_months = self.config.monitor.backfill_months
        if backfill_months is None or backfill_months <= 0:
            backfill_months = 0
        else:
            self._backfill_agendas(
                today - timedelta(days=30 * backfill_months),
                today + timedelta(days=self.config.monitor.agenda_lookahead_days),
                result,
            )

        self._scan_agendas(today, result)
        if self.config.monitor.questions_enabled:
            self._scan_questions(today, result)
        if self.config.monitor.plenary_enabled:
            self._scan_plenary(today, result)
        if self.config.monitor.eurlex_enabled:
            self._scan_eurlex(today, result)
        self._scan_funding(today, result)

        result.pending_after = len(self.state.pending_items())
        self.state.save()
        self._write_dashboard()

        if result.new_findings and self.config.email.enabled and not dry_run:
            self._send_email(result.new_findings, today)
        return result

    def backfill(self, months: int, dry_run: bool = False) -> RunResult:
        """Explicit backfill over the last N months (agendas)."""
        today = self._clock()
        result = RunResult(run_date=today)
        if months > 0:
            self._backfill_agendas(
                today - timedelta(days=30 * months),
                today + timedelta(days=self.config.monitor.agenda_lookahead_days),
                result,
            )
        self.state.save()
        self._write_dashboard()
        if result.new_findings and self.config.email.enabled and not dry_run:
            self._send_email(result.new_findings, today)
        return result

    # -- outputs ---------------------------------------------------------------

    def _write_dashboard(self) -> None:
        html_text = render_dashboard(
            self.state.findings(),
            site_name=self.config.dashboard.site_name,
            generated_at=_timestamp(),
        )
        write_dashboard(html_text, self.config.dashboard_path)

    def write_dashboard(self) -> None:
        self.state.save()
        self._write_dashboard()

    def _send_email(self, findings: list[Finding], today: date) -> None:
        if self.mailer is None:
            self.mailer = Mailer(self.config.email)
        try:
            self.mailer.send_findings(findings, today.isoformat())
        except Exception as exc:  # noqa: BLE001 — never fail the run on mail issues
            print(f"  ! e-mail alert failed: {exc}")


def _pq_ids(page: object) -> list[str]:
    """Identifiers from a question-list page payload."""
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        return []
    out: list[str] = []
    for record in page["data"]:
        ident = record.get("identifier") or ""
        if ident:
            out.append(ident)
    return out


def _pq_number(ident: str) -> int:
    """Numeric part of a question identifier (0 when unparseable).

    Numbers are allocated at tabling time, so they are chronological —
    unlike the year list, whose ordering is unstable (live-verified:
    2026-10-23, pages returned shuffled and differently between calls).
    """
    try:
        return int(str(ident).rsplit("-", 1)[-1])
    except ValueError:
        return 0


def _position_after(page_offset: int, ids: list[str], ident: str) -> int | None:
    """Absolute list position after ``ident`` within the page at page_offset."""
    try:
        return page_offset + ids.index(ident) + 1
    except ValueError:
        return None


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
