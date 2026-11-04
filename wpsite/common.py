"""Gemeinsame Teile der WordPress-Tools: Annotationen, Beschreibungstexte und
der ToolRunner. Die Tool-Module (Kopien aus wordpress-mcp) rufen
runner.read/runner.write(site, tool, args) auf; ausgeführt wird über eine
Funktion des plesk-mcp (WP-CLI per SSH, siehe server._wpsite_call).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import WPSiteError
from .fetch import FetchError, fetch_url

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
TRASH = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

CONFIRM_DOC = " Ohne confirm=true wird nichts geändert, es kommt nur eine Vorschau. confirm=true erst setzen, wenn der Benutzer die Vorschau bestätigt hat."
ADMIN_DOC = " Admin-Tool: Vorschau zuerst dem Benutzer zeigen und nur nach ausdrücklicher Bestätigung mit confirm=true ausführen."
SITE_DOC = (
    " site: Domain auf dem Plesk-Server (z.B. example.com, WordPress im Webroot der Domain) "
    "oder example.com:<wp_path> (Verzeichnis relativ zu /var/www/vhosts/<domain>/, z.B. example.com:httpdocs/blog)."
)

# call(site, tool, args) -> Ergebnis-Dict, wirft WPSiteError (synchron, läuft im Thread)
Call = Callable[[str, str, dict[str, Any]], dict[str, Any]]
# audit(action, **felder)
Audit = Callable[..., None]


@dataclass
class Ctx:
    user: str
    client_id: str


class ToolRunner:
    def __init__(
        self,
        call: Call,
        audit: Audit,
        check_site: Callable[[str], None],
        *,
        max_response_chars: int = 500 * 1024,
        max_file_bytes: int = 20 * 1024 * 1024,
        fetch_options: dict[str, Any] | None = None,
    ):
        self._call = call
        self._audit = audit
        self._check_site = check_site
        self.max_response_chars = max_response_chars
        self.max_file_bytes = max_file_bytes
        self._fetch_options = fetch_options or {}

    def context(self, need_scope: str | None = None) -> Ctx:
        # Authentifizierung macht der plesk-mcp (MCP_API_KEY) vor jedem Aufruf.
        return Ctx(user="plesk-mcp", client_id="")

    def site(self, site: str) -> None:
        try:
            self._check_site(site)
        except ValueError as exc:
            raise ToolError(str(exc))

    async def _run(self, site: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.site(site)
        args = {k: v for k, v in (args or {}).items() if v is not None}
        try:
            return await asyncio.to_thread(self._call, site, tool, args)
        except WPSiteError as exc:
            raise ToolError(exc.message)
        except ToolError:
            raise
        except (ValueError, OSError) as exc:
            raise ToolError(str(exc))

    async def read(self, site: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._run(site, tool, args)
        return self._limited({"site": site, **result})

    async def write(self, site: str, tool: str, args: dict[str, Any], ctx: Ctx | None = None) -> dict[str, Any]:
        confirmed = bool(args.get("confirm"))
        log: dict[str, Any] = {"site": site, "tool": "wp_" + tool, "confirm": confirmed}
        for key in ("id", "post_id", "target_post_id", "element_id", "plugin", "slug", "theme", "user_id",
                    "menu_id", "item_id", "widget_id", "term_id", "redirect_id"):
            if args.get(key):
                log[key] = args[key]
        try:
            result = await self._run(site, tool, args)
        except ToolError as exc:
            if confirmed:
                self._audit("wpsite_write", **log, ok=False, error=str(exc)[:300])
            raise
        if confirmed:
            for key in ("backup_id", "safety_backup_id", "backup_ids", "affected_posts"):
                if result.get(key):
                    log[key] = result[key]
            if isinstance(result.get("post"), dict) and result["post"].get("id"):
                log["post_id"] = result["post"]["id"]
            if isinstance(result.get("media"), dict):
                log["media_id"] = result["media"].get("id")
            self._audit("wpsite_write", **log, ok=True, executed=bool(result.get("confirmed")))
        return self._limited({"site": site, **result})

    def _limited(self, result: dict[str, Any]) -> dict[str, Any]:
        """Nie eine Antwort zurückgeben, die grösser als das Limit ist."""
        size = len(json.dumps(result, ensure_ascii=False))
        limit = self.max_response_chars
        if size > limit:
            raise ToolError(
                f"Antwort zu gross ({size // 1024} KB, erlaubt {limit // 1024} KB). Bitte eingrenzen, "
                "z.B. mit element_id, full=false, limit, page oder search."
            )
        return result

    async def load_file(self, url: str | None, data_base64: str | None) -> tuple[bytes, str | None]:
        if bool(url) == bool(data_base64):
            raise ToolError("Genau eines von url oder data_base64 angeben.")
        limit = self.max_file_bytes
        if url:
            try:
                downloaded = await fetch_url(url, max_bytes=limit, **self._fetch_options)
            except FetchError as exc:
                raise ToolError(f"Download fehlgeschlagen: {exc}")
            return downloaded.data, downloaded.filename
        raw = data_base64 or ""
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        raw = "".join(raw.split())
        if len(raw) * 3 // 4 > limit:
            raise ToolError(f"Datei ist grösser als {limit // 1048576} MB.")
        try:
            return base64.b64decode(raw, validate=True), None
        except (binascii.Error, ValueError):
            raise ToolError("data_base64 ist kein gültiges Base64.")
