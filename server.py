#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=2.0.0,<3.0.0",
#   "paramiko>=3.4.0",
#   "httpx>=0.27.0",
#   "starlette>=0.27.0",
#   "uvicorn>=0.23.0",
# ]
# ///
"""
plesk-mcp

Version: 0.5.1 (kein pyproject.toml mehr wie im alten src/-Package - Version
wird hier im Docstring nachgeführt; Deployment-Tracking läuft sonst über den
GHCR-Image-Tag/Git-SHA, analog zu bexio-mcp)

Read-only MCP-Server für Diagnose auf einem Plesk-Server. Drei Datenquellen:

- SSH (paramiko) für alles, was nur auf Betriebssystem-Ebene existiert
  (PHP-FPM-Status, systemd-Journal, OOM-Kills, Disk-Nutzung, Serverlast) und
  für Plesk-CLI-Befehle (plesk bin ..., plesk ext ...).
- Die Plesk-REST-API (X-API-Key) für strukturierte Plesk-eigene Daten
  (Domains, Subscriptions etc.) über das generische plesk_api_get-Tool.
- MySQL/MariaDB direkt (über den Plesk-internen Admin-DB-Zugang aus
  /etc/psa/.psa.shadow, siehe db_list/db_query/db_search) für read-only
  Datenbank-Abfragen ohne eigene, zusätzlich zu konfigurierende
  DB-Zugangsdaten.

Gedacht für Fehleranalyse bei Support-Anfragen (z.B. "Website nicht erreichbar").
Fast alle Tools sind read-only: kein Neustart von Services, keine destruktiven
Kommandos (Whitelist + Blacklist weiter unten). Ausnahme: write_vhost_file,
delete_vhost_backup, dns_add_record, dns_delete_record und dns_update_record
dürfen schreiben - die ersten beiden Dateien innerhalb von
/var/www/vhosts/<domain>/ anlegen/überschreiben/löschen (Backups), die
letzten drei DNS-Resource-Records der Domain-Zone anlegen/entfernen/ändern
(plesk bin dns --add/--del; dns_update_record kombiniert beides für einen
bestehenden Record). Alle fünf erfordern zwingend confirm=true pro Aufruf
(keine globale Freischaltung), write_vhost_file legt vor dem Überschreiben
automatisch ein Backup der alten Version an.

Transport wird über die Umgebungsvariable MCP_TRANSPORT gesteuert:
- "stdio" (Standard) - für die lokale Nutzung via uv/Claude Desktop.
- "http" - startet den Server als HTTP-Dienst (Streamable HTTP) für den Cloud-
  Einsatz, z.B. hinter einem Reverse-Proxy. Erfordert MCP_API_KEY.

Alle Zugangsdaten (SSH, Plesk-API-Key, MCP_API_KEY) werden ausschliesslich
über Umgebungsvariablen gesetzt - es sind keine echten Zugangsdaten im Code
hinterlegt.
"""

from __future__ import annotations

import base64
import datetime
import os
import re
import shlex
import stat
import sys
from typing import Any

import paramiko
from mcp.server.mcpserver import MCPServer

# Force UTF-8 on Windows where default may be cp1252
if sys.platform == "win32":
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

# HTTP mode: MCP_TRANSPORT=http (Cloud/Docker) statt stdio (lokal, Standard)
_HTTP_MODE = os.environ.get("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http")
_HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
_HTTP_PORT = int(os.environ.get("MCP_PORT", "8000"))
_MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

if _HTTP_MODE and not _MCP_API_KEY:
    raise RuntimeError(
        "MCP_TRANSPORT=http erfordert MCP_API_KEY (statisches Bearer-Token) - "
        "aus Sicherheitsgruenden kein Start ohne Token."
    )

mcp = MCPServer("plesk-mcp")

# ---------------------------------------------------------------------------
# SSH-Verbindung zum Plesk-Server
# ---------------------------------------------------------------------------

_SSH_HOST = os.environ.get("PLESK_SSH_HOST", "")
_SSH_PORT = int(os.environ.get("PLESK_SSH_PORT", "22"))
_SSH_USER = os.environ.get("PLESK_SSH_USER", "root")
_SSH_PASSWORD = os.environ.get("PLESK_SSH_PASSWORD")
_SSH_KEY_PATH = os.environ.get("PLESK_SSH_KEY_PATH")  # Alternative zu Passwort
_SSH_TIMEOUT = int(os.environ.get("PLESK_SSH_TIMEOUT", "20"))

# Kommandos, die der generische Diagnose-Tool (run_diagnostic) ausführen darf
# (read-only). Jeder Eintrag ist ein erlaubtes erstes Wort (Programm) des
# Befehls - "plesk" deckt damit auch "plesk bin dns ..." und
# "plesk ext wp-toolkit ..." ab.
ALLOWED_COMMAND_PREFIXES = {
    "plesk",
    "lveinfo",
    "lveps",
    "lve_readall",
    "systemctl",
    "journalctl",
    "grep",
    "tail",
    "head",
    "cat",
    "find",
    "ls",
    "du",
    "df",
    "dmesg",
    "hostname",
    "uptime",
    "top",
    "free",
    "ps",
    "wc",
    # Nur lesende Dekomprimierung von rotierten .gz-Logs - im Gegensatz zu
    # "gunzip" (löscht standardmässig die Originaldatei) bewusst NICHT
    # freigegeben, obwohl gunzip sonst ein plausibler Kandidat wäre.
    "zcat",
    "zgrep",
}

# Subcommands/Wörter, die trotz erlaubtem Programmnamen verboten bleiben
# (z.B. systemctl restart/stop). Als ganzes Wort geprüft (Regex \b...\b),
# damit z.B. "lve_kill_log" (legitimer Dateiname) nicht fälschlich wegen
# "kill" blockiert wird, "rm -rf" aber schon.
FORBIDDEN_WORDS = {
    "restart", "stop", "reload", "start", "kill", "killall", "rm", "delete",
    "remove", "passwd", "shutdown", "reboot", "mv", "dd", "chmod", "chown",
    "mkfs", "bash", "sh", "zsh", "python", "python3", "perl", "curl", "wget",
    "nc", "eval", "exec",
    # Generische Schreib-/Zustands-Subcommands, v.a. relevant fuer
    # "plesk bin dns/...": --add/-a, --del/-d, --set, --reset, --on/--off,
    # --update-soa etc. sind alle schreibend, auch wenn "plesk" selbst in
    # ALLOWED_COMMAND_PREFIXES steht - ohne diese Woerter waeren sie ueber
    # run_diagnostic trotz "read-only"-Anspruch ausfuehrbar gewesen (siehe
    # dns_add_record/dns_delete_record/dns_update_record fuer den
    # vorgesehenen, confirm=true-abgesicherten Weg, DNS-Records zu aendern).
    "add", "del", "set", "reset", "on", "off", "update",
}
_FORBIDDEN_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN_WORDS) + r")\b", re.IGNORECASE
)

