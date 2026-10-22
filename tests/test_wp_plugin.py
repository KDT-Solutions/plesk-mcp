"""Tests für die Validierung von wp_plugin_list/wp_plugin_update/wp_plugin_rollback (ohne Serverzugriff)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def test_slugs_ok_and_dedup():
    assert server._wp_plugin_slugs([" woocommerce ", "contact-form-7", "woocommerce"]) == [
        "woocommerce", "contact-form-7"]
    assert server._wp_plugin_slugs("akismet") == ["akismet"]
    assert server._wp_plugin_slugs([]) == []


@pytest.mark.parametrize("slug", ["", "a b", "../x", "a/b", "x;id", "$(id)", "-x", "a..b", "x" * 101])
def test_slugs_rejected(slug):
    with pytest.raises(ValueError, match="Plugin-Name"):
        server._wp_plugin_slugs([slug])


def test_slugs_max():
    with pytest.raises(ValueError, match="Höchstens"):
        server._wp_plugin_slugs([f"p{i}" for i in range(server._WP_PLUGIN_MAX + 1)])


@pytest.mark.parametrize("v", ["", "1.2.3", "10.0.0-beta1", "3.1"])
def test_version_ok(v):
    assert server._wp_plugin_check_version(v) == v


@pytest.mark.parametrize("v", ["latest", "1 0", "1;id", "v1.0", "1.0/../x"])
def test_version_rejected(v):
    with pytest.raises(ValueError, match="Version"):
        server._wp_plugin_check_version(v)


def test_update_argument_rules():
    with pytest.raises(ValueError, match="nicht beides"):
        server.wp_plugin_update("example.com", "httpdocs", plugins=["akismet"], all_updates=True)
    with pytest.raises(ValueError, match="plugins angeben"):
        server.wp_plugin_update("example.com", "httpdocs")
    with pytest.raises(ValueError, match="genau einem"):
        server.wp_plugin_update("example.com", "httpdocs", plugins=["a", "b"], version="1.0")
    with pytest.raises(ValueError, match="genau einem"):
        server.wp_plugin_update("example.com", "httpdocs", all_updates=True, version="1.0")


def test_rollback_bad_id():
    with pytest.raises(ValueError, match="backup_id"):
        server.wp_plugin_rollback("x; rm -rf /")


INSTALLED = {
    "akismet": {"name": "akismet", "status": "active", "version": "5.3", "update": "available",
                "update_version": "5.4"},
    "hello": {"name": "hello", "status": "inactive", "version": "1.7.2", "update": "none",
              "update_version": ""},
    "object-cache": {"name": "object-cache", "status": "dropin", "version": "", "update": "none"},
    "premium": {"name": "premium", "status": "active", "version": "2.0", "update": "unavailable"},
}


def test_targets_all_updates():
    t, skipped = server._wp_plugin_targets(INSTALLED, [], True, "")
    assert t == ["akismet"] and skipped == {}


def test_targets_named():
    t, skipped = server._wp_plugin_targets(
        INSTALLED, ["akismet", "hello", "missing", "object-cache", "premium"], False, "")
    assert t == ["akismet"]
    assert set(skipped) == {"hello", "missing", "object-cache", "premium"}
    assert skipped["missing"] == "nicht installiert"


def test_targets_version():
    t, _ = server._wp_plugin_targets(INSTALLED, ["hello"], False, "1.7.0")
    assert t == ["hello"]
    t, skipped = server._wp_plugin_targets(INSTALLED, ["hello"], False, "1.7.2")
    assert t == [] and "bereits" in skipped["hello"]
