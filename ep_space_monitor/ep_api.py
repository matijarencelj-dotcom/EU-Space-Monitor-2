"""EP official API clients: discovery through JSON APIs only.

The original doceo URL probing against www.europarl.europa.eu stopped
working: EP bot protection answers every automated request from
datacenter IPs (GitHub Actions runners included) with HTTP 202 and an
empty body, which silently produced zero findings. Discovery is now
rebuilt on two official JSON APIs, neither of which applies that block:

eMeeting — committee meeting agendas (OJ)
    base: https://emeeting.europarl.europa.eu/emeeting/plmrep/
    * ``agenda/agendaArchive?language=en&year=Y&organ=COMM``
      every published OJ reference of a committee for a whole year
      (used for backfill; dates are day-first, e.g. "22.10.2026").
    * ``meeting/events/eventsByMonth?language=en&year&month&organ``
      all meetings of a month, including future ones whose agenda has
      not been published yet (heads-up window).
    * ``OJ/oj?language=en&reference=REF&securedContext=false``
      the full agenda: numbered items plus attached documents.
      An unpublished agenda returns HTTP 200 with an empty body.

EP Open Data API v2 — questions and plenary
    base: https://data.europarl.europa.eu/api/v2/  (JSON-LD; 500 req/5min)
    * ``parliamentary-questions?year=Y&offset&limit``
      question identifiers for a year, ascending; new questions are
      appended at the end, so a stored offset cursor reaches them in a
      couple of requests. Details (``/parliamentary-questions/{id}``)
      carry the subject line, the alternative title (author, addressee)
      and direct document links.
    * ``speeches?title=Q&search-language=en&limit&offset``
      server-side search over plenary debate titles; the response gives
      the debate each match belongs to (activity id, sitting date,
      label), which is scored locally against the keyword list. The
      endpoint's own sitting-date filters and sort parameter are broken
      (HTTP 500 or ignored — live-verified), so paging is done with
      limit/offset and the date window is filtered locally.

Publications Office CELLAR — EUR-Lex / Council documents (SPARQL)
    endpoint: https://publications.europa.eu/webapi/rdf/sparql (no auth)
    * ``cellar_url(terms, date_from, limit)`` builds one SPARQL query
      over the CELLAR knowledge graph: works whose expression titles
      full-text-match the terms, dated since ``date_from``. Live-verified
      quirk: works are identified by cellar-URI subjects (the celex URI
      is only an alias), titles live on expressions
      (``cdm:expression_belongs_to_work``; there is no
      ``work_has_expression`` in the store) and ``work_date_document``
      is a plain literal, so the date filter compares strings. The same
      document is loaded once per source system (celex:, oj:, eli:,
      immc:, consil:, genpub: …) — parse_cellar deduplicates and prefers
      the CELEX view. Council working-party documents surface here too
      (consil:/immc: ids), which is the machine-readable path to Council
      output the monitor otherwise could not reach.

Every network error is raised loudly (never swallowed as "no document").
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote, quote_plus, urlencode, urlparse

from .allowlist import assert_allowed

EMEETING_API = "https://emeeting.europarl.europa.eu/emeeting/plmrep/"
EMEETING_SITE = "https://emeeting.europarl.europa.eu/emeeting/"
OPENDATA_API = "https://data.europarl.europa.eu/api/v2/"
OPENDATA_DIST = "https://data.europarl.europa.eu/"
CELLAR_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_BASE = "https://eur-lex.europa.eu/"
OP_PORTAL_BASE = "https://op.europa.eu/en/publication-detail/-/publication/"

# Finding kinds produced by the sources in this module.
KIND_AGENDA = "OJ"
KIND_QUESTION = "PQ"
KIND_SPEECH = "PS"
KIND_EURLEX = "LX"

# Statuses that indicate EP bot protection rather than a missing document.
BLOCKED_STATUSES = (202, 403, 429)

# The Open Data API negotiates the response format via the Accept header:
# without "application/ld+json" it serves RDF/XML, which fails JSON parsing.
OPENDATA_HOST = "data.europarl.europa.eu"

# The CELLAR SPARQL endpoint answers JSON only for this Accept header.
CELLAR_HOST = "publications.europa.eu"


def accept_header_for(url: str) -> str:
    """MIME type to request for ``url`` (host-specific, live-verified)."""
    host = urlparse(url).netloc.split("@")[-1].split(":")[0].lower()
    if host == OPENDATA_HOST:
        return "application/ld+json"
    if host == CELLAR_HOST:
        return "application/sparql-results+json"
    return "application/json"


# ---------------------------------------------------------------------------
# URL builders (pure functions, unit-tested against the live API shapes)
# ---------------------------------------------------------------------------


def archive_url(organ: str, year: int, language: str = "en") -> str:
    """Year archive of published OJ references for one committee."""
    return (
        f"{EMEETING_API}agenda/agendaArchive"
        f"?language={quote(language.lower())}&year={year}&organ={quote(organ)}"
    )


def events_url(organ: str, year: int, month: int, language: str = "en") -> str:
    """Meetings of one committee-month (includes future, unpublished OJs)."""
    return (
        f"{EMEETING_API}meeting/events/eventsByMonth"
        f"?language={quote(language.lower())}&year={year}&month={month}&organ={quote(organ)}"
    )


def oj_url(reference: str, language: str = "en") -> str:
    """Full agenda (OJ) for a meeting reference such as ITRE(2026)1008_1."""
    return (
        f"{EMEETING_API}OJ/oj"
        f"?language={quote(language.lower())}&reference={quote(reference)}&securedContext=false"
    )


def pq_list_url(year: int, offset: int = 0, limit: int = 100) -> str:
    """Parliamentary-question identifiers for a year (ascending order)."""
    return f"{OPENDATA_API}parliamentary-questions?year={year}&offset={offset}&limit={limit}"


def pq_detail_url(identifier: str) -> str:
    """One question: subject line, alternative title and document links."""
    return f"{OPENDATA_API}parliamentary-questions/{quote(identifier)}"


def speeches_url(
    query: str,
    limit: int,
    offset: int = 0,
    language: str = "en",
) -> str:
    """Debates whose title matches ``query`` (Open Data ``/speeches``).

    Live-verified (October 2026): the ``sitting-date`` filters and the
    ``sort-by`` parameter of this endpoint are broken — they either
    fail with HTTP 500 or are silently ignored — so only title search
    with ``limit``/``offset`` paging is used. The caller filters the
    sitting-date window locally.
    """
    return (
        f"{OPENDATA_API}speeches?title={quote_plus(query)}"
        f"&search-language={quote(language.lower())}"
        f"&limit={limit}&offset={offset}"
    )


def speech_record_url(activity_id: str) -> str:
    """Stable API link for one plenary speech/debate record."""
    return f"{OPENDATA_API}speeches/{quote(activity_id)}"


def pq_document_url(distribution_path: str) -> str:
    """Absolute download URL for a question's document (docx/pdf)."""
    return f"{OPENDATA_DIST}{distribution_path.lstrip('/')}"


