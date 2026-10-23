# EP Space Monitor

A service that tracks **EU activity on the space sector** for ESPI
(European Space Policy Institute). It queries official EU JSON/SPARQL
APIs for EP committee agendas, parliamentary questions, plenary debates,
EUR-Lex/Council documents and EU funding calls & tenders, scores
everything against a weighted multilingual keyword list, and flags
anything space-relevant: EU Space Programme, Galileo, Copernicus, IRIS²,
EU Space Act, launchers, space defence & security, satellites, ESA,
EUSPA, and more.

Findings are published on an **ESPI-branded dashboard** and sent as an
**e-mail alert** after every run — never twice for the same item.

## How it works

```
        ┌────────────────────────────────────────────────────────┐
        │  Daily run (cron, GitHub Actions)                      │
        └────────────────────────────────────────────────────────┘
   1. Committee agendas (eMeeting API): scan the window months
      (21 days back, 21 days ahead) for every watched committee,
      fetch each published agenda, score every item
   2. Watch future meetings whose agenda is not published yet —
      retried on every run, dropped after 30 days
   3. Parliamentary questions (EP Open Data API): walk the
      question register forward via a stored cursor, score each
      new question on its subject line
   4. Plenary debates (EP Open Data API): title search per
      configured query, score matching debates locally
   5. EUR-Lex / Council documents (Publications Office CELLAR
      SPARQL): one query per run combines all configured terms,
      date window filtered server-side; Council working-party
      documents (consil:/immc: ids) surface here too
   6. Funding & Tenders Portal (SEDIA search API, keyless):
      open and forthcoming calls/tenders matching the configured
      terms, English records, publication window filtered locally
   7. Store new findings in data/state.json + data/findings.jsonl
      → state guarantees alerts are never repeated
   8. Regenerate the dashboard, send the alert e-mail
```

### Why official APIs (and not scraping)?

The previous version addressed committee documents through the predictable
doceo URL scheme on `www.europarl.europa.eu`. That stopped working: EP bot
protection answers every automated request from datacenter IPs — GitHub
Actions runners included — with HTTP 202 and an empty body, which silently
produced zero findings. v2 is rebuilt on official APIs that do not apply
that block:

- **eMeeting** (`emeeting.europarl.europa.eu/emeeting/plmrep/`, no auth)
  - `agenda/agendaArchive?language=en&year=Y&organ=COMM` — every published
    agenda (OJ) reference of a committee for a year; used for first-run
    backfill (dates arrive day-first, e.g. `22.10.2026`).
  - `meeting/events/eventsByMonth?language=en&year&month&organ` — all
    meetings of a committee-month, including future ones whose agenda has
    not been published yet (heads-up window).
  - `OJ/oj?language=en&reference=REF&securedContext=false` — the full
    agenda: numbered items plus attached documents; an unpublished agenda
    returns HTTP 200 with an empty body.
- **EP Open Data API v2** (`data.europarl.europa.eu/api/v2/`, JSON-LD,
  500 requests / 5 min) — requires an `Accept: application/ld+json` header.
  - `parliamentary-questions?year&offset&limit` + `/parliamentary-questions/{id}`
    — the question register. Live-verified quirk: the list ordering is
    unstable (pages come back shuffled and differently between calls), so
    the scan never trusts list order for recency — identifiers are walked
    by descending question number (numbers are allocated at tabling time
    and are chronological), and new questions are still appended to the
    end of the list for the steady-state cursor.
  - `speeches?title=Q&search-language=en&limit&offset` — plenary debates
    whose title matches a query. Live-verified quirk: the endpoint's own
    `sitting-date` filters and `sort-by` parameter are broken (HTTP 500 or
    silently ignored), so the scan filters the date window locally.
- **Publications Office CELLAR** (`publications.europa.eu`, SPARQL — the
  RDF store behind EUR-Lex; requires `Accept: application/sparql-results+json`).
  Live-verified quirks: the old `publications.europa.eu/sparql` endpoint is
  dead — the current one is `publications.europa.eu/webapi/rdf/sparql`; works
  are stored under `cellar:` URIs with titles only on expressions
  (`cdm:expression_belongs_to_work` + `cdm:expression_title`); document dates
  are plain ISO string literals, so the window is a string comparison
  (`FILTER(STR(?date) >= "…")`); full-text matching uses Virtuoso `bif:contains`.
  The same document is stored once per source system (`celex:`, `eli:`, `oj:`,
  `immc:`, `consil:`, `genpub:` …) — the monitor dedupes by title+date and
  prefers the CELEX view. Bonus: Council documents (working parties etc.)
  surface here, and the Council register has no API of its own.
