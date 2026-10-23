"""EP API URL builders and payload parsers (verified against live payloads)."""

import pytest

from ep_space_monitor.ep_api import (
    EMEETING_API,
    OPENDATA_API,
    ApiClient,
    ApiError,
    archive_url,
    events_url,
    ft_multipart_body,
    ft_query,
    ft_search_url,
    oj_url,
    parse_archive,
    parse_events,
    parse_ft,
    parse_oj,
    parse_question,
    parse_speeches,
    pq_detail_url,
    pq_list_url,
    speech_record_url,
    speeches_url,
)
from ep_space_monitor.ep_api import FT_STATUS_FORTHCOMING, FT_STATUS_OPEN  # noqa: E402


class Getter:
    def __init__(self, responses):
        self.responses = responses  # url -> (status, body, ctype)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        return self.responses.get(url, (204, b"", ""))


# -- URL builders (shapes verified against the live APIs) --------------------


def test_archive_url():
    assert archive_url("ITRE", 2026) == (
        f"{EMEETING_API}agenda/agendaArchive?language=en&year=2026&organ=ITRE"
    )


def test_events_url():
    assert events_url("ITRE", 2026, 10) == (
        f"{EMEETING_API}meeting/events/eventsByMonth"
        "?language=en&year=2026&month=10&organ=ITRE"
    )


def test_oj_url_quotes_reference():
    assert oj_url("ITRE(2026)1008_1") == (
        f"{EMEETING_API}OJ/oj?language=en&reference=ITRE%282026%291008_1&securedContext=false"
    )


def test_pq_urls():
    assert pq_list_url(2026, 100, 50) == (
        f"{OPENDATA_API}parliamentary-questions?year=2026&offset=100&limit=50"
    )
    assert pq_detail_url("E-10-2026-000002") == (
        f"{OPENDATA_API}parliamentary-questions/E-10-2026-000002"
    )


def test_speeches_url_encodes_query():
    url = speeches_url("eu space programme", limit=25, offset=50)
    assert url.startswith(f"{OPENDATA_API}speeches?title=eu+space+programme")
    assert "&search-language=en" in url
    assert url.endswith("&limit=25&offset=50")


def test_speech_record_url():
    assert speech_record_url("MTG-PL-2026-09-16-OTH-1") == (
        f"{OPENDATA_API}speeches/MTG-PL-2026-09-16-OTH-1"
    )


# -- parsers ------------------------------------------------------------------


def test_parse_archive_day_first_dates():
    data = [
        {
            "year": 2026,
            "months": [
                {
                    "month": "October",
                    "ojs": [
                        {"date": "22.10.2026", "ojReference": "ITRE(2026)1022_1"},
                        {"date": "08.10.2026", "ojReference": "ITRE(2026)1008_1"},
                    ],
                }
            ],
        }
    ]
    refs = parse_archive(data)
    assert refs == [
        ("ITRE(2026)1022_1", "2026-10-22"),
        ("ITRE(2026)1008_1", "2026-10-08"),
    ]


def test_parse_archive_tolerates_none():
    assert parse_archive(None) == []
    assert parse_archive([{"months": None}, {}]) == []


def test_parse_events_epoch_milliseconds():
    # 2026-10-08 09:00 UTC in epoch ms (from the live API).
    data = [{"meetingReference": "ITRE(2026)1008_1", "start": 1791442800000}]
    assert parse_events(data) == [("ITRE(2026)1008_1", "2026-10-08")]
    assert parse_events(None) == []
    assert parse_events([{"meetingReference": "X"}]) == []  # no start


