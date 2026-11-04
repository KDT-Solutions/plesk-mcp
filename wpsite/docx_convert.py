"""Word (.docx) to clean HTML or Gutenberg block markup.

mammoth maps Word styles to semantic HTML (headings, lists, bold, italic,
links, tables, quotes) and ignores fonts, colours and sizes. The result is
cleaned (empty paragraphs, bookmarks, spans, attributes, embedded images) and
optionally wrapped into core blocks with the markup the block editor saves, so
the editor does not report invalid blocks.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from typing import Any, Literal

import mammoth
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

MAX_UNCOMPRESSED = 100 * 1024 * 1024

# German and English Word style names. mammoth knows "Heading 1".."Heading 6" already.
STYLE_MAP = "\n".join(
    [
        "p[style-name='Title'] => h1:fresh",
        "p[style-name='Titel'] => h1:fresh",
        "p[style-name='Subtitle'] => p:fresh",
        "p[style-name='Untertitel'] => p:fresh",
        *[f"p[style-name='Überschrift {n}'] => h{n}:fresh" for n in range(1, 7)],
        *[f"p[style-name='heading {n}'] => h{n}:fresh" for n in range(1, 7)],
        "p[style-name='Quote'] => blockquote > p:fresh",
        "p[style-name='Zitat'] => blockquote > p:fresh",
        "p[style-name='Intense Quote'] => blockquote > p:fresh",
        "p[style-name='Intensives Zitat'] => blockquote > p:fresh",
        "r[style-name='Strong'] => strong",
        "r[style-name='Fett'] => strong",
    ]
)

ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "a", "br", "sup", "sub", "s",
    "table", "thead", "tbody", "tr", "th", "td", "blockquote",
}
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
SAFE_HREF = re.compile(r"^(https?:|mailto:|tel:|#)", re.I)


class DocxError(Exception):
    pass


def _check_zip(data: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocxError("Die Datei ist kein .docx (kein ZIP-Archiv). Alte .doc-Dateien bitte in Word als .docx speichern.") from exc
    names = archive.namelist()
    if "word/document.xml" not in names:
        raise DocxError("Die Datei ist kein Word-Dokument (.docx).")
    if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED:
        raise DocxError("Das Dokument ist entpackt zu gross.")


def _clean(soup: BeautifulSoup, stats: dict[str, int]) -> None:
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for img in soup.find_all("img"):
        stats["images_removed"] += 1
        img.decompose()
    # Bookmarks from Word (<a id="_Toc..."></a>) and other anchors without href
    for a in soup.find_all("a"):
        if not a.get("href") and not (a.get("id") or "").startswith("footnote"):
            a.unwrap()
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        keep: dict[str, Any] = {}
        if tag.name == "a":
            href = (tag.get("href") or "").strip()
            if href and SAFE_HREF.match(href):
                keep["href"] = href
            elif href:
                stats["links_removed"] += 1
            if (tag.get("id") or "").startswith("footnote"):
                keep["id"] = tag["id"]
        if tag.name == "li" and (tag.get("id") or "").startswith("footnote"):
            keep["id"] = tag["id"]
        if tag.name in ("td", "th"):
            for attr in ("colspan", "rowspan"):
                if tag.get(attr) and str(tag[attr]).isdigit() and str(tag[attr]) != "1":
                    keep[attr] = tag[attr]
        tag.attrs = keep
        if tag.name == "a" and not keep:
            tag.unwrap()
    # Non-breaking spaces from Word become normal spaces
    for text in soup.find_all(string=True):
        if "\xa0" in text:
            text.replace_with(text.replace("\xa0", " "))
    # Empty paragraphs and headings (Word uses them for spacing)
    for tag in soup.find_all(["p", *HEADINGS, "li"]):
        if not tag.get_text(strip=True) and not tag.find(["br", "table"]):
            tag.decompose()
    # Table cells: Word wraps cell text in paragraphs
    for cell in soup.find_all(["td", "th"]):
        paragraphs = cell.find_all("p", recursive=False)
        if len(paragraphs) == 1:
            paragraphs[0].unwrap()
    # Line breaks at the start or end of a block
    for tag in soup.find_all(["p", *HEADINGS, "li", "td", "th"]):
        while tag.contents and isinstance(tag.contents[-1], Tag) and tag.contents[-1].name == "br":
            tag.contents[-1].decompose()
        while tag.contents and isinstance(tag.contents[0], Tag) and tag.contents[0].name == "br":
            tag.contents[0].decompose()
    # Empty inline tags (<strong></strong>) and merged neighbours (<strong>a</strong><strong>b</strong>)
    for tag in soup.find_all(["strong", "em", "s", "sup", "sub"]):
        if not tag.get_text():
            tag.decompose()
    for name in ("strong", "em"):
        for tag in soup.find_all(name):
            nxt = tag.next_sibling
            while isinstance(nxt, Tag) and nxt.name == name:
                for child in list(nxt.contents):
                    tag.append(child)
                following = nxt.next_sibling
                nxt.decompose()
                nxt = following


def _inner(tag: Tag) -> str:
    return "".join(str(c) for c in tag.contents).strip()


def _cell_html(cell: Tag) -> str:
    # The table block stores cell text as rich text without <p>.
    paragraphs = cell.find_all("p", recursive=False)
    if paragraphs:
        return "<br>".join(_inner(p) for p in paragraphs if _inner(p))
    return _inner(cell)


def _list_block(tag: Tag) -> str:
    if tag.find("li", id=True):
        # Footnotes keep their anchors; the list block cannot store ids.
        return f"<!-- wp:html -->\n{tag}\n<!-- /wp:html -->"
    ordered = tag.name == "ol"
    attrs = ' {"ordered":true}' if ordered else ""
    items = []
    for li in tag.find_all("li", recursive=False):
        nested = [child for child in li.find_all(["ul", "ol"], recursive=False)]
        for child in nested:
            child.extract()
        paragraphs = li.find_all("p", recursive=False)
        inner = "<br>".join(_inner(p) for p in paragraphs if _inner(p)) if paragraphs else _inner(li)
        nested_html = "".join(_list_block(child) for child in nested)
        items.append(f"<!-- wp:list-item -->\n<li>{inner}{nested_html}</li>\n<!-- /wp:list-item -->")
    return f'<!-- wp:list{attrs} -->\n<{tag.name} class="wp-block-list">' + "\n\n".join(items) + f"</{tag.name}>\n<!-- /wp:list -->"


def _table_block(tag: Tag) -> str:
    rows_head: list[str] = []
    rows_body: list[str] = []
    for tr in tag.find_all("tr"):
        cells = []
        is_head = tr.parent is not None and tr.parent.name == "thead"
        for cell in tr.find_all(["td", "th"], recursive=False):
            name = "th" if is_head else cell.name
            extra = "".join(f' {k}="{html.escape(str(v))}"' for k, v in cell.attrs.items() if k in ("colspan", "rowspan"))
            cells.append(f"<{name}{extra}>{_cell_html(cell)}</{name}>")
        (rows_head if is_head else rows_body).append("<tr>" + "".join(cells) + "</tr>")
    head = f"<thead>{''.join(rows_head)}</thead>" if rows_head else ""
    body = f"<tbody>{''.join(rows_body)}</tbody>"
    return f'<!-- wp:table -->\n<figure class="wp-block-table"><table class="has-fixed-layout">{head}{body}</table></figure>\n<!-- /wp:table -->'


def _paragraph_block(inner: str) -> str:
    return f"<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->"


def _to_blocks(soup: BeautifulSoup) -> str:
    blocks: list[str] = []
    for node in list(soup.contents):
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                blocks.append(_paragraph_block(html.escape(text)))
            continue
        if not isinstance(node, Tag):
            continue
        if node.name == "p":
            blocks.append(_paragraph_block(_inner(node)))
        elif node.name in HEADINGS:
            level = int(node.name[1])
            attrs = "" if level == 2 else " " + json.dumps({"level": level}, separators=(",", ":"))
            blocks.append(f'<!-- wp:heading{attrs} -->\n<{node.name} class="wp-block-heading">{_inner(node)}</{node.name}>\n<!-- /wp:heading -->')
        elif node.name in ("ul", "ol"):
            blocks.append(_list_block(node))
        elif node.name == "table":
            blocks.append(_table_block(node))
        elif node.name == "blockquote":
            inner = "".join(_paragraph_block(_inner(p)) for p in node.find_all("p", recursive=False)) or _paragraph_block(_inner(node))
            blocks.append(f'<!-- wp:quote -->\n<blockquote class="wp-block-quote">{inner}</blockquote>\n<!-- /wp:quote -->')
        else:
            blocks.append(f"<!-- wp:html -->\n{node}\n<!-- /wp:html -->")
    return "\n\n".join(blocks)


def docx_to_content(
    data: bytes,
    *,
    output: Literal["blocks", "html"] = "blocks",
    extract_title: bool = True,
    normalize_headings: bool = True,
) -> dict[str, Any]:
    _check_zip(data)
    try:
        result = mammoth.convert_to_html(io.BytesIO(data), style_map=STYLE_MAP)
    except Exception as exc:  # noqa: BLE001 - mammoth raises many types for broken files
        raise DocxError(f"Das Word-Dokument kann nicht gelesen werden ({exc.__class__.__name__}).") from exc

    stats = {"images_removed": 0, "links_removed": 0}
    soup = BeautifulSoup(result.value, "html.parser")
    _clean(soup, stats)

    title = None
    if extract_title:
        first = next((c for c in soup.contents if isinstance(c, Tag)), None)
        if first is not None and first.name in HEADINGS:
            title = first.get_text(" ", strip=True)
            first.decompose()

    shift = 0
    if normalize_headings:
        levels = [int(h.name[1]) for h in soup.find_all(HEADINGS)]
        if levels:
            shift = 2 - min(levels)
            for h in soup.find_all(HEADINGS):
                h.name = f"h{min(6, max(2, int(h.name[1]) + shift))}"

    warnings = sorted({m.message for m in result.messages if m.type == "warning"})
    if stats["images_removed"]:
        warnings.append(f"{stats['images_removed']} eingebettete(s) Bild(er) entfernt. Bilder bitte separat mit media_upload hochladen.")
    if stats["links_removed"]:
        warnings.append(f"{stats['links_removed']} Link(s) mit unzulässigem Ziel entfernt (nur http, https, mailto, tel).")

    counts = {
        "paragraphs": len(soup.find_all("p")),
        "headings": len(soup.find_all(HEADINGS)),
        "lists": len(soup.find_all(["ul", "ol"])),
        "tables": len(soup.find_all("table")),
        "links": len(soup.find_all("a", href=True)),
        "images_removed": stats["images_removed"],
        "characters": len(soup.get_text()),
    }
    content = _to_blocks(soup) if output == "blocks" else "\n".join(str(c) for c in soup.contents if str(c).strip())
    return {
        "title": title,
        "content": content,
        "format": output,
        "heading_shift": shift,
        "stats": counts,
        "warnings": warnings,
    }
