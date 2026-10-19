"""Tests für die Validierung der wc_coupon_*-Tools (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("code", ["SOMMER10", "welcome-2026", "Ä10%"])
def test_code_ok(code):
    assert server._wc_check_code(f" {code} ") == code


@pytest.mark.parametrize("code", ["", "a b", "x'y", 'x"y', "a\\b", "x" * 101])
def test_code_rejected(code):
    with pytest.raises(ValueError, match="Ungültiger Gutscheincode"):
        server._wc_check_code(code)


def test_fields_normalized():
    out = server._wc_check_fields({
        "code": "X", "discount_type": "percent", "amount": "10", "date_expires": "2026-12-31",
        "usage_limit": 5, "individual_use": True, "product_ids": [3, 3, 7],
        "email_restrictions": [" A@Example.com "], "minimum_amount": "", "status": "draft",
    })
    assert out["product_ids"] == [3, 7]
    assert out["email_restrictions"] == ["a@example.com"]
    assert out["minimum_amount"] == ""
    assert out["usage_limit"] == 5


@pytest.mark.parametrize("fields", [
    {}, {"foo": 1}, {"discount_type": "bogo"}, {"amount": "10,50"}, {"amount": "abc"},
    {"date_expires": "31.12.2026"}, {"individual_use": "yes"}, {"usage_limit": -1},
    {"usage_limit": True}, {"product_ids": [0]}, {"product_ids": "1,2"},
    {"email_restrictions": ["nope"]}, {"status": "trashed"}, {"code": "a b"},
])
def test_fields_rejected(fields):
    with pytest.raises(ValueError):
        server._wc_check_fields(fields)


def test_cli_args():
    args = server._wc_cli_args({"code": "X", "individual_use": True, "free_shipping": False,
                                "product_ids": [1, 2], "date_expires": None, "amount": "10"})
    assert "--code=X" in args and "--individual_use=true" in args and "--free_shipping=false" in args
    assert "--product_ids=[1, 2]" in args and "--date_expires=" in args and "--amount=10" in args


def test_norm_compares_woocommerce_formats():
    assert server._wc_norm("10") == server._wc_norm("10.00")
    assert server._wc_norm("2026-12-31T00:00:00") == server._wc_norm("2026-12-31")
    assert server._wc_norm([2, 1]) == server._wc_norm([1, 2])
    assert server._wc_norm(None) == server._wc_norm("")
    assert server._wc_norm(True) != server._wc_norm(False)


def test_list_validation():
    with pytest.raises(ValueError, match="status"):
        server.wc_coupon_list("example.com", "httpdocs", status="old")
    with pytest.raises(ValueError, match="limit"):
        server.wc_coupon_list("example.com", "httpdocs", limit=101)


def test_create_validation_before_connect():
    with pytest.raises(ValueError, match="discount_type"):
        server.wc_coupon_create("example.com", "httpdocs", "X", "bogo", "10")
    with pytest.raises(ValueError, match="Unbekanntes Feld"):
        server.wc_coupon_create("example.com", "httpdocs", "X", "percent", "10", fields={"foo": 1})


def test_rollback_backup_id_validation():
    with pytest.raises(ValueError, match="Ungültige backup_id"):
        server.wc_coupon_rollback("x")
