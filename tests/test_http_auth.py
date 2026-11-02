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


# --- Brute-Force-Bremse -----------------------------------------------------------

def test_rate_limit_after_failures(http_client, monkeypatch):
    bad = {**HEADERS, "Authorization": "Bearer wrong"}
    good = {**HEADERS, "Authorization": f"Bearer {API_KEY}"}
    for _ in range(server.AUTH_MAX_FAILURES):
        resp = http_client.post("/mcp", json=init_request(), headers=bad)
        assert resp.status_code == 401 and resp.json() == {"error": "unauthorized"}
    resp = http_client.post("/mcp", json=init_request(), headers=bad)
    assert resp.status_code == 429
    assert resp.json() == {"error": "too many failed attempts"}
    assert resp.headers["Retry-After"] == str(int(server.AUTH_FAILURE_WINDOW))
    assert http_client.get("/").status_code == 429  # ohne Token ebenfalls gebremst
    # gueltiges Token wird nie gebremst, /healthz bleibt frei
    assert http_client.post("/mcp", json=init_request(), headers=good).status_code == 200
    assert http_client.get("/healthz").status_code == 200
    # nach Ablauf des Fensters wieder 401
    now = server.time.monotonic()
    monkeypatch.setattr(server.time, "monotonic", lambda: now + server.AUTH_FAILURE_WINDOW + 1)
    assert http_client.post("/mcp", json=init_request(), headers=bad).status_code == 401


def test_rate_limit_defaults():
    lim = server._AuthFailureLimiter()
    assert lim.max_failures == 10 and lim.window == 600.0


def test_rate_limit_logs_without_token(http_client, caplog):
    caplog.set_level("WARNING", logger="plesk_mcp.auth")
    http_client.post("/mcp", json=init_request(), headers={**HEADERS, "Authorization": "Bearer geheim-xyz"})
    assert any("Auth fehlgeschlagen (1/" in r.getMessage() for r in caplog.records)
    assert "geheim-xyz" not in caplog.text


def test_failure_limiter_window():
    lim = server._AuthFailureLimiter(max_failures=2, window=100)
    assert not lim.blocked()
    lim._failures.extend([0.0, 1.0])
    assert lim.blocked() is (server.time.monotonic() - 1.0 <= 100)


def test_rate_limit_env_override(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_FAILURES", "2")
    monkeypatch.setenv("AUTH_FAILURE_WINDOW", "30")
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", server._auth_limit_setting("AUTH_MAX_FAILURES", 10, int))
    monkeypatch.setattr(server, "AUTH_FAILURE_WINDOW", server._auth_limit_setting("AUTH_FAILURE_WINDOW", 600.0, float))
    assert server.AUTH_MAX_FAILURES == 2 and server.AUTH_FAILURE_WINDOW == 30.0
    monkeypatch.setattr(server, "_MCP_API_KEY", API_KEY)
    bad = {**HEADERS, "Authorization": "Bearer wrong"}
    with TestClient(server._build_http_app(), base_url="https://mcp.example.com") as client:
        assert client.post("/mcp", json=init_request(), headers=bad).status_code == 401
        assert client.post("/mcp", json=init_request(), headers=bad).status_code == 401
        resp = client.post("/mcp", json=init_request(), headers=bad)
        assert resp.status_code == 429 and resp.headers["Retry-After"] == "30"


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "1.5"])
def test_rate_limit_env_invalid_falls_back(monkeypatch, raw):
    monkeypatch.setenv("AUTH_MAX_FAILURES", raw)
    assert server._auth_limit_setting("AUTH_MAX_FAILURES", 10, int) == 10


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