# Shell-Konstrukte, die Command-Chaining/Injection ermöglichen - werden als
# reiner Substring-Check verboten, unabhängig vom Rest des Befehls.
FORBIDDEN_SHELL_CONSTRUCTS = (";", "&&", "||", "`", "$(", ">", "<", "&")


class SSHError(RuntimeError):
    pass


def _ssh_connect() -> paramiko.SSHClient:
    if not _SSH_HOST or not _SSH_USER:
        raise SSHError(
            "PLESK_SSH_HOST/PLESK_SSH_USER sind nicht gesetzt (Umgebungsvariablen fehlen)."
        )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = dict(
        hostname=_SSH_HOST,
        port=_SSH_PORT,
        username=_SSH_USER,
        timeout=_SSH_TIMEOUT,
        banner_timeout=_SSH_TIMEOUT,
        auth_timeout=_SSH_TIMEOUT,
    )
    if _SSH_KEY_PATH:
        connect_kwargs["key_filename"] = _SSH_KEY_PATH
    elif _SSH_PASSWORD:
        connect_kwargs["password"] = _SSH_PASSWORD
    else:
        raise SSHError("Weder PLESK_SSH_PASSWORD noch PLESK_SSH_KEY_PATH gesetzt.")
    client.connect(**connect_kwargs)
    return client


def ssh_run(command: str, timeout: int | None = None) -> str:
    """Führt ein Kommando ungeprüft aus (nur für die fest verdrahteten Tools,
    NICHT für Nutzereingaben ohne Whitelist-Check - dafür ssh_run_whitelisted).
    """
    client = _ssh_connect()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout or _SSH_TIMEOUT)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
    finally:
        client.close()

    result = out
    if err.strip():
        result += f"\n[stderr]\n{err}"
    if exit_code != 0:
        result += f"\n[exit code: {exit_code}]"
    return result.strip() or "(keine Ausgabe)"


def ssh_run_whitelisted(command: str, timeout: int | None = None) -> str:
    """Führt ein Kommando aus, das direkt von Claude/dem Nutzer kommt. Prüft:
    1. Keine Shell-Chaining-Konstrukte (;, &&, ||, `, $(, >, <, &)
    2. Jedes Pipe-Segment beginnt mit einem erlaubten Programm
    3. Keine verbotenen Wörter (restart/stop/kill/rm/... als eigenständiges
       Wort, nicht als Teilstring in Dateinamen wie "lve_kill_log")
    """
    stripped = command.strip()
    if not stripped:
        raise SSHError("Leeres Kommando.")

    for construct in FORBIDDEN_SHELL_CONSTRUCTS:
        if construct in stripped:
            raise SSHError(
                f"Kommando enthält verbotenes Shell-Konstrukt '{construct}'. "
                "Command-Chaining/Umleitung ist nicht erlaubt."
            )

    match = _FORBIDDEN_WORD_RE.search(stripped)
    if match:
        raise SSHError(
            f"Kommando enthält verbotenes Wort '{match.group(1)}'. "
            "Dieses Tool ist read-only für Diagnosezwecke."
        )

    try:
        tokens = shlex.split(stripped)
    except ValueError as e:
        raise SSHError(f"Kommando konnte nicht geparst werden: {e}")
    if not tokens:
        raise SSHError("Leeres Kommando.")

    # Whitelist pro Pipe-Segment prüfen (falls mehrere Programme verkettet sind)
    segment: list[str] = []
    segments: list[list[str]] = [segment]
    for tok in tokens:
        if tok == "|":
            segment = []
            segments.append(segment)
        else:
            segment.append(tok)

    for seg in segments:
        if not seg:
            raise SSHError("Leeres Pipe-Segment im Kommando.")
        program = seg[0]
        if program not in ALLOWED_COMMAND_PREFIXES:
            raise SSHError(
                f"Programm '{program}' ist nicht erlaubt. "
                f"Erlaubt sind: {', '.join(sorted(ALLOWED_COMMAND_PREFIXES))}"
            )

    return ssh_run(stripped, timeout=timeout)


def _domain_arg(domain: str) -> str:
    """Validiert grob, dass domain wie ein Domainname aussieht, um
    Shell-Injection über die vorformulierten Kommandos zu vermeiden.
    """
    d = domain.strip()
    if not d or any(c in d for c in " ;|&`$(){}<>\"'\n\t"):
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return d


_VHOST_BASE = "/var/www/vhosts"
_BACKUP_SUFFIX_RE = re.compile(r"\.bak-\d{14}$")


def _vhost_path(domain: str, path: str) -> str:
    """Löst domain+relativen Pfad zu einem absoluten Pfad auf, der zwingend
    innerhalb von /var/www/vhosts/<domain>/ liegen muss. Verhindert
    Path-Traversal (z.B. "../../../etc/passwd") über os.path.normpath +
    Prefix-Check.
    """
    d = _domain_arg(domain)
    base = f"{_VHOST_BASE}/{d}"
    rel = path.strip().lstrip("/")
    if not rel:
        raise ValueError("path darf nicht leer sein.")
    full = os.path.normpath(f"{base}/{rel}")
    if full != base and not full.startswith(base + "/"):
        raise ValueError(
            f"Pfad {path!r} verlässt das Vhost-Verzeichnis von '{d}' - nicht erlaubt."
        )
    return full


def _sftp_makedirs(sftp, remote_dir: str) -> None:
    """Legt ein Verzeichnis inkl. Eltern über SFTP an, falls es noch nicht
    existiert - paramikos SFTPClient hat kein makedirs eingebaut."""
    if remote_dir in ("", "/", _VHOST_BASE):
        return
    try:
        sftp.stat(remote_dir)
        return
    except FileNotFoundError:
        pass
    _sftp_makedirs(sftp, remote_dir.rsplit("/", 1)[0])
    sftp.mkdir(remote_dir)


# ---------------------------------------------------------------------------
# Plesk-REST-API (X-API-Key) - für strukturierte Plesk-eigene Daten
# ---------------------------------------------------------------------------

import httpx  # noqa: E402  (bewusst nach den Konstanten, analog zu bexio-mcp)

_API_HOST = os.environ.get("PLESK_API_HOST", _SSH_HOST)
_API_PORT = int(os.environ.get("PLESK_API_PORT", "8443"))
_API_KEY = os.environ.get("PLESK_API_KEY", "")
_API_VERIFY_SSL = os.environ.get("PLESK_API_VERIFY_SSL", "true").strip().lower() not in (
    "false", "0", "no",
)
_API_TIMEOUT = int(os.environ.get("PLESK_API_TIMEOUT", "20"))


