"""Tests für wp_theme_list/wp_theme_update/wp_theme_rollback (ohne Serverzugriff, mit Mocks)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

WP_DIR = "/var/www/vhosts/example.com/httpdocs"
THEME_DIR = WP_DIR + "/wp-content/themes"

INSTALLED = {
    "child": {"name": "child", "status": "active", "version": "1.0", "update": "none", "update_version": ""},
    "parenttheme": {"name": "parenttheme", "status": "parent", "version": "2.0", "update": "available",
                    "update_version": "2.1"},
    "twentytwentyfive": {"name": "twentytwentyfive", "status": "inactive", "version": "1.1",
                         "update": "available", "update_version": "1.2"},
}


@pytest.mark.parametrize("slug", ["", "a b", "../x", "a/b", "*", "x*", "a..b", "$(id)", "-x"])
def test_theme_slugs_rejected(slug):
    with pytest.raises(ValueError, match="Theme-Name"):
        server._wp_theme_slugs([slug])


def test_theme_slugs_ok():
    assert server._wp_theme_slugs(["twentytwentyfive", "twentytwentyfive", "hello-elementor"]) == [
        "twentytwentyfive", "hello-elementor"]


def test_theme_argument_rules():
    with pytest.raises(ValueError, match="nicht beides"):
        server.wp_theme_update("example.com", "httpdocs", themes=["a"], all_updates=True)
    with pytest.raises(ValueError, match="themes angeben"):
        server.wp_theme_update("example.com", "httpdocs")
    with pytest.raises(ValueError, match="genau einem Theme"):
        server.wp_theme_update("example.com", "httpdocs", themes=["a", "b"], version="1.0")
    with pytest.raises(ValueError, match="backup_id"):
        server.wp_theme_rollback("../../etc@example.com")


def test_child_theme_hint():
    assert "child" in server._wp_theme_hint(INSTALLED, ["parenttheme"])
    assert server._wp_theme_hint(INSTALLED, ["twentytwentyfive"]) is None


class _Closable:
    def close(self):
        pass

    def open_sftp(self):
        return _Closable()


@pytest.fixture
def env(monkeypatch):
    calls = {"backup": 0, "cli": [], "restore": 0, "http": []}
    state = {"after": None, "http": []}

    monkeypatch.setattr(server, "_ssh_connect", lambda: _Closable())
    monkeypatch.setattr(server, "_wp_context", lambda client, d: {
        "domain": d, "login": "kunde", "home": "/var/www/vhosts/example.com",
        "php": "/opt/plesk/php/8.2/bin/php", "memory_limit": "512M", "warnings": []})
    monkeypatch.setattr(server, "_wp_install_dir", lambda sftp, d, p: WP_DIR)
    monkeypatch.setattr(server, "_assert_real_within_vhost", lambda sftp, full, d: None)
    lists = {"n": 0}

    def fake_list(client, ctx, wp_dir, kind):
        assert kind == "theme"
        lists["n"] += 1
        return INSTALLED if lists["n"] == 1 or state["after"] is None else state["after"]

    monkeypatch.setattr(server, "_wp_ext_list", fake_list)
    monkeypatch.setattr(server, "_wp_ext_dir", lambda client, ctx, wp_dir, d, kind: THEME_DIR)

    def fake_backup(client, ctx, kind, wp_path, pdir, items, source):
        calls["backup"] += 1
        return "20260101120000-1a2b3c4d@example.com"

    monkeypatch.setattr(server, "_wp_ext_backup", fake_backup)

    def fake_cli(client, ctx, wp_dir, args, stdin_data=None, timeout=None):
        calls["cli"].append(args)
        return "Success", "", 0

    monkeypatch.setattr(server, "_wp_cli_run", fake_cli)

    def fake_http(d):
        status = state["http"].pop(0)
        calls["http"].append(status)
        return status, f"https://{d}/ -> {status}"

    monkeypatch.setattr(server, "_phps_http", fake_http)
    monkeypatch.setattr(server, "_wp_ext_read_backup", lambda client, ctx, kind, bid: {
        "domain": "example.com", "wp_path": "httpdocs", "theme_dir": THEME_DIR,
        "themes": {"twentytwentyfive": {"entry": "twentytwentyfive", "archive": "twentytwentyfive.tar.gz",
                                        "version": "1.1"}}})

    def fake_restore(client, ctx, kind, bid, record, slugs):
        calls["restore"] += 1
        return []

    monkeypatch.setattr(server, "_wp_ext_restore", fake_restore)
    return calls, state


def test_preview_changes_nothing(env):
    calls, _ = env
    res = json.loads(server.wp_theme_update("example.com", "httpdocs", all_updates=True))
    assert "Vorschau" in res["ergebnis"]
    assert {u["name"] for u in res["updates"]} == {"parenttheme", "twentytwentyfive"}
    assert calls["backup"] == 0 and calls["cli"] == [] and calls["restore"] == 0 and calls["http"] == []


def test_update_ok_with_child_hint(env):
    calls, state = env
    state["after"] = dict(INSTALLED, parenttheme=dict(INSTALLED["parenttheme"], version="2.1"))
    state["http"] = [200, 200]
    res = json.loads(server.wp_theme_update("example.com", "httpdocs", themes=["parenttheme"], confirm=True))
    assert res["aktualisiert"] == ["parenttheme"]
    assert calls["cli"] == [["theme", "update", "parenttheme"]]
    assert calls["backup"] == 1 and calls["restore"] == 0
    assert "child" in res["hinweis_child_theme"]
    assert res["backup_ordner"].endswith("/.plesk-mcp/wp-theme-backups/20260101120000-1a2b3c4d@example.com")


def test_update_rolls_back_on_new_5xx(env):
    calls, state = env
    state["after"] = dict(INSTALLED, twentytwentyfive=dict(INSTALLED["twentytwentyfive"], version="1.2"))
    state["http"] = [200, 500, 200]
    res = json.loads(server.wp_theme_update("example.com", "httpdocs", themes=["twentytwentyfive"], confirm=True))
    assert calls["restore"] == 1
    assert res["automatisch_zurueckgestellt"] is True
    assert res["ergebnis"].startswith("ZURÜCKGESTELLT")
    assert res["aktualisiert"] == [] and res["zurueckgestellt"] == ["twentytwentyfive"]


def test_no_rollback_if_5xx_before(env):
    calls, state = env
    state["after"] = dict(INSTALLED, twentytwentyfive=dict(INSTALLED["twentytwentyfive"], version="1.2"))
    state["http"] = [503, 503]
    res = json.loads(server.wp_theme_update("example.com", "httpdocs", themes=["twentytwentyfive"], confirm=True))
    assert calls["restore"] == 0 and res["aktualisiert"] == ["twentytwentyfive"]


def test_rollback_preview_changes_nothing(env):
    calls, _ = env
    res = json.loads(server.wp_theme_rollback("20260101120000-1a2b3c4d@example.com"))
    assert "Vorschau" in res["ergebnis"]
    assert res["themes"][0]["version_backup"] == "1.1"
    assert calls["backup"] == 0 and calls["restore"] == 0
