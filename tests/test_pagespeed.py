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

    calls = []

    def fake_get(*a, **k):
        calls.append(k)
        return Resp()

    monkeypatch.setattr(server.httpx, "get", fake_get)
    out = server.pagespeed_insights("example.com", strategy="both")
    assert "SECRETKEY123" not in out
    # Key nur im Header, nie in der URL (sonst landet er im httpx-Log)
    assert all(k["headers"] == {"X-goog-api-key": "SECRETKEY123"} for k in calls)
    assert all("SECRETKEY123" not in str(k["params"]) for k in calls)
    data = json.loads(out)
    assert data["mobile"]["error"].startswith("HTTP 400") and "desktop" in data


def _lh13_sample(doc_size=85000, lcp_label="Willkommen"):
    return {
        "lighthouseResult": {
            "requestedUrl": "https://example.com/", "mainDocumentUrl": "https://www.example.com/",
            "finalDisplayedUrl": "https://www.example.com/", "lighthouseVersion": "13.0.0",
            "categories": {"performance": {"score": 0.5, "auditRefs": [
                {"id": "render-blocking-insight", "group": "insights"},
                {"id": "document-latency-insight", "group": "insights"},
                {"id": "image-delivery-insight", "group": "insights"},
                {"id": "server-response-time", "group": "hidden"},
            ]}},
            "audits": {
                "server-response-time": {
                    "title": "Server response", "score": 0, "scoreDisplayMode": "metricSavings",
                    "numericValue": 1450.4, "numericUnit": "millisecond", "displayValue": "Root document took 1,450 ms",
                    "details": {"type": "opportunity", "items": [{"url": "https://www.example.com/", "responseTime": 1450.4}]},
                },
                "document-latency-insight": {
                    "title": "Document request latency", "score": 0, "scoreDisplayMode": "metricSavings",
                    "metricSavings": {"FCP": 1350, "LCP": 1350},
                    "details": {
                        "type": "checklist",
                        "items": {
                            "noRedirects": {"label": "Had redirects (1 redirects, +120 ms)", "value": False},
                            "serverResponseIsFast": {"label": "Server responded slowly", "value": False},
                            "usesCompression": {"label": "Applies text compression", "value": True},
                        },
                        "debugData": {"type": "debugdata", "redirectDuration": 120,
                                      "serverResponseTime": 1450.4, "uncompressedResponseBytes": 0},
                    },
                },
                "network-rtt": {"numericValue": 12.3, "numericUnit": "millisecond", "displayValue": "10 ms",
                                "details": {"type": "table", "items": [{"origin": "https://www.example.com", "rtt": 12.3}]}},
                "render-blocking-insight": {
                    "title": "Render blocking", "score": 0, "scoreDisplayMode": "metricSavings",
                    "metricSavings": {"FCP": 600, "LCP": 600},
                    "details": {"type": "table", "items": [
                        {"url": "https://www.example.com/a.css", "totalBytes": 30000, "wastedMs": 150},
                        {"url": "https://www.example.com/b.css", "totalBytes": 90000, "wastedMs": 450},
                        {"url": "https://www.example.com/c.js", "totalBytes": 5000, "wastedMs": 50},
                    ]},
                },
                "image-delivery-insight": {
                    "title": "Images", "score": 0, "scoreDisplayMode": "metricSavings",
                    "details": {"type": "table", "items": [{
                        "node": {"type": "node", "nodeLabel": "Logo", "snippet": "<img src=logo.png>"},
                        "url": "https://www.example.com/logo.png", "totalBytes": 400000, "wastedBytes": 300000,
                        "subItems": {"type": "subitems", "items": [{"reason": "Use WebP", "wastedBytes": 300000}]},
                    }], "overallSavingsBytes": 300000},
                },
                "largest-contentful-paint-element": {
                    "details": {"type": "list", "items": [{"type": "table", "items": [
                        {"node": {"type": "node", "nodeLabel": lcp_label}}]}]},
                },
                "network-requests": {"details": {"type": "table", "items": [
                    {"url": "https://example.com/", "resourceType": "Document", "statusCode": 301,
                     "transferSize": 300, "resourceSize": 0},
                    {"url": "https://www.example.com/", "resourceType": "Document", "statusCode": 200,
                     "mimeType": "text/html", "transferSize": 21000, "resourceSize": doc_size},
                ]}},
            },
        },
    }


def test_server_timing_lh13():
    out = server._psi_summarize(_lh13_sample(), ["performance"], 10)
    st = out["lab_metrics"]["server_timing"]
    assert st["server_response_time"]["numeric_value"] == 1450.4
    assert st["server_response_time"]["response_time_ms"] == 1450.4
    dl = st["document_latency_insight"]
    assert dl["checks"]["noRedirects"]["passed"] is False
    assert dl["checks"]["usesCompression"]["passed"] is True
    assert dl["redirect_duration_ms"] == 120 and dl["uncompressed_response_bytes"] == 0
    assert st["network_rtt"]["origins"][0]["rtt_ms"] == 12.3
    assert st["network_server_latency"] == server._PSI_MISSING
    # Bestehende Felder bleiben unverändert, Details nur auf Wunsch
    assert "details" not in out["performance_findings"][0]


def test_server_timing_missing_everything():
    st = server._psi_server_timing({})
    assert set(st.values()) == {server._PSI_MISSING}


def test_finding_details():
    out = server._psi_summarize(_lh13_sample(), ["performance"], 10, include_details=True, max_items=2)
    by_id = {f["id"]: f for f in out["performance_findings"]}
    rb = by_id["render-blocking-insight"]["details"]
    assert rb["items_total"] == 3
    assert [i["url"].rsplit("/", 1)[1] for i in rb["items"]] == ["b.css", "a.css"]
    img = by_id["image-delivery-insight"]["details"]["items"][0]
    assert img["wastedBytes"] == 300000 and img["reasons"][0]["reason"] == "Use WebP"
    assert "note" in by_id["document-latency-insight"]["details"]


def test_page_check_ok():
    pc = server._psi_summarize(_lh13_sample(), ["performance"], 10)["page_check"]
    assert pc["main_document"]["status_code"] == 200
    assert pc["main_document"]["url"] == "https://www.example.com/"
    assert pc["title"] == server._PSI_MISSING
    assert pc["warnings"] == []


def test_page_check_challenge():
    pc = server._psi_page_check(_lh13_sample(doc_size=4000, lcp_label="Just a moment...")["lighthouseResult"])
    assert len(pc["warnings"]) == 2
    assert all(w.startswith("moegliche Challenge-/Zwischenseite") for w in pc["warnings"])


def test_page_check_without_network_requests():
    pc = server._psi_page_check({"finalDisplayedUrl": "https://example.com/", "audits": {}})
    assert pc["main_document"] == server._PSI_MISSING and pc["warnings"] == []


def test_tool_passes_details(monkeypatch):
    monkeypatch.setattr(server, "_psi_run", lambda *a: _lh13_sample())
    data = json.loads(server.pagespeed_insights("example.com", include_details=True, max_items=500))
    f = {x["id"]: x for x in data["mobile"]["performance_findings"]}
    assert len(f["render-blocking-insight"]["details"]["items"]) == 3
    assert "server_timing" in data["mobile"]["lab_metrics"]