def plesk_api_get_raw(path: str, params: dict[str, str] | None = None) -> str:
    """GET-Request gegen die Plesk-REST-API (https://<host>:8443/api/v2/<path>).
    Nur GET - schreibende Requests sind über dieses Tool bewusst nicht möglich.
    """
    if not _API_HOST:
        raise RuntimeError(
            "PLESK_API_HOST/PLESK_SSH_HOST ist nicht gesetzt (Umgebungsvariable fehlt)."
        )
    if not _API_KEY:
        raise RuntimeError("PLESK_API_KEY ist nicht gesetzt (Umgebungsvariable fehlt).")

    clean_path = path.strip().lstrip("/")
    url = f"https://{_API_HOST}:{_API_PORT}/api/v2/{clean_path}"
    resp = httpx.get(
        url,
        params=params or {},
        headers={"X-API-Key": _API_KEY, "Accept": "application/json"},
        timeout=_API_TIMEOUT,
        verify=_API_VERIFY_SSL,
    )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# MySQL/MariaDB-Datenbanken - read-only, via Plesk-internen Admin-DB-Zugang
# ---------------------------------------------------------------------------
#
# Plesk legt den Login und das Klartext-Passwort seines eigenen MySQL/MariaDB-
# Administrator-Accounts (Login "admin") in /etc/psa/.psa.shadow ab - damit
# verwaltet Plesk selbst alle Kunden-Datenbanken (z.B. für phpMyAdmin-
# Single-Sign-on, Backups, Migrationen). Da diese Tools ohnehin per SSH als
# root laufen (also die Datei bereits lesen könnten), wird dieser Account
# genutzt, um automatisch - ohne eigene, zusätzlich zu konfigurierende
# Datenbank-Zugangsdaten - auf beliebige Datenbanken auf dem Server zugreifen
# zu können. Das ist technisch ein sehr mächtiger Zugang (faktisch DB-root);
# die Beschränkung auf read-only passiert daher ausschliesslich in diesem
# Code (siehe _validate_select_sql) und nicht durch MySQL-Rechte selbst.

_DB_SHADOW_PATH = "/etc/psa/.psa.shadow"
_DB_ADMIN_USER = "admin"

# Schlüsselwörter, die in einer db_query/db_search-Query verboten sind - auch
# wenn das Statement mit SELECT/SHOW/EXPLAIN/DESCRIBE beginnt. Als ganzes Wort
# geprüft (Regex \b...\b), damit z.B. eine Spalte "start_date" nicht wegen
# "start" fälschlich blockiert wird (bekannte Einschränkung: eine Spalte, die
# GENAU "start" heisst, würde blockiert - siehe README).
_SQL_FORBIDDEN_WORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "rename", "replace", "call", "exec", "execute",
    "lock", "unlock", "commit", "rollback", "set", "load_file", "outfile",
    "dumpfile", "sleep", "benchmark", "start",
}
_SQL_FORBIDDEN_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _SQL_FORBIDDEN_WORDS) + r")\b", re.IGNORECASE
)

_DB_SYSTEM_SCHEMAS = {
    "information_schema", "mysql", "performance_schema", "sys",
    "psa", "phpmyadmin", "roundcube", "horde",
}


def _db_admin_password() -> str:
    """Liest das Plesk-Admin-DB-Passwort aus /etc/psa/.psa.shadow (root-only,
    lesbar da wir ohnehin als root per SSH verbunden sind)."""
    out = ssh_run(f"cat {_DB_SHADOW_PATH} 2>&1")
    pw = out.strip()
    if not pw or "No such file" in pw or "Permission denied" in pw or "\n" in pw:
        raise RuntimeError(
            f"Konnte das Plesk-DB-Admin-Passwort nicht aus {_DB_SHADOW_PATH} lesen "
            f"(Ausgabe: {pw!r}). Ist dies ein Plesk-Server mit lokaler MySQL/"
            "MariaDB-Installation und root-SSH-Zugang?"
        )
    return pw


def _quote_ident(name: str) -> str:
    """MySQL-Identifier-Escaping (Tabellen-/Spaltennamen) - verdoppelt
    Backticks im Namen und umschliesst ihn mit Backticks."""
    return "`" + name.replace("`", "``") + "`"


