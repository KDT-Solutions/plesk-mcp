"""Navigation, settings and appearance: menus, options (whitelist), Customizer CSS, widgets, redirects."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .common import CONFIRM_DOC, READ_ONLY, TRASH, WRITE, ToolRunner

BackupId = Annotated[str | None, Field(description="ID der Sicherung (aus dem Ergebnis der Änderung oder der Lese-Tools), leer = neueste.")]
ItemType = Literal["custom", "post_type", "taxonomy"]


def register_site_tools(mcp: MCPServer, runner: ToolRunner) -> None:
    @mcp.tool(description="Menüs der Seite mit ID, Name, Anzahl Einträge und zugewiesenen Positionen, dazu die Menü-Positionen des Themes.", annotations=READ_ONLY)
    async def menu_list(site: str) -> dict[str, Any]:
        return await runner.read(site, "menu_list", {})

    @mcp.tool(description="Einträge eines Menüs als Baum (ID, Titel, Typ, Ziel, URL, Eltern, Position) und die vorhandenen Sicherungen.", annotations=READ_ONLY)
    async def menu_get(site: str, menu_id: int) -> dict[str, Any]:
        return await runner.read(site, "menu_get", {"menu_id": menu_id})

    @mcp.tool(
        description=(
            "Legt einen Menüeintrag an: type=custom mit url, type=post_type mit object_id (Seite/Beitrag) oder type=taxonomy mit object_id (Kategorie). "
            "parent_id = Eltern-Eintrag, position ab 1. Das Menü wird vorher gesichert (menu_rollback)." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def menu_item_create(
        site: str,
        menu_id: int,
        type: ItemType,
        title: str | None = None,
        object_id: int | None = None,
        url: str | None = None,
        parent_id: int | None = None,
        position: int | None = None,
        target: Literal["", "_blank"] | None = None,
        classes: str | None = None,
        attr_title: str | None = None,
        description: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "menu_id": menu_id, "type": type, "title": title, "object_id": object_id, "url": url, "parent_id": parent_id, "position": position,
            "target": target, "classes": classes, "attr_title": attr_title, "description": description, "confirm": confirm,
        }
        return await runner.write(site, "menu_item_create", args)

    @mcp.tool(description="Ändert die übergebenen Felder eines Menüeintrags. Das Menü wird vorher gesichert (menu_rollback)." + CONFIRM_DOC, annotations=WRITE)
    async def menu_item_update(
        site: str,
        item_id: int,
        title: str | None = None,
        type: ItemType | None = None,
        object_id: int | None = None,
        url: str | None = None,
        target: Literal["", "_blank"] | None = None,
        classes: str | None = None,
        attr_title: str | None = None,
        description: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "item_id": item_id, "title": title, "type": type, "object_id": object_id, "url": url, "target": target, "classes": classes,
            "attr_title": attr_title, "description": description, "confirm": confirm,
        }
        return await runner.write(site, "menu_item_update", args)

    @mcp.tool(description="Verschiebt einen Menüeintrag unter parent_id (0 = oberste Ebene) an position (ab 1)." + CONFIRM_DOC, annotations=WRITE)
    async def menu_item_move(site: str, item_id: int, parent_id: int | None = None, position: int | None = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "menu_item_move", {"item_id": item_id, "parent_id": parent_id, "position": position, "confirm": confirm})

    @mcp.tool(description="Entfernt einen Menüeintrag (Untereinträge rücken eine Ebene nach oben). Das Menü wird vorher gesichert (menu_rollback)." + CONFIRM_DOC, annotations=TRASH)
    async def menu_item_remove(site: str, item_id: int, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "menu_item_remove", {"item_id": item_id, "confirm": confirm})

    @mcp.tool(description='Weist Menü-Positionen des Themes ein Menü zu, z.B. {"primary": 12}, 0 = keines. Nur die übergebenen Positionen.' + CONFIRM_DOC, annotations=WRITE)
    async def menu_locations_set(
        site: str, locations: Annotated[dict[str, int], Field(description='Position -> Menü-ID, z.B. {"primary": 12}.')], confirm: bool = False
    ) -> dict[str, Any]:
        return await runner.write(site, "menu_locations_set", {"locations": locations, "confirm": confirm})

    @mcp.tool(description="Stellt ein Menü (Einträge) oder die Menü-Positionen aus einer Sicherung wieder her (backup_id oder neueste Sicherung von menu_id). Der aktuelle Stand wird vorher gesichert." + CONFIRM_DOC, annotations=WRITE)
    async def menu_rollback(site: str, backup_id: BackupId = None, menu_id: int | None = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "menu_rollback", {"backup_id": backup_id, "menu_id": menu_id, "confirm": confirm})

    @mcp.tool(
        description="Liest Einstellungen aus der Whitelist (Allgemein, Lesen, Diskussion, Medien, Permalinks, z.B. blogname, page_on_front, posts_per_page). Ohne names alle erlaubten.",
        annotations=READ_ONLY,
    )
    async def option_get(site: str, names: list[str] | None = None) -> dict[str, Any]:
        return await runner.read(site, "option_get", {"names": names})

    @mcp.tool(
        description=(
            "Ändert Einstellungen aus der Whitelist, z.B. {\"blogname\": \"…\", \"show_on_front\": \"page\", \"page_on_front\": 12}. Werte werden wie im wp-admin geprüft. "
            "Nicht änderbar: siteurl, home, admin_email, Registrierung, Standardrolle, Plugins, Theme und alles mit Schlüsseln/Passwörtern. "
            "Alter Stand wird gesichert (option_rollback)." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def option_update(
        site: str, values: Annotated[dict[str, str | int | float | bool], Field(description="Name der Einstellung -> neuer Wert.")], confirm: bool = False
    ) -> dict[str, Any]:
        return await runner.write(site, "option_update", {"values": values, "confirm": confirm})

    @mcp.tool(
        description="Stellt Einstellungen aus einer Sicherung wieder her (Standard: neueste). Mit der backup_id von wptc_set_revision_limit auch die WPTC-Speicherdauer, von wptc_repair die WPTC-Pfade (backup_db_path und Pfad-Flags)." + CONFIRM_DOC,
        annotations=WRITE,
    )
    async def option_rollback(site: str, backup_id: BackupId = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "option_rollback", {"backup_id": backup_id, "confirm": confirm})

    @mcp.tool(description="Zusätzliches CSS des aktiven Themes (Customizer) und die vorhandenen Sicherungen.", annotations=READ_ONLY)
    async def custom_css_get(site: str) -> dict[str, Any]:
        return await runner.read(site, "custom_css_get", {})

    @mcp.tool(description="Ersetzt das zusätzliche CSS des aktiven Themes (komplettes CSS übergeben). Vorschau mit geänderten Zeilen, alter Stand wird gesichert (custom_css_rollback)." + CONFIRM_DOC, annotations=WRITE)
    async def custom_css_update(site: str, css: str, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "custom_css_update", {"css": css, "confirm": confirm})

    @mcp.tool(description="Stellt das zusätzliche CSS aus einer Sicherung wieder her (Standard: neueste des aktiven Themes)." + CONFIRM_DOC, annotations=WRITE)
    async def custom_css_rollback(site: str, backup_id: BackupId = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "custom_css_rollback", {"backup_id": backup_id, "confirm": confirm})

    @mcp.tool(description="Seitenleisten des Themes mit ihren klassischen Widgets (ID, Typ, Einstellungen) und inaktive Widgets.", annotations=READ_ONLY)
    async def widget_list(site: str) -> dict[str, Any]:
        return await runner.read(site, "widget_list", {})

    @mcp.tool(
        description=(
            "Ändert Einstellungen eines Widgets (widget_id aus widget_list, nur übergebene Keys, null entfernt) und verschiebt es optional "
            "in eine andere Seitenleiste (sidebar, position ab 1; wp_inactive_widgets = deaktivieren). Alter Stand wird gesichert (widget_rollback)." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def widget_update(
        site: str,
        widget_id: str,
        settings: Annotated[dict[str, Any] | None, Field(description='Widget-Einstellungen, z.B. {"title": "…", "text": "…"}.')] = None,
        sidebar: str | None = None,
        position: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await runner.write(site, "widget_update", {"widget_id": widget_id, "settings": settings, "sidebar": sidebar, "position": position, "confirm": confirm})

    @mcp.tool(description="Stellt Widget-Einstellungen und Seitenleisten aus einer Sicherung wieder her (Standard: neueste)." + CONFIRM_DOC, annotations=WRITE)
    async def widget_rollback(site: str, backup_id: BackupId = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "widget_rollback", {"backup_id": backup_id, "confirm": confirm})

    @mcp.tool(description="Weiterleitungen (nur wenn das Plugin Redirection aktiv ist, siehe get_version redirect_plugin), Suche in der Quell-URL.", annotations=READ_ONLY)
    async def redirect_list(site: str, search: str | None = None, limit: int = 50, page: int = 1) -> dict[str, Any]:
        return await runner.read(site, "redirect_list", {"search": search, "limit": limit, "page": page})

    @mcp.tool(description="Legt eine Weiterleitung im Plugin Redirection an (Standard 301, regex=true für reguläre Ausdrücke)." + CONFIRM_DOC, annotations=WRITE)
    async def redirect_create(
        site: str,
        source: str,
        target: str,
        code: Literal[301, 302, 307, 308] = 301,
        regex: bool = False,
        title: str | None = None,
        group_id: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {"source": source, "target": target, "code": code, "regex": regex, "title": title, "group_id": group_id, "confirm": confirm}
        return await runner.write(site, "redirect_create", args)

    @mcp.tool(description="Ändert eine Weiterleitung oder schaltet sie ab (enabled=false). Gelöscht wird nicht. Alter Stand wird gesichert." + CONFIRM_DOC, annotations=WRITE)
    async def redirect_update(
        site: str,
        redirect_id: int,
        source: str | None = None,
        target: str | None = None,
        code: Literal[301, 302, 307, 308] | None = None,
        regex: bool | None = None,
        title: str | None = None,
        enabled: bool | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "redirect_id": redirect_id, "source": source, "target": target, "code": code, "regex": regex, "title": title, "enabled": enabled,
            "confirm": confirm,
        }
        return await runner.write(site, "redirect_update", args)