def _cellar_sparql(terms: list[str], date_from: str, limit: int) -> str:
    """SPARQL over the CELLAR knowledge graph (shape live-verified).

    Works are cellar-URI subjects; titles live on the expressions that
    belong to them and ``work_date_document`` is a plain ISO-date
    literal, so the date window is compared as a string.
    """
    phrase = " or ".join(f"'{term}'" for term in terms)
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?work ?id ?date ?title WHERE {\n"
        "  ?expr cdm:expression_belongs_to_work ?work .\n"
        "  ?expr cdm:expression_title ?title .\n"
        f"  ?title bif:contains \"{phrase}\" .\n"
        "  FILTER(lang(?title) = \"en\")\n"
        "  ?work cdm:work_id_document ?id .\n"
        "  ?work cdm:work_date_document ?date .\n"
        f"  FILTER(STR(?date) >= \"{date_from}\")\n"
        "}\n"
        "ORDER BY DESC(STR(?date))\n"
        f"LIMIT {limit}\n"
    )


def cellar_url(terms: list[str], date_from: str, limit: int = 100) -> str:
    """One SPARQL query URL: CELLAR works matching ``terms`` since ``date_from``."""
    safe_terms = [
        term.replace("'", "").replace('"', "").replace("\\", "").strip()
        for term in terms if term.strip()
    ]
    query = _cellar_sparql(safe_terms, date_from, limit)
    return f"{CELLAR_SPARQL}?{urlencode({'query': query, 'format': 'application/sparql-results+json'})}"


