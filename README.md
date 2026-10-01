# Plesk MCP Connector

MCP-Server für Diagnose (und, mit expliziter Bestätigung, gezielte Datei-Edits) auf einem Plesk-Server. Läuft in zwei Betriebsarten:

- **stdio (lokal)** — klassischer lokaler MCP-Server via `uv`/Claude Desktop.
- **HTTP (Cloud)** — als Docker-Container mit Streamable-HTTP-Transport (z.B. via Portainer), für den Zugriff aus Cloud-Sessions ohne laufenden lokalen Rechner.

Drei Datenquellen: SSH/SFTP (für alles, was nur auf Betriebssystem-Ebene existiert - PHP-FPM-Status, systemd-Journal, OOM-Kills, Disk-Nutzung, Serverlast, Vhost-Dateizugriff - sowie für Plesk-CLI-Befehle), die Plesk-REST-API (für strukturierte Plesk-eigene Daten wie Domains/Subscriptions über das `plesk_api_get`-Tool) und MySQL/MariaDB direkt (`db_list`/`db_query`/`db_search`, read-only, über den Plesk-internen Admin-DB-Zugang - siehe Abschnitt "Sicherheit").

Alle Zugangsdaten (SSH, Plesk-API-Key, Auth-Token) werden ausschliesslich über Umgebungsvariablen konfiguriert - im Code stehen keine Secrets. Dieses Repo ist **öffentlich**: Code, README, Tests und Beispiele enthalten keine echten Hostnamen, IPs, Domains, Tokens oder Kundennamen - nur Platzhalter wie `example.com`, `plesk-mcp.example.com` oder `upload.example.com`. Echte Werte gehören ausschliesslich in die Umgebungsvariablen (z.B. in Portainer), nie ins Repo.

Fast alle Tools sind read-only: kein Neustart von Services, keine destruktiven Kommandos (Whitelist + Blacklist in `server.py`), und das `plesk_api_get`-Tool kann nur GET-Requests stellen. Ausnahme: `write_vhost_file`, `delete_vhost_backup`, `delete_vhost_log`, `dns_add_record`, `dns_delete_record` und `dns_update_record` sowie `imunify_ignore_add`/`imunify_ignore_remove`, `wp_option_update`/`wp_option_rollback` und die Datei-Tools `upload_begin`, `fetch_to_vhost`, `move_vhost_file`, `delete_vhost_file`, `restore_vhost_trash`, `empty_vhost_trash` dürfen schreiben - die ersten beiden Dateien innerhalb von `/var/www/vhosts/<domain>/` anlegen/überschreiben/löschen (Backups), `delete_vhost_log` Logdateien unter `/var/www/vhosts/<domain>/logs/` löschen (rotierte) bzw. auf 0 Bytes leeren (aktive), die drei `dns_*`-Tools DNS-Resource-Records der Domain-Zone anlegen/entfernen/ändern (`dns_update_record` kombiniert Löschen+Neuanlegen für einen bestehenden Record in einem Aufruf), die beiden Imunify-Tools ausschliesslich die Imunify360-Malware-Ignore-Liste, die beiden `wp_option_*`-Tools einzelne freigegebene WordPress-Optionen, die Datei-Tools Dateien unter `/var/www/vhosts/<domain>/` hochladen, verschieben, in den Papierkorb legen oder endgültig löschen (siehe "Datei-Upload" und "Dateiverwaltung"). Alle schreibenden Tools erfordern zwingend `confirm=true` pro Aufruf (keine globale Freischaltung), `write_vhost_file` legt vor dem Überschreiben automatisch ein Backup der alten Version an. Details siehe Abschnitt "Sicherheit" unten.

## Lokale Installation (stdio)

Kein `pip install` nötig - `uv` lädt die Dependencies automatisch beim ersten Start (siehe PEP-723-Metadaten am Dateianfang von `server.py`).

### Konfiguration in Claude Desktop (claude_desktop_config.json)

```json
{
  "mcpServers": {
    "plesk": {
      "command": "uv",
      "args": ["run", "C:\\Pfad\\zu\\plesk-mcp\\server.py"],
      "env": {
        "PLESK_SSH_HOST": "dein-plesk-server.example.com",
        "PLESK_SSH_PORT": "22",
        "PLESK_SSH_USER": "root",
        "PLESK_SSH_PASSWORD": "dein-passwort"
      }
    }
  }
}
```

