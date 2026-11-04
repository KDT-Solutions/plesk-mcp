"""Administration: plugins, themes, core updates, site health, cache, users, application passwords, audit log.

The plugin checks the matching capability for each ability (by default only
administrators have them). Every write needs confirm=true and the user's
confirmation of the preview.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .common import ADMIN_DOC, CONFIRM_DOC, READ_ONLY, TRASH, WRITE, ToolRunner

Plugin = Annotated[str, Field(description="Plugin-Datei aus plugin_list (z.B. redirection/redirection.php) oder Verzeichnis-Slug (redirection).")]
Theme = Annotated[str, Field(description="Theme-Verzeichnis (theme aus theme_list).")]


def register_admin_tools(mcp: MCPServer, runner: ToolRunner) -> None:
    @mcp.tool(description="Installierte Plugins mit Version, aktiv, verfügbarem Update und Auto-Update. refresh=true fragt vorher wordpress.org nach Updates.", annotations=READ_ONLY)
    async def plugin_list(site: str, refresh: bool = False) -> dict[str, Any]:
        return await runner.read(site, "plugin_list", {"refresh": refresh})

    @mcp.tool(description="Aktiviert ein installiertes Plugin." + CONFIRM_DOC + ADMIN_DOC, annotations=WRITE)
    async def plugin_activate(site: str, plugin: Plugin, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "plugin_activate", {"plugin": plugin, "confirm": confirm})

    @mcp.tool(description="Deaktiviert ein Plugin (kein Löschen). tabside-mcp selbst lässt sich nicht deaktivieren." + CONFIRM_DOC + ADMIN_DOC, annotations=TRASH)
    async def plugin_deactivate(site: str, plugin: Plugin, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "plugin_deactivate", {"plugin": plugin, "confirm": confirm})

    @mcp.tool(description="Aktualisiert ein Plugin auf die verfügbare Version, aktive Plugins bleiben aktiv." + CONFIRM_DOC + ADMIN_DOC, annotations=WRITE)
    async def plugin_update(site: str, plugin: Plugin, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "plugin_update", {"plugin": plugin, "confirm": confirm})

    @mcp.tool(description="Schaltet automatische Updates eines Plugins ein oder aus." + CONFIRM_DOC + ADMIN_DOC, annotations=WRITE)
    async def plugin_auto_update_set(site: str, plugin: Plugin, enabled: bool, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "plugin_auto_update_set", {"plugin": plugin, "enabled": enabled, "confirm": confirm})

    @mcp.tool(
        description=(
            "Installiert ein Plugin aus dem WordPress-Verzeichnis per Slug (wordpress.org/plugins/<slug>/), nie aus anderen Quellen oder ZIP-Dateien. "
            "Die Vorschau zeigt Name, Version, Autor, Installationen und Bewertung aus dem Verzeichnis. activate=true aktiviert es gleich." + CONFIRM_DOC + ADMIN_DOC
        ),
        annotations=WRITE,
    )
    async def plugin_install(site: str, slug: str, activate: bool = False, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "plugin_install", {"slug": slug, "activate": activate, "confirm": confirm})

    @mcp.tool(description="Installierte Themes mit Version, aktiv, Eltern-Theme, Block-Theme, verfügbarem Update und Auto-Update.", annotations=READ_ONLY)
    async def theme_list(site: str, refresh: bool = False) -> dict[str, Any]:
        return await runner.read(site, "theme_list", {"refresh": refresh})

    @mcp.tool(
        description="Wechselt das aktive Theme. ACHTUNG: die ganze Seite ändert ihr Aussehen, Menü-Positionen und Widgets können sich verschieben." + CONFIRM_DOC + ADMIN_DOC,
        annotations=WRITE,
    )
    async def theme_activate(site: str, theme: Theme, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "theme_activate", {"theme": theme, "confirm": confirm})

    @mcp.tool(description="Aktualisiert ein Theme auf die verfügbare Version." + CONFIRM_DOC + ADMIN_DOC, annotations=WRITE)
    async def theme_update(site: str, theme: Theme, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "theme_update", {"theme": theme, "confirm": confirm})

    @mcp.tool(description="Installierte WordPress-Version und verfügbare Updates; minor_available nennt das Minor-Update, das core_update installieren darf.", annotations=READ_ONLY)
    async def core_update_check(site: str, refresh: bool = False) -> dict[str, Any]:
        return await runner.read(site, "core_update_check", {"refresh": refresh})

    @mcp.tool(description="Installiert das verfügbare Minor-Update von WordPress (gleicher Zweig, z.B. 6.9.1 -> 6.9.2), nie eine neue Hauptversion." + CONFIRM_DOC + ADMIN_DOC, annotations=WRITE)
    async def core_update(site: str, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "core_update", {"confirm": confirm})

    @mcp.tool(description="Website-Zustand: Ergebnisse der direkten Tests (good/recommended/critical), PHP-Version, Speicherlimit, Cron-Status, Debug-Modus, Objekt-Cache, HTTPS.", annotations=READ_ONLY)
    async def site_health(site: str) -> dict[str, Any]:
        return await runner.read(site, "site_health", {})

    @mcp.tool(
        description=(
            "Leert Objekt-Cache, Elementor-CSS und erkannte Cache-Plugins (WP Super Cache, W3 Total Cache, WP Rocket, LiteSpeed, Autoptimize, SG Optimizer, Breeze). "
            "Mit post_id nur für diesen Beitrag. Nach Änderungen über die anderen Tools läuft das automatisch." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def cache_flush(site: str, post_id: int | None = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "cache_flush", {"post_id": post_id, "confirm": confirm})

    @mcp.tool(
        description=(
            "Setzt bei WP Time Capsule die Speicherdauer der Sicherungen (revision_limit, in Tagen) in der WPTC-Tabelle der Seite, "
            "nur wenn WPTC aktiv ist. days 1..365 (Vorgabe 30). Die Vorschau zeigt alten und neuen Wert, danach wird erneut ausgelesen (verify). "
            "Der alte Wert wird gesichert: zurückstellen mit option_rollback und der zurückgegebenen backup_id. Aktuellen Wert zeigt site_health (wptc)."
            + CONFIRM_DOC + ADMIN_DOC
        ),
        annotations=WRITE,
    )
    async def wptc_set_revision_limit(
        site: str, days: Annotated[int, Field(ge=1, le=365, description="Speicherdauer in Tagen, 1 bis 365.")] = 30, confirm: bool = False
    ) -> dict[str, Any]:
        return await runner.write(site, "wptc_set_revision_limit", {"days": days, "confirm": confirm})

    @mcp.tool(
        description=(
            "Detaillierter Stand von WP Time Capsule: Konto und Verbindung zum WPTC-Dienst (cron_status = letzter Verbindungstest), Backup-Ziel, "
            "Speicherdauer, Zeitplan, letzte erfolgreiche Sicherung, laufende Sicherung mit letztem Lebenszeichen (hanging = über 60 Minuten still), "
            "Warteschlange, letzte Sicherungen und die neuesten Einträge des WPTC-Aktivitätsprotokolls (log_limit, 0 = keine). warnings meldet ein fehlendes oder "
            "ungültiges Temp-Verzeichnis (tmp_dir_ok=false, beheben mit wptc_repair) und eine letzte erfolgreiche Sicherung älter als 48 Stunden oder keine "
            "(last_success.stale; last_success.source = option oder activity_log). Nur für Administratoren."
        ),
        annotations=READ_ONLY,
    )
    async def wptc_status(site: str, log_limit: Annotated[int, Field(ge=0, le=100)] = 20) -> dict[str, Any]:
        return await runner.read(site, "wptc_status", {"log_limit": log_limit})

    @mcp.tool(
        description=(
            "Startet bei WP Time Capsule eine Sicherung (wie 'Backup now'). Eine hängende Sicherung (letztes Lebenszeichen über 60 Minuten alt) "
            "wird vorher abgebrochen und zurückgesetzt, eine aktive nur mit abort_running=true. Nie während einer Wiederherstellung, nicht ohne verbundenes "
            "WPTC-Konto und nicht ohne gültiges Temp-Verzeichnis (dann zuerst wptc_repair). Danach läuft die Sicherung über den WPTC-Dienst weiter, Fortschritt mit wptc_status prüfen." + CONFIRM_DOC + ADMIN_DOC
        ),
        annotations=WRITE,
    )
    async def wptc_backup_start(site: str, abort_running: bool = False, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "wptc_backup_start", {"abort_running": abort_running, "confirm": confirm})

    @mcp.tool(
        description=(
            "Repariert WP Time Capsule ohne wp-admin, z.B. wenn wptc_status tmp_dir_ok=false oder hanging=true meldet. Diagnose: Temp-Verzeichnis "
            "(vorhanden, beschreibbar, Besitzer, index.php), Pfad-Einstellung backup_db_path, laufende oder hängende Sicherung, Konto. Dann mit den "
            "WPTC-eigenen Funktionen wp-content/uploads/tCapsule/backups samt index.php anlegen (0755, Besitzer = PHP-Benutzer) und den Pfad neu setzen, "
            "hängende Sicherung abbrechen und zurücksetzen und mit start_backup=true (Vorgabe) eine frische Sicherung starten. Der Pfad wird immer aus der "
            "WordPress-Konfiguration abgeleitet und lässt sich nicht übergeben. Eine aktive, nicht hängende Sicherung nur mit abort_running=true, nie während "
            "einer Wiederherstellung. Alte Pfad-Werte werden gesichert: zurück mit option_rollback und backup_id; danach wird erneut ausgelesen (verify)."
            + CONFIRM_DOC + ADMIN_DOC
        ),
        annotations=WRITE,
    )
    async def wptc_repair(site: str, start_backup: bool = True, abort_running: bool = False, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "wptc_repair", {"start_backup": start_backup, "abort_running": abort_running, "confirm": confirm})

    @mcp.tool(description="Benutzer mit Login, Name, E-Mail, Rolle, Registrierung, Anzahl Anwendungspasswörter und letzter Nutzung eines Anwendungspassworts.", annotations=READ_ONLY)
    async def user_list(site: str, role: str | None = None, search: str | None = None, limit: int = 50, page: int = 1) -> dict[str, Any]:
        return await runner.read(site, "user_list", {"role": role, "search": search, "limit": limit, "page": page})

    @mcp.tool(description="Ein Benutzer mit Namen, Biografie, Rollen und Anwendungspasswörtern (Name, erstellt, zuletzt benutzt; nie das Passwort).", annotations=READ_ONLY)
    async def user_get(site: str, user_id: int) -> dict[str, Any]:
        return await runner.read(site, "user_get", {"user_id": user_id})

    @mcp.tool(
        description=(
            "Ändert nur Anzeigename, Vor-/Nachname, Biografie oder Rolle eines Benutzers. Kein Passwort, keine E-Mail, kein Anlegen oder Löschen. "
            "Rolle administrator und Änderungen an Administratoren nur durch Administratoren, die eigene Rolle nie." + CONFIRM_DOC + ADMIN_DOC
        ),
        annotations=WRITE,
    )
    async def user_update(
        site: str,
        user_id: int,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        description: str | None = None,
        role: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "user_id": user_id, "display_name": display_name, "first_name": first_name, "last_name": last_name, "description": description,
            "role": role, "confirm": confirm,
        }
        return await runner.write(site, "user_update", args)

    @mcp.tool(description="Anwendungspasswörter des WordPress-Benutzers, mit dem das Gateway angemeldet ist: Name, erstellt, zuletzt benutzt, letzte IP. Nie das Passwort.", annotations=READ_ONLY)
    async def application_password_list(site: str) -> dict[str, Any]:
        return await runner.read(site, "application_password_list", {})

    @mcp.tool(
        description="Protokoll der über tabside MCP ausgeführten Änderungen auf der Seite (Zeit, Benutzer, Tool, Ziel, Sicherungs-ID), neueste zuerst, höchstens 500. Administratoren sehen alle Einträge, andere nur die eigenen.",
        annotations=READ_ONLY,
    )
    async def audit_log(site: str, limit: int = 50, user: str | None = None) -> dict[str, Any]:
        return await runner.read(site, "audit_log", {"limit": limit, "user": user})