def test_parse_oj_skips_headers_keeps_numbered_items():
    data = {
        "meetingReference": "ITRE(2026)1008_1",
        "date": 1791442800000,
        "items": [
            {"number": None, "title": "8 October 2026, 09.00 - 11.15", "type": {"code": "SITT"}},
            {"number": 1, "title": "Adoption of agenda", "documentSets": []},
            {
                "number": 5,
                "title": "European space policy",
                "dossierReference": "2026/0001",
                "documentSets": [
                    {
                        "type": "OTH",
                        "documents": [
                            {"geproCode": "OJ", "title": "Revised draft agenda"},
                            {"geproCode": "PR", "title": "Draft report on the EU Space Act"},
                        ],
                    }
                ],
            },
        ],
        "ojDocument": {
            "geproCode": "OJ",
            "documentLinks": [
                {
                    "type": "pdf",
                    "languages": [
                        {"code": "BG", "url": "https://example/bg.pdf"},
                        {"code": "EN", "url": "https://example/en.pdf"},
                    ],
                }
            ],
        },
    }
    agenda = parse_oj(data)
    assert agenda is not None
    assert agenda.reference == "ITRE(2026)1008_1"
    assert agenda.committee == "ITRE"
    assert agenda.day == "2026-10-08"
    assert [item.number for item in agenda.items] == [1, 5]
    # The scoring text includes the item title, dossier reference and
    # attached document titles (but not the agenda document itself).
    item5 = agenda.items[1]
    assert "European space policy" in item5.text
    assert "2026/0001" in item5.text
    assert "EU Space Act" in item5.text
    assert "Revised draft agenda" not in item5.text
    assert agenda.source_url == "https://example/en.pdf"


def test_parse_oj_empty_payloads():
    assert parse_oj(None) is None
    assert parse_oj({}) is None
    assert parse_oj({"items": []}) is None


def test_parse_oj_fallback_source_url():
    data = {"meetingReference": "SEDE(2026)1_1", "date": 1791442800000, "items": [
        {"number": 1, "title": "Adoption of agenda"}
    ], "ojDocument": {"documentLinks": [{"type": "pdf", "languages": [{"code": "FR", "url": "x"}]}]}}
    agenda = parse_oj(data, fallback_url="https://emeeting.europarl.europa.eu/emeeting/")
    assert agenda.source_url == "https://emeeting.europarl.europa.eu/emeeting/"


def test_parse_question_english_expression():
    data = {
        "data": [
            {
                "identifier": "E-10-2026-000002",
                "document_date": "2026-01-02",
                "is_realized_by": [
                    {
                        "language": "http://publications.europa.eu/resource/authority/language/DEU",
                        "title": {"de": "Deutscher Titel"},
                    },
                    {
                        "language": "http://publications.europa.eu/resource/authority/language/ENG",
                        "title": {"en": "EU funding for the space sector"},
                        "title_alternative": {
                            "en": "Question for written answer E-000002/2026 - to the Commission"
                        },
                        "is_embodied_by": [
                            {"is_exemplified_by": "distribution/reds_iMaQp/E-10-2026-000002/E-10-2026-000002_en.docx"}
                        ],
                    },
                ],
            }
        ]
    }
    q = parse_question(data)
    assert q.identifier == "E-10-2026-000002"
    assert q.day == "2026-01-02"
    assert q.title == "EU funding for the space sector"
    assert q.alt_title.startswith("Question for written answer")
    assert q.url == (
        "https://data.europarl.europa.eu/distribution/reds_iMaQp/"
        "E-10-2026-000002/E-10-2026-000002_en.docx"
    )


def test_parse_question_falls_back_without_english():
    data = {
        "data": [
            {
                "identifier": "E-10-2026-000009",
                "document_date": "2026-01-05",
                "is_realized_by": [
                    {"language": "x/FRA", "title": {"fr": "Titre français"}}
                ],
            }
        ]
    }
    q = parse_question(data)
    assert q.title == "Titre français"
    assert q.url == pq_detail_url("E-10-2026-000009")


def test_parse_question_rejects_unusable():
    assert parse_question(None) is None
    assert parse_question({"data": []}) is None
    assert parse_question({"data": [{"identifier": "", "is_realized_by": []}]}) is None


def test_parse_speeches_dedupes_and_picks_english():
    data = {
        "data": [
            {
                "activity_id": "MTG-PL-2026-09-16-OTH-1",
                "activity_date": "2026-09-16",
                "activity_label": {"en": "Debate on the EU Space Strategy", "fr": "Débat"},
            },
            {
                "activity_id": "MTG-PL-2026-09-16-OTH-1",
                "activity_date": "2026-09-16",
                "activity_label": {"en": "Duplicate"},
            },
            {
                "activity_id": "MTG-PL-2026-09-16-OTH-2",
                "activity_date": "2026-09-16",
                "activity_label": {"it": "Solo italiano"},
            },
        ]
    }
    hits = parse_speeches(data)
    assert len(hits) == 2
    assert hits[0].title == "Debate on the EU Space Strategy"
    assert hits[0].url == f"{OPENDATA_API}speeches/MTG-PL-2026-09-16-OTH-1"
    assert hits[1].title == "Solo italiano"
    assert parse_speeches(None) == []
    assert parse_speeches({"data": "not-a-list"}) == []