def lex_doc_url(doc_id: str, work_uri: str) -> str:
    """Best human link for a CELLAR work (live-verified patterns)."""
    if doc_id.startswith("celex:"):
        return f"{EURLEX_BASE}legal-content/EN/TXT/?uri=CELEX:{quote(doc_id[len('celex:'):])}"
    if doc_id.startswith("eli:"):
        return f"{EURLEX_BASE}legal-content/EN/TXT/?uri={quote_plus(doc_id)}"
    uuid = work_uri.rstrip("/").rsplit("/cellar/", 1)[-1]
    return f"{OP_PORTAL_BASE}{quote(uuid)}"


# ---------------------------------------------------------------------------
# Data types returned by the parsers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgendaItem:
    """A numbered agenda item with the text used for scoring."""

    number: int
    title: str
    text: str  # title + attached document titles (+ dossier reference)


@dataclass(frozen=True)
class Agenda:
    """A full committee agenda (OJ)."""

    reference: str
    committee: str
    day: str  # ISO date of the meeting
    items: tuple[AgendaItem, ...]
    source_url: str  # EN PDF of the agenda (or the eMeeting site)


@dataclass(frozen=True)
class Question:
    """A parliamentary question with its EN metadata."""

    identifier: str
    day: str  # ISO date the question was tabled
    title: str  # subject line
    alt_title: str  # "Question for written answer E-000002/2026 - to the ..."
    url: str  # direct document link (docx) or the API record URL


@dataclass(frozen=True)
class LexDoc:
    """One document found in the CELLAR knowledge graph (EUR-Lex view)."""

    work: str
    doc_id: str
    day: str
    title: str

    @property
    def url(self) -> str:
        return lex_doc_url(self.doc_id, self.work)


@dataclass(frozen=True)
class SpeechHit:
    """A plenary search hit: the debate a matching speech belongs to."""

    activity_id: str
    day: str  # ISO sitting date
    title: str  # debate label (EN)
    url: str


# ---------------------------------------------------------------------------
# Parsers (pure functions over the JSON payloads)
# ---------------------------------------------------------------------------


