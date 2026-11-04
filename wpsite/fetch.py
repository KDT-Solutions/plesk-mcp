"""Download of files from URLs given by the client (docx_to_content, media_upload).

The gateway runs inside our network, so a URL must never reach internal
addresses: only http/https without credentials, the host is resolved once and
every address must be public, the connection is pinned to the checked address
(Host header and TLS SNI keep the original name, certificate checked against
it), every redirect hop is checked again, size limit while streaming.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from . import __version__

MAX_REDIRECTS = 5

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    pass


@dataclass
class Downloaded:
    data: bytes
    filename: str | None
    content_type: str | None
    url: str


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"Host {host} nicht auflösbar") from exc
    return sorted({info[4][0] for info in infos})


def _filename(response: httpx.Response, url: str) -> str | None:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)"?', disposition, re.I)
    name = unquote(match.group(1)) if match else unquote(urlparse(url).path.rsplit("/", 1)[-1])
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


async def fetch_url(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Downloaded:
    current = url.strip()
    async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise FetchError("Nur http- und https-Adressen sind erlaubt")
            if parsed.username or parsed.password:
                raise FetchError("Adressen mit Zugangsdaten sind nicht erlaubt")
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = [host] if _is_ip(host) else resolver(host, port)
            if not addresses or not all(_is_public(a) for a in addresses):
                raise FetchError(f"{host} zeigt auf eine nicht öffentliche Adresse")
            ip = addresses[0]
            netloc = f"[{ip}]" if ":" in ip else ip
            if parsed.port:
                netloc += f":{parsed.port}"
            pinned = parsed._replace(netloc=netloc).geturl()
            host_header = host if not parsed.port else f"{host}:{parsed.port}"
            extensions = {"sni_hostname": host} if parsed.scheme == "https" and not _is_ip(host) else {}
            request = client.build_request(
                "GET", pinned, headers={"Host": host_header, "User-Agent": f"plesk-mcp-wpsite/{__version__}"}, extensions=extensions
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise FetchError(f"Download fehlgeschlagen ({exc.__class__.__name__})") from exc
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("Weiterleitung ohne Ziel")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise FetchError(f"Download fehlgeschlagen (HTTP {response.status_code})")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"Datei ist grösser als {max_bytes // 1048576} MB")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise FetchError(f"Datei ist grösser als {max_bytes // 1048576} MB")
                ctype = (response.headers.get("content-type") or "").split(";")[0].strip() or None
                return Downloaded(bytes(chunks), _filename(response, current), ctype, current)
            finally:
                await response.aclose()
    raise FetchError("Zu viele Weiterleitungen")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
