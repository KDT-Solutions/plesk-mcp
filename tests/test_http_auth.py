"""HTTP-Schutz: Bearer-Token auf allen Pfaden ausser GET /healthz."""

import os
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

API_KEY = "test-mcp-api-key-0123456789abcdef"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def http_client(monkeypatch):
    monkeypatch.setattr(server, "_MCP_API_KEY", API_KEY)
    calls = []
    # Ein Healthcheck darf weder SSH noch die Plesk-API anfassen
    monkeypatch.setattr(server, "ssh_run", lambda *a, **k: calls.append(a) or "")
    with TestClient(server._build_http_app(), base_url="https://mcp.example.com") as client:
        client.upstream_calls = calls
        yield client


def init_request():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    }


def test_healthz_without_token_is_200(http_client):
    resp = http_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert http_client.upstream_calls == []


def test_mcp_without_token_is_401(http_client):
    resp = http_client.post("/mcp", json=init_request(), headers=HEADERS)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("auth", ["Bearer wrong", f"bearer {API_KEY}", API_KEY, "Basic abc"])
def test_mcp_wrong_token_is_401(http_client, auth):
    assert http_client.post("/mcp", json=init_request(), headers={**HEADERS, "Authorization": auth}).status_code == 401


def test_mcp_valid_token_passes(http_client):
    resp = http_client.post("/mcp", json=init_request(), headers={**HEADERS, "Authorization": f"Bearer {API_KEY}"})
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/healthz/", "/healthzx", "/mcp/healthz", "/"])
def test_other_paths_still_need_token(http_client, path):
    assert http_client.get(path).status_code == 401


def test_healthz_only_get(http_client):
    assert http_client.post("/healthz").status_code == 401


def test_upload_without_token_still_401(http_client):
    assert http_client.put("/upload/doesnotexist", content=b"x").status_code == 401


def test_bearer_compared_time_constant(http_client, monkeypatch):
    seen = []
    real = server.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(server.hmac, "compare_digest", spy)
    assert http_client.post("/mcp", json=init_request(), headers={**HEADERS, "Authorization": "Bearer wrong"}).status_code == 401
    assert (b"Bearer wrong", f"Bearer {API_KEY}".encode()) in seen


def _startup(monkeypatch, key=API_KEY, host_key="ssh-ed25519 AAAA", known_hosts="", public="", proxies=frozenset()):
    monkeypatch.setattr(server, "_MCP_API_KEY", key)
    monkeypatch.setattr(server, "_SSH_HOST_KEY", host_key)
    monkeypatch.setattr(server, "_SSH_KNOWN_HOSTS", known_hosts)
    monkeypatch.setattr(server, "_PUBLIC_BASE_URL", public)
    monkeypatch.setattr(server, "_UPLOAD_TRUSTED_PROXIES", set(proxies))
    return server._check_startup_config()


def test_startup_token_checks(monkeypatch):
    with pytest.raises(RuntimeError, match="fehlt"):
        _startup(monkeypatch, key="")
    with pytest.raises(RuntimeError, match="zu kurz"):
        _startup(monkeypatch, key="a" * 31)
    assert _startup(monkeypatch, key="a" * 32) == []


def test_startup_requires_pinned_host_key(monkeypatch):
    with pytest.raises(RuntimeError, match="PLESK_SSH_HOST_KEY"):
        _startup(monkeypatch, host_key="", known_hosts="")
    assert _startup(monkeypatch, host_key="", known_hosts="/run/secrets/known_hosts") == []


def test_startup_warns_without_trusted_proxies(monkeypatch):
    assert _startup(monkeypatch, public="https://mcp.example.com") != []
    assert _startup(monkeypatch, public="https://mcp.example.com", proxies={"172.18.0.1"}) == []
    assert _startup(monkeypatch, public="") == []


def test_main_refuses_short_token(monkeypatch):
    monkeypatch.setattr(server, "_MCP_API_KEY", "kurz")
    monkeypatch.setattr(server, "_run_http_server", lambda: (_ for _ in ()).throw(AssertionError("darf nicht starten")))
    with pytest.raises(RuntimeError, match="zu kurz"):
        server.main()
