"""Network allowlist enforcement tests."""

import pytest

from ep_space_monitor.allowlist import AllowlistError, assert_allowed

ALLOWLIST = ("www.europarl.europa.eu", "eur-lex.europa.eu")


def test_allowed_host_passes():
    assert (
        assert_allowed("https://www.europarl.europa.eu/doceo/document/ITRE-OJ-2025-06-25-2_EN.html", ALLOWLIST)
        == "www.europarl.europa.eu"
    )


def test_other_ep_host_blocked():
    with pytest.raises(AllowlistError):
        assert_allowed("https://evil.example.com/ITRE-OJ-2025-06-25-2_EN.html", ALLOWLIST)


def test_subdomain_not_implicitly_allowed():
    with pytest.raises(AllowlistError):
        assert_allowed("https://attacker.europarl.europa.eu/", ALLOWLIST)


def test_non_http_scheme_blocked():
    with pytest.raises(AllowlistError):
        assert_allowed("ftp://www.europarl.europa.eu/doc.pdf", ALLOWLIST)
    with pytest.raises(AllowlistError):
        assert_allowed("file:///etc/passwd", ALLOWLIST)


def test_no_host_blocked():
    with pytest.raises(AllowlistError):
        assert_allowed("not-a-url", ALLOWLIST)
