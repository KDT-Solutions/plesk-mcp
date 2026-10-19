"""Tests für die Validierung von wp_custom_css_* (ohne Serverzugriff).

Ausführen: pip install -r requirements.txt pytest && pytest
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("name", ["hello-elementor", "twentytwentyfive", "my_theme.v2", "Astra"])
def test_stylesheet_ok(name):
    assert server._wp_check_stylesheet(f" {name} ") == name


def test_stylesheet_empty_means_active_theme():
    assert server._wp_check_stylesheet("") == ""
    assert server._wp_css_php_literal("") == "get_stylesheet()"
    assert server._wp_css_php_literal("hello-elementor") == "'hello-elementor'"


@pytest.mark.parametrize("name", ["a b", "x;id", "a/b", "..", "a..b", "x'y", "$(id)", "x" * 101])
def test_stylesheet_rejected(name):
    with pytest.raises(ValueError, match="Ungültiger Theme-Name"):
        server._wp_check_stylesheet(name)


def test_css_ok():
    css = ".tml .tml-button { background: #ff6600; }"
    assert server._wp_check_css(css) == css
    assert server._wp_check_css("") == ""


@pytest.mark.parametrize("css", ["a\x00b", "x </style><script>", "a</STYLE>"])
def test_css_rejected(css):
    with pytest.raises(ValueError):
        server._wp_check_css(css)


def test_css_too_large():
    with pytest.raises(ValueError, match="zu gross"):
        server._wp_check_css("a" * (server._WP_CSS_MAX_BYTES + 1))


def test_php_code_never_contains_css():
    # Das CSS geht per stdin - der feste PHP-Code darf keine Platzhalter
    # ausser dem Stylesheet enthalten.
    assert server._WP_CSS_SET_PHP.count("%s") == 1
    assert server._WP_CSS_GET_PHP.count("%s") == 1
    assert "STDIN" in server._WP_CSS_SET_PHP


def test_diff_is_unified_and_bounded():
    d = server._wp_css_diff("a {}\n", "a {}\nb {}\n")
    assert d.startswith("--- alt")
    assert "+b {}" in d
    big = server._wp_css_diff("", "x\n" * 50000)
    assert big.endswith("(gekürzt)")


def test_update_mode_validation():
    with pytest.raises(ValueError, match="mode"):
        server.wp_custom_css_update("example.com", "httpdocs", "a{}", mode="prepend")
    with pytest.raises(ValueError, match="Leeres css"):
        server.wp_custom_css_update("example.com", "httpdocs", "   ", mode="append")


def test_rollback_backup_id_validation():
    with pytest.raises(ValueError, match="Ungültige backup_id"):
        server.wp_custom_css_rollback("../x")
