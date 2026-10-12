"""Tests für die Validierung von wp_option_update/wp_option_rollback (ohne Serverzugriff).

Ausführen: pip install -r requirements.txt pytest && pytest
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("name", ["wp_rocket_settings", "imagify_settings", "blog_public", "siteurl", "my-option"])
def test_any_valid_option_allowed(name):
    assert server._wp_check_option(f" {name} ") == name


@pytest.mark.parametrize("name", ["", "a b", "x;rm", "a/b", "x" * 192, "$(id)"])
def test_invalid_option_name_rejected(name):
    with pytest.raises(ValueError, match="Ungültiger Optionsname"):
        server._wp_check_option(name)


@pytest.mark.parametrize(
    "bid",
    ["20260101120000-1a2b3c4d@example.com", "20260101120000-00000000@xn--mnchen-3ya.example"],
)
def test_backup_id_ok(bid):
    assert server._WP_BACKUP_ID_RE.match(bid)


@pytest.mark.parametrize(
    "bid",
    ["../../etc/passwd", "20260101120000-1a2b3c4d@../x", "20260101120000-1a2b3c4d", "x@example.com",
     "20260101120000-1A2B3C4D@example.com", "20260101120000-1a2b3c4d@example.com/../x"],
)
def test_backup_id_rejected(bid):
    m = server._WP_BACKUP_ID_RE.match(bid)
    if m:
        with pytest.raises(ValueError):
            server._wp_domain(m.group(3))


def test_plan_keys():
    missing = server._WP_MISSING
    plan = server._wp_plan({"a": 1, "b": "x"}, "keys", {"a": 1, "b": "y", "c": 2, "d": missing})
    assert plan["a"]["changed"] is False
    assert plan["b"]["changed"] is True and plan["b"]["old"] == "x"
    assert plan["c"]["changed"] is True and plan["c"]["old"] is missing
    assert plan["d"]["changed"] is False
    view = server._wp_plan_view(plan)
    assert view["c"]["alt"] == "(nicht vorhanden)"
    assert view["a"]["status"] == "unverändert"


def test_plan_value_type_change_counts():
    plan = server._wp_plan("1", "value", {"": 1})
    assert plan[""]["changed"] is True