Ohne gesetztes `MCP_TRANSPORT` (oder mit `MCP_TRANSPORT=stdio`) verhält sich der Server als klassischer lokaler MCP-Server - die Cloud/HTTP-Erweiterung ändert am lokalen Betrieb nichts.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `PLESK_SSH_HOST` | ja | – | Hostname/IP des Plesk-Servers |
| `PLESK_SSH_PORT` | nein | `22` | SSH-Port |
| `PLESK_SSH_USER` | nein | `root` | SSH-Benutzer |
| `PLESK_SSH_PASSWORD` | ja, ausser bei Key-Auth | – | SSH-Passwort |
| `PLESK_SSH_KEY_PATH` | nein | – | Alternative zu Passwort: Pfad zu einem privaten SSH-Key |
| `PLESK_SSH_TIMEOUT` | nein | `20` | SSH-Verbindungs-Timeout in Sekunden |
| `PLESK_SSH_HOST_KEY` | empfohlen | – | Host-Key des Plesk-Servers als `<typ> <base64>` (z.B. `ssh-ed25519 AAAAC3Nza...`), die Ausgabe von `ssh-keyscan` mit Hostname davor wird ebenfalls akzeptiert. Aktiviert die strikte Host-Key-Prüfung (MITM-Schutz) |
| `PLESK_SSH_KNOWN_HOSTS` | nein | – | Alternative zu `PLESK_SSH_HOST_KEY`: Pfad zu einer in den Container gemounteten known_hosts-Datei |
| `PLESK_API_HOST` | nein | `PLESK_SSH_HOST` | Host für die Plesk-REST-API, falls abweichend vom SSH-Host |
| `PLESK_API_PORT` | nein | `8443` | Port der Plesk-REST-API |
| `PLESK_API_KEY` | ja, für `plesk_api_get` | – | Secret Key, erzeugt via `plesk bin secret_key --create` |
| `PLESK_API_VERIFY_SSL` | nein | `true` | `false`, falls der Plesk-Server ein selbstsigniertes Zertifikat nutzt |
| `PLESK_API_TIMEOUT` | nein | `20` | Timeout in Sekunden für Requests gegen die Plesk-REST-API |
| `PAGESPEED_API_KEY` | ja, für `pagespeed_insights` | – | Google-API-Key für die PageSpeed Insights API v5 (ohne Key praktisch immer HTTP 429) |
| `PAGESPEED_TIMEOUT` | nein | `120` | Timeout in Sekunden pro PageSpeed-Analyse |
| `WP_OPTION_ALLOWLIST` | nein | – | Zusätzliche WordPress-Optionen (kommagetrennt), die `wp_option_update` ändern darf - zusätzlich zu `wp_rocket_settings` und `elementor_font_display` |
| `WP_CLI_PATH` | nein | `/usr/local/bin/wp-standalone` | Pfad zur WP-CLI-Phar auf dem Plesk-Server |
| `WP_CLI_TIMEOUT` | nein | `120` | Timeout in Sekunden pro WP-CLI-Aufruf |
| `PUBLIC_BASE_URL` | ja, für Uploads | – | Öffentliche Basis-URL des Containers hinter dem Reverse-Proxy, z.B. `https://plesk-mcp.example.com` (daraus wird die Upload-URL gebaut) |
| `UPLOAD_MAX_BYTES` | nein | `26214400` (25 MB) | Grössenlimit für Upload und Fetch; pro Upload per `max_bytes` weiter verkleinerbar |
| `UPLOAD_RATE_LIMIT_PER_MIN` | nein | `20` | Requests pro Minute und IP auf `/upload/` (inkl. Fehlversuche) |
| `UPLOAD_MAX_CONCURRENT` | nein | `2` | Maximal gleichzeitig laufende Uploads |
| `UPLOAD_MIN_FREE_MB` | nein | `256` | Freier Speicher, der auf dem Plesk-Server zusätzlich zum Upload-Limit frei bleiben muss |
| `UPLOAD_TIMEOUT` | nein | `300` | Maximale Dauer eines Uploads in Sekunden |
| `UPLOAD_TRUSTED_PROXIES` | nein | – | Kommagetrennte IPs des Reverse-Proxys (wie der Container sie sieht); nur dann wird `X-Forwarded-For` für das Rate-Limit ausgewertet |
| `FETCH_TIMEOUT` | nein | `120` | Timeout in Sekunden für `fetch_to_vhost` |
| `VHOST_TRASH_MAX_MB` | nein | `500` | Maximale Grösse pro Löschung in den Papierkorb |
| `VHOST_TRASH_AUTOCLEAN_DAYS` | nein | `0` (aus) | > 0: beim Containerstart Papierkorb-Einträge aller Domains löschen, die älter sind |
| `PLESK_HELPER_PYTHON` | nein | `/usr/libexec/platform-python` | Python (>= 3.3) auf dem Plesk-Server für den Datei-Helper |
| `PLESK_HELPER_TIMEOUT` | nein | `600` | Timeout in Sekunden pro Helper-Aufruf |
| `AUDIT_LOG_FILE` | nein | – | Optionaler Pfad im Container für das Audit-Log (zusätzlich zu stderr) |
| `MCP_TRANSPORT` | nein | `stdio` | `stdio` = lokal (Standard) / `http` = Cloud-Modus (Streamable HTTP) |
| `MCP_API_KEY` | ja, nur im HTTP-Modus | – | Statisches Bearer-Token zum Schutz des öffentlichen Endpoints. Ohne dieses Token startet der HTTP-Modus nicht (fail-safe) |
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse des HTTP-Servers *innerhalb* des Containers |
| `MCP_PORT` | nein | `8000` | Port des HTTP-Servers *innerhalb* des Containers |
| `MCP_BIND_ADDR` | nein | `127.0.0.1` | Bind-Adresse *auf dem Docker-Host* (docker-compose Port-Mapping) |
| `MCP_HOST_PORT` | nein | `8422` | Port *auf dem Docker-Host* (docker-compose Port-Mapping) |

## Image-Build (GitHub Actions → GitHub Container Registry)

Das Docker-Image wird bei jedem Push auf `main` automatisch per GitHub Actions gebaut und nach `ghcr.io/kdt-solutions/plesk-mcp:latest` veröffentlicht (`.github/workflows/docker-publish.yml`) - kein lokaler Build in Portainer nötig (das ist zuverlässiger, siehe Erfahrung mit dem zammad-mcp-Repo).

**GHCR-Package:** Die Sichtbarkeit des Packages wird auf GitHub unabhängig vom Repo eingestellt (Package-Settings → Danger Zone → Change visibility). Ist das Package privat, muss in Portainer unter **Registries → Add registry → Custom registry** ein Eintrag für `ghcr.io` mit deinem GitHub-Benutzernamen und einem Personal Access Token (Scope `read:packages`) hinterlegt werden.

## Cloud-Betrieb via Portainer (empfohlen)

1. In Portainer **Registries** den ghcr.io-Zugang hinterlegen (siehe oben)
2. **Stacks → Add stack**, **Build method: Repository** wählen, Repo-URL eintragen (Branch `main`), Compose-Pfad `docker-compose.yml`
3. Unter **Environment variables** die Werte aus der Tabelle oben setzen (landen NICHT im Repo)
4. **Deploy the stack**
5. Für ein Update später: **Pull and redeploy** klicken

## Netzwerk / Reverse-Proxy

Der Container bindet standardmässig nur auf `127.0.0.1:8422` auf dem Docker-Host - nicht direkt öffentlich erreichbar. Für den Zugriff aus einer Cloud-Claude-Session braucht es zusätzlich:

