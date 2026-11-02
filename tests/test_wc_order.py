"""Tests für die Validierung der wc_order_*-Tools (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("val,exp", [("2039", 2039), (" #2039 ", 2039), (17, 17)])
def test_order_id_ok(val, exp):
    assert server._wc_check_order_id(val) == exp


@pytest.mark.parametrize("val", ["", "0", "-5", "20 39", "abc", True, "1" * 10])
def test_order_id_rejected(val):
    with pytest.raises(ValueError):
        server._wc_check_order_id(val)


def test_tracking_ok():
    assert server._wc_check_tracking(" 99.60.123456.12345678 ") == "99.60.123456.12345678"
    assert server._wc_check_tracking("9960 1327 5100") == "996013275100"


@pytest.mark.parametrize("val", ["", "abc", "x'y12", "a;rm -rf", "1" * 51, "-12345"])
def test_tracking_rejected(val):
    with pytest.raises(ValueError):
        server._wc_check_tracking(val)


def test_provider():
    assert server._wc_check_provider("Swiss-Post") == "swiss-post"
    for bad in ["", "swiss post", "a'b", "x" * 51]:
        with pytest.raises(ValueError):
            server._wc_check_provider(bad)


def test_php_has_no_placeholders():
    # Werte kommen nur per stdin, nie per String-Formatierung in den Code
    for php in (server._WC_ORDER_GET_PHP, server._WC_ORDER_SHIP_PHP):
        assert "%s" not in php and "stream_get_contents(STDIN)" in php


def test_php_supports_advanced_shipment_tracking():
    # Advanced Shipment Tracking 4.x kennt nur ast_add_tracking_number.
    assert "ast_add_tracking_number" in server._WC_ORDER_GET_PHP
    assert "ast_add_tracking_number" in server._WC_ORDER_SHIP_PHP
    assert "wc_st_add_tracking_number" in server._WC_ORDER_SHIP_PHP


def test_status_target():
    assert server._wc_check_status("", True) == "completed"
    assert server._wc_check_status("", False) == ""
    assert server._wc_check_status(" WC-Partial-Shipped ", True) == "partial-shipped"
    assert server._wc_check_status("completed", False) == "completed"
    for bad in ["a b", "x'y", "-x", "x" * 40, "1"]:
        with pytest.raises(ValueError):
            server._wc_check_status(bad, True)


def test_php_view_has_backorder_and_label_list():
    assert '"backordered"' in server._WC_ORDER_VIEW_PHP and '"label_tracking_list"' in server._WC_ORDER_VIEW_PHP
    assert '__("Backordered", "woocommerce")' in server._WC_ORDER_VIEW_PHP
    # Zielstatus wird gegen die im Shop registrierten Status geprueft, nie blind gesetzt
    assert "wc_get_order_statuses()" in server._WC_ORDER_SHIP_PHP and '$in["status"]' in server._WC_ORDER_SHIP_PHP


# --- PHP-Vorlage gegen WooCommerce-Stubs ---------------------------------------

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

SHIP_STUBS = r'''<?php
$GLOBALS["log"] = array();
function plog($x) { $GLOBALS["log"][] = $x; }
function __($s, $d) { return "Im Lieferrueckstand"; }
function current_time($f) { return "2026-11-02"; }
function wc_get_order_statuses() { return array("wc-processing" => "In Bearbeitung", "wc-completed" => "Abgeschlossen", "wc-partial-shipped" => "Teilweise versendet"); }
function ast_add_tracking_number($id, $nr, $prov, $date, $st) { plog("ast:" . $nr . ":" . $prov . ":" . $st); $GLOBALS["order"]->tracking[] = array("tracking_number" => $nr); }
class It { function __construct($bo) { $this->bo = $bo; } function get_name() { return "Zylinder"; } function get_quantity() { return 2; } function get_total() { return "320"; }
  function get_meta($k) { return ($k === "Im Lieferrueckstand" && $this->bo) ? $this->bo : ""; } }
class O { public $status = "processing"; public $tracking = array();
  function __call($n, $a) { plog("order:" . $n . "=" . json_encode($a)); if ($n === "update_status") { $this->status = $a[0]; } return ""; }
  function get_id() { return 2052; } function get_order_number() { return "2052"; } function get_status() { return $this->status; }
  function get_type() { return "shop_order"; } function get_date_created() { return null; }
  function get_items() { return array(new It(0), new It(1)); }
  function get_meta($k) { if ($k === "_spap_tracking") { return "996000000000000691"; } if ($k === "_spap_tracking_list") { return "996000000000000687\n996000000000000691"; } if ($k === "_wc_shipment_tracking_items") { return $this->tracking; } return ""; } }
$GLOBALS["order"] = new O();
function wc_get_order($id) { return $GLOBALS["order"]; }
register_shutdown_function(function () { fwrite(STDERR, "\nLOG " . json_encode($GLOBALS["log"])); });
'''


def _php(tmp_path, template, payload):
    f = tmp_path / "t.php"
    f.write_text(SHIP_STUBS + template)
    r = subprocess.run(["php", str(f)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    return r, json.loads(r.stderr.rsplit("LOG ", 1)[1])


@pytest.mark.skipif(not shutil.which("php"), reason="php nicht installiert")
def test_php_get_reports_backorder_and_labels(tmp_path):
    r, _ = _php(tmp_path, server._WC_ORDER_GET_PHP, {"id": 2052, "label_keys": ["_spap_tracking"], "label_list_keys": ["_spap_tracking_list"]})
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)["order"]
    assert o["backordered"] is True and [i["backordered"] for i in o["items"]] == [0, 1]
    assert o["label_tracking"] == {"_spap_tracking": "996000000000000691"}
    assert o["label_tracking_list"] == ["996000000000000687", "996000000000000691"]


@pytest.mark.skipif(not shutil.which("php"), reason="php nicht installiert")
def test_php_ship_partial_status(tmp_path):
    base = {"id": 2052, "tracking_number": "996000000000000691", "provider": "swiss-post",
            "label_keys": ["_spap_tracking"], "label_list_keys": ["_spap_tracking_list"]}
    r, log = _php(tmp_path, server._WC_ORDER_SHIP_PHP, {**base, "status": "partial-shipped"})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["done"] == ["tracking_hinzugefuegt", "status_partial-shipped"] and out["order"]["status"] == "partial-shipped"
    assert "ast:996000000000000691:swiss-post:0" in log
    assert any(x.startswith('order:update_status=["partial-shipped","Als teilweise verschickt') for x in log)
    # nicht registrierter Status: Abbruch mit Fehler, kein update_status
    r, log = _php(tmp_path, server._WC_ORDER_SHIP_PHP, {**base, "status": "shipped"})
    assert r.returncode == 1 and "nicht registriert" in r.stderr
    assert not any(x.startswith("order:update_status") for x in log)
    # ohne Status: nur Tracking und Notiz
    r, log = _php(tmp_path, server._WC_ORDER_SHIP_PHP, {**base, "status": ""})
    assert r.returncode == 0 and json.loads(r.stdout)["done"] == ["tracking_hinzugefuegt"]
    assert any(x.startswith("order:add_order_note") for x in log)
