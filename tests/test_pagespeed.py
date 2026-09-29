"""Offline-Tests für das pagespeed_insights-Tool (kein Netzwerkzugriff)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com/"),
    ("https://www.example.com/shop?x=1", "https://www.example.com/shop?x=1"),
    ("http://example.ch", "http://example.ch/"),
])
def test_normalize_ok(raw, expected):
    assert server._psi_normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "ftp://example.com", "https://user:pw@example.com", "localhost",
    "http://127.0.0.1", "https://10.0.0.1/", "exa mple.com", "https://-bad.com",
])
def test_normalize_rejects(raw):
    with pytest.raises(ValueError):
        server._psi_normalize_url(raw)


SAMPLE = {
    "loadingExperience": {
        "id": "https://example.com/", "overall_category": "AVERAGE",
        "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2900, "category": "AVERAGE"}},
    },
    "lighthouseResult": {
        "finalDisplayedUrl": "https://example.com/", "fetchTime": "2026-09-29T10:00:00Z",
        "lighthouseVersion": "12.8.0",
        "categories": {
            "performance": {"score": 0.62, "auditRefs": [
                {"id": "largest-contentful-paint", "group": "metrics"},
                {"id": "render-blocking-resources", "group": "diagnostics"},
                {"id": "server-response-time", "group": "diagnostics"},
                {"id": "uses-long-cache-ttl", "group": "diagnostics"},
                {"id": "good-audit", "group": "diagnostics"},
            ]},
            "seo": {"score": 0.9, "auditRefs": [{"id": "meta-description"}, {"id": "document-title"}]},
        },
        "audits": {
            "largest-contentful-paint": {"displayValue": "4.1 s", "score": 0.3},
            "render-blocking-resources": {"title": "Render-blocking", "score": 0.4, "scoreDisplayMode": "metricSavings",
                                          "details": {"type": "opportunity", "overallSavingsMs": 820}},
            "server-response-time": {"title": "TTFB", "score": 0, "scoreDisplayMode": "binary",
                                     "metricSavings": {"FCP": 1200, "LCP": 1200}},
            "uses-long-cache-ttl": {"title": "Cache", "score": 0.5, "scoreDisplayMode": "numeric",
                                    "details": {"overallSavingsBytes": 204800}},
            "good-audit": {"title": "ok", "score": 1, "scoreDisplayMode": "binary"},
            "meta-description": {"title": "Meta description fehlt", "score": 0, "scoreDisplayMode": "binary"},
            "document-title": {"title": "Titel", "score": 1, "scoreDisplayMode": "binary"},
        },
    },
}


def test_summarize():
    out = server._psi_summarize(SAMPLE, ["performance", "seo"], 10)
    assert out["scores"] == {"performance": 62, "seo": 90}
    ids = [f["id"] for f in out["performance_findings"]]
    assert ids == ["server-response-time", "render-blocking-resources", "uses-long-cache-ttl"]
    assert out["performance_findings"][2]["est_savings_kib"] == 200
    assert out["field_data_url"]["metrics"]["largest_contentful_paint"]["p75"] == 2900
    assert out["field_data_origin"] is None
    assert [a["id"] for a in out["seo_failed_audits"]] == ["meta-description"]


def test_tool_rejects_bad_input_without_network():
    assert "error" in json.loads(server.pagespeed_insights("localhost"))
    assert "error" in json.loads(server.pagespeed_insights("example.com", strategy="tablet"))
    assert "error" in json.loads(server.pagespeed_insights("example.com", categories="pwa"))


def test_api_key_not_leaked(monkeypatch):
    monkeypatch.setattr(server, "_PSI_API_KEY", "SECRETKEY123")

    class Resp:
        status_code = 400
        text = ""
        def json(self):
            return {"error": {"message": "bad key=SECRETKEY123"}}

    monkeypatch.setattr(server.httpx, "get", lambda *a, **k: Resp())
    out = server.pagespeed_insights("example.com", strategy="both")
    assert "SECRETKEY123" not in out
    data = json.loads(out)
    assert data["mobile"]["error"].startswith("HTTP 400") and "desktop" in data