def epoch_ms_to_day(ms: int | float | None) -> str | None:
    """Convert epoch milliseconds to an ISO date string."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _day_first_date(text: str) -> str | None:
    """Convert eMeeting "22.10.2026" into an ISO date."""
    try:
        day, month, year = text.strip().split(".")
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except (AttributeError, ValueError):
        return None


def parse_archive(data: object) -> list[tuple[str, str]]:
    """[(reference, ISO day)] for every published OJ in a year archive."""
    out: list[tuple[str, str]] = []
    if not isinstance(data, list):
        return out
    for year_block in data:
        for month_block in year_block.get("months", []) or []:
            for oj in month_block.get("ojs", []) or []:
                reference = oj.get("ojReference") or ""
                day = _day_first_date(oj.get("date") or "")
                if reference and day:
                    out.append((reference, day))
    return out


def parse_events(data: object) -> list[tuple[str, str]]:
    """[(meetingReference, ISO day)] for every meeting in an events month."""
    out: list[tuple[str, str]] = []
    if not isinstance(data, list):
        return out
    for event in data:
        reference = event.get("meetingReference") or ""
        day = epoch_ms_to_day(event.get("start"))
        if reference and day:
            out.append((reference, day))
    return out


def _en_document_link(doc: dict) -> str | None:
    """First EN pdf/docx link of an eMeeting document entry."""
    for link in doc.get("documentLinks") or []:
        for lang in link.get("languages") or []:
            if lang.get("code") == "EN" and lang.get("url"):
                return lang["url"]
    return None


def _item_text(item: dict) -> str:
    """Agenda item title plus attached document titles (scoring input)."""
    parts: list[str] = [item.get("title") or ""]
    if item.get("dossierReference"):
        parts.append(item["dossierReference"])
    for doc_set in item.get("documentSets") or []:
        for doc in doc_set.get("documents") or []:
            if doc.get("geproCode") == "OJ":
                continue  # the agenda document itself adds no signal
            if doc.get("title"):
                parts.append(doc["title"])
    return " ".join(p for p in parts if p).strip()


def parse_oj(data: object, fallback_url: str = "") -> Agenda | None:
    """Build an Agenda from an OJ payload; None when it is empty/absent.

    Items without a number are headings and separators ("*** Electronic
    vote ***", sitting times) and are skipped.
    """
    if not isinstance(data, dict) or not data.get("items"):
        return None
    reference = data.get("meetingReference") or ""
    committee = reference.split("(")[0] if "(" in reference else ""
    day = epoch_ms_to_day(data.get("date")) or ""

    items: list[AgendaItem] = []
    for item in data.get("items", []):
        number = item.get("number")
        if number is None:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        items.append(
            AgendaItem(number=int(number), title=title, text=_item_text(item) or title)
        )

    oj_doc = data.get("ojDocument") or {}
    source_url = _en_document_link(oj_doc) or fallback_url or EMEETING_SITE
    return Agenda(
        reference=reference,
        committee=committee,
        day=day,
        items=tuple(items),
        source_url=source_url,
    )


def _language_value(mapping: object) -> str:
    """Best value from a language-tagged dict: English, else the first present.

    Some questions (rarely) have no English expression; the Open Data API
    still returns the title in whatever language exists. Prefer "en", then
    fall back to the first non-empty language instead of dropping the record.
    """
    if not isinstance(mapping, dict):
        return ""
    value = mapping.get("en")
    if value:
        return value
    for candidate in mapping.values():
        if candidate:
            return candidate
    return ""


def _expression_titles(expression: dict) -> tuple[str, str]:
    title = _language_value(expression.get("title"))
    alt = _language_value(expression.get("title_alternative"))
    return title, alt


def _expression_document_url(expression: dict) -> str | None:
    for manifestation in expression.get("is_embodied_by") or []:
        if manifestation.get("is_exemplified_by"):
            return pq_document_url(manifestation["is_exemplified_by"])
    return None


def parse_question(data: object, identifier: str = "") -> Question | None:
    """Build a Question from a detail payload; None when unusable."""
    if not isinstance(data, dict) or not data.get("data"):
        return None
    record = data["data"]
    if isinstance(record, list):
        if not record:
            return None
        record = record[0]

    ident = record.get("identifier") or identifier
    day = record.get("document_date") or ""
    title, alt = "", ""
    url = None
    expressions = record.get("is_realized_by") or []
    for expression in expressions:
        lang = (expression.get("language") or "").rsplit("/", 1)[-1].upper()
        if lang != "EN":
            continue
        title, alt = _expression_titles(expression)
        url = _expression_document_url(expression)
        break
    if not title:  # fall back to any language before giving up
        for expression in expressions:
            title, alt = _expression_titles(expression)
            url = _expression_document_url(expression)
            if title:
                break
    if not ident or not title:
        return None
    return Question(
        identifier=ident,
        day=day,
        title=title,
        alt_title=alt,
        url=url or pq_detail_url(ident),
    )


def speech_records(data: object) -> list[dict]:
    """Raw records of one search page (used for paging decisions)."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return []
    return [record for record in data["data"] if isinstance(record, dict)]


def parse_speeches(data: object) -> list[SpeechHit]:
    """Speech search hits -> debates (deduplicated by activity)."""
    out: list[SpeechHit] = []
    seen: set[str] = set()
    for record in speech_records(data):
        activity_id = record.get("activity_id") or ""
        if not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        labels = record.get("activity_label") or {}
        title = labels.get("en") or next(iter(labels.values()), "")
        day = record.get("activity_date") or ""
        if not title or not day:
            continue
        out.append(
            SpeechHit(
                activity_id=activity_id,
                day=day,
                title=title,
                url=speech_record_url(activity_id),
            )
        )
    return out


# Same document, one work per source system; prefer the EUR-Lex view.
_ID_PREFERENCE = ("celex:", "eli:", "oj:", "immc:", "consil:", "genpub:")


def parse_cellar(data: object) -> list[LexDoc]:
    """SPARQL results -> documents, deduplicated across source systems.

    CELLAR stores one work per source system (celex:, oj:, eli:, immc:,
    consil:, genpub: …) for the same document; each comes with its own
    id and title. Rows are grouped by (title, date) and the CELEX view
    is preferred so a document appears once, with its stable identifier.
    """
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        return []
    bindings = data["results"].get("bindings")
    if not isinstance(bindings, list):
        return []
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        def value(name: str) -> str:
            node = binding.get(name)
            return node.get("value", "") if isinstance(node, dict) else ""
        title, day, work, doc_id = value("title"), value("date"), value("work"), value("id")
        if not (title and day[:4].isdigit() and work and doc_id):
            continue
        groups.setdefault((title, day), []).append((doc_id, work))
    out: list[LexDoc] = []
    for (title, day), candidates in groups.items():
        def rank(item: tuple[str, str]) -> tuple[int, str]:
            doc_id = item[0]
            for i, prefix in enumerate(_ID_PREFERENCE):
                if doc_id.startswith(prefix):
                    return (i, doc_id)
            return (len(_ID_PREFERENCE), doc_id)
        doc_id, work = sorted(candidates, key=rank)[0]
        out.append(LexDoc(work=work, doc_id=doc_id, day=day[:10], title=title))
    return out


# ---------------------------------------------------------------------------
# Getter + client
# ---------------------------------------------------------------------------


class UrlGetter(Protocol):
    """Anything that can perform a GET and return (status, body, content-type)."""

    def __call__(self, url: str, timeout: int) -> tuple[int, bytes, str]: ...


class ApiError(RuntimeError):
    """Raised when an API request fails or returns an unusable payload."""


def _blocked_message(status: int, url: str, body: bytes) -> str:
    if status in BLOCKED_STATUSES:
        return f"{url} -> HTTP {status} ({len(body)} bytes) — looks like EP bot protection, not a missing document"
    return f"{url} -> HTTP {status} ({len(body)} bytes)"


@dataclass
class HttpUrlGetter:
    """requests-based getter with allowlist enforcement, UA and retries.

    ``requests`` is imported lazily so the rest of the package (URL
    building, parsing, detection, state) stays usable without it.
    """

    user_agent: str
    allowlist: tuple[str, ...]
    retries: int = 2
    session: object | None = None

    def _make_session(self) -> object:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers["User-Agent"] = self.user_agent
        return session

    def __call__(self, url: str, timeout: int) -> tuple[int, bytes, str]:
        assert_allowed(url, self.allowlist)
        if self.session is None:
            self.session = self._make_session()
        response = self.session.get(url, timeout=timeout, headers={"Accept": accept_header_for(url)})
        return response.status_code, response.content, response.headers.get("Content-Type", "")


class ApiClient:
    """Polite JSON client over a UrlGetter.

    Every non-200 response raises ApiError loudly — the previous probing
    design silently treated blocked responses (202) as "document absent".
    """

    def __init__(self, getter: UrlGetter, delay: float = 0.25):
        self.getter = getter
        self.delay = delay
        self.calls: list[str] = []

    def get_json(
        self,
        url: str,
        timeout: int = 30,
        allow_empty: bool = False,
    ) -> object | None:
        """GET a URL and parse the JSON body.

        With ``allow_empty`` a 200 with an empty body returns None (an
        unpublished agenda); otherwise it raises. Every other failure
        and every non-200 status raises ApiError.
        """
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(url)
        status, body, _content_type = self.getter(url, timeout)
        if status == 204:
            return None  # "No content": an empty result set, not an error
        if status != 200:
            raise ApiError(_blocked_message(status, url, body))
        if not body:
            if allow_empty:
                return None
            raise ApiError(f"{url} -> HTTP 200 with empty body (unexpected)")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(f"{url} -> invalid JSON: {exc}") from exc
