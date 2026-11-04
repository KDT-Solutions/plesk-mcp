"""Elementor tools: pages, templates and popups down to widget level.

The tools are always listed; on a site without Elementor the plugin does not
register the abilities and the call ends with "Elementor ist ... nicht aktiv"
(wordpress.missing_ability).
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .common import CONFIRM_DOC, READ_ONLY, TRASH, WRITE, ToolRunner

ElementId = Annotated[str, Field(description="Element-ID aus elementor_get (8 Zeichen hex).")]
Settings = Annotated[
    dict[str, Any],
    Field(description='Elementor-Settings als Objekt, nur die zu ändernden Keys, z.B. {"title": "Neu", "title_color": "#1B4D46"}. null entfernt einen Key.'),
]
IdMap = Annotated[dict[str, int] | None, Field(description='Zuordnung alte ID -> neue ID, z.B. {"123": 456}.')]
Element = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            'Element im Elementor-Format: {"elType": "widget", "widgetType": "heading", "settings": {"title": "…"}, "elements": []} '
            'oder ein Container {"elType": "container", "settings": {}, "elements": [...]}. IDs werden neu vergeben.'
        )
    ),
]


def register_elementor_tools(mcp: MCPServer, runner: ToolRunner) -> None:
    read = runner.read
    write = runner.write

    @mcp.tool(description="Elementor- und Pro-Version, aktive Experimente (Container ja/nein), Kit-ID, globale Farben und Schriften, Breakpoints, Beitragstypen mit Elementor.", annotations=READ_ONLY)
    async def elementor_info(site: str) -> dict[str, Any]:
        return await read(site, "elementor_info", {})

    @mcp.tool(
        description=(
            "Aufbau einer Elementor-Seite, -Vorlage oder eines Popups als Baum: pro Element id, elType (container/section/column/widget), widgetType, Pfad "
            "und eine lesbare Zusammenfassung der Inhalte (Texte, Überschriften, Links, Bilder, Icons) sowie verknüpfte popups und templates. "
            "full=true liefert alle Settings inkl. Styles (bei grossen Seiten mit element_id einen Teilbaum wählen)."
        ),
        annotations=READ_ONLY,
    )
    async def elementor_get(site: str, post_id: int, element_id: str | None = None, full: bool = False) -> dict[str, Any]:
        return await read(site, "elementor_get", {"post_id": post_id, "element_id": element_id, "full": full})

    @mcp.tool(
        description="Volltextsuche in den Elementor-Inhalten einer Seite (post_id) oder aller Seiten, Vorlagen und Popups: Beitrag, Element-ID, Feld und Fundstelle mit Kontext. Zeigt, wo ein Satz überall vorkommt.",
        annotations=READ_ONLY,
    )
    async def elementor_find(site: str, search: str, post_id: int | None = None, case_sensitive: bool = False) -> dict[str, Any]:
        return await read(site, "elementor_find", {"search": search, "post_id": post_id, "case_sensitive": case_sensitive})

    @mcp.tool(
        description="Wo eine Vorlage oder ein Popup verwendet wird: Vorlagen-Widgets, Shortcodes [elementor-template], Popup-Links und dynamische Popup-Tags, dazu die eigenen Anzeigebedingungen.",
        annotations=READ_ONLY,
    )
    async def elementor_references(site: str, post_id: int) -> dict[str, Any]:
        return await read(site, "elementor_references", {"post_id": post_id})

    @mcp.tool(
        description="Elementor-Vorlagen (elementor_library), optional nach template_type gefiltert (page, section, container, popup, header, footer, single, archive, loop-item …), mit Bedingungen und Anzahl Verwendungen.",
        annotations=READ_ONLY,
    )
    async def elementor_template_list(site: str, template_type: str | None = None, search: str | None = None) -> dict[str, Any]:
        return await read(site, "elementor_template_list", {"template_type": template_type, "search": search})

    @mcp.tool(
        description=(
            "Setzt Settings eines Elements (nur die übergebenen Keys). Die Vorschau zeigt alt/neu pro Key. Gespeichert wird über die Elementor-Dokument-API, "
            "vorher gesichert (zurückholen mit post_rollback), danach CSS neu erzeugt und Caches geleert." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_update_element(site: str, post_id: int, element_id: ElementId, settings: Settings, confirm: bool = False) -> dict[str, Any]:
        return await write(site, "elementor_update_element", {"post_id": post_id, "element_id": element_id, "settings": settings, "confirm": confirm})

    @mcp.tool(
        description=(
            "Ersetzt einen Text exakt in den Elementor-Settings eines Elements (post_id + element_id), einer Seite (post_id) oder mehrerer Seiten (post_ids). "
            "expected_count = erwartete Anzahl Treffer insgesamt; weicht sie ab, wird nichts geändert und die Fundstellen kommen zurück. "
            "Jede Seite wird vorher gesichert (backup_ids)." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_replace_text(
        site: str,
        search: str,
        replace: str,
        expected_count: int,
        post_id: int | None = None,
        post_ids: list[int] | None = None,
        element_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "search": search, "replace": replace, "expected_count": expected_count, "post_id": post_id, "post_ids": post_ids,
            "element_id": element_id, "confirm": confirm,
        }
        return await write(site, "elementor_replace_text", args)

    @mcp.tool(
        description=(
            "Fügt ein Element ein: aus JSON (element) oder als Kopie eines bestehenden Elements (source_post_id + source_element_id, auch aus einer anderen Seite). "
            "parent_id = Eltern-Element (leer = oberste Ebene), index = Position (leer = am Ende). IDs werden neu vergeben, Widget-Typen geprüft." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_insert_element(
        site: str,
        post_id: int,
        element: Element = None,
        source_post_id: int | None = None,
        source_element_id: str | None = None,
        parent_id: str | None = None,
        index: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "post_id": post_id, "element": element, "source_post_id": source_post_id, "source_element_id": source_element_id,
            "parent_id": parent_id, "index": index, "confirm": confirm,
        }
        return await write(site, "elementor_insert_element", args)

    @mcp.tool(description="Verschiebt ein Element unter parent_id (leer = oberste Ebene) an Position index." + CONFIRM_DOC, annotations=WRITE)
    async def elementor_move_element(site: str, post_id: int, element_id: ElementId, parent_id: str | None = None, index: int | None = None, confirm: bool = False) -> dict[str, Any]:
        return await write(site, "elementor_move_element", {"post_id": post_id, "element_id": element_id, "parent_id": parent_id, "index": index, "confirm": confirm})

    @mcp.tool(description="Entfernt ein Element samt Inhalt aus dem Aufbau. Die alte Fassung bleibt in der Sicherung (post_rollback)." + CONFIRM_DOC, annotations=TRASH)
    async def elementor_remove_element(site: str, post_id: int, element_id: ElementId, confirm: bool = False) -> dict[str, Any]:
        return await write(site, "elementor_remove_element", {"post_id": post_id, "element_id": element_id, "confirm": confirm})

    @mcp.tool(
        description=(
            "Übernimmt den kompletten Elementor-Aufbau von source_post_id in target_post_id (Seite X soll aussehen wie Seite Y), auch zwischen Beitragstypen. "
            "Der bisherige Aufbau des Ziels wird gesichert und ersetzt. replace_popup_ids bzw. replace_template_ids ordnen alte IDs neuen zu, "
            "damit Kacheln und Links auf die neuen Popups/Vorlagen zeigen (z.B. nach post_duplicate der Popups). copy_page_settings übernimmt Seiteneinstellungen und Template." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_copy_layout(
        site: str,
        source_post_id: int,
        target_post_id: int,
        replace_popup_ids: IdMap = None,
        replace_template_ids: IdMap = None,
        copy_page_settings: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "source_post_id": source_post_id, "target_post_id": target_post_id, "replace_popup_ids": replace_popup_ids,
            "replace_template_ids": replace_template_ids, "copy_page_settings": copy_page_settings, "confirm": confirm,
        }
        return await write(site, "elementor_copy_layout", args)

    @mcp.tool(
        description=(
            "Setzt Anzeigebedingungen eines Popups oder einer Theme-Vorlage (z.B. include/general, include/singular/page/12, exclude/singular/post) "
            "und bei Popups triggers (Auslöser) und timing (erweiterte Regeln). Nur mit Elementor Pro." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_set_conditions(
        site: str,
        post_id: int,
        conditions: list[str] | None = None,
        triggers: Annotated[dict[str, Any] | None, Field(description='Nur Popups, ersetzt die bisherigen, z.B. {"page_load": "yes", "page_load_delay": 3}.')] = None,
        timing: Annotated[dict[str, Any] | None, Field(description="Nur Popups, erweiterte Regeln, ersetzt die bisherigen.")] = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await write(site, "elementor_set_conditions", {"post_id": post_id, "conditions": conditions, "triggers": triggers, "timing": timing, "confirm": confirm})

    @mcp.tool(
        description=(
            "Ändert die Elementor-Seiteneinstellungen (_elementor_page_settings), z.B. hide_title, Abstände, Hintergrund, eigenes CSS. Nur die übergebenen Keys, "
            "null entfernt einen Key. Titel, Status und Template über post_update." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def elementor_page_settings_update(site: str, post_id: int, settings: Settings, confirm: bool = False) -> dict[str, Any]:
        return await write(site, "elementor_page_settings_update", {"post_id": post_id, "settings": settings, "confirm": confirm})