# -- ApiClient: loud errors, empty semantics ---------------------------------


def test_client_204_means_empty_not_error():
    client = ApiClient(Getter({}), delay=0.0)
    assert client.get_json("https://x/api") is None


def test_client_blocked_status_raises_loudly():
    client = ApiClient(Getter({"https://x/api": (202, b"", "")}), delay=0.0)
    with pytest.raises(ApiError, match="bot protection"):
        client.get_json("https://x/api")


def test_client_non_200_raises():
    client = ApiClient(Getter({"https://x/api": (500, b"oops", "")}), delay=0.0)
    with pytest.raises(ApiError, match="HTTP 500"):
        client.get_json("https://x/api")


def test_client_empty_body_allowed_or_raised():
    getter = Getter({"https://x/api": (200, b"", "")})
    client = ApiClient(getter, delay=0.0)
    assert client.get_json("https://x/api", allow_empty=True) is None
    with pytest.raises(ApiError, match="empty body"):
        client.get_json("https://x/api")


def test_client_invalid_json_raises():
    client = ApiClient(Getter({"https://x/api": (200, b"not json", "")}), delay=0.0)
    with pytest.raises(ApiError, match="invalid JSON"):
        client.get_json("https://x/api")


# -- Accept negotiation (live-verified: Open Data serves RDF/XML otherwise) ----


def test_accept_header_prefers_json_ld_on_opendata():
    from ep_space_monitor.ep_api import accept_header_for

    assert accept_header_for("https://data.europarl.europa.eu/api/v2/parliamentary-questions?year=2026") == "application/ld+json"


def test_accept_header_plain_json_on_emeeting():
    from ep_space_monitor.ep_api import accept_header_for

    assert (
        accept_header_for("https://emeeting.europarl.europa.eu/emeeting/plmrep/agenda/agendaArchive?language=en&year=2026&organ=ITRE")
        == "application/json"
    )


