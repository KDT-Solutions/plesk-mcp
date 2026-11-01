"""Tests für wp_sites_overview, wp_core_update und wptc_set_revision_limit (ohne Serverzugriff)."""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("v", ["6", "abc", "6.8.3.1", "6.8;id", "../6.8"])
def test_core_version_rejected(v):
    with pytest.raises(ValueError, match="Version"):
        server.wp_core_update("example.com", "httpdocs", version=v)


def test_core_version_and_minor_exclusive():
    with pytest.raises(ValueError, match="minor_only"):
        server.wp_core_update("example.com", "httpdocs", version="6.8.3", minor_only=True)


@pytest.mark.parametrize("days", [0, -1, 366, 1000])
def test_wptc_days_rejected(days):
    with pytest.raises(ValueError, match="days"):
        server.wptc_set_revision_limit("example.com", "httpdocs", days=days)


def test_overview_age_rejected():
    with pytest.raises(ValueError, match="max_backup_age_days"):
        server.wp_sites_overview(max_backup_age_days=0)


def test_wptc_read_rejects_bad_identifiers():
    assert server._wptc_read(None, "db;drop", "wp_") is None
    assert server._wptc_read(None, "wordpress", "wp_`x") is None


def test_ts_to_iso():
    assert server._ts_to_iso("") is None
    assert server._ts_to_iso("abc") is None
    assert server._ts_to_iso("1700000000").startswith("2023-11-1")


def test_scan_script_on_local_tree(tmp_path, monkeypatch):
    """Das Such-Skript läuft unverändert gegen einen nachgebauten Vhost-Baum."""
    base = tmp_path / "vhosts"
    site = base / "example.com" / "httpdocs"
    (site / "wp-includes").mkdir(parents=True)
    (site / "wp-load.php").write_text("<?php")
    (site / "wp-includes" / "version.php").write_text("<?php\n$wp_version = '6.8.3';\n")
    (site / "wp-config.php").write_text(
        "<?php\ndefine( 'DB_NAME', 'wp_abc' );\n$table_prefix = 'xy_';\n")
    noise = base / "example.com" / "httpdocs" / "wp-content" / "plugins" / "x"
    noise.mkdir(parents=True)
    (noise / "wp-config.php").write_text("<?php")
    script = server._WP_SCAN_SH.replace("/var/www/vhosts", str(base))
    out = subprocess.run(["sh", "-c", script, "sh", "6"], capture_output=True, text=True).stdout
    lines = [line.split("\t") for line in out.strip().splitlines()]
    assert lines == [[str(site), "6.8.3", "wp_abc", "xy_"]]