1. Eine eigene Subdomain (z.B. `plesk-mcp.deine-domain.ch`)
2. Einen Reverse-Proxy (Plesk/nginx) mit TLS-Zertifikat, der auf `127.0.0.1:8422` weiterleitet (Endpoint-Pfad: `/mcp`)
3. In der Cloud-Claude-Session wird der Server dann als Remote-MCP mit `https://plesk-mcp.deine-domain.ch/mcp` und dem `MCP_API_KEY` als Bearer-Token eingebunden - im Connector-Setup **"Keine Anmeldung"** wählen (kein OAuth) und einen Request-Header `Authorization: Bearer <MCP_API_KEY>` hinzufügen

## Sicherheit

- Der HTTP-Modus startet nur, wenn `MCP_API_KEY` gesetzt ist - es gibt also nie einen ungeschützten öffentlichen Endpoint.
- Jeder HTTP-Request muss den Header `Authorization: Bearer <MCP_API_KEY>` mitschicken, sonst Antwort `401 unauthorized`.
- `run_diagnostic` prüft Programmname gegen eine Whitelist und blockt bekannte destruktive Subcommands/Shell-Konstrukte zusätzlich per Blacklist-Substring-Check. Kein Freibrief für beliebige Shell-Befehle.
- `plesk_api_get` kann nur GET-Requests stellen - schreibender Zugriff auf die Plesk-REST-API ist über dieses Tool nicht möglich.
- `.env` ist in `.gitignore` und wird nie committet.
- Das Repo ist öffentlich - die Sicherheit beruht nicht auf Geheimhaltung des Codes, sondern auf `MCP_API_KEY`, den Einmal-Tokens für Uploads und den Prüfungen im Code. Secrets und echte Server-/Kundendaten nie committen.
- Für produktiven Einsatz: dedizierten SSH-User mit eingeschränkten Rechten (statt root) und/oder Key-Auth statt Passwort erwägen; für `plesk_api_get` den Secret Key optional per `-ip-address` auf die Docker-Host-IP einschränken (siehe `plesk bin secret_key --create`).
- `dns_add_record`/`dns_delete_record`/`dns_update_record` erfordern alle drei `confirm=true` als expliziten Parameter pro Aufruf (kein globaler Schreib-Schalter) und rufen ausschliesslich `plesk bin dns --add`/`--del <domain> -<typ> ...` auf - unterstützte Record-Typen: A, AAAA, CNAME, MX, NS, TXT, SRV (PTR/CAA/DS/HTTPS/TLSA sind nicht abgedeckt, dafür weiterhin `plesk`-CLI direkt auf dem Server nutzen). Legt Plesk bereits einen identischen Record vor, meldet der Server selbst einen Fehler zurück (kein stillschweigendes Überschreiben/Duplizieren). Plesk kennt kein natives Update für einzelne Records - `dns_update_record` bildet das als Delete (anhand der alten Werte) gefolgt von Add (mit den neuen Werten) ab; schlägt das Delete fehl, wird vorher abgebrochen und nichts geändert, schlägt das nachfolgende Add fehl, ist der alte Record bereits weg (Fehlermeldung wird vollständig zurückgegeben, damit er nötigenfalls per `dns_add_record` manuell wiederhergestellt werden kann). Alle drei Tools ändern live auflösbare DNS-Einträge auf einem Produktivserver.
- `write_vhost_file`/`delete_vhost_backup` sind strikt auf `/var/www/vhosts/<domain>/` beschränkt (Path-Traversal wie `../../etc/passwd` wird über `os.path.normpath` + Prefix-Check blockiert) und erfordern beide `confirm=true` als expliziten Parameter pro Aufruf - es gibt keinen globalen Schreib-Schalter, ein Aufruf ohne `confirm=true` schlägt immer fehl.
- `write_vhost_file` legt vor jedem Überschreiben automatisch ein Backup der alten Version als `<path>.bak-<YYYYMMDDHHMMSS>` an und bricht ab, falls der Zielpfad ein Symlink ist oder ein Elternordner ein Symlink ist (kein Schreiben durch Symlinks hindurch). Es nutzt dieselbe Schreiblogik wie Upload und Fetch (siehe "Datei-Upload"): neue Datei per Temp-Datei und atomarem `rename` - die Datei erhält dabei einen neuen Inode (Hardlinks auf die alte Datei bleiben beim alten Inhalt).
- `write_vhost_file` setzt Besitzer und Rechte so, dass der Kunde die Dateien per FTP/Dateimanager weiter bearbeiten kann: Bestehende Dateien und ihr Backup behalten Besitzer, Gruppe und Rechte der alten Datei. Neue Dateien (`0644`) und vom Tool neu angelegte Zwischenordner (`0755`) erhalten als Besitzer den User des nächsten bereits existierenden übergeordneten Ordners unterhalb von `/var/www/vhosts/<domain>/` - gehört dieser root, wird stattdessen der Besitzer von `/var/www/vhosts/<domain>/httpdocs` (Subscription-User) verwendet, root wird nie übernommen. Als Gruppe wird immer die primäre Gruppe dieses Users gesetzt (bei Plesk `psacln`); die Gruppe `psaserv` gehört nur auf `httpdocs` selbst (damit Webserver und FTP hineinkommen) und wird nicht übernommen. Unterhalb von `httpdocs` werden ausserdem bestehende Dateien und übergeordnete Ordner, die noch root gehören (z.B. von einer früheren Version angelegte `wp-content/mu-plugins`), beim Schreiben auf diesen Besitzer umgestellt; die Rechte bleiben dabei unverändert, eine Datei wird dafür neu angelegt und atomar über die alte umbenannt. Ausserhalb von `httpdocs` (z.B. `conf/`, `logs/`) bleiben root-Einträge unangetastet, da Plesk sie so vorsieht. Neue Dateien werden exklusiv angelegt (`O_EXCL`) und per Handle (`fchown`/`fchmod`) angepasst, Ordner per `chown -h` - Symlinks werden dabei nie verfolgt. Die Rückgabe nennt den gesetzten Besitzer, z.B. `Neu erstellt: /var/www/vhosts/example.com/httpdocs/test.php (120 Bytes). Besitzer: kunde:psacln, Rechte: 0644.`
- `write_vhost_file` ermittelt den Besitzer in dieser Reihenfolge: nächster existierender Ordner, `httpdocs`, `/var/www/vhosts/<domain>/` - gehören alle root, wird ohne zu schreiben abgebrochen. Ein root-eigenes `httpdocs` wird auf `<user>:psaserv` umgestellt (Plesk-Standard, damit der Webserver hineinkommt), Ordner darunter auf `<user>:psacln`. Eine root-eigene Datei ausserhalb von `httpdocs` (z.B. unter `conf/`) wird nicht überschrieben, da das Ergebnis root-eigen wäre bzw. sonst Plesk-Konfiguration an den Kunden übergehen würde.
- `wp_option_update`/`wp_option_rollback` rufen WP-CLI (`WP_CLI_PATH`) mit der PHP-Version der Domain als System-User der Subscription auf (`runuser`, leere Umgebung, nie root), mit `--path` auf die Installation - so laufen die WordPress-Hooks (`update_option`) und ein Objekt-Cache (Redis) wird korrekt aktualisiert. Teilschlüssel serialisierter Arrays über `wp option patch update` (bzw. `insert` für neue Schlüssel), einfache Werte über `wp option update`; Werte werden als JSON übergeben (`--format=json`), Typen bleiben erhalten. Nur Optionen aus der Allowlist (`wp_rocket_settings`, `elementor_font_display`, erweiterbar per `WP_OPTION_ALLOWLIST`), alles andere wird abgelehnt. System-User, Home und PHP-Handler der Domain kommen aus der Plesk-Datenbank über `plesk db` (authentifiziert sich selbst) - `wp-config.php`, DB-Passwörter, Salts und `/etc/psa/.psa.shadow` werden von diesen Tools weder gelesen noch ausgegeben oder geloggt. Vor jeder Änderung wird der alte Zustand als JSON unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-option-backups/<backup_id>.json` abgelegt (Home der Subscription, ausserhalb des Webroots, vom Subscription-User geschrieben, `0600`); ohne Backup wird nichts geändert. Nach dem Schreiben wird der Wert erneut gelesen und kontrolliert. `wp_option_rollback` stellt nur die damals geänderten Schlüssel wieder her (damals fehlende werden entfernt) und legt vorher selbst ein Backup an.
- `delete_vhost_backup` löscht ausschliesslich Dateien, deren Pfad auf `.bak-<14-stellige Zeitstempel>` endet - kein generisches Lösch-Tool für beliebige Vhost-Dateien.
- `delete_vhost_log` arbeitet ausschliesslich direkt im Verzeichnis `/var/www/vhosts/<domain>/logs/` (nur reiner Dateiname, keine Pfade/`..`), fasst nur reguläre Dateien an (keine Symlinks/Verzeichnisse) und erfordert `confirm=true`. Rotierte Logs (`.gz`, `.<Zahl>`, `-JJJJMMTT`) werden gelöscht, aktive Logs (z.B. `access_log`, `error_log`) nur auf 0 Bytes geleert, da der Webserver sie offen hält - ein echtes Löschen würde den Platz erst nach einem Reload freigeben. Mit `all_rotated=true` werden alle rotierten Logs der Domain auf einmal gelöscht.
- `imunify_ignore_add`/`imunify_ignore_remove` ändern ausschliesslich die Imunify360-Malware-Ignore-Liste (keine anderen Imunify-Einstellungen, keine Dateien) und erfordern `confirm=true`. Erlaubt sind nur absolute, bereits normalisierte Pfade unter `/var/www/vhosts/<domain>/...` (bzw. `/var/www/vhosts/system/<domain>/...`), mindestens eine Ebene unter dem Domain-Verzeichnis - nie ganze Domains. `..`, `.`, `//`, Leerzeichen, Shell-Sonderzeichen und Wildcards werden abgelehnt (die CLI unterstützt laut ihrem Schema nur absolute Pfade, keine Glob-Muster). Zusätzlich wird per SFTP geprüft, dass das Ziel kein Symlink ist und der kanonische Pfad (Symlinks in Elternverzeichnissen aufgelöst) exakt dem angegebenen entspricht. Die Argumente werden als Liste gebaut und einzeln gequotet an `imunify360-agent` übergeben. `imunify360-agent` ist bewusst **nicht** in `run_diagnostic` freigegeben, nur die fest verdrahteten Unterbefehle `malware malicious list` und `malware ignore list|add|delete` sind erreichbar. Da die CLI Einträge nur per ID entfernt (`malware ignore delete <id>`), ermittelt `imunify_ignore_remove` die ID vorher über `malware ignore list --search <path>` und löscht nur Einträge mit exakt diesem Pfad; die Datei wird danach von Imunify standardmässig neu gescannt (`skip_rescan=true` unterdrückt das).
- `db_list`/`db_query`/`db_search` nutzen automatisch den Plesk-internen MySQL/MariaDB-Admin-Zugang (Login `admin`, Passwort im Klartext in `/etc/psa/.psa.shadow` - damit verwaltet Plesk selbst alle Kunden-Datenbanken, z.B. für phpMyAdmin-SSO). Das ist technisch ein sehr mächtiger Zugang (faktisch DB-root) und wird genutzt, weil die Tools ohnehin per Root-SSH laufen und diese Datei sonst ebenso lesen könnten - es wird also kein zusätzlicher, separat abgesicherter Zugang geschaffen. Die Beschränkung auf read-only passiert ausschliesslich in `server.py` (`_validate_select_sql`), nicht durch MySQL-Rechte: erlaubt sind nur einzelne SELECT/SHOW/EXPLAIN/DESCRIBE-Statements ohne `;`-Verkettung, eine Blacklist blockt zusätzlich Schreib-Befehle (INSERT/UPDATE/DELETE/DROP/...) sowie dateisystemnahe Funktionen (`LOAD_FILE`, `INTO OUTFILE`/`DUMPFILE`) und potenzielle DoS-Funktionen (`SLEEP`, `BENCHMARK`). Das Passwort wird beim Aufruf über die Umgebungsvariable `MYSQL_PWD` an den `mysql`-Client übergeben statt als `-p`-Flag, damit es nicht in `ps aux` für andere lokale User auf dem Server sichtbar ist.

