"""Registrierung der WordPress-Tools im plesk-mcp.

Namen: wp_<name des wordpress-mcp> (post_list -> wp_post_list), die
WP-Time-Capsule-Tools behalten ihr Präfix (wptc_status). Gibt es den Namen im
plesk-mcp schon (wp_plugin_list, wp_plugin_update, wp_theme_list,
wp_theme_update, wp_core_update, wp_option_update, wp_option_rollback,
wptc_set_revision_limit), bleibt das bestehende Tool, die Kopie wird nicht
registriert. custom_css_* entfällt, weil es wp_custom_css_show/update/rollback
schon gibt.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .common import SITE_DOC, ToolRunner
from .tools_admin import register_admin_tools
from .tools_content import register_content_tools
from .tools_elementor import register_elementor_tools
from .tools_site import register_site_tools

SKIP = {"custom_css_get", "custom_css_update", "custom_css_rollback"}


def tool_name(base: str) -> str:
    return base if base.startswith("wptc_") else "wp_" + base


class _Registrar:
    """Ersetzt mcp für die Tool-Module: vergibt den Namen mit Präfix, hängt
    die Beschreibung von site an und überspringt vorhandene Namen."""

    def __init__(self, tool_decorator: Callable[..., Any], existing: set[str]):
        self._tool = tool_decorator
        self._existing = existing
        self.registered: list[str] = []
        self.skipped: list[str] = []

    def tool(self, *args: Any, description: str | None = None, annotations: Any = None, **kwargs: Any):
        def decorator(fn):
            base = fn.__name__
            name = tool_name(base)
            if base in SKIP or name in self._existing:
                self.skipped.append(name)
                return fn
            text = description or (inspect.getdoc(fn) or "")
            if "site" in inspect.signature(fn).parameters:
                text += SITE_DOC
            self._tool(name=name, description=text, annotations=annotations, **kwargs)(fn)
            self._existing.add(name)
            self.registered.append(name)
            return fn

        return decorator


def register_all(tool_decorator: Callable[..., Any], runner: ToolRunner, existing: set[str]) -> dict[str, list[str]]:
    reg = _Registrar(tool_decorator, set(existing))
    register_content_tools(reg, runner)
    register_elementor_tools(reg, runner)
    register_site_tools(reg, runner)
    register_admin_tools(reg, runner)
    return {"registered": reg.registered, "skipped": reg.skipped}
