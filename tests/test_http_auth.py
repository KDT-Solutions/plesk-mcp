"""HTTP-Schutz: Bearer-Token auf allen Pfaden ausser GET /healthz."""

import os
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

API_KEY = "test-mcp-api-key"
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
