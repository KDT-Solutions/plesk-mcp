"""Inhalte (Beiträge aller Typen, Begriffe, Medien) und Word-Umwandlung.
Aus wordpress-mcp (wordpressmcp/tools.py, register_tools) übernommen, ohne
get_version/list_sites; dafür site_info (Version der Seite).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .common import CONFIRM_DOC, READ_ONLY, TRASH, WRITE, ToolRunner
from .docx_convert import DocxError, docx_to_content as convert_docx

READ = "wordpress:read"
WRITE_SCOPE = "wordpress:write"

PostType = Annotated[str, Field(description="Beitragstyp aus post_type_list, z.B. post, page, elementor_library oder ein eigener Typ wie service.")]
Status = Literal["draft", "pending", "publish", "future", "private"]
TermList = list[int | str]
TermMap = Annotated[
    dict[str, list[int | str]] | None,
    Field(description='Begriffe weiterer Taxonomien: {"taxonomie": [ID, Slug oder Name]}. Müssen existieren (term_create). Ersetzt die bisherigen dieser Taxonomie.'),
]


def register_content_tools(mcp: Any, runner: ToolRunner) -> None:
    @mcp.tool(
        description=(
            "WordPress-, Elementor- und PHP-Version der Seite, Benutzer, unter dem die Tools laufen (erster Administrator), "
            "erkannte Cache- und Weiterleitungs-Plugins, Zeitzone, maximale Upload-Grösse und welche Plugin-Code-Version verwendet wird."
        ),
        annotations=READ_ONLY,
    )
    async def site_info(site: str) -> dict[str, Any]:
        return await runner.read(site, "get_version", {})

    @mcp.tool(
        description="Alle Beitragstypen der Seite mit Verwaltungsoberfläche: Slug (für type), Bezeichnung, URL-Präfix (rewrite_slug), Taxonomien, ob Elementor ihn bearbeitet, Anzahl.",
        annotations=READ_ONLY,
    )
    async def post_type_list(site: str) -> dict[str, Any]:
        return await runner.read(site, "post_type_list", {})

    @mcp.tool(
        description=(
            "Listet Beiträge eines Beitragstyps (type aus post_type_list, z.B. post, page, elementor_library, service; any = alle), neueste zuerst. "
            "Filter: status (Standard any = alles ausser Papierkorb), search, category (ID oder Slug), parent, meta_key/meta_value "
            "(nur _elementor_edit_mode, _elementor_template_type, _wp_page_template; z.B. Popups: type=elementor_library, meta_key=_elementor_template_type, meta_value=popup). Max. 100 pro Seite."
        ),
        annotations=READ_ONLY,
    )
    async def post_list(
        site: str,
        type: str = "post",
        status: Literal["draft", "pending", "publish", "future", "private", "trash", "any"] = "any",
        search: str | None = None,
        category: int | str | None = None,
        parent: int | None = None,
        meta_key: Literal["_elementor_edit_mode", "_elementor_template_type", "_wp_page_template"] | None = None,
        meta_value: str | None = None,
        orderby: Literal["date", "modified", "title", "menu_order", "ID"] = "date",
        order: Literal["ASC", "DESC"] = "DESC",
        limit: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        args = {
            "type": type, "status": status, "search": search, "category": category, "parent": parent, "meta_key": meta_key,
            "meta_value": meta_value, "orderby": orderby, "order": order, "limit": limit, "page": page,
        }
        return await runner.read(site, "post_list", args)

    @mcp.tool(
        description=(
            "Ein Beitrag beliebigen Typs: Inhalt (roh, HTML oder Blöcke), Datum, Status, Begriffe, Beitragsbild, Template, Eltern-Seite, Reihenfolge, "
            "Elementor-Angaben (elementor_info: Vorlagentyp, Bedingungen, Version), Zusatzfelder, Vorschau-Link und die Sicherungen (backups) für post_rollback. "
            "Den Elementor-Aufbau liefert elementor_get."
        ),
        annotations=READ_ONLY,
    )
    async def post_get(site: str, id: int) -> dict[str, Any]:
        return await runner.read(site, "post_get", {"id": id})

    @mcp.tool(description="Kategorien der Seite mit ID, Name, Slug, Eltern-ID und Anzahl Beiträge.", annotations=READ_ONLY)
    async def category_list(site: str, search: str | None = None, limit: int = 100) -> dict[str, Any]:
        return await runner.read(site, "category_list", {"search": search, "limit": limit})

    @mcp.tool(description="Schlagwörter der Seite mit ID, Name, Slug und Anzahl Beiträge.", annotations=READ_ONLY)
    async def tag_list(site: str, search: str | None = None, limit: int = 100) -> dict[str, Any]:
        return await runner.read(site, "tag_list", {"search": search, "limit": limit})

    @mcp.tool(
        description="Begriffe einer beliebigen Taxonomie (category, post_tag oder eigene) mit ID, Name, Slug, Beschreibung, Eltern-ID und Anzahl. Ohne taxonomy: Liste der Taxonomien.",
        annotations=READ_ONLY,
    )
    async def term_list(site: str, taxonomy: str | None = None, search: str | None = None, parent: int | None = None, limit: int = 100, page: int = 1) -> dict[str, Any]:
        return await runner.read(site, "term_list", {"taxonomy": taxonomy, "search": search, "parent": parent, "limit": limit, "page": page})

    @mcp.tool(description="Legt einen Begriff in einer Taxonomie an (Kategorie, Schlagwort oder eigene)." + CONFIRM_DOC, annotations=WRITE)
    async def term_create(
        site: str, taxonomy: str, name: str, slug: str | None = None, description: str | None = None, parent: int | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        return await runner.write(site, "term_create", {"taxonomy": taxonomy, "name": name, "slug": slug, "description": description, "parent": parent, "confirm": confirm})

    @mcp.tool(description="Ändert Name, Slug, Beschreibung oder Eltern-Begriff eines Begriffs. Alter Stand wird gesichert." + CONFIRM_DOC, annotations=WRITE)
    async def term_update(
        site: str,
        taxonomy: str,
        term_id: int,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        parent: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {"taxonomy": taxonomy, "term_id": term_id, "name": name, "slug": slug, "description": description, "parent": parent, "confirm": confirm}
        return await runner.write(site, "term_update", args)

    @mcp.tool(
        description="Löscht einen Begriff, aber nur, wenn ihm keine Beiträge zugeordnet sind. Name, Slug, Beschreibung und Zusatzfelder werden vorher gesichert." + CONFIRM_DOC,
        annotations=TRASH,
    )
    async def term_delete(site: str, taxonomy: str, term_id: int, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "term_delete", {"taxonomy": taxonomy, "term_id": term_id, "confirm": confirm})

    @mcp.tool(
        description="Dateien der Mediathek, neueste zuerst, mit URL zum Verlinken. mime_type z.B. application/pdf oder image.",
        annotations=READ_ONLY,
    )
    async def media_list(site: str, search: str | None = None, mime_type: str | None = None, limit: int = 20, page: int = 1) -> dict[str, Any]:
        return await runner.read(site, "media_list", {"search": search, "mime_type": mime_type, "limit": limit, "page": page})

    @mcp.tool(
        description=(
            "Legt einen Beitrag beliebigen Typs an (type aus post_type_list, Standard post). content als HTML oder Gutenberg-Blöcke (z.B. aus docx_to_content). "
            "status: draft (Standard), pending, publish, future, private; publish mit Datum in der Zukunft wird automatisch future (geplant). "
            "date: YYYY-MM-DD HH:MM in der Zeitzone der Seite oder ISO 8601. categories als ID, Slug oder Name (müssen existieren), "
            "tags als ID oder Name (fehlende werden angelegt), featured_media = Bild-ID aus der Mediathek, "
            "hide_featured_on_single=true blendet das Beitragsbild auf der Beitragsseite aus (Theme-Option, z.B. Blockchain). "
            "terms für weitere Taxonomien, parent/menu_order/template/comment_status wie im wp-admin." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def post_create(
        site: str,
        title: str,
        content: str = "",
        type: PostType = "post",
        status: Status = "draft",
        date: str | None = None,
        categories: TermList | None = None,
        tags: TermList | None = None,
        featured_media: int | None = None,
        excerpt: str | None = None,
        slug: str | None = None,
        hide_featured_on_single: bool | None = None,
        terms: TermMap = None,
        parent: int | None = None,
        menu_order: int | None = None,
        template: str | None = None,
        comment_status: Literal["open", "closed"] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "type": type, "title": title, "content": content, "status": status, "date": date, "categories": categories,
            "tags": tags, "featured_media": featured_media, "excerpt": excerpt, "slug": slug,
            "hide_featured_on_single": hide_featured_on_single, "terms": terms, "parent": parent, "menu_order": menu_order,
            "template": template, "comment_status": comment_status, "confirm": confirm,
        }
        return await runner.write(site, "post_create", args)

    @mcp.tool(
        description=(
            "Ändert einen Beitrag beliebigen Typs, nur die übergebenen Felder. Vor dem Ändern sichert das Plugin den alten Stand "
            "(Rückgabe backup_id, zurückholen mit post_rollback). categories, tags und terms ersetzen die bisherigen, featured_media=0 entfernt das Beitragsbild. "
            "template z.B. default, elementor_canvas, elementor_header_footer. Elementor-Inhalte mit den elementor_*-Tools ändern." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def post_update(
        site: str,
        id: int,
        title: str | None = None,
        content: str | None = None,
        status: Status | None = None,
        date: str | None = None,
        categories: TermList | None = None,
        tags: TermList | None = None,
        featured_media: int | None = None,
        excerpt: str | None = None,
        slug: str | None = None,
        hide_featured_on_single: bool | None = None,
        terms: TermMap = None,
        parent: int | None = None,
        menu_order: int | None = None,
        template: str | None = None,
        comment_status: Literal["open", "closed"] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        args = {
            "id": id, "title": title, "content": content, "status": status, "date": date, "categories": categories,
            "tags": tags, "featured_media": featured_media, "excerpt": excerpt, "slug": slug,
            "hide_featured_on_single": hide_featured_on_single, "terms": terms, "parent": parent, "menu_order": menu_order,
            "template": template, "comment_status": comment_status, "confirm": confirm,
        }
        return await runner.write(site, "post_update", args)

    @mcp.tool(
        description=(
            "Dupliziert einen Beitrag beliebigen Typs (auch Elementor-Seiten, -Vorlagen und -Popups) mit allen Zusatzfeldern, Begriffen und Beitragsbild. "
            "Elementor-Element-IDs werden neu vergeben, die Daten bleiben byte-genau (verify zeigt die Kontrolle). Status Standard draft. "
            "Popups und Theme-Vorlagen erhalten keine Anzeigebedingungen, ausser conditions wird übergeben (nur Elementor Pro)." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def post_duplicate(
        site: str,
        id: int,
        title: str | None = None,
        slug: str | None = None,
        status: Literal["draft", "pending", "publish", "private"] = "draft",
        conditions: Annotated[list[str] | None, Field(description='Elementor-Anzeigebedingungen der Kopie, z.B. ["include/singular/page/12"].')] = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await runner.write(site, "post_duplicate", {"id": id, "title": title, "slug": slug, "status": status, "conditions": conditions, "confirm": confirm})

    @mcp.tool(
        description=(
            "Lädt eine Datei (z.B. PDF oder Bild) in die Mediathek der Seite. Quelle: url (öffentlich erreichbar) oder data_base64. "
            "Rückgabe media.url zum Verlinken im Beitrag, media.id als featured_media für Bilder. post_id ordnet die Datei einem Beitrag zu." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def media_upload(
        site: str,
        url: str | None = None,
        data_base64: str | None = None,
        filename: str | None = None,
        title: str | None = None,
        alt_text: str | None = None,
        caption: str | None = None,
        description: str | None = None,
        post_id: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        ctx = runner.context(WRITE_SCOPE)
        runner.site(site)
        data, detected = await runner.load_file(url, data_base64)
        name = (filename or detected or "").strip()
        if not name or "." not in name:
            raise ToolError("filename mit Endung angeben (z.B. merkblatt.pdf).")
        args = {
            "filename": name, "data_base64": base64.b64encode(data).decode(), "title": title, "alt_text": alt_text,
            "caption": caption, "description": description, "post_id": post_id, "confirm": confirm,
        }
        return await runner.write(site, "media_upload", args, ctx)

    @mcp.tool(
        description="Verschiebt einen Beitrag beliebigen Typs in den Papierkorb (kein endgültiges Löschen, in WordPress wiederherstellbar)." + CONFIRM_DOC,
        annotations=TRASH,
    )
    async def post_trash(site: str, id: int, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "post_trash", {"id": id, "confirm": confirm})

    @mcp.tool(
        description=(
            "Stellt einen Beitrag aus einer Sicherung wieder her (backup_id aus post_get, leer = neueste Sicherung), auch die Elementor-Daten exakt "
            "(nach elementor_*-Änderungen). Der aktuelle Stand wird vorher ebenfalls gesichert, der Rollback ist also selbst umkehrbar." + CONFIRM_DOC
        ),
        annotations=WRITE,
    )
    async def post_rollback(site: str, id: int, backup_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return await runner.write(site, "post_rollback", {"id": id, "backup_id": backup_id, "confirm": confirm})

    @mcp.tool(
        description=(
            "Wandelt ein Word-Dokument (.docx, per url oder data_base64) in Inhalt für post_create um: output=blocks (Gutenberg, Standard) "
            "oder html. Übernimmt Überschriften, Listen, Fett/Kursiv, Links und Tabellen, entfernt Schriftarten, Farben, leere Absätze "
            "und eingebettete Bilder. Die erste Überschrift wird als title zurückgegeben (extract_title), die übrigen Überschriften "
            "beginnen bei H2 (normalize_headings). Ändert nichts auf einer Seite."
        ),
        annotations=READ_ONLY,
    )
    async def docx_to_content(
        url: str | None = None,
        data_base64: str | None = None,
        output: Literal["blocks", "html"] = "blocks",
        extract_title: bool = True,
        normalize_headings: bool = True,
    ) -> dict[str, Any]:
        runner.context(READ)
        data, filename = await runner.load_file(url, data_base64)
        try:
            result = await asyncio.to_thread(convert_docx, data, output=output, extract_title=extract_title, normalize_headings=normalize_headings)
        except DocxError as exc:
            raise ToolError(str(exc))
        if filename:
            result["source_filename"] = filename
        return result
