"""WordPress-Tools für Seiten auf dem Plesk-Server, ohne Plugin auf der Seite.

Die Funktionen sind dieselben wie im WordPress-MCP (Plugin tabside-mcp, Repo
tabside/wordpress-mcp): Inhalte aller Beitragstypen, Medien, Elementor, Menüs,
Einstellungen, Widgets, Weiterleitungen, Plugins, Themes, Benutzer. Statt über
die REST-API einer installierten Plugin-Kopie laufen sie hier per WP-CLI:

    wp eval-file -   (als System-User der Subscription, Skript über stdin)

Das Skript enthält die PHP-Klassen des Plugins (wpsite/php/tabside-mcp/, nur
geladen, wenn das Plugin auf der Seite nicht aktiv ist), die Eingabe und den
Runner (wpsite/php/runner.php). Auf dem Server wird nichts installiert oder
abgelegt; Sicherungen und das Änderungsprotokoll speichert der Plugin-Code wie
gewohnt in der Datenbank der Seite (Post-Meta bzw. Optionen ohne Autoload).

Aktualisieren: wpsite/php/tabside-mcp/*.php aus tabside/wordpress-mcp
(plugin/tabside-mcp/includes/) übernehmen und VERSION anpassen.
Die tools_site/tools_admin/tools_elementor.py sind Kopien aus wordpressmcp/,
nur der Import (.common statt .tools) ist angepasst.
"""

from __future__ import annotations

import base64
import functools
import json
from pathlib import Path
from typing import Any

PHP_DIR = Path(__file__).resolve().parent / "php"
PLUGIN_DIR = PHP_DIR / "tabside-mcp"
# Reihenfolge wie in tabside-mcp.php. Der Updater wird nur geladen (Tabside_MCP_Updater::flush
# wird vom Admin-Code aufgerufen), aber nicht gestartet: es wird nichts installiert.
PLUGIN_FILES = ("plugin", "backups", "store", "cache", "abilities", "elementor", "site", "admin", "updater")
MARKER = "@@PLESKMCP-WPSITE@@"


def _vendor() -> tuple[str, str]:
    try:
        version, commit = (PLUGIN_DIR / "VERSION").read_text(encoding="utf-8").split()[:2]
    except (OSError, ValueError):
        return "unbekannt", "unbekannt"
    return version, commit


PLUGIN_VERSION, PLUGIN_COMMIT = _vendor()
__version__ = PLUGIN_VERSION


class WPSiteError(Exception):
    def __init__(self, message: str, *, code: str = "", data: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


def ability_name(tool: str) -> str:
    """post_list -> post-list (Slug der Ability im Plugin)."""
    return tool.replace("_", "-")


def _strip_php_open(source: str) -> str:
    text = source.lstrip("﻿").lstrip()
    if not text.startswith("<?php"):
        raise ValueError("PHP-Datei beginnt nicht mit <?php")
    return text[len("<?php"):]


def _php_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@functools.lru_cache(maxsize=1)
def _prelude() -> str:
    """Klassen des Plugins, nur wenn es auf der Seite nicht schon aktiv ist."""
    classes = "\n".join(
        _strip_php_open((PLUGIN_DIR / f"class-tabside-mcp-{name}.php").read_text(encoding="utf-8"))
        for name in PLUGIN_FILES
    )
    return (
        "<?php\n"
        "if ( class_exists( 'Tabside_MCP_Abilities', false ) ) {\n"
        "\t$__pmcp_source = 'site-plugin ' . ( defined( 'TABSIDE_MCP_VERSION' ) ? TABSIDE_MCP_VERSION : '?' );\n"
        "} else {\n"
        f"\t$__pmcp_source = {_php_string('plesk-mcp ' + PLUGIN_VERSION)};\n"
        f"\tdefined( 'TABSIDE_MCP_VERSION' ) || define( 'TABSIDE_MCP_VERSION', {_php_string(PLUGIN_VERSION)} );\n"
        f"\tdefined( 'TABSIDE_MCP_COMMIT' ) || define( 'TABSIDE_MCP_COMMIT', {_php_string(PLUGIN_COMMIT[:12])} );\n"
        "\tdefined( 'TABSIDE_MCP_MIN_WP' ) || define( 'TABSIDE_MCP_MIN_WP', '6.9' );\n"
        "\tdefined( 'TABSIDE_MCP_DIR' ) || define( 'TABSIDE_MCP_DIR', trailingslashit( WP_PLUGIN_DIR ) . 'tabside-mcp/' );\n"
        f"{classes}\n"
        "}\n"
    )


@functools.lru_cache(maxsize=1)
def _runner() -> str:
    return (PHP_DIR / "runner.php").read_text(encoding="utf-8")


def build_script(ability: str, payload: dict[str, Any]) -> bytes:
    """PHP-Skript für `wp eval-file -`. Die Eingabe steht base64-kodiert im
    Skript (keine Shell, keine Argumente, kein Quoting-Risiko)."""
    data = base64.b64encode(
        json.dumps({"ability": ability, "input": payload}, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return (
        _prelude()
        + f"$__pmcp = json_decode( base64_decode( '{data}' ), true );\n"
        + _runner()
    ).encode("utf-8")


def missing_ability(tool: str) -> str:
    if tool.startswith("elementor_"):
        return "Elementor ist auf dieser Seite nicht aktiv."
    if tool.startswith("redirect_"):
        return "Kein unterstütztes Weiterleitungs-Plugin aktiv (unterstützt: Redirection)."
    if tool.startswith("wptc_"):
        return "WP Time Capsule ist auf dieser Seite nicht aktiv."
    return "Diese Funktion gibt es auf dieser Seite nicht (auf der Seite ist eine ältere Version des Plugins tabside-mcp aktiv)."


def parse_output(tool: str, out: str) -> dict[str, Any]:
    """Ergebnis aus der WP-CLI-Ausgabe (letzte Marker-Zeile). Fehler des
    Plugins werden als WPSiteError mit der Meldung des Plugins gemeldet."""
    pos = out.rfind(MARKER)
    if pos < 0:
        raise WPSiteError("Keine Antwort des WordPress-Runners erhalten.", code="pmcp_no_output")
    # Nur an "\n" trennen: splitlines() trennt auch an U+2028/U+0085 usw., die im JSON roh stehen dürfen.
    line = out[pos + len(MARKER):].lstrip("\r\n").split("\n", 1)[0].strip()
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise WPSiteError("Antwort des WordPress-Runners ist kein gültiges JSON.", code="pmcp_bad_output") from exc
    if not isinstance(data, dict):
        raise WPSiteError("Unerwartete Antwort des WordPress-Runners.", code="pmcp_bad_output")
    if data.get("ok"):
        result = data.get("result")
        result = result if isinstance(result, dict) else {"result": result}
        if data.get("source"):
            result.setdefault("plugin_code", data["source"])
        return result
    code = str(data.get("code") or "")
    message = str(data.get("message") or "") or json.dumps(data)[:300]
    if code == "pmcp_ability_not_found":
        message = missing_ability(tool)
    extra = data.get("data")
    if isinstance(extra, dict) and extra.get("hits"):
        # z.B. elementor_replace_text mit anderer Trefferzahl: wo der Text gefunden wurde
        message += " Fundstellen: " + json.dumps(extra["hits"], ensure_ascii=False)[:20000]
    raise WPSiteError(message[:21000], code=code, data=extra)
