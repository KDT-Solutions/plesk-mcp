"""Tests für die Validierung der wc_stock_*-Tools (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("val,exp", [
    ("1943", ("id", "1943")),
    (1943, ("id", "1943")),
    ("0759973055585", ("sku", "0759973055585")),
    ("ABC-123.x", ("sku", "ABC-123.x")),
    (" 0781274179417 ", ("sku", "0781274179417")),
])
def test_product_ref_ok(val, exp):
    assert server._wc_check_product_ref(val) == exp


@pytest.mark.parametrize("val", ["", "0", True, "a b", "x'y", "-1", "a;rm", "x" * 65])
def test_product_ref_rejected(val):
    with pytest.raises(ValueError):
        server._wc_check_product_ref(val)


@pytest.mark.parametrize("q,c,exp", [
    (25, None, ("set", 25)),
    (0, None, ("set", 0)),
    (-2, None, ("set", -2)),
    (None, 5, ("increase", 5)),
    (None, -3, ("decrease", 3)),
])
def test_stock_op_ok(q, c, exp):
    assert server._wc_check_stock_op(q, c) == exp


@pytest.mark.parametrize("q,c", [
    (None, None), (5, 3), (None, 0), ("5", None), (None, "2"), (True, None), (None, 10_001),
    (1_000_000, None), (2.5, None),
])
def test_stock_op_rejected(q, c):
    with pytest.raises(ValueError):
        server._wc_check_stock_op(q, c)


def test_note():
    assert server._wc_check_note(" Inventur ") == "Inventur"
    for bad in ["x" * 201, "a\nb", "a\x00b"]:
        with pytest.raises(ValueError):
            server._wc_check_note(bad)


def test_php_has_no_placeholders():
    for php in (server._WC_STOCK_GET_PHP, server._WC_STOCK_UPDATE_PHP):
        assert "%s" not in php and "stream_get_contents(STDIN)" in php
    assert "wc_update_product_stock" in server._WC_STOCK_UPDATE_PHP
