"""Tests für die Validierung der wc_order_*-Tools (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("val,exp", [("2039", 2039), (" #2039 ", 2039), (17, 17)])
def test_order_id_ok(val, exp):
    assert server._wc_check_order_id(val) == exp


@pytest.mark.parametrize("val", ["", "0", "-5", "20 39", "abc", True, "1" * 10])
def test_order_id_rejected(val):
    with pytest.raises(ValueError):
        server._wc_check_order_id(val)


def test_tracking_ok():
    assert server._wc_check_tracking(" 99.60.123456.12345678 ") == "99.60.123456.12345678"
    assert server._wc_check_tracking("9960 1327 5100") == "996013275100"


@pytest.mark.parametrize("val", ["", "abc", "x'y12", "a;rm -rf", "1" * 51, "-12345"])
def test_tracking_rejected(val):
    with pytest.raises(ValueError):
        server._wc_check_tracking(val)


def test_provider():
    assert server._wc_check_provider("Swiss-Post") == "swiss-post"
    for bad in ["", "swiss post", "a'b", "x" * 51]:
        with pytest.raises(ValueError):
            server._wc_check_provider(bad)


def test_php_has_no_placeholders():
    # Werte kommen nur per stdin, nie per String-Formatierung in den Code
    for php in (server._WC_ORDER_GET_PHP, server._WC_ORDER_SHIP_PHP):
        assert "%s" not in php and "stream_get_contents(STDIN)" in php