**Bekannte Einschränkung von `run_diagnostic`:** Die Wort-Blacklist (restart, stop, start, kill, rm, add, del, set, reset, on, off, update, ...) matcht auch dann, wenn das Wort z.B. in einem Suchmuster für `find`/`grep` vorkommt (z.B. `find / -iname "*kill*"` wird blockiert). Für den Anwendungsfall "Fehler/Support-Tickets diagnostizieren" ist das unkritisch, da die dedizierten Tools (`check_oom_kills`, `search_log`) die relevanten Fälle direkt abdecken. Die Woerter add/del/set/reset/on/off/update/start wurden nachtraeglich ergaenzt, nachdem aufgefallen ist, dass `plesk bin dns --add/--del/--set/...` darueber ausfuehrbar waren - ohne `confirm=true`-Absicherung und entgegen dem "read-only"-Anspruch des Tools. Fuer DNS-Aenderungen ausschliesslich `dns_add_record`/`dns_delete_record`/`dns_update_record` verwenden.

**Bekannte Einschränkung von `db_query`/`db_search`:** Die SQL-Wort-Blacklist prüft auf ganze Wörter, blockt also z.B. eine Query mit einer Spalte, die exakt `start` heisst (nicht aber `start_date` o.ä.) - analog zur Einschränkung von `run_diagnostic`. `db_search` ist ein einfacher `LIKE '%term%'`-Scan ohne Index-Nutzung und daher bei sehr grossen Tabellen langsam; `max_tables`/`limit_per_table` begrenzen den Aufwand pro Aufruf.

