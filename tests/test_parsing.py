"""Parsing tests: agendas (HTML) and minutes (PDF/DOCX) into agenda items."""

from ep_space_monitor.parsing import (
    html_to_lines,
    parse_document,
    pdf_bytes_to_text,
    split_items,
)
from tests.conftest import make_docx_bytes, make_pdf_bytes

MINUTES_PARAGRAPHS = [
    "ITRE_PV(2025)06-25_2",
    "Committee on Industry, Research and Energy",
    "MINUTES OF THE SITTING",
    "1. Adoption of the minutes of 12-13 May 2025",
    "The minutes were adopted.",
    "6. Exchange of views with Rodrigo da Costa, Executive Director of the European Union Agency for the Space Programme (EUSPA)",
    "The committee discussed the EU Space Programme budget and Galileo service provision.",
    "8. Exchange of views with Andrius Kubilius, Commissioner for Defence and Space",
    "Discussion covered the Space Package and the IRIS² constellation.",
]


def test_html_to_lines_skips_script_and_style(agenda_html):
    lines = html_to_lines(agenda_html)
    joined = " ".join(lines)
    assert "should" not in joined.replace("should not appear", "") or "not appear" in joined
    assert "display: none" not in joined
    assert "European Parliament" in joined


def test_parse_agenda_html_into_items(agenda_html):
    items = parse_document(agenda_html.encode("utf-8"), "text/html")
    titles = [item.title for item in items]
    # 9 numbered items (8 in the first table, 12 -> 9 after the noise block).
    assert len(items) == 9
    assert items[0].title == "Adoption of agenda"
    assert any("Space Programme (EUSPA)" in t for t in titles)
    assert items[5].index == 6
    assert "ITRE/10/02358" in items[3].text  # document references attach to items


def test_noise_lines_dropped(agenda_html):
    items = parse_document(agenda_html.encode("utf-8"), "text/html")
    all_text = " — ".join(item.text for item in items)
    assert "Brussels" not in all_text
    assert "Room: ANTALL" not in all_text
    assert "***" not in all_text
    assert "9.00" not in all_text


def test_split_items_ignores_header_lines():
    lines = [
        "Committee on Industry, Research and Energy",
        "DRAFT AGENDA",
        "1. Adoption of agenda",
        "2. Chair's announcements",
    ]
    items = split_items(lines)
    assert len(items) == 2
    assert items[0].title == "Adoption of agenda"


def test_lone_number_column_layout():
    lines = ["Committee meeting", "1.", "Adoption of agenda", "2.", "Adoption of minutes"]
    items = split_items(lines)
    assert [item.title for item in items] == ["Adoption of agenda", "Adoption of minutes"]


def test_parse_minutes_pdf():
    pdf = make_pdf_bytes(MINUTES_PARAGRAPHS)
    items = parse_document(pdf, "application/pdf")
    assert items[0].title.startswith("Adoption of the minutes")
    titles = [item.title for item in items]
    assert any("EUSPA" in t for t in titles)
    # Continuation lines attach: the Galileo discussion belongs to item 6.
    euspa_item = next(item for item in items if "EUSPA" in item.title)
    assert "Galileo" in euspa_item.text


def test_pdf_bytes_to_text_extracts_content():
    pdf = make_pdf_bytes(["Hello Copernicus world"])
    assert "Copernicus" in pdf_bytes_to_text(pdf)


def test_parse_minutes_docx():
    docx = make_docx_bytes(MINUTES_PARAGRAPHS)
    items = parse_document(docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert len(items) >= 3
    assert items[0].title.startswith("Adoption of the minutes")
    assert any("IRIS" in item.text for item in items)


def test_parse_document_dispatches_on_content(agenda_html):
    # No content type: the HTML prologue should still be recognised.
    items = parse_document(agenda_html.encode("utf-8"), "")
    assert len(items) == 9