- **Funding & Tenders Portal — SEDIA search API**
  (`api.tech.ec.europa.eu/search-api/prod/rest/search`, keyless — the
  portal's own public search backend uses the well-known API key `SEDIA`;
  no EU-Login token required). One POST per configured term for
  Forthcoming/Open calls and tenders (type 0), English records. Live-verified
  quirks: the body is multipart/form-data whose single `query` part must
  carry `Content-Type: application/json` (plain parts answer HTTP 500);
  the free-text term is a URL parameter and cannot be empty; the narrow
  Elasticsearch dialect supports `terms` filters but silently ignores
  `range` and sorting — so the publication-date window
  (`ft_lookback_days`) is filtered locally; every record exists once per
  language (pinned to English server-side, deduped by identifier).

Minutes (PV) are no longer probed: they are not exposed through any API, and
the doceo URLs are behind the bot block.

### Loud by design

A blocked or broken source never fails silently: every unexpected HTTP status
or payload raises, is recorded per-run, and shows up in the run log and the
e-mail — instead of quietly returning "no findings today".

## Layout

```
config.toml          # committees, windows, sources, e-mail, allowlist
keywords.toml        # weighted keywords (EN/FR/DE) + scoring threshold
ep_space_monitor/
  ep_api.py          # EP + CELLAR URL builders, payload parsers (live-verified)
  allowlist.py        # hard host allowlist, enforced on every request
  detection.py       # weighted keyword scoring engine
  parsing.py         # document (HTML/PDF/DOCX) -> agenda items, used by backfill
  state.py           # processed records, pending meetings, findings (no dupes)
  runner.py          # orchestration: scan -> detect -> persist -> alert
  dashboard.py       # ESPI-branded static dashboard
  emailer.py          # SMTP alert e-mail (Brevo-compatible)
  cli.py             # command-line interface
tests/               # pytest suite (no network; fake API + fixtures)
scripts/selfcheck.py # stdlib-only self-check for restricted environments
.github/workflows/monitor.yml   # daily cron + test job
```

## Install & run

```bash
pip install -r requirements.txt
pip install -e .

# Daily monitoring pass. First run auto-backfills 6 months of agendas.
export EP_SPACE_MONITOR_SMTP_PASSWORD="your-brevo-smtp-key"
ep-space-monitor run

# Explicit backfill
ep-space-monitor backfill --months 6

# Regenerate the dashboard only
ep-space-monitor dashboard

# Verify SMTP settings
ep-space-monitor test-email
```

Useful flags: `--config path/to/config.toml`, `--dry-run` (no e-mail),
`run --backfill-months N` (0 = never, default auto on first run).

## Configuration (`config.toml`)

| Section | Key | Meaning |
|---|---|---|
| `[monitor]` | `committees` | Committee codes to watch (ITRE, SEDE, TRAN, BUDG) |
| | `agenda_lookback_days` | How far back to scan committee months each run |
| | `agenda_lookahead_days` | How far ahead to look (early heads-up on upcoming items) |
| | `pending_max_age_days` | How long to retry a meeting without a published agenda |
| | `questions_enabled` | Turn the parliamentary-questions source on/off |
| | `questions_lookback_days` | Age window for question subjects |
| | `questions_first_run_days` | First-run catch-up horizon for questions |
| | `questions_max_new` / `questions_page_size` / `questions_max_pages` | Per-run caps on the question register walk |
| | `plenary_enabled` | Turn the plenary-debates source on/off |
| | `plenary_lookback_days` | Age window for debate sitting dates (filtered locally) |
| | `plenary_queries` | Title-search queries against `/speeches` |
| | `plenary_limit_per_query` | Page size of the debate search |
| | `eurlex_enabled` | Turn the EUR-Lex / CELLAR source on/off |
| | `eurlex_lookback_days` | Date window for CELLAR documents (server-side) |
| | `eurlex_queries` | Search terms combined into the SPARQL query |
| | `eurlex_limit` | Cap on returned documents per run |
| | `ft_enabled` | Turn the Funding & Tenders source on/off |
| | `ft_lookback_days` | Publication window for calls/tenders (filtered locally) |
| | `ft_queries` | Free-text terms for the SEDIA search |
| | `ft_page_size` | Page size per search (max 100) |
| | `request_delay_seconds` / `max_retries` | Politeness delay and retry policy |
| | `first_run_backfill` / `backfill_months` | First-run backfill horizon |
| `[email]` | `to` | Alert recipients (`matija.rencelj@espi.eu`) |
| | `smtp_host` | Brevo relay: `smtp-relay.brevo.com:587` (STARTTLS) |
| | `smtp_password_env` | Env var holding the SMTP key (never in the file) |
| `[network]` | `allowlist` | Hard allowlist; any other host is refused |
| `[dashboard]` | `output_path` | Where the HTML dashboard is written |