## Verfügbare Tools

| Tool | Beschreibung |
|------|--------------|
| `get_version` | Version des laufenden MCP-Servers (Redeploy-Kontrolle) |
| `domain_info` | Plesk-Domain-Infos (Status, Disk, Traffic, SSL, Subscription) |
| `dns_records` | DNS-Resource-Records der Domain-Zone (`plesk bin dns --info`) |
| `dns_add_record` | Legt einen DNS-Resource-Record an (A/AAAA/CNAME/MX/NS/TXT/SRV) - **erfordert `confirm=true`** |
| `dns_delete_record` | Entfernt einen DNS-Resource-Record (gleiche Parameter wie beim Anlegen) - **erfordert `confirm=true`** |
| `dns_update_record` | Ändert einen bestehenden DNS-Resource-Record (Delete alter Werte + Add neuer Werte in einem Aufruf) - **erfordert `confirm=true`** |
| `backup_list` | Vorhandene lokale Backup-Dateien (Server-/Domain-Backups unter `/var/lib/psa/dumps`) |
| `wp_toolkit_list` | Vom WP Toolkit verwaltete WordPress-Installationen auflisten |
| `wp_toolkit_info` | Detail-Infos zu einer WordPress-Installation (Version, Updates, Plugins/Themes) |
| `plesk_api_get` | Read-only GET gegen die Plesk-REST-API für strukturierte JSON-Daten (z.B. Domains, Subscriptions) |
| `lve_stats` | CloudLinux-LVE-Ressourcen-Faults (CPU/IO-Limits) |
| `fpm_service_status` | Findet den PHP-FPM-Pool-Service der Domain und zeigt dessen Status |
| `fpm_journal` | systemd-Journal des FPM-Service in einem Zeitfenster |
| `search_log` | Durchsucht proxy_error_log/error_log/access_log (optional inkl. rotierter .gz-Logs) |
| `search_main_nginx_log` | Durchsucht das serverweite nginx-Log unter /var/log/nginx/ (access/error, optional inkl. rotierter .gz-Logs) - erfasst auch 408/523-Fehler vor dem Routing zum Vhost |
| `check_oom_kills` | Kernel-OOM-Kills im Zeitfenster |
| `disk_usage` | Speichernutzung des Vhost-Verzeichnisses |
| `read_vhost_file` | Liest eine Datei aus dem Vhost-Verzeichnis (Text oder Base64) |
| `write_vhost_file` | Schreibt/überschreibt eine Datei im Vhost-Verzeichnis - **erfordert `confirm=true`**, legt vorher automatisch ein Backup an, setzt Besitzer/Rechte passend zur Subscription (nie root) |
| `upload_begin` | Startet einen Datei-Upload (Upload-URL + Einmal-Token) - **erfordert `confirm=true`**, siehe "Datei-Upload" |
| `upload_status` | Status eines Uploads (pending/uploading/completed/expired/failed) mit path, size, sha256 |
| `fetch_to_vhost` | Server lädt eine Datei selbst von einer https-URL (SSRF-geschützt) - **erfordert `confirm=true`** |
| `list_vhost_dir` | Listet ein Vhost-Verzeichnis (Typ, Grösse, Rechte, Besitzer, Datum), max. Tiefe 3 / 500 Einträge |
| `move_vhost_file` | Verschiebt/benennt atomar um, nur innerhalb desselben Docroots - **erfordert `confirm=true`** |
| `delete_vhost_file` | Löscht in den Papierkorb (oder `permanent=true`), Ordner zweistufig mit Dry-Run und `delete_token` - **erfordert `confirm=true`** |
| `restore_vhost_trash` | Stellt einen Papierkorb-Eintrag wieder her (überschreibt nie) - **erfordert `confirm=true`** |
| `empty_vhost_trash` | Leert Papierkorb-Einträge, die älter als N Tage sind - **erfordert `confirm=true`** |
| `delete_vhost_backup` | Löscht eine von `write_vhost_file` angelegte `.bak-*`-Datei - **erfordert `confirm=true`** |
| `wp_option_update` | Ändert eine WordPress-Option (Allowlist) per WP-CLI als Subscription-User - Teilschlüssel (`keys`) oder ganzer Wert (`value`); ohne `confirm=true` nur Vorschau, mit `confirm=true` vorher JSON-Backup, Rückgabe mit `backup_id` |
| `wp_option_rollback` | Spielt ein Backup von `wp_option_update` zurück (`backup_id`) - ohne `confirm=true` nur Vorschau |
| `delete_vhost_log` | Löscht rotierte Logs bzw. leert aktive Logs unter `/var/www/vhosts/<domain>/logs/` (einzeln oder `all_rotated=true`) - **erfordert `confirm=true`** |
| `imunify_malware_list` | Aktuelle Imunify360-Malware-Treffer (Pfad, Zeitpunkt, Signatur, Status) als JSON, optional gefiltert nach Domain, Pfad oder Status |
| `imunify_ignore_list` | Einträge der Imunify360-Malware-Ignore-Liste als JSON (optional Suche nach Pfad) |
| `imunify_ignore_add` | Fügt eine Datei unter `/var/www/vhosts/` zur Imunify360-Ignore-Liste hinzu (z.B. False Positives in rotierten Logs) - **erfordert `confirm=true`** |
| `imunify_ignore_remove` | Entfernt einen Pfad wieder aus der Imunify360-Ignore-Liste - **erfordert `confirm=true`** |
| `db_list` | Listet MySQL/MariaDB-Datenbanken (optional gefiltert nach Domain) inkl. Tabellen, Zeilenanzahl und Grösse |
| `db_query` | Führt eine einzelne read-only SQL-Query (SELECT/SHOW/EXPLAIN/DESCRIBE) gegen eine Datenbank aus |
| `db_search` | Durchsucht alle Text-Spalten aller Tabellen einer Datenbank nach einem Begriff (LIKE-Scan) |
| `server_load` | Allgemeine Serverlast (uptime, free, Prozessanzahl) |
| `pagespeed_insights` | Google PageSpeed Insights (Lighthouse) für eine URL/Domain: Scores, Labor-Messwerte inkl. Server-Antwortzeit/TTFB (`lab_metrics.server_timing`), CrUX-Felddaten, Top-Verbesserungen (optional mit betroffenen Ressourcen via `include_details`), Plausibilitätscheck der analysierten Seite (`page_check`) - mobile/desktop/both, read-only und extern |
| `run_diagnostic` | Generischer Fallback, nur Whitelist an read-only Befehlen erlaubt |

