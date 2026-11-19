"""Nur-Lese-Modus (MCP_READ_ONLY): Es sind nur ausdrücklich als lesend
markierte Tools registriert, der Upload-Endpunkt ist zu. Dazu die Regel, dass
jedes Tool entweder als lesend markiert ist oder eine confirm-Bestätigung hat."""

import inspect
import json
import os
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402

API_KEY = "test-mcp-api-key-0123456789abcdef"


def _tools():
    return server.mcp._tool_manager.list_tools()


def _is_read_only(tool) -> bool:
    return server._is_read_only_tool(tool.annotations)


def _has_confirm(tool) -> bool:
    return "confirm" in inspect.signature(tool.fn).parameters


def test_default_mode_registers_everything():
    assert server._READ_ONLY is False
    assert server._READ_ONLY_HIDDEN == []
    names = {t.name for t in _tools()}
    assert {"domain_info", "write_vhost_file", "subscription_delete", "wp_post_get", "wp_post_update"} <= names


def test_no_read_only_tool_asks_for_confirm():
    wrong = [t.name for t in _tools() if _is_read_only(t) and _has_confirm(t)]
    assert wrong == [], f"als lesend markiert, aber mit confirm-Parameter: {wrong}"


def test_every_server_tool_is_read_only_or_has_confirm():
    """Tools aus server.py: ohne confirm-Parameter muss ein Tool als lesend
    markiert sein (annotations=_READ_ONLY_TOOL) - sonst fehlt es im
    Nur-Lese-Modus, obwohl es nur liest, oder es schreibt ohne Bestätigung."""
    wpsite = set(server._WPSITE_TOOLS["registered"])
    unclear = [t.name for t in _tools()
               if t.name not in wpsite and not _is_read_only(t) and not _has_confirm(t)]
    assert unclear == [], f"weder lesend markiert noch mit confirm: {unclear}"


def _read_only_process(extra_env: dict[str, str] | None = None) -> dict:
    code = (
        "import json, server\n"
        "print('RESULT ' + json.dumps({"
        "'tools': sorted(t.name for t in server.mcp._tool_manager.list_tools()),"
        "'hidden': sorted(server._READ_ONLY_HIDDEN),"
        "'wpsite': sorted(server._WPSITE_TOOLS['registered']),"
        "'version': json.loads(server.get_version()),"
        "'warnings': server._check_startup_config()}))\n"
    )
    env = {**os.environ, "MCP_READ_ONLY": "true", "MCP_API_KEY": API_KEY,
           "PLESK_SSH_HOST_KEY": "ssh-ed25519 AAAA", **(extra_env or {})}
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120, check=True)
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])


def test_read_only_mode_registers_only_read_tools():
    expected_read = {t.name for t in _tools() if _is_read_only(t)}
    expected_hidden = {t.name for t in _tools() if not _is_read_only(t)}
    res = _read_only_process()
    assert set(res["tools"]) == expected_read
    assert expected_hidden <= set(res["hidden"])
    assert not set(res["tools"]) & set(res["hidden"])
    # Stichproben: Lesen ja, Schreiben nein - auch bei den WordPress-Tools
    for name in ("get_version", "domain_info", "read_vhost_file", "search_log", "db_query", "wp_post_get"):
        assert name in res["tools"]
    for name in ("write_vhost_file", "upload_begin", "delete_vhost_file", "subscription_delete",
                 "dns_add_record", "wp_option_update", "wp_post_update", "wp_post_trash", "wp_plugin_install"):
        assert name not in res["tools"]
    assert set(res["wpsite"]) <= set(res["tools"])
    assert res["version"]["read_only"] is True
    assert res["version"]["hidden_write_tools"] == len(res["hidden"])


def test_read_only_mode_warns_about_trash_autoclean():
    res = _read_only_process({"VHOST_TRASH_AUTOCLEAN_DAYS": "30"})
    assert any("VHOST_TRASH_AUTOCLEAN_DAYS" in w for w in res["warnings"])


def test_get_version_reports_mode():
    info = json.loads(server.get_version())
    assert info["read_only"] is False and info["ssh_sudo"] is False
    assert "hidden_write_tools" not in info
    assert info["license"] == "AGPL-3.0-only"
    assert info["source"].startswith("https://")


@pytest.fixture
def http_client(monkeypatch):
    monkeypatch.setattr(server, "_MCP_API_KEY", API_KEY)
    with TestClient(server._build_http_app(), base_url="https://mcp.example.com") as client:
        yield client


def test_upload_endpoint_closed_in_read_only_mode(http_client, monkeypatch):
    monkeypatch.setattr(server, "_READ_ONLY", True)
    for headers in ({}, {"Authorization": "Bearer irgendein-token"}, {"Authorization": f"Bearer {API_KEY}"}):
        resp = http_client.put("/upload/abcdefghijklmnopqrstuv", content=b"x", headers=headers)
        assert resp.status_code == 403
        assert resp.json() == {"error": "read_only"}
    # Der Healthcheck bleibt erreichbar
    assert http_client.get("/healthz").status_code == 200


def test_upload_endpoint_unchanged_in_default_mode(http_client):
    resp = http_client.put("/upload/abcdefghijklmnopqrstuv", content=b"x")
    assert resp.status_code == 401
