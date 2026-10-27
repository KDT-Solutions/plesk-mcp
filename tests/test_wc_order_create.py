"""Tests für wc_order_create: Validierung, Vorschau und PHP-Templates
(php -l und Ablauf gegen WooCommerce-Stubs, wenn php installiert ist)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

BILLING = {"company": "Muster AG", "first_name": "Max", "last_name": "Muster",
           "address_1": "Musterstrasse 1", "postcode": "3000", "city": "Bern",
           "country": "ch", "email": "Info@Example.com", "phone": "+41 31 000 00 00"}
ITEMS = [{"product_id": 123, "quantity": 1, "price": 0}]


def _payload(**kw):
    args = dict(billing=BILLING, shipping=None, items=ITEMS, shipping_method="flat_rate:3",
                shipping_cost=0, status="processing", suppress_customer_emails=True,
                customer_note="", order_note="Muster leihweise, Ticket#123")
    args.update(kw)
    return server._wc_order_create_payload(**args)


def test_payload_ok():
    p = _payload()
    assert p["billing"]["country"] == "CH" and p["billing"]["email"] == "info@example.com"
    assert "email" not in p["shipping"] and p["shipping"]["company"] == "Muster AG"
    assert p["items"] == [{"product_id": 123, "quantity": 1, "price": "0"}]
    assert p["shipping_cost"] == "0" and p["suppress_customer_emails"] is True


@pytest.mark.parametrize("kw,match", [
    ({"status": "cancelled"}, "status"),
    ({"shipping_method": "flat_rate"}, "Versandart"),
    ({"shipping_method": "flat rate:3"}, "Versandart"),
    ({"shipping_cost": "-1"}, "Preis"),
    ({"shipping_cost": "1e3"}, "Preis"),
    ({"items": []}, "Positionen"),
    ({"items": [{"product_id": "12a", "quantity": 1}]}, "product_id"),
    ({"items": [{"product_id": 0, "quantity": 1}]}, "product_id"),
    ({"items": [{"product_id": 5, "quantity": 0}]}, "quantity"),
    ({"items": [{"product_id": 5, "quantity": 1, "price": "abc"}]}, "Preis"),
    ({"items": [{"product_id": 5, "quantity": 1, "sku": "x"}]}, "unbekannte"),
    ({"billing": {**BILLING, "email": "kein-mail"}}, "email"),
    ({"billing": {**BILLING, "postcode": "30;00"}}, "postcode"),
    ({"billing": {**BILLING, "country": "CHE"}}, "country"),
    ({"billing": {**BILLING, "city": "<script>"}}, "city"),
    ({"billing": {**BILLING, "evil": "x"}}, "unbekannte"),
    ({"billing": {k: v for k, v in BILLING.items() if k != "email"}}, "email"),
    ({"shipping": {"first_name": "Max"}}, "Pflichtfelder"),
    ({"order_note": "x" * 1001}, "order_note"),
    ({"customer_note": "<b>"}, "customer_note"),
])
def test_payload_rejected(kw, match):
    with pytest.raises(ValueError, match=match):
        _payload(**kw)


def test_php_templates_take_values_only_from_stdin():
    for php in (server._WC_ORDER_PREVIEW_PHP, server._WC_ORDER_CREATE_PHP):
        assert "%s" not in php and "stream_get_contents(STDIN)" in php
    assert "wc_create_order" in server._WC_ORDER_CREATE_PHP
    assert "wc_create_order" not in server._WC_ORDER_PREVIEW_PHP


class _Closable:
    def close(self):
        pass


def test_preview_creates_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_wc_open", lambda d, p: (_Closable(), {}, "/var/www/vhosts/example.com/httpdocs", "1", d))

    def fake_eval(client, ctx, wp_dir, php, stdin_data=None):
        calls.append(php)
        return {"items": [{"product_id": 123, "name": "Muster", "quantity": 1, "unit_price": "0"}],
                "shipping_method": {"id": "flat_rate:3", "title": "Post Priority"}, "shipping_cost": "0",
                "customer_emails": ["customer_processing_order"], "orders_same_email_24h": [], "errors": [],
                "available_shipping_methods": []}

    monkeypatch.setattr(server, "_wp_eval", fake_eval)
    res = json.loads(server.wc_order_create("example.com", BILLING, ITEMS, shipping_method="flat_rate:3",
                                            shipping_cost="0", suppress_customer_emails=True))
    assert calls == [server._WC_ORDER_PREVIEW_PHP]
    assert "Vorschau" in res["ergebnis"] and "unterdrückt" in res["kundenmails"]


def test_preview_without_shipping_lists_methods(monkeypatch):
    monkeypatch.setattr(server, "_wc_open", lambda d, p: (_Closable(), {}, "/x", "1", d))
    monkeypatch.setattr(server, "_wp_eval", lambda *a, **k: {
        "items": [], "shipping_method": None, "errors": [], "orders_same_email_24h": [],
        "available_shipping_methods": [{"id": "flat_rate:3", "enabled": True}, {"id": "free_shipping:1", "enabled": False}]})
    res = json.loads(server.wc_order_create("example.com", BILLING, ITEMS, confirm=True))
    assert res["ergebnis"].startswith("Nicht ausführbar")
    assert [m["id"] for m in res["versandarten_verfuegbar"]] == ["flat_rate:3"]


# --- PHP gegen WooCommerce-Stubs -------------------------------------------

STUBS = r'''<?php
$GLOBALS["log"] = array(); $GLOBALS["filters"] = array();
function plog($x) { $GLOBALS["log"][] = $x; }
function add_filter($h, $cb, $p = 10) { $GLOBALS["filters"][] = $h; }
function is_wp_error($x) { return false; }
function wc_prices_include_tax() { return true; }
function get_woocommerce_currency() { return "CHF"; }
class M { public $id; public $instance_id; public $instance_settings;
  function __construct($id, $iid, $t, $c) { $this->id = $id; $this->instance_id = $iid; $this->t = $t; $this->instance_settings = array("cost" => $c); }
  function get_title() { return $this->t; } function is_enabled() { return true; } }
class WC_Shipping_Zones { static function get_zones() { return array(array("id" => 1, "zone_name" => "Schweiz", "shipping_methods" => array(new M("flat_rate", 3, "Post Priority", "9.00"), new M("flat_rate", 4, "Formel", "[qty] * 2")))); } }
class WC_Shipping_Zone { function __construct($id) {} function get_shipping_methods() { return array(); } }
class P { function __construct($id, $type = "variation") { $this->id = $id; $this->type = $type; }
  function is_type($t) { return $this->type === $t; } function get_status() { return "publish"; }
  function get_id() { return $this->id; } function get_parent_id() { return 100; } function get_name() { return "Zylinder 37.5/32.5"; }
  function get_sku() { return "ZY-1"; } function get_type() { return $this->type; } function get_price() { return "129.00"; }
  function managing_stock() { return true; } function get_stock_quantity() { return 5; } function is_in_stock() { return true; } }
function wc_get_product($id) { if ($id == 123) { return new P(123); } if ($id == 100) { return new P(100, "variable"); } return false; }
class E { function __construct($id, $c) { $this->id = $id; $this->c = $c; } function is_customer_email() { return $this->c; } }
class Mailer { function get_emails() { return array(new E("new_order", false), new E("customer_processing_order", true)); } }
function WC() { return new class { function mailer() { return new Mailer(); } }; }
function wc_get_orders($a) { plog("wc_get_orders:" . $a["billing_email"]); return array(); }
function wc_get_price_excluding_tax($p, $a) { return $a["price"] * $a["qty"]; }
class WC_Order_Item_Shipping { function __call($n, $a) { plog("ship:" . $n . "=" . json_encode($a[0])); } }
class It { function get_name() { return "Zylinder"; } function get_quantity() { return 1; } function get_total() { return "0"; }
  function get_product_id() { return 100; } function get_variation_id() { return 123; } }
class DS { function get_stock_reduced($id) { return true; } }
class O { public $status = "pending";
  function __call($n, $a) { plog("order:" . $n . "=" . json_encode($a)); if ($n === "update_status") { $this->status = $a[0]; } }
  function get_id() { return 4711; } function get_order_number() { return "4711"; } function get_status() { return $this->status; }
  function get_total() { return "0.00"; } function get_currency() { return "CHF"; } function get_shipping_total() { return "0"; }
  function get_items() { return array(new It()); } function get_data_store() { return new DS(); } }
$GLOBALS["order"] = new O();
function wc_create_order($a) { plog("create:" . json_encode($a)); return $GLOBALS["order"]; }
function wc_get_order($id) { return $GLOBALS["order"]; }
function wc_maybe_reduce_stock_levels($id) { plog("reduce:" . $id); }
register_shutdown_function(function () { fwrite(STDERR, "\nLOG " . json_encode(array("log" => $GLOBALS["log"], "filters" => $GLOBALS["filters"]))); });
'''

needs_php = pytest.mark.skipif(not shutil.which("php"), reason="php nicht installiert")


def _run_php(tmp_path, template, payload):
    f = tmp_path / "t.php"
    f.write_text(STUBS + template)
    r = subprocess.run(["php", str(f)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    log = json.loads(r.stderr.rsplit("LOG ", 1)[1])
    return r, log


@needs_php
@pytest.mark.parametrize("name", ["_WC_ORDER_PREVIEW_PHP", "_WC_ORDER_CREATE_PHP", "_WC_ORDER_GET_PHP",
                                  "_WC_ORDER_SHIP_PHP"])
def test_php_lint(tmp_path, name):
    f = tmp_path / "x.php"
    f.write_text("<?php\n" + getattr(server, name))
    r = subprocess.run(["php", "-l", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_php
def test_php_preview_with_stubs(tmp_path):
    r, log = _run_php(tmp_path, server._WC_ORDER_PREVIEW_PHP, _payload())
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["shipping_method"]["title"] == "Post Priority"
    assert out["items"][0]["unit_price"] == "0" and out["items"][0]["price_override"] is True
    assert out["customer_emails"] == ["customer_processing_order"]
    assert not any(x.startswith("create:") for x in log["log"])


@needs_php
def test_php_preview_errors(tmp_path):
    p = _payload(items=[{"product_id": 100, "quantity": 1}, {"product_id": 999, "quantity": 1}],
                 shipping_method="flat_rate:4", shipping_cost=None)
    r, _ = _run_php(tmp_path, server._WC_ORDER_PREVIEW_PHP, p)
    errs = json.loads(r.stdout)["errors"]
    assert any("variabel" in e for e in errs) and any("999" in e for e in errs) and any("Formel" in e for e in errs)


@needs_php
def test_php_create_with_stubs(tmp_path):
    r, log = _run_php(tmp_path, server._WC_ORDER_CREATE_PHP, _payload())
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["id"] == 4711 and out["status"] == "processing"
    assert out["customer_emails_suppressed"] == ["customer_processing_order"]
    assert "woocommerce_email_enabled_customer_processing_order" in log["filters"]
    assert "woocommerce_email_enabled_new_order" not in log["filters"]
    entries = log["log"]
    assert any(x.startswith('order:add_product=') and '"subtotal":0' in x for x in entries)
    assert 'ship:set_method_id="flat_rate"' in entries and "ship:set_instance_id=3" in entries
    assert any(x.startswith('order:update_status=["processing"') for x in entries)
    assert "reduce:4711" in entries
    assert any("Muster leihweise" in x for x in entries)


@needs_php
def test_php_create_refuses_on_errors(tmp_path):
    r, log = _run_php(tmp_path, server._WC_ORDER_CREATE_PHP, _payload(items=[{"product_id": 999, "quantity": 1}]))
    assert r.returncode == 1 and "nichts angelegt" in r.stderr
    assert not any(x.startswith("create:") for x in log["log"])


@needs_php
def test_php_create_without_suppression_sets_no_filters(tmp_path):
    r, log = _run_php(tmp_path, server._WC_ORDER_CREATE_PHP, _payload(suppress_customer_emails=False))
    assert r.returncode == 0 and log["filters"] == []