- `pagespeed_insights` läuft komplett extern: Die Ziel-URL wird von Google geladen, nicht vom Container oder vom Plesk-Server - kein SSH, kein Request vom Container auf die Ziel-URL. Erlaubt sind nur `http`/`https` mit öffentlichem Hostnamen (keine IPs, kein `localhost`, keine `user:pass@`-Zugangsdaten in der URL, da diese an Google übermittelt würden). Der `PAGESPEED_API_KEY` wird als Header (`X-goog-api-key`) an Google geschickt, nie in der Request-URL, nie geloggt und aus Fehlermeldungen entfernt; den Key in der Google Cloud Console per API-Einschränkung nur für die PageSpeed Insights API freigeben.

## Beispiel: PageSpeed einer Kundenseite prüfen

1. `pagespeed_insights(url="example.com", strategy="both")` - Scores, LCP/CLS/TBT und Top-Verbesserungen für mobile und desktop
2. `pagespeed_insights(url="https://www.example.com/shop", categories="all")` - zusätzlich Accessibility, Best Practices und SEO
3. `pagespeed_insights(url="example.com", include_details=true, max_items=5)` - je Performance-Finding die Top-5-Ressourcen mit `wastedMs` / `wastedBytes` / `totalBytes` (z.B. render-blocking-insight, unused-css-rules, unused-javascript, image-delivery-insight, cache-insight, unsized-images)
4. Server-Antwortzeit in `lab_metrics.server_timing` prüfen: `server_response_time`, `document_latency_insight` (Teilchecks Redirects, Serverantwort, Textkomprimierung), `network_server_latency`, `network_rtt`. Fehlt ein Audit im Lighthouse-Ergebnis, steht dort `"nicht im Lighthouse-Ergebnis enthalten"` - es wird nichts geschätzt.
5. `page_check` kontrollieren: finale URL, HTTP-Status und Grösse des Hauptdokuments. Eine Warnung `moegliche Challenge-/Zwischenseite` erscheint, wenn das Hauptdokument kleiner als 20 KB ist oder ein Seitenelement typische Challenge-Texte enthält ("Just a moment", "One moment, please", "Checking your browser" usw.). Den `<title>` liefert Lighthouse nicht mit, daher steht dort immer `"nicht im Lighthouse-Ergebnis enthalten"`.
6. Bei hoher Server-Antwortzeit auf dem Plesk-Server weiter mit `server_load`, `lve_stats` und `fpm_service_status` der Domain

## Datei-Upload

Binärdateien (Bilder, Schriften, ZIPs) laufen nicht mehr als Base64 durch den Chat. Der Inhalt geht direkt vom Client an den Container und von dort per SSH-Stream auf den Plesk-Server - nie durch den LLM-Kontext, nie komplett im RAM.

**Ablauf**

1. `upload_begin(domain="example.com", path="httpdocs/img/logo.png", sha256="<optional>", confirm=true)` prüft Domain (muss in Plesk als Subscription existieren), Pfad, geschützte Bereiche und Besitzer und liefert `upload_id`, `upload_url`, ein einmaliges `token` und `expires_at`.
2. Der Client lädt direkt hoch:
   ```bash
   curl -X PUT -H "Authorization: Bearer <token>" --data-binary @logo.png https://plesk-mcp.example.com/upload/<upload_id>
   ```
   Antwort (JSON): `domain`, `path`, `size`, `sha256`, `backup`.
3. `upload_status(upload_id)` meldet `pending`/`uploading`/`completed`/`expired`/`failed` mit `path`, `size`, `sha256` - zur Kontrolle im Chat.

Alternativ lädt `fetch_to_vhost(domain, path, url, sha256=None, allow_executable=false, confirm=true)` eine Datei selbst von einer https-URL.