def _sql_quote(value: str) -> str:
    """Escaped einen String-Wert für die Verwendung als SQL-Literal (einfache
    Anführungszeichen)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _validate_select_sql(sql: str) -> str:
    """Stellt sicher, dass sql ein einzelnes, reines Lese-Statement ist:
    genau ein Statement (kein ';' zum Verketten, ein optionales
    Trailing-';' ist erlaubt), beginnt mit SELECT/SHOW/EXPLAIN/DESCRIBE/DESC,
    und enthält kein Wort aus _SQL_FORBIDDEN_WORDS (insert/update/delete/...,
    load_file, into outfile/dumpfile, sleep/benchmark als DoS-Schutz, etc.)."""
    s = sql.strip()
    if not s:
        raise ValueError("Leere Query.")
    body = s[:-1].strip() if s.endswith(";") else s
    if ";" in body:
        raise ValueError(
            "Mehrere Statements ('; ...') sind nicht erlaubt - nur eine "
            "einzelne Lese-Query pro Aufruf."
        )
    if not body:
        raise ValueError("Leere Query.")
    first_word = body.split(None, 1)[0].lower()
    if first_word not in ("select", "show", "explain", "describe", "desc"):
        raise ValueError(
            f"Nur SELECT/SHOW/EXPLAIN/DESCRIBE-Queries sind erlaubt, nicht "
            f"'{first_word}'. Dieses Tool ist strikt read-only."
        )
    match = _SQL_FORBIDDEN_WORD_RE.search(body)
    if match:
        raise ValueError(
            f"Query enthält verbotenes Schlüsselwort '{match.group(1)}'. "
            "Dieses Tool ist strikt read-only (siehe README für die volle Blacklist)."
        )
    return body


def _validate_db_name(database: str) -> str:
    db = database.strip()
    if not db or any(c in db for c in " ;|&`$(){}<>\"'\n\t"):
        raise ValueError(f"Ungültiger Datenbankname: {database!r}")
    return db


def _mysql_exec(
    sql: str,
    database: str = "",
    timeout: int | None = None,
    skip_column_names: bool = False,
) -> str:
    """Führt sql via `mysql`-CLI als Plesk-Admin-User aus. Passwort wird über
    die Umgebungsvariable MYSQL_PWD übergeben (nicht als -p-Flag), damit es
    nicht in `ps aux` für andere lokale User auf dem Server sichtbar ist."""
    pw = _db_admin_password()
    db_part = f" {shlex.quote(database)}" if database else ""
    flags = "--connect-timeout=10 --batch --raw"
    if skip_column_names:
        flags += " -N"
    cmd = (
        f"MYSQL_PWD={shlex.quote(pw)} mysql -u{_DB_ADMIN_USER} {flags}"
        f"{db_part} -e {shlex.quote(sql)}"
    )
    return ssh_run(cmd, timeout=timeout)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def domain_info(domain: str) -> str:
    """Zeigt allgemeine Plesk-Domain-Infos: Status, Disk-Space (Limit/Nutzung),
    Traffic, Hosting-Typ, IP, SSL-Zertifikat, Subscription/Service-Plan.
    Entspricht `plesk bin domain -i <domain>`.
    """
    d = _domain_arg(domain)
    return ssh_run(f"plesk bin domain -i {shlex.quote(d)}")


@mcp.tool()
def dns_records(domain: str) -> str:
    """Zeigt die DNS-Resource-Records der Domain-Zone.
    Entspricht `plesk bin dns --info <domain>`.
    """
    d = _domain_arg(domain)
    return ssh_run(f"plesk bin dns --info {shlex.quote(d)}")


def _dns_build_cmd(
    action: str,
    domain: str,
    record_type: str,
    subdomain: str,
    value: str,
    priority: str,
    srv_service: str,
    srv_protocol: str,
    srv_port: str,
    srv_weight: str,
) -> str:
    """Baut das `plesk bin dns --add/--del`-Kommando für einen Record.
    Gemeinsame Logik für dns_add_record/dns_delete_record - Plesk erwartet
    beim Löschen exakt dieselben Parameter wie beim Anlegen (zur eindeutigen
    Identifikation des Records), daher identischer Aufbau für beide Aktionen.
    """
    d = _domain_arg(domain)
    rt = record_type.strip().lower()
    sub = subdomain.strip()
    if sub == "@":
        sub = ""
    val = value.strip()

    flag = "--add" if action == "add" else "--del"
    cmd = f"plesk bin dns {flag} {shlex.quote(d)}"

    if rt == "a":
        if not val:
            raise ValueError("value (IP-Adresse) ist für A-Records erforderlich.")
        cmd += f" -a {shlex.quote(sub)} -ip {shlex.quote(val)}"
    elif rt == "aaaa":
        if not val:
            raise ValueError("value (IPv6-Adresse) ist für AAAA-Records erforderlich.")
        cmd += f" -aaaa {shlex.quote(sub)} -ip {shlex.quote(val)}"
    elif rt == "cname":
        if not val:
            raise ValueError("value (Zieldomain) ist für CNAME-Records erforderlich.")
        cmd += f" -cname {shlex.quote(sub)} -canonical {shlex.quote(val)}"
    elif rt == "mx":
        if not val:
            raise ValueError("value (Mailserver) ist für MX-Records erforderlich.")
        if not priority.strip():
            raise ValueError("priority ist für MX-Records erforderlich.")
        cmd += (
            f" -mx {shlex.quote(sub)} -mailexchanger {shlex.quote(val)}"
            f" -priority {shlex.quote(priority.strip())}"
        )
    elif rt == "ns":
        if not val:
            raise ValueError("value (Nameserver) ist für NS-Records erforderlich.")
        cmd += f" -ns {shlex.quote(sub)} -nameserver {shlex.quote(val)}"
    elif rt == "txt":
        if not val:
            raise ValueError("value (Text-Inhalt) ist für TXT-Records erforderlich.")
        cmd += f" -txt {shlex.quote(val)} -domain {shlex.quote(sub)}"
    elif rt == "srv":
        missing = [
            name
            for name, v in (
                ("value (Ziel-Host)", val),
                ("priority", priority.strip()),
                ("srv_service", srv_service.strip()),
                ("srv_protocol", srv_protocol.strip()),
                ("srv_port", srv_port.strip()),
                ("srv_weight", srv_weight.strip()),
            )
            if not v
        ]
        if missing:
            raise ValueError(
                "Für SRV-Records sind alle folgenden Parameter erforderlich, "
                f"es fehlen: {', '.join(missing)}."
            )
        cmd += (
            f" -srv {shlex.quote(sub)} -srv-service {shlex.quote(srv_service.strip())}"
            f" -srv-target-host {shlex.quote(val)}"
            f" -srv-protocol {shlex.quote(srv_protocol.strip())}"
            f" -srv-port {shlex.quote(srv_port.strip())}"
            f" -srv-priority {shlex.quote(priority.strip())}"
            f" -srv-weight {shlex.quote(srv_weight.strip())}"
        )
    else:
        raise ValueError(
            f"Unbekannter record_type {record_type!r}. "
            "Unterstützt: a, aaaa, cname, mx, ns, txt, srv."
        )
    return cmd


@mcp.tool()
def dns_add_record(
    domain: str,
    record_type: str,
    subdomain: str = "",
    value: str = "",
    priority: str = "",
    srv_service: str = "",
    srv_protocol: str = "",
    srv_port: str = "",
    srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Fügt einen DNS-Resource-Record zur Zone einer Domain hinzu.
    Entspricht `plesk bin dns --add <domain> -<typ> ...`.

    ACHTUNG - schreibt auf einem Produktivserver (ändert live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung), analog zu write_vhost_file.
    Existiert exakt derselbe Record bereits, meldet Plesk selbst einen
    Fehler zurück (kein stillschweigendes Duplizieren/Überschreiben) - zum
    Ändern eines bestehenden Records daher erst dns_delete_record, dann
    dns_add_record mit dem neuen Wert.

    record_type (Gross-/Kleinschreibung egal): a | aaaa | cname | mx | ns | txt | srv

    subdomain: Hostname-Teil relativ zur Zone, z.B. "www" oder "mail3".
               Leer ("") oder "@" steht für die Zone/Domain-Root selbst.
    value: Zielwert je nach Typ - a/aaaa: IP-Adresse; cname: kanonischer
           Name; mx: Mailserver; ns: Nameserver; txt: Text-Inhalt;
           srv: Ziel-Host (srv-target-host).
    priority: nur für mx (0-50) und srv (srv-priority) erforderlich.
    srv_service/srv_protocol/srv_port/srv_weight: nur für record_type="srv"
        erforderlich, zusätzlich zu subdomain, value und priority - z.B. für
        einen SIP-SRV-Record: subdomain="", value="sipserver.example.com",
        srv_service="sip", srv_protocol="tcp", srv_port="5060",
        srv_weight="5", priority="0".
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    cmd = _dns_build_cmd(
        "add", domain, record_type, subdomain, value, priority,
        srv_service, srv_protocol, srv_port, srv_weight,
    )
    return ssh_run(cmd)


@mcp.tool()
def dns_delete_record(
    domain: str,
    record_type: str,
    subdomain: str = "",
    value: str = "",
    priority: str = "",
    srv_service: str = "",
    srv_protocol: str = "",
    srv_port: str = "",
    srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Entfernt einen DNS-Resource-Record aus der Zone einer Domain.
    Entspricht `plesk bin dns --del <domain> -<typ> ...` - Plesk erfordert
    dieselben Parameter wie beim Anlegen (dns_add_record), um den zu
    löschenden Record eindeutig zu identifizieren; vorher am besten
    dns_records aufrufen, um die exakten aktuellen Werte zu bestätigen.

    ACHTUNG - schreibt auf einem Produktivserver (entfernt live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung).

    Parameter identisch zu dns_add_record (record_type, subdomain, value,
    priority, srv_*) - siehe dort für die Bedeutung je Record-Typ.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    cmd = _dns_build_cmd(
        "del", domain, record_type, subdomain, value, priority,
        srv_service, srv_protocol, srv_port, srv_weight,
    )
    return ssh_run(cmd)


@mcp.tool()
def dns_update_record(
    domain: str,
    record_type: str,
    old_subdomain: str,
    old_value: str,
    new_subdomain: str = "",
    new_value: str = "",
    old_priority: str = "",
    new_priority: str = "",
    old_srv_service: str = "",
    old_srv_protocol: str = "",
    old_srv_port: str = "",
    old_srv_weight: str = "",
    new_srv_service: str = "",
    new_srv_protocol: str = "",
    new_srv_port: str = "",
    new_srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Ändert einen bestehenden DNS-Record. Plesk kennt kein natives
    'Update' für einzelne Records (nur `--set` für die komplette Zone auf
    einmal) - dieses Tool bildet die Änderung als zusammenhängenden
    Delete+Add ab (`plesk bin dns --del` mit den old_*-Werten, direkt
    gefolgt von `--add` mit den new_*-Werten), damit dafür nur ein
    confirm=true-Aufruf nötig ist statt zwei einzelne (dns_delete_record +
    dns_add_record).

    old_subdomain/old_value/old_priority/old_srv_*: identifizieren den zu
        ändernden Record - müssen exakt den aktuellen Werten entsprechen
        (vorher am besten dns_records aufrufen, um sie zu bestätigen).
    new_subdomain/new_value/new_priority/new_srv_*: die neuen Werte. Wird
        einer davon leer gelassen, bleibt er wie im alten Record - z.B. nur
        die IP eines A-Records ändern: record_type="a", old_subdomain="www",
        old_value="<alte IP>", new_value="<neue IP>" (new_subdomain kann
        weggelassen werden, bleibt dann "www").
    record_type (Gross-/Kleinschreibung egal): a | aaaa | cname | mx | ns | txt | srv
        - gilt für den Record vor UND nach der Änderung; um einen Record in
        einen anderen Typ umzuwandeln, stattdessen dns_delete_record +
        dns_add_record einzeln verwenden.

    ACHTUNG - schreibt auf einem Produktivserver (ändert live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung). Falls das nachträgliche --add
    fehlschlägt (z.B. Tippfehler im neuen Wert), ist der alte Record
    bereits gelöscht - die vollständige Fehlermeldung wird zurückgegeben,
    damit der alte Record bei Bedarf manuell per dns_add_record wieder
    angelegt werden kann. Scheitert bereits das --del (z.B. weil
    old_subdomain/old_value nicht exakt zum aktuellen Record passen), wird
    abgebrochen, BEVOR irgendetwas geändert wird.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )

    eff_new_subdomain = new_subdomain.strip() or old_subdomain
    eff_new_value = new_value.strip() or old_value
    eff_new_priority = new_priority.strip() or old_priority
    eff_new_srv_service = new_srv_service.strip() or old_srv_service
    eff_new_srv_protocol = new_srv_protocol.strip() or old_srv_protocol
    eff_new_srv_port = new_srv_port.strip() or old_srv_port
    eff_new_srv_weight = new_srv_weight.strip() or old_srv_weight

    del_cmd = _dns_build_cmd(
        "del", domain, record_type, old_subdomain, old_value, old_priority,
        old_srv_service, old_srv_protocol, old_srv_port, old_srv_weight,
    )
    del_result = ssh_run(del_cmd)
    if "[exit code:" in del_result:
        return (
            "Abgebrochen - alter Record konnte nicht gelöscht werden, es "
            "wurde nichts geändert. Prüfe old_subdomain/old_value/"
            "old_priority (am besten zuerst dns_records aufrufen).\n\n"
            f"Ausgabe von --del:\n{del_result}"
        )

    add_cmd = _dns_build_cmd(
        "add", domain, record_type, eff_new_subdomain, eff_new_value, eff_new_priority,
        eff_new_srv_service, eff_new_srv_protocol, eff_new_srv_port, eff_new_srv_weight,
    )
    add_result = ssh_run(add_cmd)

    return (
        f"Alter Record gelöscht:\n{del_result}\n\n"
        f"Neuer Record angelegt:\n{add_result}"
    )


@mcp.tool()
def backup_list(domain: str = "") -> str:
    """Listet vorhandene lokale Backup-Dateien auf (Server- und
    Domain-/Subscription-Backups unter /var/lib/psa/dumps, dem Plesk-
    Standardpfad für lokale Backups). Ohne domain wird das gesamte
    Backup-Verzeichnis durchsucht, mit domain nur Treffer für diese Domain.
    Zeigt Dateiname, Grösse und Änderungsdatum - keine Backups werden erstellt
    oder gelöscht.
    """
    base = "/var/lib/psa/dumps"
    # ".discovered"/".run" sind interne Plesk-Metadaten-Verzeichnisse (ein
    # Eintrag pro Backup-Lauf, keine eigentlichen Backup-Dateien) - werden
    # ausgeblendet (-prune), sonst ist die Ausgabe bei vielen Domains
    # unlesbar lang.
    prune = r"\( -name '.discovered' -o -name '.run' \) -prune -o"
    if domain:
        d = _domain_arg(domain)
        cmd = f"find {base} {prune} -iname '*{d}*' -exec ls -lhd {{}} \\; 2>&1 | head -n 200"
    else:
        cmd = f"find {base} {prune} -maxdepth 4 -type d -print 2>&1 | head -n 200"
    out = ssh_run(cmd)
    if not out or out == "(keine Ausgabe)":
        return f"Keine Backups gefunden (Pfad {base})."
    return out


@mcp.tool()
def wp_toolkit_list(with_plugins: bool = False) -> str:
    """Listet alle vom WP Toolkit verwalteten WordPress-Installationen auf.
    Entspricht `plesk ext wp-toolkit --list` (optional mit -plugins für
    Plugin-Infos je Installation).
    """
    cmd = "plesk ext wp-toolkit --list"
    if with_plugins:
        cmd += " -plugins"
    return ssh_run(cmd)


@mcp.tool()
def wp_toolkit_info(instance_id: str) -> str:
    """Zeigt Detail-Infos zu einer einzelnen WordPress-Installation (Version,
    Pfad, Updates, Plugins/Themes). instance_id kommt aus wp_toolkit_list.
    Entspricht `plesk ext wp-toolkit --info -instance-id <id>`.
    """
    iid = instance_id.strip()
    if not iid or not iid.isdigit():
        raise ValueError(f"Ungültige instance_id: {instance_id!r} (muss numerisch sein).")
    return ssh_run(f"plesk ext wp-toolkit --info -instance-id {iid}")


@mcp.tool()
def plesk_api_get(path: str, query: str = "") -> str:
    """Read-only GET-Request gegen die Plesk-REST-API für strukturierte
    JSON-Daten (Domains, Subscriptions, Kunden etc.), als Ergänzung zu den
    SSH/CLI-Tools. Beispiel: path="domains" für GET /api/v2/domains.
    query: optionale Querystring-Parameter im Format "key1=val1&key2=val2".
    Erfordert PLESK_API_KEY (siehe README). Nur GET - keine schreibenden
    Requests möglich.
    """
    params: dict[str, str] = {}
    for pair in query.split("&"):
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        params[k.strip()] = v.strip()
    return plesk_api_get_raw(path, params)


@mcp.tool()
def lve_stats(domain: str) -> str:
    """Sucht die aktuellen LVE-Ressourcenwerte (CPU/IO-Limits und -Faults)
    für die angegebene Domain, z.B. um kurzzeitige Nichterreichbarkeit durch
    Ressourcenlimits (CloudLinux LVE) zu diagnostizieren.
    """
    d = _domain_arg(domain)
    out = ssh_run(f"lveinfo --limit 500 2>&1 | grep -i -A3 -B3 {shlex.quote(d)}")
    if not out or out == "(keine Ausgabe)":
        # Fallback über lveps, falls lveinfo den Namen nicht direkt matcht
        out = ssh_run(f"lveps 2>&1 | grep -i {shlex.quote(d)}")
    return out or f"Keine LVE-Daten für '{d}' gefunden."


@mcp.tool()
def fpm_service_status(domain: str) -> str:
    """Findet den PHP-FPM-systemd-Service der Domain (Plesk erstellt pro Domain
    einen eigenen Pool-Service, z.B. plesk-php82-fpm_domain.ch_216.service)
    und zeigt dessen Status inkl. letzter Log-Zeilen (Start/Stop/Fehler).
    """
    d = _domain_arg(domain)
    units = ssh_run(f"systemctl list-units --type=service --all 2>&1 | grep -i {shlex.quote(d)}")
    if not units or units == "(keine Ausgabe)":
        return f"Kein FPM-Service für '{d}' gefunden."
    first_unit = units.strip().splitlines()[0].split()[0]
    status = ssh_run(f"systemctl status {shlex.quote(first_unit)} --no-pager -l")
    return f"Gefundene Units:\n{units}\n\nStatus von {first_unit}:\n{status}"


@mcp.tool()
def fpm_journal(domain: str, since: str = "-2h", until: str = "") -> str:
    """Zeigt systemd-Journal-Einträge (Start/Stop/Crash/Fehler) des
    FPM-Service der Domain im angegebenen Zeitfenster.
    since/until im journalctl-Format, z.B. "2026-09-17 21:00" oder "-2h".
    """
    d = _domain_arg(domain)
    units = ssh_run(f"systemctl list-units --type=service --all 2>&1 | grep -i {shlex.quote(d)}")
    if not units or units == "(keine Ausgabe)":
        return f"Kein FPM-Service für '{d}' gefunden."
    first_unit = units.strip().splitlines()[0].split()[0]

    cmd = f"journalctl -u {shlex.quote(first_unit)} --no-pager --since {shlex.quote(since)}"
    if until:
        cmd += f" --until {shlex.quote(until)}"
    return ssh_run(cmd)


@mcp.tool()
def search_log(
    domain: str,
    log_type: str = "proxy_error_log",
    pattern: str = "",
    lines: int = 200,
    include_rotated: bool = False,
) -> str:
    """Durchsucht ein Vhost-Log der Domain nach einem optionalen Muster
    (regex, an grep -E übergeben) und gibt die letzten `lines` Treffer zurück.
    log_type: proxy_error_log | error_log | access_log
    Pfad: /var/www/vhosts/<domain>/logs/<log_type>
    include_rotated: zusätzlich die rotierten, gzip-komprimierten Logs
    (<log_type>-YYYYMMDD.gz im selben Verzeichnis) mit durchsuchen - nötig
    für Vorfälle, die länger als die aktuelle Logrotation zurückliegen.
    """
    d = _domain_arg(domain)
    allowed_logs = {"proxy_error_log", "error_log", "access_log"}
    if log_type not in allowed_logs:
        raise ValueError(f"log_type muss einer von {allowed_logs} sein.")

    log_dir = f"/var/www/vhosts/{d}/logs"
    path = f"{log_dir}/{log_type}"

    if include_rotated:
        if not pattern:
            raise ValueError(
                "include_rotated benötigt ein pattern (sonst zu viele Treffer "
                "über mehrere komprimierte Dateien hinweg)."
            )
        cmd = (
            f"zgrep -E {shlex.quote(pattern)} "
            f"{shlex.quote(log_dir)}/{log_type}*.gz 2>&1 | tail -n {int(lines)}"
        )
        return ssh_run(cmd)

    if pattern:
        cmd = f"grep -E {shlex.quote(pattern)} {shlex.quote(path)} 2>&1 | tail -n {int(lines)}"
    else:
        cmd = f"tail -n {int(lines)} {shlex.quote(path)} 2>&1"
    return ssh_run(cmd)


@mcp.tool()
def search_main_nginx_log(
    pattern: str,
    log: str = "access",
    lines: int = 200,
    include_rotated: bool = False,
) -> str:
    """Durchsucht das serverweite nginx-Log (nicht pro-Vhost) unter
    /var/log/nginx/ - dort landen u.a. 408/523-Fehler, die CloudLinux
    Web-Monitoring-Reports zeigen, die einzelnen Vhost-Logs aber oft nicht
    erfassen (Fehler vor dem Routing zum Backend).
    log: "access" oder "error"
    include_rotated: auch die rotierten .gz-Dateien der letzten Tage durchsuchen.
    """
    if log not in {"access", "error"}:
        raise ValueError("log muss 'access' oder 'error' sein.")

    if include_rotated:
        cmd = (
            f"zgrep -E {shlex.quote(pattern)} "
            f"/var/log/nginx/{log}.log* 2>&1 | tail -n {int(lines)}"
        )
    else:
        cmd = (
            f"grep -E {shlex.quote(pattern)} "
            f"/var/log/nginx/{log}.log 2>&1 | tail -n {int(lines)}"
        )
    return ssh_run(cmd)


@mcp.tool()
def check_oom_kills(since: str = "-24h", until: str = "", domain: str = "") -> str:
    """Prüft im Kernel-Journal auf Out-Of-Memory-Kills im angegebenen
    Zeitfenster - klassischer Grund für "Seite bricht immer wieder weg".
    Optional nach Domain/Prozessname filtern.
    """
    cmd = f"journalctl -k --no-pager --since {shlex.quote(since)}"
    if until:
        cmd += f" --until {shlex.quote(until)}"
    cmd += " | grep -iE 'oom|killed process'"
    if domain:
        d = _domain_arg(domain)
        cmd += f" | grep -i {shlex.quote(d)}"
    out = ssh_run(cmd)
    if not out or out == "(keine Ausgabe)":
        return "Keine OOM-Kills im angegebenen Zeitfenster gefunden."
    return out


@mcp.tool()
def disk_usage(domain: str) -> str:
    """Zeigt die Disk-Nutzung des Vhost-Verzeichnisses der Domain
    (httpdocs, logs, etc. einzeln aufgeschlüsselt)."""
    d = _domain_arg(domain)
    return ssh_run(f"du -sh /var/www/vhosts/{shlex.quote(d)}/* 2>&1")


@mcp.tool()
def read_vhost_file(domain: str, path: str, encoding: str = "text") -> str:
    """Liest eine Datei aus dem Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>, path relativ, z.B. "httpdocs/index.php").
    encoding: "text" (UTF-8, Standard) oder "base64" (für Binärdateien wie
    Bilder). Rein lesend - kein confirm nötig.
    """
    if encoding not in {"text", "base64"}:
        raise ValueError("encoding muss 'text' oder 'base64' sein.")
    full = _vhost_path(domain, path)

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(full, "rb") as f:
                data = f.read()
        finally:
            sftp.close()
    finally:
        client.close()

    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            "Datei ist nicht UTF-8-dekodierbar (vermutlich Binärdatei) - "
            "mit encoding='base64' erneut versuchen."
        )


@mcp.tool()
def write_vhost_file(
    domain: str,
    path: str,
    content: str,
    confirm: bool = False,
    encoding: str = "text",
) -> str:
    """Schreibt/überschreibt eine Datei im Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>) - legt auch fehlende Zwischenordner an.
    encoding: "text" (content ist Klartext, UTF-8 - Standard) oder "base64"
    (content ist base64-kodiert, für Binärdateien wie Bilder/ZIPs).

    ACHTUNG - schreibt auf einem Produktivserver: erfordert confirm=true als
    bewusste Bestätigung PRO Aufruf (keine globale Freischaltung). Existiert
    die Zieldatei bereits, wird vorher automatisch ein Backup als
    "<path>.bak-<YYYYMMDDHHMMSS>" im selben Verzeichnis angelegt (siehe
    delete_vhost_backup zum späteren Aufräumen, sobald die Änderung verifiziert ist).
    Ist der Zielpfad bereits ein Symlink, wird aus Sicherheitsgründen
    abgebrochen (kein Überschreiben durch Symlinks hindurch).
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    if encoding not in {"text", "base64"}:
        raise ValueError("encoding muss 'text' oder 'base64' sein.")

    full = _vhost_path(domain, path)

    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception as e:
            raise ValueError(f"content ist kein gültiges Base64: {e}")
    else:
        data = content.encode("utf-8")

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            exists = False
            backup_note = ""
            try:
                lst = sftp.lstat(full)
                if stat.S_ISLNK(lst.st_mode):
                    raise ValueError(
                        f"'{full}' ist ein Symlink - wird aus Sicherheitsgründen "
                        "nicht überschrieben."
                    )
                exists = True
            except FileNotFoundError:
                exists = False

            if exists:
                with sftp.open(full, "rb") as f:
                    old_data = f.read()
                timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                backup_path = f"{full}.bak-{timestamp}"
                with sftp.open(backup_path, "wb") as f:
                    f.write(old_data)
                backup_note = f" Backup der alten Version: {backup_path}"

            _sftp_makedirs(sftp, full.rsplit("/", 1)[0])
            with sftp.open(full, "wb") as f:
                f.write(data)
        finally:
            sftp.close()
    finally:
        client.close()

    verb = "Überschrieben" if exists else "Neu erstellt"
    return f"{verb}: {full} ({len(data)} Bytes).{backup_note}"


