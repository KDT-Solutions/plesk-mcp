"""Tests für die WordPress-Tools (wpsite): Seitenangabe, PHP-Skript, Auswertung,
Registrierung. Ohne Serverzugriff.

Optionaler Integrationstest gegen eine lokale WordPress-Installation (ohne
Plugin tabside-mcp), z.B. aus tabside/wordpress-mcp plugin/tests/setup-wordpress.sh:

    WPSITE_TEST_WP=/tmp/wp WPSITE_TEST_WPCLI=~/.cache/tabside-wp-test/wp-cli.phar pytest tests/test_wpsite.py
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import wpsite  # noqa: E402
from wpsite.tools import tool_name  # noqa: E402


@pytest.mark.parametrize("site,expected", [
    ("example.com", ("example.com", None)),
    (" Example.COM ", ("example.com", None)),
    ("example.com:httpdocs", ("example.com", "httpdocs")),
    ("example.com:/httpdocs/blog/", ("example.com", "httpdocs/blog")),
])
def test_parse_site(site, expected):
    assert server._wpsite_parse(site) == expected


@pytest.mark.parametrize("site", ["", "exa mple.com", "example.com:", "../etc", "example.com;id"])
def test_parse_site_rejected(site):
    with pytest.raises(ValueError):
        server._wpsite_parse(site)


def test_wp_path_traversal_rejected():
    with pytest.raises(ValueError, match="verlässt"):
        server._vhost_path("example.com", "httpdocs/../../other.com/httpdocs")


def test_build_script_has_classes_and_payload():
    script = wpsite.build_script("post-get", {"id": 5, "title": "O'Reilly \\ $x"}).decode()
    assert script.startswith("<?php\n")
    assert len(re.findall(r"^<\?php", script, re.M)) == 1
    assert "class_exists( 'Tabside_MCP_Abilities', false )" in script
    assert "final class Tabside_MCP_Abilities" in script
    # geladen, aber nie gestartet (keine Hooks, keine Update-Abfragen)
    assert "Tabside_MCP_Updater::boot" not in script
    assert "Tabside_MCP_Plugin::boot" not in script
    m = re.search(r"json_decode\( base64_decode\( '([A-Za-z0-9+/=]+)' \), true \)", script)
    assert m
    assert json.loads(base64.b64decode(m.group(1))) == {"ability": "post-get", "input": {"id": 5, "title": "O'Reilly \\ $x"}}
    assert "O'Reilly" not in script


def test_ability_name():
    assert wpsite.ability_name("elementor_replace_text") == "elementor-replace-text"


def test_parse_output_ok_with_noise():
    out = "Warnung von einem Plugin\n" + wpsite.MARKER + json.dumps({"ok": True, "result": {"a": 1}, "source": "plesk-mcp 2"}) + "\n"
    assert wpsite.parse_output("post_get", out) == {"a": 1, "plugin_code": "plesk-mcp 2"}


def test_parse_output_unicode_line_separators():
    result = {"text": "a\u2028b\u0085c\x1cd"}
    out = wpsite.MARKER + json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n"
    assert wpsite.parse_output("post_get", out) == result


def test_parse_output_errors():
    with pytest.raises(wpsite.WPSiteError, match="Keine Antwort"):
        wpsite.parse_output("post_get", "Fatal error")
    with pytest.raises(wpsite.WPSiteError, match="nicht gefunden"):
        wpsite.parse_output("post_get", wpsite.MARKER + json.dumps({"ok": False, "code": "x", "message": "Beitrag 3 nicht gefunden."}))
    with pytest.raises(wpsite.WPSiteError, match="Elementor ist"):
        wpsite.parse_output("elementor_get", wpsite.MARKER + json.dumps({"ok": False, "code": "pmcp_ability_not_found", "message": ""}))
    with pytest.raises(wpsite.WPSiteError, match="Fundstellen"):
        wpsite.parse_output("elementor_replace_text", wpsite.MARKER + json.dumps(
            {"ok": False, "code": "c", "message": "3 statt 1 Treffer.", "data": {"hits": [{"id": "a"}]}}))


def test_tool_names_and_skips():
    assert tool_name("post_list") == "wp_post_list"
    assert tool_name("wptc_status") == "wptc_status"
    reg = server._WPSITE_TOOLS["registered"]
    assert {"wp_post_update", "wp_elementor_replace_text", "wp_menu_get", "wp_user_list", "wp_media_upload"} <= set(reg)
    # vorhandene Tools des plesk-mcp bleiben, keine Doppelten
    assert {"wp_plugin_list", "wp_option_update", "wp_custom_css_get", "wptc_set_revision_limit"} <= set(server._WPSITE_TOOLS["skipped"])
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(names) == len(set(names))


def test_site_description_appended():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "example.com:httpdocs/blog" in tools["wp_post_get"].description
    assert "example.com:httpdocs/blog" not in tools["wp_docx_to_content"].description


# --------------------------------------------------------------------------- Integration (optional)

WP_DIR = os.environ.get("WPSITE_TEST_WP")
WP_CLI = os.path.expanduser(os.environ.get("WPSITE_TEST_WPCLI", "~/.cache/tabside-wp-test/wp-cli.phar"))
integration = pytest.mark.skipif(not WP_DIR or not os.path.isfile(WP_CLI), reason="WPSITE_TEST_WP nicht gesetzt")


def _local_call(site, tool, args):
    script = wpsite.build_script(wpsite.ability_name(tool), args)
    p = subprocess.run(["php", WP_CLI, "--allow-root", f"--path={WP_DIR}", "eval-file", "-"], input=script, capture_output=True, timeout=120)
    return wpsite.parse_output(tool, p.stdout.decode())


def _call(name, args):
    result = asyncio.run(server.mcp.call_tool(name, args))
    return result


@pytest.fixture
def local_site(monkeypatch):
    monkeypatch.setattr(server._wpsite_runner, "_call", _local_call)
    audit = []
    monkeypatch.setattr(server._wpsite_runner, "_audit", lambda action, **f: audit.append((action, f)))
    return audit


def _data(result):
    if isinstance(result, tuple):
        result = result[1] if isinstance(result[1], dict) else result[0]
    if isinstance(result, dict):
        return result.get("result", result)
    text = result[0].text if isinstance(result, list) else result.content[0].text
    return json.loads(text)


@integration
def test_integration_post_roundtrip(local_site):
    info = _data(_call("wp_site_info", {"site": "example.com"}))
    assert info["plugin_code"].startswith(("plesk-mcp", "site-plugin"))
    assert info["admin_tools"] == "enabled"

    preview = _data(_call("wp_post_create", {"site": "example.com", "title": "Übertitel-Test"}))
    assert preview["confirmed"] is False
    created = _data(_call("wp_post_create", {"site": "example.com", "title": "Übertitel-Test", "excerpt": "A", "confirm": True}))
    pid = created["post"]["id"]
    updated = _data(_call("wp_post_update", {"site": "example.com", "id": pid, "excerpt": "B", "confirm": True}))
    assert updated["backup_id"]
    assert _data(_call("wp_post_get", {"site": "example.com", "id": pid}))["excerpt"] == "B"
    _data(_call("wp_post_rollback", {"site": "example.com", "id": pid, "confirm": True}))
    assert _data(_call("wp_post_get", {"site": "example.com", "id": pid}))["excerpt"] == "A"
    log = _data(_call("wp_audit_log", {"site": "example.com"}))
    assert log["entries"][0]["ability"] == "post-rollback"
    assert [a for a, f in local_site if f.get("executed")]
    _data(_call("wp_post_trash", {"site": "example.com", "id": pid, "confirm": True}))


@integration
def test_integration_errors(local_site):
    with pytest.raises(Exception, match="nicht gefunden"):
        _call("wp_post_get", {"site": "example.com", "id": 999999})