**Schreiblogik (Upload, Fetch und `write_vhost_file` gemeinsam)**: Die Daten werden in eine Temp-Datei im Zielverzeichnis gestreamt (`.<name>.mcp-<zufall>.tmp`, `O_EXCL`, `0600`), das Grössenlimit wird dabei hart durchgesetzt. Danach sha256-Prüfung (falls angegeben - bei Abweichung bleibt die alte Datei unverändert), `fchown`/`fchmod`, `fsync`, Backup einer bestehenden Datei als `<path>.bak-<YYYYMMDDHHMMSS>` (kompatibel mit `delete_vhost_backup`) und atomarer `rename` an den Zielpfad. Besitzer/Rechte wie bei `write_vhost_file` (siehe "Sicherheit"): Dateien `0644`, neue Ordner `0755`, Besitzer vom nächsten existierenden Elternordner, nie root. Vor dem Schreiben wird der freie Speicherplatz geprüft (Limit + `UPLOAD_MIN_FREE_MB`).

**Sicherheitsmodell Upload-Endpunkt** (`PUT /upload/{upload_id}`, im selben Container):

- Token: 256 Bit zufällig, nur als sha256-Hash im Speicher, einmal verwendbar (wird beim Start des Uploads verbraucht, auch bei Fehlern), 5 Minuten gültig, gebunden an die upload_id und damit an Domain, Pfad, max_bytes und optional sha256.
- Token nur im `Authorization`-Header, nie in der URL (Proxy-Logs sehen nur die upload_id).
- Ohne gültiges, unverbrauchtes Token immer `401 {"error":"unauthorized"}` - unabhängig davon, ob die upload_id existiert.
- Rate-Limit pro IP (`UPLOAD_RATE_LIMIT_PER_MIN`, zählt auch fehlgeschlagene Versuche), maximal `UPLOAD_MAX_CONCURRENT` gleichzeitige Uploads (sonst `429`).
- Fehlerantworten enthalten nur einen Code (`too_large`, `sha256_mismatch`, `no_space`, ...), keine internen Pfade oder Stacktraces.
- Upload-Registry liegt im Speicher des Containers: Ein Neustart verwirft offene Tokens (Upload neu starten).
- Nur im HTTP-Modus mit gesetzter `PUBLIC_BASE_URL` verfügbar.

**SSRF-Schutz `fetch_to_vhost`**: nur `https`, keine Zugangsdaten in der URL, max. 3 Redirects, Timeout `FETCH_TIMEOUT`, Grössenlimit (auch per `Content-Length`). Der Hostname wird aufgelöst, alle Adressen müssen öffentlich sein (Loopback, private Netze, Link-Local inkl. `169.254.169.254`, CGNAT, Multicast, reservierte Bereiche und die IPv6-Entsprechungen inkl. IPv4-mapped werden blockiert). Die Verbindung geht direkt an die geprüfte IP (Host-Header und TLS-SNI/Zertifikatsprüfung auf den Originalnamen), Proxy-Umgebungsvariablen werden ignoriert. Bei jedem Redirect wird das Ziel erneut geprüft und gepinnt (gegen DNS-Rebinding).

**Reverse-Proxy**: Steht nginx (z.B. Plesk) vor dem Container, muss das Body-Limit mindestens `UPLOAD_MAX_BYTES` erlauben (nginx-Standard: 1 MB) und die Timeouts müssen zum Upload passen. Beispiel für die zusätzlichen nginx-Direktiven der Proxy-Domain (z.B. `plesk-mcp.example.com`):

```nginx
location /upload/ {
    client_max_body_size 26m;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_pass http://127.0.0.1:8422;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

`PUBLIC_BASE_URL` muss die öffentliche Basis-URL dieses Proxys sein (z.B. `https://plesk-mcp.example.com`). Damit das Rate-Limit die echte Client-IP sieht, die Adresse des Proxys, wie der Container sie sieht (z.B. das Docker-Gateway), in `UPLOAD_TRUSTED_PROXIES` eintragen - nur dann wird `X-Forwarded-For` ausgewertet (letzter Eintrag).

## Dateiverwaltung (Verschieben, Löschen, Papierkorb)

| Tool | Zweck |
|---|---|
| `list_vhost_dir(domain, path=".", depth=1)` | Nur lesend: Name, Typ (file/dir/symlink), Grösse, Rechte, Besitzer, Änderungsdatum. Tiefe max. 3, max. 500 Einträge (danach Hinweis auf Kürzung) |
| `move_vhost_file(domain, src, dst, overwrite=false, create_parents=false, allow_executable=false, confirm=true)` | Atomarer `rename`, nur innerhalb desselben Docroots derselben Domain. Existiert `dst`, Abbruch - ausser `overwrite=true` (nur Dateien, `dst` wird vorher als `.bak-<Zeitstempel>` gesichert) |
| `delete_vhost_file(domain, path, recursive=false, permanent=false, dry_run=false, delete_token=None, allow_executable=false, confirm=true)` | Standard: Verschieben in den Papierkorb. `permanent=true` löscht endgültig |
| `restore_vhost_trash(domain, trash_entry, allow_executable=false, confirm=true)` | Stellt einen Papierkorb-Eintrag am ursprünglichen Ort wieder her - überschreibt nie (liegt dort etwas, Abbruch). Ohne `confirm` Vorschau |
| `empty_vhost_trash(domain, older_than_days=14, confirm=true)` | Löscht Papierkorb-Einträge, die älter als `older_than_days` sind, endgültig. Ohne `confirm` Vorschau |

**Papierkorb**: `/var/www/vhosts/<domain>/.mcp-trash/<YYYYMMDDHHMMSS>/<relativer Pfad>` - ausserhalb jedes Docroots (nicht per Web erreichbar), Ordner `0700`, Besitzer = System-User der Subscription. Daneben liegt `.mcp-trash-meta.json` mit Ursprungspfad, Zeitpunkt, Typ, Anzahl Dateien und Grösse. Einträge listen mit `list_vhost_dir(domain, ".mcp-trash", 2)`. Pro Löschung maximal `VHOST_TRASH_MAX_MB` (Standard 500 MB), sonst Abbruch mit Hinweis auf `permanent=true`. Mit `VHOST_TRASH_AUTOCLEAN_DAYS` > 0 werden beim Containerstart alle Papierkorb-Einträge aller Domains gelöscht, die älter sind (Standard: aus).

