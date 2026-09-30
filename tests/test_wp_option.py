"""Tests für die Validierung von wp_option_update/wp_option_rollback (ohne Serverzugriff).

Ausführen: pip install -r requirements.txt pytest && pytest
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def test_allowlist_default():
    assert server._wp_check_option("wp_rocket_settings") == "wp_rocket_settings"
    assert server._wp_check_option("elementor_font_display") == "elementor_font_display"


@pytest.mark.parametrize("name", ["siteurl", "home", "admin_email", "active_plugins", "", "a b", "x;rm"])
def test_allowlist_rejects(name):
    with pytest.raises(ValueError):
        server._wp_check_option(name)


def test_allowlist_env(monkeypatch):
    monkeypatch.setenv("WP_OPTION_ALLOWLIST", "blog_public, bad name ,my_option")
    assert server._wp_check_option("my_option") == "my_option"
    assert server._wp_check_option("blog_public") == "blog_public"
    with pytest.raises(ValueError):
        server._wp_check_option("bad name")


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
