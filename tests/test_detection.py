"""Detection engine tests against the real keyword set."""

from ep_space_monitor.detection import excerpt_around, load_keywords, score_text

AGENDA_SPACE_ITEM = (
    "Exchange of views with Rodrigo da Costa, Executive Director of the "
    "European Union Agency for the Space Programme (EUSPA)"
)
AGENDA_NON_SPACE_ITEM = (
    "Amending Regulation (EU)2017/1938 as regards the role of gas storage for "
    "securing gas supplies ahead of the winter season"
)


def test_real_space_item_flagged(keywords):
    match = score_text(AGENDA_SPACE_ITEM, keywords)
    assert match is not None
    terms = {term for term, _lang, _weight in match.matched}
    assert "euspa" in terms
    assert "space programme" in terms
    assert match.score >= keywords.threshold


def test_non_space_item_ignored(keywords):
    assert score_text(AGENDA_NON_SPACE_ITEM, keywords) is None


def test_french_terms(keywords):
    match = score_text("Échange de vues sur la politique spatiale et les lanceurs", keywords)
    assert match is not None
    terms = {term for term, _lang, _weight in match.matched}
    assert "politique spatiale" in terms
    assert "lanceurs" in terms


def test_german_terms(keywords):
    match = score_text("Aussprache über das Weltraumprogramm und Trägerraketen", keywords)
    assert match is not None
    terms = {term for term, _lang, _weight in match.matched}
    assert "weltraumprogramm" in terms


def test_word_boundary_prevents_false_positive(keywords):
    # "ESA" must not match inside other words, and weak "space" alone stays below threshold.
    assert score_text("Debate on the thesaurus of parliamentary terminology", keywords) is None
    assert score_text("Time and space for informal discussion", keywords) is None


def test_iris2_variants(keywords):
    assert score_text("Debate on the IRIS² constellation", keywords) is not None
    assert score_text("Debate on the IRIS2 constellation", keywords) is not None


def test_multi_keyword_weak_signal_crosses_threshold(keywords):
    # "space" (1.0) + "satellite" (2.0) should cross the 2.0 threshold.
    match = score_text("Orbital satellite services roundtable", keywords)
    assert match is not None


def test_excerpt_collapses_whitespace_and_ellipsizes():
    text = "Intro.   \n\n   Discussion about the EU Space Programme budget and Galileo services. " * 5
    excerpt = excerpt_around(text, 60, 120)
    assert "  " not in excerpt
    assert excerpt.endswith("…")


def test_keyword_file_loads_and_compiles():
    keywords = load_keywords("keywords.toml")
    assert keywords.threshold > 0
    assert len(keywords) > 40