def test_getter_sends_accept_header():
    """HttpUrlGetter must negotiate JSON-LD with the Open Data host."""
    from ep_space_monitor import ep_api

    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        headers = {"Content-Type": "application/ld+json"}

    class FakeSession:
        def get(self, url, timeout=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    getter = ep_api.HttpUrlGetter(user_agent="test", allowlist=("data.europarl.europa.eu",), session=FakeSession())
    getter("https://data.europarl.europa.eu/api/v2/speeches?text=space", timeout=5)
    assert captured["headers"]["Accept"] == "application/ld+json"

    getter_emeeting = ep_api.HttpUrlGetter(
        user_agent="test", allowlist=("emeeting.europarl.europa.eu",), session=FakeSession()
    )
    getter_emeeting("https://emeeting.europarl.europa.eu/emeeting/plmrep/agenda/agendaArchive", timeout=5)
    assert captured["headers"]["Accept"] == "application/json"


# -- CELLAR SPARQL (EUR-Lex / Council documents) -------------------------------


def test_cellar_url_encodes_query():
    from urllib.parse import unquote_plus

    from ep_space_monitor.ep_api import cellar_url

    url = cellar_url(["space", "galileo"], "2026-10-08", limit=50)
    assert url.startswith("https://publications.europa.eu/webapi/rdf/sparql?")
    decoded = unquote_plus(url)
    assert "bif:contains \"'space' or 'galileo'\"" in decoded
    assert 'FILTER(STR(?date) >= "2026-10-08")' in decoded
    assert "LIMIT 50" in decoded


def test_cellar_url_strips_quote_injection():
    from urllib.parse import unquote_plus

    from ep_space_monitor.ep_api import cellar_url

    decoded = unquote_plus(cellar_url(["gal'ileo"], "2026-01-01", 10))
    assert "'galileo'" in decoded and "gal'ileo" not in decoded


def test_accept_header_sparql_for_cellar():
    from ep_space_monitor.ep_api import accept_header_for

    assert (
        accept_header_for("https://publications.europa.eu/webapi/rdf/sparql?query=SELECT")
        == "application/sparql-results+json"
    )


def test_parse_cellar_prefers_celex_view_and_dedupes():
    from ep_space_monitor.ep_api import parse_cellar

    rows = {"results": {"bindings": [
        {"work": {"value": "http://publications.europa.eu/resource/cellar/u1"},
         "id": {"value": "oj:L_202602322"}, "date": {"value": "2026-10-09"},
         "title": {"value": "Council Decision (EU) 2026/2322"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/u2"},
         "id": {"value": "celex:32026D2322"}, "date": {"value": "2026-10-09"},
         "title": {"value": "Council Decision (EU) 2026/2322"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/u3"},
         "id": {"value": "consil:ST_4447"}, "date": {"value": "2026-10-05"},
         "title": {"value": "Space Working Party meeting"}},
        {"work": {"value": "http://x"}, "id": {"value": ""}, "date": {"value": ""},
         "title": {"value": ""}},
    ]}}
    docs = parse_cellar(rows)
    assert len(docs) == 2
    by_title = {d.title: d for d in docs}
    assert by_title["Council Decision (EU) 2026/2322"].doc_id == "celex:32026D2322"
    assert by_title["Council Decision (EU) 2026/2322"].url == (
        "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026D2322"
    )
    council = by_title["Space Working Party meeting"]
    assert council.doc_id == "consil:ST_4447"
    assert council.url == "https://op.europa.eu/en/publication-detail/-/publication/u3"


def test_parse_cellar_rejects_malformed():
    from ep_space_monitor.ep_api import parse_cellar

    assert parse_cellar(None) == []
    assert parse_cellar({}) == []
    assert parse_cellar({"results": {"bindings": "nope"}}) == []


def test_parse_question_prefers_english_expression_over_earlier_german():
    """Open Data language URIs carry ISO 639-2 codes (ENG/DEU/FRA)."""
    data = {
        "data": [
            {
                "identifier": "E-10-2026-000002",
                "document_date": "2026-01-02",
                "is_realized_by": [
                    {
                        "language": "http://publications.europa.eu/resource/authority/language/DEU",
                        "title": {"de": "Deutscher Titel"},
                    },
                    {
                        "language": "http://publications.europa.eu/resource/authority/language/ENG",
                        "title": {"en": "EU funding for the space sector"},
                        "title_alternative": {
                            "en": "Question for written answer E-000002/2026 - to the Commission"
                        },
                        "is_embodied_by": [
                            {"is_exemplified_by": "distribution/reds_iMaQp/E-10-2026-000002/E-10-2026-000002_en.docx"}
                        ],
                    },
                ],
            }
        ]
    }
    q = parse_question(data)
    assert q.identifier == "E-10-2026-000002"
    assert q.day == "2026-01-02"
    assert q.title == "EU funding for the space sector"
    assert q.alt_title == "Question for written answer E-000002/2026 - to the Commission"
    assert q.url.startswith("https://data.europarl.europa.eu/distribution/")


def test_parse_question_skips_english_expression_without_english_title():
    # English expression whose title dict only has a non-English value:
    # a later English expression with a real English title must win.
    data = {
        "data": [
            {
                "identifier": "E-10-2026-000013",
                "document_date": "2026-01-07",
                "is_realized_by": [
                    {"language": "x/ENG", "title": {"de": "Nur deutsch"}},
                    {"language": "x/ENG", "title": {"en": "English later"}},
                ],
            }
        ]
    }
    q = parse_question(data)
    assert q.title == "English later"


def test_parse_question_english_without_title_falls_back_to_any_language():
    data = {
        "data": [
            {
                "identifier": "E-10-2026-000014",
                "document_date": "2026-01-08",
                "is_realized_by": [
                    {"language": "x/ENG", "title": {}},
                    {"language": "x/DEU", "title": {"de": "Deutsche Frage"}},
                ],
            }
        ]
    }
    q = parse_question(data)
    assert q.title == "Deutsche Frage"


# -- Funding & Tenders (SEDIA search API) -------------------------------------


def test_ft_search_url_shape():
    url = ft_search_url("eu space", 100)
    assert url.startswith("https://api.tech.ec.europa.eu/search-api/prod/rest/search?")
    assert "apiKey=SEDIA" in url
    assert "text=eu+space" in url
    assert "pageSize=100" in url and "pageNumber=1" in url


def test_ft_search_url_encodes_terms():
    url = ft_search_url("space' \"quoted\"", 50)
    assert "%27" in url and "%22" in url  # quotes must be percent-encoded
    assert "'" not in url and '"' not in url


def test_ft_search_url_empty_term_becomes_match_all():
    assert "text=%2A%2A%2A" in ft_search_url("", 50)  # the endpoint rejects empty text


def test_ft_query_pins_type_status_language():
    q = ft_query()
    must = q["bool"]["must"]
    assert {"terms": {"type": ["0"]}} in must
    assert {"terms": {"status": [FT_STATUS_FORTHCOMING, FT_STATUS_OPEN]}} in must
    assert {"terms": {"language": ["en"]}} in must


def test_ft_multipart_body_json_part_header():
    body = ft_multipart_body({"bool": {"must": []}})
    text = body.decode("utf-8")
    assert 'Content-Disposition: form-data; name="query"' in text
    assert "Content-Type: application/json" in text  # required or the API 500s
    assert text.startswith("----")
    assert '{"bool": {"must": []}}' in text


def test_parse_ft_unwraps_metadata_arrays():
    data = {"results": [{
        "reference": "uuid1-en",
        "url": "https://example.eu/call/1",
        "metadata": {
            "identifier": ["uuid1"],
            "callIdentifier": ["HORIZON-CL4-2027-SPACE-01"],
            "title": ["European Space Programme research topic"],
            "status": ["31094502"],
            "es_SortDate": ["2026-10-15T00:00:00.000+0000"],
            "deadlineDate": ["2027-02-01T23:59:59.000+0000"],
            "url": ["https://example.eu/call/1"],
            "language": ["en"],
            "description": ["Research and innovation action on space debris."],
        },
    }]}
    calls = parse_ft(data)
    assert len(calls) == 1
    c = calls[0]
    assert c.identifier == "uuid1" and c.call_id == "HORIZON-CL4-2027-SPACE-01"
    assert c.day == "2026-10-15" and c.deadline == "2027-02-01"
    assert c.status == "open" and c.key == "FT:uuid1"
    assert c.url == "https://example.eu/call/1"
    assert "space debris" in c.text and "Deadline: 2027-02-01" in c.text


def test_parse_ft_dedupes_by_identifier():
    data = {"results": [
        {"metadata": {"identifier": ["uuid1"], "title": ["Call title"], "language": ["en"],
                      "status": ["31094501"], "es_SortDate": ["2026-10-01T00:00:00.000+0000"]}},
        {"metadata": {"identifier": ["uuid1"], "title": ["Call title"], "language": ["fr"],
                      "status": ["31094501"], "es_SortDate": ["2026-10-01T00:00:00.000+0000"]}},
    ]}
    calls = parse_ft(data)
    assert len(calls) == 1


def test_parse_ft_falls_back_to_portal_url():
    data = {"results": [{
        "metadata": {"identifier": ["abc-123"], "title": ["Call title"], "status": ["31094501"],
                     "es_SortDate": ["2026-10-01T00:00:00.000+0000"]},
    }]}
    c = parse_ft(data)[0]
    assert c.url.startswith("https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/abc-123")


def test_parse_ft_rejects_malformed():
    assert parse_ft(None) == [] and parse_ft({}) == [] and parse_ft({"results": None}) == []
    # missing identifier or title -> row skipped
    calls = parse_ft({"results": [
        {"metadata": {"title": ["no identifier"], "es_SortDate": ["2026-10-01T00:00:00.000+0000"]}},
        {"metadata": {"identifier": ["x"], "es_SortDate": ["2026-10-01T00:00:00.000+0000"]}},
        "not-a-dict",
    ]})
    assert calls == []