**Ordner löschen nur zweistufig**: Ein erster Aufruf mit `recursive=true, dry_run=true` liefert Anzahl Dateien/Ordner, Gesamtgrösse, die ersten 50 Pfade, ob ausführbare Dateien enthalten sind, und ein `delete_token` (HMAC über Domain, Pfad, `permanent` und eine Prüfsumme der kompletten Auflistung inkl. Grösse, mtime und Inode, 15 Minuten gültig). Der zweite Aufruf mit `delete_token` und `confirm=true` führt nur aus, wenn sich die Auflistung seither nicht geändert hat - sonst Abbruch. Ein Token für den Papierkorb gilt nicht für `permanent=true`.

**Regeln für alle Pfade**: relativ zu `/var/www/vhosts/<domain>/`, keine Wildcards/Globs (`* ? [ ] { }`), kein `..`, kein `.`, keine absoluten Pfade. Symlinks werden nie verfolgt: Bei Symlinks wird nur der Link selbst verschoben bzw. entfernt, ein Symlink im Pfad (Elternordner) führt zum Abbruch, Symlinks werden nie überschrieben. Ausführbare Dateien (`.php`, `.php3`-`.php8`, `.phtml`, `.pht`, `.phps`, `.phar`, `.cgi`, `.pl`, `.py`, `.sh` - auch als Zwischenendung wie `bild.php.jpg` - sowie `.htaccess` und `.user.ini`) dürfen nur mit `allow_executable=true` UND `confirm=true` geschrieben, verschoben, gelöscht oder wiederhergestellt werden; bei Ordnern gilt das, sobald darin eine solche Datei liegt.

**Geschützte Pfade** (immer abgelehnt, auch mit `confirm=true`) für Upload, Fetch, Verschieben, Löschen und Wiederherstellen:

- das Vhost-Root `/var/www/vhosts/<domain>/` selbst
- jedes Docroot der Subscription selbst (laut Plesk-Datenbank, z.B. `httpdocs`) - Verschieben auch nie aus einem Docroot heraus oder in ein anderes
- `cgi-bin` im Vhost-Root oder direkt in einem Docroot
- die Bereiche `conf`, `logs`, `statistics`, `.ssh`, `.mcp-trash` (inkl. Inhalt)
- Mail-Verzeichnisse `mail`, `Maildir`, `mailnames` im Vhost-Root
- das chroot-Skelett der Plesk-Shell im Vhost-Root: `bin`, `dev`, `etc`, `lib`, `lib64`, `usr`, `var`, `tmp`
- alle Dotfiles und Dot-Ordner im Vhost-Root (z.B. `.ssh`, `.cagefs`, `.wp-cli`)

**Technik**: Der Container hat keinen direkten Dateisystemzugriff auf den Plesk-Server. Alle Datei-Aktionen (auch `write_vhost_file`) laufen über einen kleinen Python-Helper, der pro Aktion per SSH mit `PLESK_HELPER_PYTHON` gestartet wird (Python >= 3.3, Standard `/usr/libexec/platform-python`, auf EL8/CloudLinux 8 immer vorhanden). Er arbeitet nur mit Verzeichnis-Handles (`openat` mit `O_NOFOLLOW`, `renameat2` mit `RENAME_NOREPLACE`, `fchown`/`fchmod`), damit zwischen Prüfung und Aktion kein Symlink untergeschoben werden kann, und prüft unmittelbar vor der Aktion erneut, dass sich Ziel bzw. Auflistung nicht geändert haben.

**Audit-Log**: Jede Aktion (Upload-Start, Upload, Fetch, Move, Delete, Restore, Papierkorb leeren) schreibt eine JSON-Zeile mit Zeitpunkt, Domain, Pfaden, Grösse, sha256 (wo vorhanden) und Ergebnis nach stderr (`docker logs`, Präfix `AUDIT`) und optional in `AUDIT_LOG_FILE`. Keine Tokens, Passwörter oder Dateiinhalte.

## Beispiel: WordPress-Option gezielt ändern

1. Vorschau: `wp_option_update(domain="example.com", wp_path="httpdocs", option_name="wp_rocket_settings", keys={"minify_css": 1, "defer_all_js": 1})` - zeigt alt/neu je Schlüssel, schreibt nichts
2. Ausführen: dasselbe mit `confirm=true` - Rückgabe mit `backup_id` (z.B. `20260101120000-1a2b3c4d@example.com`) und alt/neu je Schlüssel
3. Einfache Option: `wp_option_update(domain="example.com", wp_path="httpdocs", option_name="elementor_font_display", value="swap", confirm=true)`
4. Rückgängig: `wp_option_rollback(backup_id="20260101120000-1a2b3c4d@example.com")` für die Vorschau, dann mit `confirm=true`

## Beispiel: Imunify-False-Positive in rotierten Logs

Imunify360 meldet gelegentlich "Malware" in rotierten Logdateien, weil dort protokollierte Angriffsversuche stehen (kein ausführbarer Code):

1. `imunify_malware_list(domain="example.com")` - Treffer mit Pfad und Signatur prüfen
2. `imunify_ignore_add(path="/var/www/vhosts/example.com/logs/error_log.1.gz", confirm=true)`
3. `imunify_ignore_list(search="/var/www/vhosts/example.com/")` - Eintrag kontrollieren
4. Rückgängig: `imunify_ignore_remove(path="/var/www/vhosts/example.com/logs/error_log.1.gz", confirm=true)`

## Tests

Die Pfadvalidierung der Imunify-Tools, URL-Validierung und Auswertung von `pagespeed_insights`, die Validierung von `wp_option_update` sowie Upload, Fetch (SSRF) und Dateiverwaltung sind ohne Plesk-Server testbar. `tests/test_vhost_files.py` startet den Datei-Helper lokal gegen ein temporäres Verzeichnis und braucht dafür Linux und root (`chown`), sonst werden diese Tests übersprungen:

```bash
pip install -r requirements.txt pytest
pytest
```

Die Tests laufen nicht in GitHub Actions und landen nicht im Docker-Image (dort wird nur `server.py` kopiert).