@mcp.tool()
def delete_vhost_backup(domain: str, path: str, confirm: bool = False) -> str:
    """Löscht eine von write_vhost_file angelegte Backup-Datei
    (/var/www/vhosts/<domain>/<path>, path muss auf ".bak-<14-stellige
    Zeitstempel>" enden, z.B. "httpdocs/index.php.bak-20260919143012") -
    zum Aufräumen, nachdem eine Änderung verifiziert wurde. Löscht bewusst
    NUR Dateien mit diesem Namensmuster, keine sonstigen Vhost-Dateien.
    Erfordert confirm=true pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool löscht eine Datei auf "
            "einem Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    full = _vhost_path(domain, path)
    if not _BACKUP_SUFFIX_RE.search(full):
        raise ValueError(
            f"'{path}' sieht nicht wie eine von write_vhost_file angelegte "
            "Backup-Datei aus (erwartet: ...bak-JJJJMMTTHHMMSS). Aus "
            "Sicherheitsgründen löscht dieses Tool ausschliesslich solche Dateien."
        )

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            sftp.remove(full)
        finally:
            sftp.close()
    finally:
        client.close()

    return f"Backup gelöscht: {full}"


@mcp.tool()
def db_list(domain: str = "") -> str:
    """Listet MySQL/MariaDB-Datenbanken auf dem Server auf (SHOW DATABASES,
    authentifiziert über den Plesk-internen Admin-DB-Zugang aus
    /etc/psa/.psa.shadow - keine eigenen Datenbank-Zugangsdaten nötig).

    Mit domain werden nur Datenbanken gefiltert, deren Name die Domain
    (Punkte durch Unterstriche ersetzt) enthält - Plesk benennt Datenbanken
    nicht immer exakt nach der Domain, bei Bedarf ohne domain-Parameter alle
    auflisten und selbst zuordnen. Für jede gefundene (Nicht-System-)
    Datenbank werden zusätzlich deren Tabellen mit Zeilenanzahl und Grösse
    in MB angezeigt (aus information_schema.tables).
    """
    out = _mysql_exec("SHOW DATABASES", skip_column_names=True)
    all_dbs = [line.strip() for line in out.splitlines() if line.strip()]

    if domain:
        d = _domain_arg(domain)
        needle = d.replace(".", "_")
        matches = [db for db in all_dbs if needle in db or d in db]
        if not matches:
            return (
                f"Keine Datenbank mit '{domain}' im Namen gefunden.\n"
                f"Vorhandene Datenbanken: {', '.join(all_dbs)}"
            )
        detail_dbs = matches
    else:
        detail_dbs = [db for db in all_dbs if db not in _DB_SYSTEM_SCHEMAS]

    lines = [f"Datenbanken: {', '.join(all_dbs)}", ""]
    for db in detail_dbs:
        if db in _DB_SYSTEM_SCHEMAS:
            continue
        tables = _mysql_exec(
            "SELECT TABLE_NAME, TABLE_ROWS, "
            "ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) AS size_mb "
            f"FROM information_schema.tables WHERE table_schema = {_sql_quote(db)} "
            "ORDER BY TABLE_NAME"
        )
        lines.append(f"--- {db} ---")
        lines.append(tables or "(keine Tabellen)")
        lines.append("")
    return "\n".join(lines).strip()


@mcp.tool()
def db_query(database: str, sql: str) -> str:
    """Führt eine einzelne, reine Lese-Query gegen eine MySQL/MariaDB-
    Datenbank auf dem Server aus (SELECT/SHOW/EXPLAIN/DESCRIBE). Nutzt
    automatisch den Plesk-internen Admin-DB-Zugang (siehe db_list) - kein
    eigener Datenbank-Zugang pro Domain nötig.

    database: Datenbankname (siehe db_list für die vorhandenen Namen).
    sql: genau ein Statement, kein ';' zum Verketten mehrerer Statements.
    Schreibende oder dateisystemnahe Befehle (INSERT/UPDATE/DELETE/DROP/...,
    LOAD_FILE, INTO OUTFILE/DUMPFILE, SLEEP/BENCHMARK etc.) werden per
    Blacklist blockiert - siehe README für bekannte Einschränkungen dieser
    Prüfung (z.B. eine Spalte namens exakt "start"). Ausgabe auf 200 Zeilen
    begrenzt.
    """
    db = _validate_db_name(database)
    checked_sql = _validate_select_sql(sql)
    out = _mysql_exec(checked_sql, database=db, timeout=30)
    result_lines = out.splitlines()
    if len(result_lines) > 200:
        out = "\n".join(result_lines[:200]) + (
            f"\n... ({len(result_lines) - 200} weitere Zeilen abgeschnitten)"
        )
    return out or "(keine Ausgabe)"


@mcp.tool()
def db_search(
    database: str,
    term: str,
    max_tables: int = 30,
    limit_per_table: int = 5,
) -> str:
    """Durchsucht alle Text-Spalten (char/varchar/text/tinytext/mediumtext/
    longtext) aller Tabellen einer Datenbank nach term (einfacher
    LIKE '%term%'-Vergleich, kein Volltextindex nötig) - praktisch um z.B.
    eine E-Mail-Adresse oder Bestellnummer zu finden, ohne die
    Tabellenstruktur zu kennen. Scannt aus Performancegründen maximal
    max_tables Tabellen und zeigt maximal limit_per_table Treffer pro
    Tabelle - bei sehr grossen Datenbanken ggf. mehrfach mit gezielterem
    term oder db_query direkt verwenden. Nutzt den Plesk-internen
    Admin-DB-Zugang, strikt read-only.
    """
    db = _validate_db_name(database)
    term = term.strip()
    if not term:
        raise ValueError("term darf nicht leer sein.")
    if not (1 <= max_tables <= 100):
        raise ValueError("max_tables muss zwischen 1 und 100 liegen.")
    if not (1 <= limit_per_table <= 50):
        raise ValueError("limit_per_table muss zwischen 1 und 50 liegen.")

    cols_out = _mysql_exec(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
        f"WHERE table_schema = {_sql_quote(db)} AND DATA_TYPE IN "
        "('char','varchar','text','tinytext','mediumtext','longtext') "
        "ORDER BY TABLE_NAME",
        database=db,
        skip_column_names=True,
    )
    by_table: dict[str, list[str]] = {}
    for line in cols_out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        table, col = parts
        by_table.setdefault(table, []).append(col)

    if not by_table:
        return f"Keine Text-Spalten in '{db}' gefunden (oder Datenbank existiert nicht)."

    escaped_term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = _sql_quote(f"%{escaped_term}%")

    results = []
    scanned = 0
    for table, cols in by_table.items():
        if scanned >= max_tables:
            break
        scanned += 1
        where = " OR ".join(f"{_quote_ident(c)} LIKE {needle}" for c in cols)
        query = f"SELECT * FROM {_quote_ident(table)} WHERE {where} LIMIT {int(limit_per_table)}"
        try:
            out = _mysql_exec(query, database=db, timeout=15)
        except Exception as e:
            results.append(f"--- {table}: Fehler ({e}) ---")
            continue
        if out and out != "(keine Ausgabe)":
            results.append(f"--- {table} ---\n{out}")

    if not results:
        return f"Kein Treffer für '{term}' in {scanned} durchsuchten Tabellen von '{db}'."
    header = f"Treffer für '{term}' in '{db}' ({scanned} von {len(by_table)} Tabellen durchsucht):\n\n"
    return header + "\n\n".join(results)


@mcp.tool()
def server_load() -> str:
    """Zeigt allgemeine Serverlast: uptime/Load-Average, freier Speicher,
    Anzahl laufender Prozesse - für Checks, ob der ganze Server unter
    Last steht statt nur eine einzelne Domain."""
    return ssh_run("uptime && echo --- && free -h && echo --- && ps aux | wc -l")


@mcp.tool()
def run_diagnostic(command: str) -> str:
    """Führt einen read-only Diagnose-Befehl auf dem Server aus, für Fälle,
    die von den anderen Tools nicht abgedeckt sind. Nur eine Whitelist an
    Programmen ist erlaubt (plesk, lveinfo, lveps, systemctl, journalctl,
    grep, tail, head, cat, find, ls, du, df, dmesg, uptime, top, free, ps,
    hostname, wc, zcat, zgrep), destruktive Subcommands
    (restart/stop/kill/rm/delete/...) sind blockiert. Beispiel:
    "journalctl -k --since '2026-09-17 21:00'"
    """
    return ssh_run_whitelisted(command)


# ---------------------------------------------------------------------------
# HTTP-Transport (Cloud/Docker) mit Bearer-Auth
# ---------------------------------------------------------------------------


def _build_http_app():
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse

    # transport_security: DNS-Rebinding-Schutz deaktiviert, analog zu
    # bexio-mcp - sonst blockt das SDK jeden Request mit einem Host-Header,
    # der nicht "localhost"/eine IP ist (421 "Invalid Host header"), auch
    # wenn der Server bewusst über einen Reverse-Proxy unter einer echten
    # Domain erreichbar gemacht wird. Die eigentliche Absicherung übernimmt
    # ohnehin MCP_API_KEY/_BearerAuthMiddleware unten.
    # json_response=True: einfache application/json-Antworten statt SSE-
    # Stream - robuster hinter Reverse-Proxies (siehe bexio-mcp).
    starlette_app = mcp.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=_HTTP_HOST,
    )

    class _BearerAuthMiddleware:
        """Minimalistische ASGI-Middleware: prüft 'Authorization: Bearer <token>'."""

        def __init__(self, app, token: str):
            self.app = app
            self.token = token

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("latin-1")
            if auth_header != f"Bearer {self.token}":
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)

    secured_app = _BearerAuthMiddleware(starlette_app, _MCP_API_KEY)
    # CORS aussen um die Auth-Middleware: Browser-basierte MCP-Clients (z.B.
    # Claude.ai) rufen den Endpoint per Cross-Origin-JS-Fetch auf. Preflight-
    # OPTIONS-Requests (ohne Authorization-Header) werden von CORSMiddleware
    # direkt beantwortet, bevor sie die Bearer-Pruefung erreichen.
    return CORSMiddleware(
        secured_app,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )


async def _run_http_server() -> None:
    import uvicorn

    app = _build_http_app()
    config = uvicorn.Config(app, host=_HTTP_HOST, port=_HTTP_PORT, log_level="info")
    srv = uvicorn.Server(config)
    print(f"plesk-mcp HTTP server running on {_HTTP_HOST}:{_HTTP_PORT}", flush=True)
    await srv.serve()


def main() -> None:
    if _HTTP_MODE:
        import asyncio

        asyncio.run(_run_http_server())
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