### Questions cursor

The Open Data question register's list ordering is unstable (live-verified:
pages come back shuffled and differently between calls), so the monitor never
trusts list order for recency. Question numbers, however, are allocated at
tabling time and are chronological: the first-run catch-up walks identifiers
by descending number and only accepts the history boundary after a long run
of consecutive old questions — a single old question never ends the walk.
Steady state stores a per-year offset cursor (`pq_offset_{year}` plus a
`pq_scan_v2_{year}` marker) and walks forward with a one-page safety rewind —
typically a couple of pages per run. On the first run it covers
`questions_first_run_days` of history or `questions_max_new` questions, so
the first e-mail already covers the recent past.

### Keywords (`keywords.toml`)

`[keywords.en]`, `[keywords.fr]`, `[keywords.de]` map terms to weights:

```toml
[scoring]
threshold = 2.0        # an item is flagged when its summed weights reach this
excerpt_chars = 260

[keywords.en]
"eu space programme" = 3.5
"space" = 1.0          # weak signal: "space" alone never crosses the threshold
"iris²" = 4.0
```

Single tokens are matched with word boundaries (so `ESA` does not match inside
other words); phrases match as substrings. `term = { weight = 2.0, regex = true }`
enables raw regex terms. Tuning is pure data — no code changes needed.

The shipped list covers ~230 terms across all three languages: the EU Space
Programme family (Galileo, EGNOS, Copernicus, IRIS², EUSPA), GNSS and
navigation (GNSS, GPS, BeiDou, GLONASS, jamming/spoofing), debris and
sustainability (active debris removal, space traffic management, Kessler),
counterspace and security (anti-satellite, weaponisation of space, EU SST),
exploration (Artemis, lunar, human spaceflight, ISS, Mars), quantum
communications (EuroQCI, quantum key distribution), launch and small
satellites (Ariane, spaceport, cubesat, megaconstellation), plus governance,
law and Earth observation.

## Output

- **Dashboard** (`data/dashboard.html`): findings grouped by date, filterable
  by committee/kind and free-text search, styled per the ESPI style guide
  (dark Woodsmoke `#0C1112` / Gondola `#240D16` background, Razzmatazz
  `#E82275` and Aquamarine `#8ACCD0` accents, Raleway Black all-caps headings,
  Gugi wordmark, kind badges: agenda / question / plenary / eur-lex / funding).
- **E-mail**: after each run with new findings to `matija.rencelj@espi.eu`,
  with committee, date, kind (agenda heads-up / question / plenary / EUR-LEX /
  FUNDING), title, matched keywords, excerpt and source link. Capped at 25 items
  per mail.

## Scheduling (GitHub Actions)

`.github/workflows/monitor.yml` runs the test suite, then the monitor daily at
06:30 UTC, and commits `data/` (state, findings, dashboard) back to the repo so
alerts are never repeated across runs. Set the `BREVO_SMTP_KEY` repository
secret. A backfill can be triggered manually via `workflow_dispatch`.

To publish the dashboard, enable GitHub Pages on the branch of your choice and
point it at `data/dashboard.html`, or serve `data/` from any static host.

## Tests

```bash
pip install -e ".[test]"
pytest
```

The suite covers URL construction (including SPARQL-injection-hardened
CELLAR queries), Accept-header negotiation, payload parsing (verified against
live API shapes), allowlist enforcement, keyword scoring (EN/FR/DE, word
boundaries, thresholds), state dedupe (no repeated alerts), question-cursor
paging (including the unstable year-list ordering and the automatic
re-scan of pre-2.2.1 states), plenary paging with local date filtering, the
EUR-Lex source (dedupe
across source-system ids, local window filtering, noise rejection), the
Funding & Tenders source (SEDIA multipart body, facet query, per-language
dedupe, local window filtering, portal-URL fallback), dashboard
rendering, e-mail rendering and a full multi-run simulation with a fake API —
including meetings whose agenda appears with a delay between runs, blocked
responses being reported loudly, per-source on/off switches, and each source
being individually excludable. No network access required.

In restricted environments without the third-party packages,
`python scripts/selfcheck.py` runs a stdlib-only self-check of the same logic
(117 checks).

## Network allowlist

`emeeting.europarl.europa.eu`, `data.europarl.europa.eu`,
`publications.europa.eu`, `api.tech.ec.europa.eu`. Enforced in code
(`allowlist.py`) on every request — the monitor cannot reach anything else
even by accident.

## Roadmap

- More committees: add codes to `committees` in `config.toml` — no code change.
- More EP Open Data sources (answers to written questions, adopted texts).
- Commission / ESA / EUSPA press and consultation sources behind the same
  detection/state/alert pipeline.
