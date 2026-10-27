# Plesk MCP Connector

MCP-Server für Diagnose (und, mit expliziter Bestätigung, gezielte Datei-Edits) auf einem Plesk-Server. Läuft ausschliesslich als Docker-Container mit Streamable-HTTP-Transport (z.B. via Portainer) hinter einem Reverse-Proxy. Ein lokaler Betrieb (stdio/`uv`) ist nicht vorgesehen.

Drei Datenquellen: SSH/SFTP (für alles, was nur auf Betriebssystem-Ebene existiert - PHP-FPM-Status, systemd-Journal, OOM-Kills, Disk-Nutzung, Serverlast, Vhost-Dateizugriff - sowie für Plesk-CLI-Befehle), die Plesk-REST-API (für strukturierte Plesk-eigene Daten wie Domains/Subscriptions über das `plesk_api_get`-Tool) und MySQL/MariaDB direkt (`db_list`/`db_query`/`db_search`, read-only, über den Plesk-internen Admin-DB-Zugang - siehe Abschnitt "Sicherheit").

Alle Zugangsdaten (SSH, Plesk-API-Key, Auth-Token) werden ausschliesslich über Umgebungsvariablen konfiguriert - im Code stehen keine Secrets. Dieses Repo ist **öffentlich**: Code, README, Tests und Beispiele enthalten keine echten Hostnamen, IPs, Domains, Tokens oder Kundennamen - nur Platzhalter wie `example.com`, `plesk-mcp.example.com` oder `upload.example.com`. Echte Werte gehören ausschliesslich in die Umgebungsvariablen (z.B. in Portainer), nie ins Repo.

Fast alle Tools sind read-only: kein Neustart von Services, keine destruktiven Kommandos (Whitelist + Blacklist in `server.py`), und das `plesk_api_get`-Tool kann nur GET-Requests stellen. Ausnahme: `write_vhost_file`, `replace_in_vhost_file`/`replace_in_vhost_files`, `set_php_handler`, `php_settings_update`/`php_settings_rollback`, `web_server_settings_update`/`web_server_settings_rollback`, `optimize_images`/`image_job`/`image_backups`, `delete_vhost_backup`, `delete_vhost_log`, `dns_add_record`, `dns_delete_record`, `dns_update_record` und `customer_update_email` (Kontakt-E-Mail eines Kundenaccounts), `subscription_suspend`/`subscription_activate` (Subscription sperren/wieder aktivieren) und `subscription_delete` (Subscription endgültig löschen, nur mit zusätzlicher Rückbestätigung des Domainnamens), `customer_delete` (Kundenaccount ohne Domains löschen, nur mit Rückbestätigung des Logins) sowie `imunify_ignore_add`/`imunify_ignore_remove`, `wp_option_update`/`wp_option_rollback`, `wp_custom_css_update`/`wp_custom_css_rollback`, `wp_comment_moderate`, `wp_plugin_update`/`wp_plugin_rollback`, `wp_theme_update`/`wp_theme_rollback`, `wc_coupon_create`/`wc_coupon_update`/`wc_coupon_rollback`/`wc_coupon_delete`, `wc_order_mark_shipped`, `wc_order_create`, `wc_stock_update` und die Datei-Tools `upload_begin`, `fetch_to_vhost`, `move_vhost_file`, `delete_vhost_file`, `restore_vhost_trash`, `empty_vhost_trash` dürfen schreiben - `write_vhost_file`, die beiden `replace_*`-Tools und `delete_vhost_backup` Dateien innerhalb von `/var/www/vhosts/<domain>/` anlegen/überschreiben/ändern/löschen (Backups), `delete_vhost_log` Logdateien unter `/var/www/vhosts/<domain>/logs/` löschen (rotierte) bzw. auf 0 Bytes leeren (aktive), die drei `dns_*`-Tools DNS-Resource-Records der Domain-Zone anlegen/entfernen/ändern (`dns_update_record` kombiniert Löschen+Neuanlegen für einen bestehenden Record in einem Aufruf), die beiden Imunify-Tools ausschliesslich die Imunify360-Malware-Ignore-Liste, die beiden `wp_option_*`-Tools einzelne freigegebene WordPress-Optionen, die `wp_custom_css_*`-Tools das "Zusätzliche CSS" des WordPress-Customizers, `wp_comment_moderate` den Status von WordPress-Kommentaren (Spam/Papierkorb/Freigabe/Löschen), `wp_plugin_update`/`wp_plugin_rollback` WordPress-Plugins und `wp_theme_update`/`wp_theme_rollback` WordPress-Themes (Update per WP-CLI, vorher tar.gz-Backup), die `wc_coupon_*`-Tools WooCommerce-Gutscheine, `wc_order_mark_shipped` Sendungsnummer und Status einer WooCommerce-Bestellung, `wc_order_create` neue WooCommerce-Bestellungen, `wc_stock_update` den Lagerbestand eines WooCommerce-Produkts, `set_php_handler` den PHP-Handler einer Domain (nur auf einen auf dem Server aktivierten Handler), `php_settings_update`/`php_settings_rollback` freigegebene PHP-Werte einer Domain (z.B. `memory_limit`, PHP-FPM-Werte), die Datei-Tools Dateien unter `/var/www/vhosts/<domain>/` hochladen, verschieben, in den Papierkorb legen oder endgültig löschen (siehe "Datei-Upload" und "Dateiverwaltung"). Alle schreibenden Tools erfordern zwingend `confirm=true` pro Aufruf (keine globale Freischaltung), `write_vhost_file` und die `replace_*`-Tools legen vor dem Überschreiben automatisch ein Backup der alten Version an. Details siehe Abschnitt "Sicherheit" unten.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `PLESK_SSH_HOST` | ja | – | Hostname/IP des Plesk-Servers |
| `PLESK_SSH_PORT` | nein | `22` | SSH-Port |
| `PLESK_SSH_USER` | nein | `root` | SSH-Benutzer |
| `PLESK_SSH_PASSWORD` | ja, ausser bei Key-Auth | – | SSH-Passwort |
| `PLESK_SSH_KEY_PATH` | nein | – | Alternative zu Passwort: Pfad zu einem privaten SSH-Key |
| `PLESK_SSH_TIMEOUT` | nein | `20` | SSH-Verbindungs- und Inaktivitäts-Timeout in Sekunden |
| `PLESK_SSH_MAX_RUNTIME` | nein | `1800` | Harte Gesamtlaufzeit pro SSH-Kommando in Sekunden (Sicherheitsnetz, unabhängig von fliessender Ausgabe); die Ausgabe ist zusätzlich je Kommando auf 4 MB begrenzt |
| `PLESK_SSH_HOST_KEY` | ja (oder `PLESK_SSH_KNOWN_HOSTS`) | – | Host-Key des Plesk-Servers als `<typ> <base64>` (z.B. `ssh-ed25519 AAAAC3Nza...`), die Ausgabe von `ssh-keyscan -t ed25519 <host>` mit Hostname davor wird ebenfalls akzeptiert. Der Host-Key wird immer strikt geprüft (MITM-Schutz) - ohne einen der beiden Werte startet der Server nicht |
| `PLESK_SSH_KNOWN_HOSTS` | ja (oder `PLESK_SSH_HOST_KEY`) | – | Alternative zu `PLESK_SSH_HOST_KEY`: Pfad zu einer in den Container gemounteten known_hosts-Datei (lesbar für UID 10001) |
| `DIAG_TIMEOUT` | nein | `60` | Laufzeitlimit in Sekunden für `run_diagnostic` (`timeout <s>` auf dem Server plus harte Deadline im Container) |
| `PLESK_API_HOST` | nein | `PLESK_SSH_HOST` | Host für die Plesk-REST-API, falls abweichend vom SSH-Host |
| `PLESK_API_PORT` | nein | `8443` | Port der Plesk-REST-API |
| `PLESK_API_KEY` | ja, für `plesk_api_get` | – | Secret Key, erzeugt via `plesk bin secret_key --create` |
| `PLESK_API_VERIFY_SSL` | nein | `true` | `false`, falls der Plesk-Server ein selbstsigniertes Zertifikat nutzt |
| `PLESK_API_TIMEOUT` | nein | `20` | Timeout in Sekunden für Requests gegen die Plesk-REST-API |
| `PAGESPEED_API_KEY` | ja, für `pagespeed_insights` | – | Google-API-Key für die PageSpeed Insights API v5 (ohne Key praktisch immer HTTP 429) |
| `PAGESPEED_TIMEOUT` | nein | `120` | Timeout in Sekunden pro PageSpeed-Analyse |
| `WP_CLI_PATH` | nein | `/usr/local/bin/wp-standalone` | Pfad zur WP-CLI-Phar auf dem Plesk-Server |
| `WP_CLI_TIMEOUT` | nein | `120` | Timeout in Sekunden pro WP-CLI-Aufruf |
| `WP_PLUGIN_UPDATE_TIMEOUT` | nein | `600` | Timeout in Sekunden für `wp plugin update`/`wp theme update` sowie Backup/Wiederherstellen eines Plugins oder Themes |
| `PUBLIC_BASE_URL` | ja, für Uploads | – | Öffentliche Basis-URL des Containers hinter dem Reverse-Proxy, z.B. `https://plesk-mcp.example.com` (daraus wird die Upload-URL gebaut) |
| `UPLOAD_MAX_BYTES` | nein | `26214400` (25 MB) | Grössenlimit für Upload und Fetch; pro Upload per `max_bytes` weiter verkleinerbar |
| `UPLOAD_RATE_LIMIT_PER_MIN` | nein | `20` | Fehlversuche (ungültiges/abgelaufenes Token) pro Minute und IP auf `/upload/`; gültige Uploads zählen nicht und werden nie durch fremde Fehlversuche blockiert |
| `UPLOAD_MAX_CONCURRENT` | nein | `2` | Maximal gleichzeitig laufende Uploads |
| `UPLOAD_MIN_FREE_MB` | nein | `256` | Freier Speicher, der auf dem Plesk-Server zusätzlich zum Upload-Limit frei bleiben muss |
| `UPLOAD_TIMEOUT` | nein | `300` | Maximale Dauer eines Uploads in Sekunden |
| `UPLOAD_TRUSTED_PROXIES` | empfohlen | – | Kommagetrennte IPs des Reverse-Proxys (wie der Container sie sieht); nur dann wird `X-Forwarded-For` für das Rate-Limit ausgewertet. Ist `PUBLIC_BASE_URL` gesetzt und dieser Wert leer, warnt der Server beim Start |
| `FETCH_TIMEOUT` | nein | `120` | Timeout in Sekunden für `fetch_to_vhost` |
| `VHOST_TRASH_MAX_MB` | nein | `500` | Maximale Grösse pro Löschung in den Papierkorb |
| `VHOST_TRASH_AUTOCLEAN_DAYS` | nein | `0` (aus) | > 0: beim Containerstart Papierkorb-Einträge aller Domains löschen, die älter sind |
| `PLESK_HELPER_PYTHON` | nein | `/usr/libexec/platform-python` | Python (>= 3.3) auf dem Plesk-Server für den Datei-Helper |
| `PLESK_HELPER_TIMEOUT` | nein | `600` | Timeout in Sekunden pro Helper-Aufruf |
| `PLESK_HOSTING_TIMEOUT` | nein | `180` | Timeout in Sekunden für `plesk bin site`-Aufrufe von `hosting_settings`/`set_php_handler`/`php_settings_*` |
| `PHP_SETTINGS_ALLOWLIST` | nein | – | Zusätzliche PHP-Direktiven für `php_settings_update` (kommagetrennt); die Nie-Liste bleibt gesperrt |
| `PHP_SETTINGS_BACKUP_DIR` | nein | `/root/.plesk-mcp/php-settings-backups` | Ablage der Backups von `php_settings_update`/`_rollback` auf dem Plesk-Server (root-only) |
| `PHP_SETTINGS_HTTP_TIMEOUT` | nein | `20` | Timeout in Sekunden für den Abruf der Startseite nach einer Änderung |
| `WEB_SETTINGS_BACKUP_DIR` | nein | `/root/.plesk-mcp/web-server-backups` | Ablage der Backups von `web_server_settings_update` auf dem Plesk-Server (root-only) |
| `IMAGE_JPEGOPTIM` / `IMAGE_OPTIPNG` | nein | `/usr/bin/jpegoptim` / `/usr/bin/optipng` | Programme für `optimize_images` auf dem Plesk-Server |
| `IMAGE_OPT_TIMEOUT` | nein | `300` | SSH-Timeout in Sekunden ohne Fortschritt für `optimize_images` |
| `IMAGE_OPT_TIME_BUDGET` | nein | `30` | Zeitbudget in Sekunden pro Aufruf von `optimize_images` |
| `IMAGE_OPT_TOOL_TIMEOUT` | nein | `15` | Timeout in Sekunden pro Aufruf von `jpegoptim`/`optipng` |
| `IMAGE_OPT_BG_TOOL_TIMEOUT` | nein | `300` | Timeout in Sekunden pro Aufruf von `jpegoptim`/`optipng` im Hintergrundmodus (`optimize_images(hintergrund=true)`) |
| `REPLACE_MAX_BYTES` | nein | `5242880` | Maximale Dateigrösse in Bytes für `replace_in_vhost_file(s)` (Standard 5 MB) |
| `AUDIT_LOG_FILE` | nein | – | Optionaler Pfad im Container für das Audit-Log (zusätzlich zu stderr) |
| `MCP_API_KEY` | ja | – | Statisches Bearer-Token zum Schutz des öffentlichen Endpoints, mindestens 32 Zeichen Zufall (z.B. `python -c "import secrets; print(secrets.token_urlsafe(32))"`). Ohne Token oder mit kürzerem Token startet der Server nicht (fail-safe) |
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse des HTTP-Servers *innerhalb* des Containers |
| `MCP_PORT` | nein | `8000` | Port des HTTP-Servers *innerhalb* des Containers |
| `MCP_BIND_ADDR` | nein | `0.0.0.0` | Bind-Adresse *auf dem Docker-Host* (docker-compose Port-Mapping) |
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

Der Container bindet standardmässig auf `0.0.0.0:8422` auf dem Docker-Host (alle Interfaces, wie bei allen MCP-Stacks); der Zugriff ist durch das Bearer-Token geschützt. Für den Zugriff aus einer Cloud-Claude-Session braucht es zusätzlich:

1. Eine eigene Subdomain (z.B. `plesk-mcp.deine-domain.ch`)
2. Einen Reverse-Proxy (Plesk/nginx) mit TLS-Zertifikat, der auf `<Host-IP>:8422` weiterleitet (Endpoint-Pfad: `/mcp`)
3. In der Cloud-Claude-Session wird der Server dann als Remote-MCP mit `https://plesk-mcp.deine-domain.ch/mcp` und dem `MCP_API_KEY` als Bearer-Token eingebunden - im Connector-Setup **"Keine Anmeldung"** wählen (kein OAuth) und einen Request-Header `Authorization: Bearer <MCP_API_KEY>` hinzufügen

## Sicherheit

- Der Server startet nur, wenn `MCP_API_KEY` gesetzt und mindestens 32 Zeichen lang ist - es gibt also nie einen ungeschützten oder nur schwach geschützten öffentlichen Endpoint.
- Jeder HTTP-Request muss den Header `Authorization: Bearer <MCP_API_KEY>` mitschicken, sonst Antwort `401 unauthorized`. Der Vergleich ist zeitkonstant (`hmac.compare_digest`).
- Der SSH-Host-Key des Plesk-Servers muss gepinnt sein (`PLESK_SSH_HOST_KEY` oder `PLESK_SSH_KNOWN_HOSTS`), sonst startet der Server nicht; ein abweichender Key führt zum Verbindungsabbruch mit beiden Fingerprints in der Meldung. Ein automatisches Akzeptieren beim ersten Connect gibt es nicht (bei Passwort-Authentifizierung könnte ein MITM sonst das Root-Passwort abgreifen).
- Jedes SSH-Kommando hat ein Inaktivitäts-Timeout (`PLESK_SSH_TIMEOUT` bzw. der Wert des Tools), eine harte Gesamtlaufzeit (`PLESK_SSH_MAX_RUNTIME`) und eine Ausgabe-Obergrenze von 4 MB je Strom - danach wird der Kanal geschlossen und die Ausgabe als gekürzt markiert. `search_log`/`search_main_nginx_log` klemmen `lines` auf 1..5000.
- Der Container läuft als Non-Root-User (UID 10001); gemountete Dateien (SSH-Key, known_hosts, Audit-Log) müssen für diesen User lesbar bzw. schreibbar sein. Abhängigkeiten sind in `requirements.txt` exakt gepinnt.
- Einzige Ausnahme ist `GET /healthz` für den Docker-Healthcheck: ohne Token, liefert nur `{"status": "ok"}` (keine Version, keine Konfiguration, keine Verbindung zu Plesk). Das Image bringt den `HEALTHCHECK` mit (interval 30s, timeout 5s, start_period 15s, retries 3).
- `run_diagnostic` prüft den Programmnamen gegen eine Whitelist, Shell-Konstrukte (`;`, `&&`, `>`, `$(`, Backticks, Newlines) und destruktive Wörter per Blacklist, und je Utility eine **Positiv-Liste**: `plesk` nur als `plesk version`, `plesk bin <utility> -i|--info|-l|--list|--show-*` (nicht `--show-password`; `-i` nur bei Domain-/Kunden-Utilities, bei `extension` wäre es `--install`) und `plesk ext <name> --list|--info` - `-c/-u/-r/-i/-e`, weitere `--`-Optionen nach der Aktion und `--wp-cli` sind gesperrt (dafür gibt es die `wp_*`-Tools); `systemctl` nur `status/is-active/list-units/show/cat`; `journalctl` ohne `--vacuum-*`, `--rotate`, `--flush`, `--sync`, `--relinquish-var`, `--setup-keys`, `--cursor-file`, `-f`; `dmesg` ohne `-C/-c/--clear/-n/-w`; `tail` ohne `-f/-F/--follow/--pid`; `hostname` nur ohne Argumente; `find` (ohne `-exec/-ok/-delete/-fprint*`, `-L/-H/-follow`), `du` (ohne `-L/-D/-H`) und `grep -r` (kein `-R`) nur mit absolutem Pfad ohne Shell-Expansionen unterhalb von `/var/www/vhosts`, `/var/log`, `/etc/nginx`, `/etc/httpd`, `/usr/local/psa`; Geräte wie `/dev/zero`, `/dev/urandom`, `/dev/random`, `/proc/kcore` sind überall gesperrt. Pipes werden auch ohne Leerzeichen (`cat x|plesk ...`) segmentweise geprüft. Das Kommando läuft auf dem Server unter `timeout -k 5 <DIAG_TIMEOUT> sh -c '...'` (eigene Prozessgruppe), dazu die harte Deadline und die 4-MB-Grenze. Kein Freibrief für beliebige Shell-Befehle - was damit nicht geht, hat ein eigenes Tool oder gehört nicht in ein read-only Diagnose-Tool.
- `plesk_api_get` kann nur GET-Requests stellen - schreibender Zugriff auf die Plesk-REST-API ist über dieses Tool nicht möglich. Der Pfad ist auf `[A-Za-z0-9_/-]` unterhalb von `/api/v2/` begrenzt (`..`, `?`, `#` werden abgelehnt), Query-Parameter nur über `query`.
- `.env` ist in `.gitignore` und wird nie committet.
- Das Repo ist öffentlich - die Sicherheit beruht nicht auf Geheimhaltung des Codes, sondern auf `MCP_API_KEY`, den Einmal-Tokens für Uploads und den Prüfungen im Code. Secrets und echte Server-/Kundendaten nie committen.
- Für produktiven Einsatz: dedizierten SSH-User mit eingeschränkten Rechten (statt root) und/oder Key-Auth statt Passwort erwägen; für `plesk_api_get` den Secret Key optional per `-ip-address` auf die Docker-Host-IP einschränken (siehe `plesk bin secret_key --create`).
- `dns_add_record`/`dns_delete_record`/`dns_update_record` erfordern alle drei `confirm=true` als expliziten Parameter pro Aufruf (kein globaler Schreib-Schalter) und rufen ausschliesslich `plesk bin dns --add`/`--del <domain> -<typ> ...` auf - unterstützte Record-Typen: A, AAAA, CNAME, MX, NS, TXT, SRV (PTR/CAA/DS/HTTPS/TLSA sind nicht abgedeckt, dafür weiterhin `plesk`-CLI direkt auf dem Server nutzen). Legt Plesk bereits einen identischen Record vor, meldet der Server selbst einen Fehler zurück (kein stillschweigendes Überschreiben/Duplizieren). Plesk kennt kein natives Update für einzelne Records - `dns_update_record` bildet das als Delete (anhand der alten Werte) gefolgt von Add (mit den neuen Werten) ab; schlägt das Delete fehl, wird vorher abgebrochen und nichts geändert, schlägt das nachfolgende Add fehl, ist der alte Record bereits weg (Fehlermeldung wird vollständig zurückgegeben, damit er nötigenfalls per `dns_add_record` manuell wiederhergestellt werden kann). Alle drei Tools ändern live auflösbare DNS-Einträge auf einem Produktivserver.
- `write_vhost_file`/`delete_vhost_backup` sind strikt auf `/var/www/vhosts/<domain>/` beschränkt (Path-Traversal wie `../../etc/passwd` wird blockiert) und erfordern beide `confirm=true` als expliziten Parameter pro Aufruf - es gibt keinen globalen Schreib-Schalter, ein Aufruf ohne `confirm=true` schlägt immer fehl.
- `write_vhost_file`, `replace_in_vhost_file` und `replace_in_vhost_files` durchlaufen dieselbe Prüfkette wie `upload_begin`/`fetch_to_vhost`/`move_vhost_file`/`delete_vhost_file`: `domain` muss die Hauptdomain einer Subscription sein (Abfrage in der Plesk-DB), der Pfad ist relativ ohne `..`/Wildcards, geschützte Bereiche (`conf`, `logs`, `.ssh`, `statistics`, Mail-Ordner, Dotfiles direkt im Vhost-Root, das Docroot selbst, `cgi-bin`, chroot-Skelett) werden abgelehnt, ausführbare Dateien (`.php`, `.phtml`, `.cgi`, `.sh`, `.htaccess`, `.user.ini` ... auch als Zwischenendung, bei `replace_in_vhost_files` auch als glob-Treffer) nur mit `allow_executable=true`. `write_vhost_file` ist auf `UPLOAD_MAX_BYTES` begrenzt und prüft den freien Platz wie der Upload.
- `write_vhost_file` legt vor jedem Überschreiben automatisch ein Backup der alten Version als `<path>.bak-<YYYYMMDDHHMMSS>` an und bricht ab, falls der Zielpfad ein Symlink ist oder ein Elternordner ein Symlink ist (kein Schreiben durch Symlinks hindurch). Es nutzt dieselbe Schreiblogik wie Upload und Fetch (siehe "Datei-Upload"): neue Datei per Temp-Datei und atomarem `rename` - die Datei erhält dabei einen neuen Inode (Hardlinks auf die alte Datei bleiben beim alten Inhalt).
- `write_vhost_file` setzt Besitzer und Rechte so, dass der Kunde die Dateien per FTP/Dateimanager weiter bearbeiten kann: Bestehende Dateien und ihr Backup behalten Besitzer, Gruppe und Rechte der alten Datei. Neue Dateien (`0644`) und vom Tool neu angelegte Zwischenordner (`0755`) erhalten als Besitzer den User des nächsten bereits existierenden übergeordneten Ordners unterhalb von `/var/www/vhosts/<domain>/` - gehört dieser root, wird stattdessen der Besitzer von `/var/www/vhosts/<domain>/httpdocs` (Subscription-User) verwendet, root wird nie übernommen. Als Gruppe wird immer die primäre Gruppe dieses Users gesetzt (bei Plesk `psacln`); die Gruppe `psaserv` gehört nur auf `httpdocs` selbst (damit Webserver und FTP hineinkommen) und wird nicht übernommen. Unterhalb von `httpdocs` werden ausserdem bestehende Dateien und übergeordnete Ordner, die noch root gehören (z.B. von einer früheren Version angelegte `wp-content/mu-plugins`), beim Schreiben auf diesen Besitzer umgestellt; die Rechte bleiben dabei unverändert, eine Datei wird dafür neu angelegt und atomar über die alte umbenannt. Ausserhalb von `httpdocs` (z.B. `conf/`, `logs/`) bleiben root-Einträge unangetastet, da Plesk sie so vorsieht. Neue Dateien werden exklusiv angelegt (`O_EXCL`) und per Handle (`fchown`/`fchmod`) angepasst, Ordner per `chown -h` - Symlinks werden dabei nie verfolgt. Die Rückgabe nennt den gesetzten Besitzer, z.B. `Neu erstellt: /var/www/vhosts/example.com/httpdocs/test.php (120 Bytes). Besitzer: kunde:psacln, Rechte: 0644.`
- `write_vhost_file` ermittelt den Besitzer in dieser Reihenfolge: nächster existierender Ordner, `httpdocs`, `/var/www/vhosts/<domain>/` - gehören alle root, wird ohne zu schreiben abgebrochen. Ein root-eigenes `httpdocs` wird auf `<user>:psaserv` umgestellt (Plesk-Standard, damit der Webserver hineinkommt), Ordner darunter auf `<user>:psacln`. Eine root-eigene Datei ausserhalb von `httpdocs` (z.B. unter `conf/`) wird nicht überschrieben, da das Ergebnis root-eigen wäre bzw. sonst Plesk-Konfiguration an den Kunden übergehen würde.
- `wp_option_update`/`wp_option_rollback` rufen WP-CLI (`WP_CLI_PATH`) mit der PHP-Version der Domain als System-User der Subscription auf (`runuser`, leere Umgebung, nie root), mit `--path` auf die Installation - so laufen die WordPress-Hooks (`update_option`) und ein Objekt-Cache (Redis) wird korrekt aktualisiert. Teilschlüssel serialisierter Arrays über `wp option patch update` (bzw. `insert` für neue Schlüssel), einfache Werte über `wp option update`; Werte werden als JSON übergeben (`--format=json`), Typen bleiben erhalten. Jede Option mit gültigem Namen (Buchstaben, Ziffern, `_`, `-`) kann geändert werden; Schutz bieten Vorschau ohne `confirm`, Backup vor jeder Änderung und `wp_option_rollback`. System-User, Home und PHP-Handler der Domain kommen aus der Plesk-Datenbank über `plesk db` (authentifiziert sich selbst) - `wp-config.php`, DB-Passwörter, Salts und `/etc/psa/.psa.shadow` werden von diesen Tools weder gelesen noch ausgegeben oder geloggt. Vor jeder Änderung wird der alte Zustand als JSON unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-option-backups/<backup_id>.json` abgelegt (Home der Subscription, ausserhalb des Webroots, vom Subscription-User geschrieben, `0600`); ohne Backup wird nichts geändert. Nach dem Schreiben wird der Wert erneut gelesen und kontrolliert. `wp_option_rollback` stellt nur die damals geänderten Schlüssel wieder her (damals fehlende werden entfernt) und legt vorher selbst ein Backup an.
- `wp_custom_css_show`/`wp_custom_css_update`/`wp_custom_css_rollback` nutzen denselben WP-CLI-Unterbau (Subscription-User, PHP der Domain, nie root) und rufen `wp eval` mit festem PHP-Code auf (`wp_get_custom_css`, `wp_update_custom_css_post`). Das CSS wird per stdin übergeben und ist nie Teil des Codes; der optionale Theme-Name wird nur nach Regex-Prüfung (`[A-Za-z0-9_.-]`, max. 100 Zeichen) als Literal eingesetzt. Grenzen: max. 512 KB, kein Nullbyte, kein `</style`. Backup des alten CSS vor jeder Änderung unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-custom-css-backups/<backup_id>.json` (0600), nach dem Schreiben wird erneut gelesen und verglichen; WordPress legt zusätzlich wie im Customizer eine Post-Revision an.
- `wp_comment_list`/`wp_comment_moderate` nutzen `wp comment list|spam|trash|approve|unapprove|unspam|untrash|delete` auf demselben Unterbau. IDs werden als positive Integer geprüft (max. 200 je Aufruf), Status und Aktion gegen feste Listen; der Suchtext geht als eigenes Argument an WP-CLI. Nur `delete` löscht endgültig (`--force`), alle anderen Aktionen sind in WordPress umkehrbar. Nach dem Ausführen wird der Status jedes Kommentars erneut gelesen und mit dem erwarteten Wert verglichen.
- `wp_plugin_list`/`wp_plugin_update`/`wp_plugin_rollback` nutzen `wp plugin list|path|update` auf demselben Unterbau (Subscription-User, nie root). Plugin-Slugs werden per Regex geprüft (`[A-Za-z0-9][A-Za-z0-9_.-]`, max. 100 Zeichen, max. 50 je Aufruf), eine optionale Version ebenso; Must-use-Plugins und Drop-ins werden nicht angefasst. Vor dem Update wird jedes betroffene Plugin (Ordner bzw. Einzeldatei im Plugin-Verzeichnis von WordPress, das im Vhost liegen muss) als `tar.gz` unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-plugin-backups/<backup_id>/` gesichert (0600, mit `manifest.json`); ohne Backup wird nichts geändert. Danach werden die Versionen erneut gelesen und die Startseite abgerufen: lieferte sie vorher keinen 5xx-Fehler und danach schon, werden die Plugins automatisch aus dem Backup zurückgestellt. `wp_plugin_rollback` entpackt das Backup in einen Temp-Ordner im Plugin-Verzeichnis und tauscht ihn gegen den aktuellen Stand, nachdem dieser selbst gesichert wurde. Datenbank-Änderungen eines Plugin-Updates (z.B. Migrationen) werden dabei nicht zurückgestellt.
- `wp_theme_list`/`wp_theme_update`/`wp_theme_rollback` arbeiten wie die Plugin-Tools mit `wp theme list|path|update`: gleiche Prüfung von Slugs (kein `..`, keine Wildcards, max. 50), Version und `backup_id`, Backup jedes Theme-Ordners als `tar.gz` unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-theme-backups/<backup_id>/` (0600, mit `manifest.json`), Kontrolle von Versionen und Startseite mit automatischem Zurückstellen bei neuem 5xx-Fehler (im Ergebnis als `automatisch_zurueckgestellt` und `ergebnis` "ZURÜCKGESTELLT: ..." gemeldet). Ist ein Child-Theme aktiv und wurde sein Parent-Theme aktualisiert, enthält das Ergebnis `hinweis_child_theme`.
- **memory_limit für WP-CLI:** Alle WP-CLI-Aufrufe laufen über eine gemeinsame Funktion und starten die WP-CLI-Phar direkt mit der PHP-CLI der Domain als `<php> -d memory_limit=<wert> -d display_errors=stderr <wp-cli> ...` (als Subscription-User, nie root). Der Wert ist der wirksame `memory_limit` der Domain aus den Plesk-PHP-Einstellungen (`plesk bin site --show-php-settings`, "Additional directives" haben Vorrang), mindestens `256M`; `-1` und Werte über `2048M` werden auf `2048M` begrenzt. Nur Zahl plus optional `K`/`M`/`G` wird akzeptiert, der Wert geht als eigenes, gequotetes Argument an PHP. Ist er nicht ermittelbar, wird `512M` verwendet und im Ergebnis als `hinweis_memory_limit` vermerkt. Der verwendete Wert steht als `php_memory_limit` im Ergebnis. Ohne das würde WP-CLI mit dem PHP-CLI-Standard (meist `128M`) laufen und bei grösseren Installationen (z.B. WooCommerce plus Jetpack) mit "Allowed memory size ... exhausted" abbrechen.
- **Warnungen:** Massgebend für Erfolg oder Fehler eines WP-CLI-Aufrufs ist der Exit-Code. PHP-Warnings, -Notices und -Deprecated-Meldungen (z.B. aus Plugins unter neueren PHP-Versionen) sowie WP-CLI-Warnungen werden aus der Ausgabe entfernt und im Ergebnis separat als `warnungen` zurückgegeben (max. 30).
- `wc_stock_get`/`wc_stock_update` laufen per `wp eval` mit festem PHP-Code über die WooCommerce-API; Produkt-ID bzw. Artikelnummer und Menge kommen nach Prüfung als JSON per stdin. Gebucht wird mit `wc_update_product_stock` (atomar, bei Varianten mit Lager auf dem Hauptprodukt dort), nur bei aktiver Lagerverwaltung. Zwischen Vorschau und Ausführung wird der Bestand erneut geprüft (`expected`), Änderungen pro Aufruf sind auf ±10000 begrenzt. Jede Ausführung steht im Audit-Log (Produkt, Aktion, Bestand vorher/nachher, Grund).
- `wc_order_create` läuft per `wp eval` mit festem PHP-Code; Adressen, Positionen, Versandart, Status und Notizen werden vorher geprüft (Feld-Whitelist, Regex für PLZ, Land, E-Mail, Telefon, Preise `\d{1,7}(.\d{1,2})?`, Versandart `<methode>:<instanz>`, Status aus fester Liste, max. 50 Positionen) und kommen nur als JSON per stdin. Angelegt wird über die WooCommerce-API (`wc_create_order`, `add_product`, `set_address`, Versandposition, `calculate_totals`, `update_status`, `wc_maybe_reduce_stock_levels`), damit dieselben Hooks laufen wie bei einer Shop-Bestellung. Die Vorschau prüft Produkte (variable Produkte nur per Variations-ID) und Versandart, listet die Versandarten des Shops und Bestellungen derselben E-Mail der letzten 24 Stunden (Schutz vor Doppelanlage nach einem Abbruch). `suppress_customer_emails=true` schaltet beim Anlegen alle WooCommerce-Kundenmails ab (`woocommerce_email_enabled_<id>` für jede Kunden-Mail), nur in diesem Aufruf; Admin-Mails bleiben, spätere Statuswechsel senden wieder Kundenmails. Jede Ausführung steht im Audit-Log.
- `wc_order_get`/`wc_order_mark_shipped` laufen per `wp eval` mit festem PHP-Code über die WooCommerce-API (`wc_get_order`, HPOS-kompatibel); Bestellnummer, Sendungsnummer und Provider kommen nach Regex-Prüfung als JSON per stdin, nie in den Code. Die Sendungsnummer wird mit `wc_st_add_tracking_number` des Shipment-Tracking-Plugins eingetragen (fehlt das Plugin, Abbruch ohne Änderung), eine bereits eingetragene Nummer wird nicht doppelt angelegt. Der Statuswechsel auf "Abgeschlossen" löst wie im Backend die WooCommerce-Kundenmail aus. Danach wird die Bestellung erneut gelesen und kontrolliert.
- Die `wc_coupon_*`-Tools nutzen die WooCommerce-CLI (`wp wc shop_coupon list|get|create|update|delete`) auf demselben Unterbau. Die CLI braucht einen Benutzer (`--user`); verwendet wird die ID des ersten Administrators der Installation (`wp user list --role=administrator`), vorher wird geprüft, dass WooCommerce aktiv ist. Felder laufen durch eine Whitelist mit Typprüfung (Betrag `\d+(.\d{1,4})?`, Datum ISO, IDs positive Integer, E-Mails, Status/Typ aus festen Listen); Listen werden als JSON an die CLI übergeben. Vor jedem `update`/`delete` wird der alte Gutschein als JSON unter `/var/www/vhosts/<domain>/.plesk-mcp/wc-coupon-backups/<backup_id>.json` (0600) gesichert; nach dem Schreiben wird der Gutschein erneut gelesen und je Feld verglichen (Beträge, Datum und Listenreihenfolge normalisiert).
- `delete_vhost_backup` löscht ausschliesslich Dateien, deren Pfad auf `.bak-<14-stellige Zeitstempel>` endet - kein generisches Lösch-Tool für beliebige Vhost-Dateien.
- `delete_vhost_log` arbeitet ausschliesslich direkt im Verzeichnis `/var/www/vhosts/<domain>/logs/` (nur reiner Dateiname, keine Pfade/`..`), fasst nur reguläre Dateien an (keine Symlinks/Verzeichnisse) und erfordert `confirm=true`. Rotierte Logs (`.gz`, `.<Zahl>`, `-JJJJMMTT`) werden gelöscht, aktive Logs (z.B. `access_log`, `error_log`) nur auf 0 Bytes geleert, da der Webserver sie offen hält - ein echtes Löschen würde den Platz erst nach einem Reload freigeben. Mit `all_rotated=true` werden alle rotierten Logs der Domain auf einmal gelöscht.
- `imunify_ignore_add`/`imunify_ignore_remove` ändern ausschliesslich die Imunify360-Malware-Ignore-Liste (keine anderen Imunify-Einstellungen, keine Dateien) und erfordern `confirm=true`. Erlaubt sind nur absolute, bereits normalisierte Pfade unter `/var/www/vhosts/<domain>/...` (bzw. `/var/www/vhosts/system/<domain>/...`), mindestens eine Ebene unter dem Domain-Verzeichnis - nie ganze Domains. `..`, `.`, `//`, Leerzeichen, Shell-Sonderzeichen und Wildcards werden abgelehnt (die CLI unterstützt laut ihrem Schema nur absolute Pfade, keine Glob-Muster). Zusätzlich wird per SFTP geprüft, dass das Ziel kein Symlink ist und der kanonische Pfad (Symlinks in Elternverzeichnissen aufgelöst) exakt dem angegebenen entspricht. Die Argumente werden als Liste gebaut und einzeln gequotet an `imunify360-agent` übergeben. `imunify360-agent` ist bewusst **nicht** in `run_diagnostic` freigegeben, nur die fest verdrahteten Unterbefehle `malware malicious list` und `malware ignore list|add|delete` sind erreichbar. Da die CLI Einträge nur per ID entfernt (`malware ignore delete <id>`), ermittelt `imunify_ignore_remove` die ID vorher über `malware ignore list --search <path>` und löscht nur Einträge mit exakt diesem Pfad; die Datei wird danach von Imunify standardmässig neu gescannt (`skip_rescan=true` unterdrückt das).
- `db_list`/`db_query`/`db_search` nutzen automatisch den Plesk-internen MySQL/MariaDB-Admin-Zugang (Login `admin`, Passwort im Klartext in `/etc/psa/.psa.shadow` - damit verwaltet Plesk selbst alle Kunden-Datenbanken, z.B. für phpMyAdmin-SSO). Das ist technisch ein sehr mächtiger Zugang (faktisch DB-root) und wird genutzt, weil die Tools ohnehin per Root-SSH laufen und diese Datei sonst ebenso lesen könnten - es wird also kein zusätzlicher, separat abgesicherter Zugang geschaffen. Die Beschränkung auf read-only passiert ausschliesslich in `server.py` (`_validate_select_sql`), nicht durch MySQL-Rechte: erlaubt sind nur einzelne SELECT/SHOW/EXPLAIN/DESCRIBE-Statements ohne `;`-Verkettung, eine Blacklist blockt zusätzlich Schreib-Befehle (INSERT/UPDATE/DELETE/DROP/...) sowie dateisystemnahe Funktionen (`LOAD_FILE`, `INTO OUTFILE`/`DUMPFILE`) und potenzielle DoS-Funktionen (`SLEEP`, `BENCHMARK`). Das Passwort wird erst auf dem Server gelesen und in die Umgebungsvariable `MYSQL_PWD` des `mysql`-Prozesses gesetzt (`MYSQL_PWD="$(cat /etc/psa/.psa.shadow)" mysql ...`): Es steht damit weder in der Kommandozeile der Remote-Shell noch im `mysql`-argv (beides für alle lokalen User in `/proc` lesbar) und verlässt den Server nie; `/proc/<pid>/environ` ist nur für root lesbar.

**Bekannte Einschränkung von `run_diagnostic`:** Die Wort-Blacklist (restart, stop, start, kill, rm, add, del, set, reset, on, off, update, sh, ...) matcht auch dann, wenn das Wort z.B. in einem Suchmuster für `find`/`grep` oder in einem Optionscluster vorkommt (z.B. `find /var/log -iname "*kill*"` oder `du -sh` werden blockiert; `du -s -h` geht). Für den Anwendungsfall "Fehler/Support-Tickets diagnostizieren" ist das unkritisch, da die dedizierten Tools (`check_oom_kills`, `search_log`) die relevanten Fälle direkt abdecken. Die Woerter add/del/set/reset/on/off/update/start wurden nachtraeglich ergaenzt, nachdem aufgefallen ist, dass `plesk bin dns --add/--del/--set/...` darueber ausfuehrbar waren - ohne `confirm=true`-Absicherung und entgegen dem "read-only"-Anspruch des Tools. Fuer DNS-Aenderungen ausschliesslich `dns_add_record`/`dns_delete_record`/`dns_update_record` verwenden.

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
| `write_vhost_file` | Schreibt/überschreibt eine Datei im Vhost-Verzeichnis - **erfordert `confirm=true`**, ausführbare Dateien nur mit `allow_executable=true`, geschützte Bereiche (`conf`, `logs`, `.ssh`, ...) abgelehnt, legt vorher automatisch ein Backup an, setzt Besitzer/Rechte passend zur Subscription (nie root) |
| `replace_in_vhost_file` | Ersetzt eine exakte Textstelle in einer Vhost-Datei, ohne die ganze Datei neu zu senden - **erfordert `confirm=true`** (`.php`/`.htaccess` zusätzlich `allow_executable=true`), Backup vorher, atomar, Besitzer/Rechte bleiben (siehe "Textstellen ersetzen") |
| `replace_in_vhost_files` | Wie `replace_in_vhost_file` für bis zu 50 Dateien (`paths` oder `glob` unter `httpdocs/`), alles-oder-nichts - **erfordert `confirm=true`** |
| `upload_begin` | Startet einen Datei-Upload (Upload-URL + Einmal-Token) - **erfordert `confirm=true`**, siehe "Datei-Upload" |
| `upload_status` | Status eines Uploads (pending/uploading/completed/expired/failed) mit path, size, sha256 |
| `fetch_to_vhost` | Server lädt eine Datei selbst von einer https-URL (SSRF-geschützt) - **erfordert `confirm=true`** |
| `list_vhost_dir` | Listet ein Vhost-Verzeichnis (Typ, Grösse, Rechte, Besitzer, Datum), max. Tiefe 3 / 500 Einträge |
| `move_vhost_file` | Verschiebt/benennt atomar um, nur innerhalb desselben Docroots - **erfordert `confirm=true`** |
| `delete_vhost_file` | Löscht in den Papierkorb (oder `permanent=true`), Ordner zweistufig mit Dry-Run und `delete_token` - **erfordert `confirm=true`** |
| `restore_vhost_trash` | Stellt einen Papierkorb-Eintrag wieder her (überschreibt nie) - **erfordert `confirm=true`** |
| `empty_vhost_trash` | Leert Papierkorb-Einträge, die älter als N Tage sind - **erfordert `confirm=true`** |
| `delete_vhost_backup` | Löscht eine von `write_vhost_file`/`replace_in_vhost_file(s)` angelegte `.bak-*`-Datei - **erfordert `confirm=true`** |
| `wp_option_update` | Ändert eine beliebige WordPress-Option per WP-CLI als Subscription-User - Teilschlüssel (`keys`) oder ganzer Wert (`value`); ohne `confirm=true` nur Vorschau, mit `confirm=true` vorher JSON-Backup, Rückgabe mit `backup_id` |
| `wp_option_rollback` | Spielt ein Backup von `wp_option_update` zurück (`backup_id`) - ohne `confirm=true` nur Vorschau |
| `wp_custom_css_show` | Zeigt das "Zusätzliche CSS" des WordPress-Customizers (aktives Theme oder `stylesheet`) - rein lesend |
| `wp_custom_css_update` | Setzt das "Zusätzliche CSS" des Customizers per WP-CLI als Subscription-User - `mode` `replace`/`append`; ohne `confirm=true` nur Vorschau mit Diff, mit `confirm=true` vorher JSON-Backup, Rückgabe mit `backup_id` |
| `wp_custom_css_rollback` | Spielt ein Backup von `wp_custom_css_update` zurück (`backup_id`) - ohne `confirm=true` nur Vorschau |
| `wp_comment_list` | Listet WordPress-Kommentare nach Status (`hold` = wartend, `approve`, `spam`, `trash`, `all`) mit Zählern - rein lesend |
| `wp_comment_moderate` | Moderiert Kommentare per WP-CLI (`spam`, `trash`, `approve`, `unapprove`, `unspam`, `untrash`, `delete`) - ohne `confirm=true` nur Vorschau, mit `confirm=true` Ausführung und Statuskontrolle |
| `wp_plugin_list` | Listet die Plugins einer Installation mit Status, Version und verfügbarem Update (`only_updates=true` nur mit Update) - rein lesend |
| `wp_plugin_update` | Aktualisiert Plugins per WP-CLI (`plugins` oder `all_updates=true`, optional `version` für genau ein Plugin) - ohne `confirm=true` nur Vorschau alt/neu, mit `confirm=true` vorher tar.gz-Backup, Kontrolle von Versionen und Startseite, bei neuem 5xx automatisch zurück; Rückgabe mit `backup_id` |
| `wp_plugin_rollback` | Stellt Plugins aus einem Backup von `wp_plugin_update` wieder her (alle oder `plugins`) - ohne `confirm=true` nur Vorschau, sichert vorher den aktuellen Stand |
| `wp_theme_list` | Listet die Themes einer Installation mit Status (`active`/`parent`/`inactive`), Version und verfügbarem Update (`only_updates=true` nur mit Update) - rein lesend |
| `wp_theme_update` | Aktualisiert Themes per WP-CLI (`themes` oder `all_updates=true`, optional `version` für genau ein Theme) - ohne `confirm=true` nur Vorschau alt/neu, mit `confirm=true` vorher tar.gz-Backup, Kontrolle von Versionen und Startseite, bei neuem 5xx automatisch zurück; Hinweis bei aktivem Child-Theme; Rückgabe mit `backup_id` |
| `wp_theme_rollback` | Stellt Themes aus einem Backup von `wp_theme_update` wieder her (alle oder `themes`) - ohne `confirm=true` nur Vorschau, sichert vorher den aktuellen Stand |
| `wc_coupon_list` / `wc_coupon_get` | WooCommerce-Gutscheine auflisten (Suche, Status) bzw. einen per ID oder Code anzeigen - rein lesend |
| `wc_coupon_create` | Gutschein anlegen (Code, `percent`/`fixed_cart`/`fixed_product`, Betrag, optionale Felder) - ohne `confirm=true` nur Vorschau inkl. Prüfung, ob der Code existiert |
| `wc_coupon_update` / `wc_coupon_rollback` | Felder eines Gutscheins ändern (Vorschau alt/neu, Backup, Kontrolle) bzw. ein Backup zurückspielen |
| `wc_coupon_delete` | Gutschein in den Papierkorb (`permanent=true` endgültig) - mit Backup, ohne `confirm=true` nur Vorschau |
| `wc_order_get` | WooCommerce-Bestellung anzeigen (Status, Adressen, Positionen, Sendungsverfolgung, Sendungsnummer des Label-Plugins) - rein lesend |
| `wc_order_mark_shipped` | Bestellung als verschickt markieren: Sendungsnummer per Shipment-Tracking-Plugin eintragen (leer = aus dem Label-Plugin übernehmen) und Status auf "Abgeschlossen" - ohne `confirm=true` nur Vorschau |
| `wc_order_create` | WooCommerce-Bestellung anlegen (Rechnungs-/Lieferadresse, Positionen mit optionalem Preis, Versandart, Status, Kundenmails unterdrücken, Notizen) - ohne `confirm=true` nur Vorschau inkl. Versandarten des Shops |
| `wc_stock_get` | Lagerbestand eines Produkts oder einer Variante (per ID oder Artikelnummer), bei variablen Produkten alle Varianten - rein lesend |
| `wc_stock_update` | Lagerbestand setzen (`quantity`) oder erhöhen/verringern (`change`), optional mit erwartetem Bestand und Grund - ohne `confirm=true` nur Vorschau, mit Audit-Log |
| `hosting_settings` | Zeigt PHP-Handler (Version, Typ), PHP-Einstellungen (`memory_limit`, Upload-Limits, PHP-FPM-Werte ...), Hosting-Angaben aus `plesk bin site --info` (SSL, HTTPS-Redirect, Docroot) und alle aktivierten PHP-Handler - read-only |
| `set_php_handler` | Stellt den PHP-Handler einer oder mehrerer Domains (max. 50, IDN als Punycode oder Unicode) um (`plesk bin site --update -php_handler_id`), nur aktivierte Handler - ohne `confirm=true` nur Vorschau, danach Kontrolle in der Plesk-DB |
| `php_settings_show` | PHP-Einstellungen einer Domain: Abschnitte der Plesk-Ausgabe, eigene Werte und Block "Additional directives" wörtlich aus der Plesk-DB - read-only |
| `php_settings_update` | Ändert freigegebene PHP-Werte (Allowlist) - ohne `confirm=true` nur Vorschau, Backup mit `backup_id`, Prüfungen danach mit automatischem Zurückstellen, siehe "PHP-Einstellungen ändern" |
| `php_settings_rollback` | Stellt einen Stand aus einem `php_settings_update`-Backup wieder her (`backup_id`) - ohne `confirm=true` nur Vorschau |
| `web_server_settings_show` | Webserver-Einstellungen einer Domain (Expires-Header, nginx-Auslieferung statischer Dateien, nginx-Cache) - read-only |
| `web_server_settings_update` | Ändert freigegebene Webserver-Einstellungen für Speed (Browser-Caching, nginx-Cache) und zusätzliche nginx-Anweisungen (nur `add_header`/`expires`) - ohne `confirm=true` nur Vorschau, Backup, Kontrolle mit automatischem Zurückstellen |
| `web_server_settings_rollback` | Stellt Webserver-Einstellungen aus einem Backup wieder her |
| `optimize_images` | Optimiert JPG/PNG einer Domain verlustfrei und entfernt GPS-Daten/Seriennummern - Standard Probelauf, ausführen mit `dry_run=false` und `confirm=true`; `hintergrund=true` startet einen Job für den ganzen Pfad, `dateien=[...]` bearbeitet einzelne Bilder |
| `image_job` | Status (Fortschritt, Ergebnis mit blätterbaren Listen) und Abbruch eines Hintergrundjobs von `optimize_images` - `cancel` ohne `confirm=true` nur Vorschau |
| `image_backups` | Listet, wiederherstellt oder löscht die Backups von `optimize_images` |
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
| `run_diagnostic` | Generischer Fallback, nur Whitelist an read-only Befehlen mit Positiv-Listen je Utility (siehe "Sicherheit"), läuft unter `timeout` mit 4-MB-Ausgabegrenze |

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
- Nur mit gesetzter `PUBLIC_BASE_URL` verfügbar.

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

## Textstellen ersetzen

`replace_in_vhost_file(domain, path, old_str, new_str, replace_all=false, confirm=true, allow_executable=false)` ersetzt eine Textstelle in einer Datei unter `/var/www/vhosts/<domain>/`, ohne dass die ganze Datei neu gesendet werden muss (z.B. eine Zeile in `wp-config.php` - als `.php`-Datei mit `allow_executable=true`).

- `old_str` muss exakt vorkommen (inkl. Leerzeichen und Einrückung), `new_str` darf leer sein (= Textstelle löschen).
- Kein Treffer: Fehler mit Hinweis. Mehr als ein Treffer ohne `replace_all=true`: Fehler mit Trefferzahl, nichts wird geändert.
- Nur UTF-8-Textdateien bis 5 MB (`REPLACE_MAX_BYTES`). Dateien mit Nullbytes oder nicht dekodierbare Dateien werden abgelehnt.
- Encoding und Zeilenenden bleiben erhalten. In reinen CRLF-Dateien wird ein `\n` in `old_str`/`new_str` automatisch als `\r\n` behandelt.
- Pfade und Sicherheitsregeln wie `write_vhost_file`/`upload_begin`: `domain` muss die Hauptdomain einer Subscription sein, relativ, kein `..`, keine absoluten Pfade, geschützte Bereiche (`conf`, `logs`, `.ssh`, Dotfiles im Vhost-Root, ...) abgelehnt, ausführbare Dateien (`.php`, `.htaccess`, `.user.ini`, ...) nur mit `allow_executable=true` (bei `replace_in_vhost_files` auch als glob-Treffer), Abbruch bei Symlinks (Datei oder Ordner im Pfad), keine root-eigenen Dateien ausserhalb von `httpdocs`. Besitzer, Gruppe und Rechte der Datei bleiben erhalten (eine root-eigene Datei unter `httpdocs` wird wie bei `write_vhost_file` auf den Subscription-User umgestellt).
- Vor dem Schreiben wird ein Backup `<path>.bak-<YYYYMMDDHHMMSS>` mit dem bisherigen Inhalt angelegt (aufräumen mit `delete_vhost_backup`). Bei mehreren Änderungen in derselben Sekunde erhält jedes Backup den nächsten freien Zeitstempel, damit kein Zwischenstand verloren geht. Danach wird atomar ersetzt (Temp-Datei im selben Ordner, `rename`), unmittelbar davor wird geprüft, dass die Datei seit dem Lesen nicht verändert wurde.
- `old_str`/`new_str` gehen per stdin an den Helper, nicht über die Kommandozeile (nicht in der Prozessliste des Servers sichtbar).
- Rückgabe (JSON): Anzahl Ersetzungen, Backup-Pfad, neue und alte Grösse, Besitzer/Rechte und je Treffer eine Kontextzeile vorher/nachher (max. 120 Zeichen, max. 50 Treffer).

`replace_in_vhost_files(domain, old_str, new_str, paths=[...] | glob="...", replace_all=false, confirm=true, allow_executable=false)` macht dasselbe für mehrere Dateien:

- Entweder `paths` (max. 50 relative Pfade) oder `glob`. `glob` ist relativ zu `/var/www/vhosts/<domain>/`, muss mit `httpdocs/` beginnen und darf höchstens 50 Dateien treffen (`*`, `?`, `[...]` pro Ordnerebene, `**` für beliebig viele Ebenen, z.B. `httpdocs/wp-content/themes/mein-theme/**/*.php`). Symlinks werden dabei nicht verfolgt (nur im Ergebnis aufgelistet), `.bak-*`-Backups und der Papierkorb werden ignoriert.
- Dateien ohne Treffer werden übersprungen und unter `ohne_treffer` aufgelistet.
- Alles-oder-nichts: Zuerst werden alle Dateien gelesen und geprüft, dann alle Temp-Dateien und Backups geschrieben und erst danach umbenannt. Ein Fehler in einer Datei (z.B. Binärdatei, Mehrfachtreffer ohne `replace_all`, Symlink) lässt alle Dateien unverändert.

Beispiel:

```text
replace_in_vhost_file(domain="example.com", path="httpdocs/wp-config.php",
                      old_str="define( 'WP_DEBUG', false );",
                      new_str="define( 'WP_DEBUG', true );", confirm=true, allow_executable=true)
```

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

**Audit-Log**: Jede Aktion (Upload-Start, Upload, Fetch, Move, Delete, Restore, Papierkorb leeren, Textersetzung) schreibt eine JSON-Zeile mit Zeitpunkt, Domain, Pfaden, Grösse, sha256 (wo vorhanden) und Ergebnis nach stderr (`docker logs`, Präfix `AUDIT`) und optional in `AUDIT_LOG_FILE`. Keine Tokens, Passwörter oder Dateiinhalte.

## Beispiel: PHP-Version einer Domain umstellen

1. `hosting_settings(domain="example.com")` zeigt den aktuellen Handler (z.B. `plesk-php82-fpm-dedicated`, PHP 8.2, Typ `fpm`) und die Liste der auf dem Server aktivierten Handler.
2. `set_php_handler(handler_id="plesk-php83-fpm-dedicated", domain="example.com")` ohne `confirm` liefert nur eine Vorschau: alter und neuer Handler, ob sich PHP-Version und/oder Typ (fpm/fastcgi/cgi) ändern.
3. Mit `confirm=true` wird `plesk bin site --update example.com -php_handler_id plesk-php83-fpm-dedicated` ausgeführt und anschliessend der Handler in der Plesk-Datenbank kontrolliert. Die Rückgabe nennt den alten Handler zum Zurücksetzen.
4. Mehrere Domains in einem Aufruf: `set_php_handler(handler_id="plesk-php84-fpm-dedicated", domains=["example.com", "example.org", "xn--kche-0ra.example"], confirm=true)`. Zuerst werden alle Domains geprüft (unbekannte Domain: nichts wird geändert), dann wird jede einzeln umgestellt und kontrolliert. Schlägt eine fehl, werden die übrigen trotzdem umgestellt und die fehlgeschlagenen unter `fehlgeschlagen` gemeldet.

Nicht aktivierte oder unbekannte Handler werden abgelehnt (mit Liste der aktivierten). Die Umstellung gilt nur für die angegebene Domain, nicht für weitere Domains derselben Subscription. Timeout über `PLESK_HOSTING_TIMEOUT` (Standard 180 Sekunden). Jede Umstellung landet im Audit-Log.

## PHP-Einstellungen ändern

`php_settings_show(domain)`, `php_settings_update(domain, settings={...}, additional_directives={...}, confirm=true)` und `php_settings_rollback(domain, backup_id, confirm=true)` lesen und ändern die PHP-Einstellungen einer Domain wie in Plesk unter "PHP-Einstellungen". Beispiel: `php_settings_update(domain="example.com", settings={"memory_limit": "512M", "pm.max_children": "20"}, additional_directives={"max_input_vars": "5000"})`.

**Stand lesen**: `plesk bin site --show-php-settings` zeigt eigene und geerbte Werte gemischt und lässt im Block "Additional directives" Abschnittsüberschriften wie `[php-fpm-pool-settings]` weg. Die Tools lesen deshalb zusätzlich aus der Plesk-Datenbank (`plesk db`): die eigenen Werte der Domain (`PhpSettingsParameters` über `dom_param.phpSettingsId`) und den Additional-Block wörtlich (`PhpSettingsCustom`). Domains ohne eigene Werte erben alles vom Service-Plan.

**Schreiben**: `plesk bin site --update-php-settings <domain> -settings <datei>` für Plesk-Standardfelder (geschrieben werden nur die geänderten Werte), `-additional-settings <datei>` für den Additional-Block. Der Block wird vollständig neu geschrieben: Neue Schlüssel kommen in den php.ini-Teil vor dem ersten Abschnitt, bestehende Zeilen werden an ihrer Stelle ersetzt, alle anderen Zeilen und Abschnitte bleiben wörtlich erhalten. Die Übergabedatei wird als root mit `0600` in `/root/.plesk-mcp/tmp` (Verzeichnis `0700`, nicht im Vhost des Kunden) angelegt und nach dem Aufruf immer gelöscht.

**Verhalten von `-settings`**: Ob Plesk nur die genannten Werte übernimmt oder alle eigenen Werte ersetzt, ist noch nicht auf dem Server geprüft. Das Tool ist für beide Fälle sicher: Fehlen nach dem Schreiben andere eigene Werte, schreibt es den kompletten Sollstand. Die Rückgabe zeigt unter `schreibvorgang`, wie Plesk sich verhalten hat (`settings_schreibvorgaenge` 1 = zusammenführend, 2 = vollständig nachgeschrieben, dazu die abweichenden Werte nach dem ersten Schreiben). Bestätigt ist bisher (Test auf einer Testdomain): Der Additional-Block bleibt samt Abschnittsüberschriften erhalten, neue Zeilen landen in `php.ini`, Pool-Direktiven bleiben in `php-fpm.conf`; nach Update und Rollback waren alle eigenen Werte unverändert. Das Ergebnis für `-settings` wird hier nachgetragen.

**Allowlist** (je Wert mit Formatprüfung, erweiterbar per `PHP_SETTINGS_ALLOWLIST`, kommagetrennt, dann generische Wertprüfung): `open_basedir`, `memory_limit`, `max_execution_time`, `max_input_time`, `max_input_vars`, `post_max_size`, `upload_max_filesize`, `realpath_cache_size`, `realpath_cache_ttl`, `opcache.memory_consumption`, `opcache.interned_strings_buffer`, `opcache.max_accelerated_files`, `opcache.revalidate_freq`, `opcache.validate_timestamps`, `opcache.enable_file_override`, `pm`, `pm.max_children`, `pm.max_requests`, `pm.start_servers`, `pm.min_spare_servers`, `pm.max_spare_servers`.

- Nie erlaubt, auch nicht per ENV: `disable_functions`, `auto_prepend_file`, `auto_append_file`, `allow_url_include`, `extension`, `zend_extension`, `sendmail_path`, `enable_dl`, `include_path`, `doc_root`, `user_dir`, `session.save_path`, `error_log`. Bestehende Werte solcher Direktiven bleiben unverändert stehen.
- `open_basedir` nur `none` oder der Plesk-Standard `{WEBSPACEROOT}{/}{:}{TMP}{/}`. `none` nur, wenn `cagefsctl --user-status <system-user>` genau `Enabled` liefert, sonst Ablehnung.
- Schlüssel und Werte ohne Zeilenumbrüche, `;`, eckige Klammern und Anführungszeichen. Plesk-Standardfelder (laut Plesk-Ausgabe der Domain, inkl. `pm.*`) nur über `settings`, alles andere nur über `additional_directives`. `pm.*` gibt es nur bei PHP-FPM-Handlern.
- Jede Ablehnung nennt Schlüssel und Regel. Pro Domain läuft immer nur eine Änderung gleichzeitig.

**Ablauf mit `confirm=true`**: Backup des kompletten Stands (eigene Werte, Additional-Block wörtlich, angezeigte Werte) als JSON unter `PHP_SETTINGS_BACKUP_DIR` (Standard `/root/.plesk-mcp/php-settings-backups/<backup_id>.json`, `0600`). Das Schema und die `backup_id` (`<YYYYMMDDHHMMSS>-<hex>@<domain>`) entsprechen den wp-option-Backups. Der Ablageort ist bewusst root-only statt im Home der Subscription, weil der Inhalt beim Rollback als root an Plesk übergeben wird und der Kunde ihn nicht verändern darf. Danach wird geschrieben und geprüft:

1. Plesk-DB: eigene Werte und Additional-Block entsprechen dem Soll.
2. `--show-php-settings` zeigt die neuen Werte.
3. Der PHP-FPM-Dienst der Domain ist aktiv (falls vorher aktiv, ein Wiederholversuch nach 3 Sekunden).
4. Die Startseite `https://<domain>/` liefert keinen 5xx (übersprungen, wenn sie schon vorher 5xx lieferte oder nicht abrufbar war).

Schlägt eine Prüfung fehl, stellt das Tool automatisch auf das Backup zurück und meldet, welche Prüfung fehlgeschlagen ist. Werte, die vorher geerbt waren, kann Plesk per CLI nicht wieder auf "geerbt" stellen: Sie werden auf den vorherigen Wert gesetzt und bleiben eigene Werte (wird gemeldet). `php_settings_rollback` arbeitet gleich, legt vorher selbst ein Backup des aktuellen Stands an und prüft danach dasselbe. Jede Änderung landet im Audit-Log (Zeit, Domain, alt/neu, `backup_id`, Ergebnis).

## Speed: Browser-Caching und nginx-Cache

`web_server_settings_show(domain)`, `web_server_settings_update(domain, settings={...}, confirm=true)` und `web_server_settings_rollback(domain, backup_id, confirm=true)` lesen und ändern die Webserver-Einstellungen einer Domain (Haupt-, Sub- oder Zusatzdomain) über `plesk bin domain --show-web-server-settings` bzw. `--update-web-server-settings <domain> -<option> <wert>`.

- Freigegeben (nur einfache, geprüfte Werte): `web-server-expires` (Sekunden, z.B. `2592000` = 30 Tage), `web-server-expires-static-only`, `nginx-serve-static`, `nginx-cache-enabled`, `nginx-cache-size` (Bytes), `nginx-cache-timeout` (Sekunden), `nginx-cache-bypass-header-nocache`, `nginx-cache-bypass-header-auth`, `nginx-cache-bypass-get-nocache`, `nginx-cache-use-stale-5xx`, `nginx-cache-use-stale-4xx`, `nginx-cache-use-stale-updating` (`true`/`false`). Dazu `nginx-directives` (siehe unten). Nicht freigegeben sind die übrigen Optionen mit freiem Format (Cache-Key, Cookie-Listen, Bypass-Pfade, zusätzliche Apache-Direktiven).
- Ohne `confirm=true` nur Vorschau. Mit `confirm=true`: Backup des kompletten Stands (root-only unter `WEB_SETTINGS_BACKUP_DIR`, Standard `/root/.plesk-mcp/web-server-backups`), Schreiben, Kontrolle (Werte laut Plesk, Startseite ohne 5xx), bei Fehler automatisch zurück.
- nginx-Cache nur bei eher statischen Seiten: Bei Shops, Logins oder Warenkörben können veraltete oder fremde Inhalte ausgeliefert werden.
- Kompression: Brotli ist auf dem Server serverweit über `/etc/nginx/conf.d/brotli.conf` aktiv; das ist nicht Teil dieser Tools.

### Zusätzliche nginx-Anweisungen (`nginx-directives`)

Die Option `nginx-directives` setzt den Inhalt von Plesk "Zusätzliche nginx-Anweisungen" einer Domain (Datei `vhost_nginx.conf`). Der Wert ist der komplette Inhalt; `web_server_settings_show` zeigt den aktuellen Stand unter `additional nginx settings`.

```
web_server_settings_update(domain="example.com",
    settings={"nginx-directives": "add_header X-Robots-Tag \"noindex, nofollow\" always;"},
    confirm=true)
```

- Whitelist: erlaubt sind nur `add_header <Name> <Wert> [always];`, `expires [modified] <Zeit>;` und `location`-Blöcke, die ausschliesslich `add_header`/`expires` enthalten (keine verschachtelten `location`), dazu Kommentare und Leerzeilen. Alles andere wird abgelehnt, z.B. `proxy_pass`, `include`, `root`, `alias`, Lua-Direktiven, `return`, `rewrite`, `auth_*`, `ssl_*`. Die Prüfung arbeitet auf nginx-Tokens (Strings in Anführungszeichen, `;`, `{`, `}`), mehrere Anweisungen auf einer Zeile werden also einzeln geprüft.
- `mode="replace"` (Standard) ersetzt den kompletten Inhalt. `mode="append"` hängt den Wert an den bestehenden Inhalt an; geprüft wird dabei nur der neue Teil, der bestehende Inhalt bleibt unverändert. Ist der Wert dort schon als ganze Zeile(n) enthalten, wird nichts geändert.
- Bestehender Inhalt wird nie stillschweigend überschrieben: Vorschau und Ergebnis enthalten alt, neu und einen `diff`; entfernt `replace` bestehende Zeilen, steht zusätzlich ein `hinweis` dabei.
- Plesk kann die zusätzlichen nginx-Anweisungen per CLI nur anzeigen, nicht setzen. Das Tool schreibt deshalb direkt in `/var/www/vhosts/system/<domain>/conf/vhost_nginx.conf`. Der Domainname wird strikt geprüft (nur gültige Hostnamen, kein `..`, kein `/`), geschrieben wird nur in diesen `conf`-Ordner, nie durch Symlinks.
- `nginx-directives` nur allein setzen, nicht zusammen mit anderen Optionen im selben Aufruf.
- Ablauf mit `confirm=true`:
  1. `nginx -t` vorher (schlägt der Test schon vor der Änderung fehl, wird nichts geändert)
  2. Backup des alten Dateistands (`backup_id` für `web_server_settings_rollback`)
  3. Datei atomar schreiben: Temp-Datei im selben Ordner, Besitzer/Rechte wie die bestehende Datei (neu: `root:nginx`, `0600`, wie Plesk sie anlegt), dann `rename`
  4. `nginx -t` - schlägt das fehl, wird die alte Datei zurückgeschrieben, nichts neu geladen und der Fehler gemeldet. Ausnahme: Wird die Datei entfernt (Rollback auf einen Stand ohne Datei), entfällt dieser Test, weil die alte Konfiguration bis zum Reconfigure noch per `include` auf die Datei verweist; geprüft wird dann nach dem Reconfigure.
  5. erst danach `plesk sbin httpdmng --reconfigure-domain <domain>` (übernimmt die Datei in die Plesk-Konfiguration und lädt nginx neu)
  6. Kontrolle: Datei hat den geschriebenen Inhalt, nochmals `nginx -t` (Plesk bindet `vhost_nginx.conf` nur ein, wenn die Datei beim Generieren existiert), Startseite ohne 5xx. Bei einem Fehler: alte Datei zurück, erneut Reconfigure, Fehler melden.
- Wurde nichts geschrieben (z.B. Schreibfehler, Datei unverändert), läuft kein Rollback; das Ergebnis lautet dann "FEHLER - nichts geändert". Dasselbe gilt für die übrigen Optionen, wenn Plesk eine Änderung ablehnt und nichts übernimmt.
- `web_server_settings_rollback` mit einer `backup_id` einer `nginx-directives`-Änderung schreibt den damaligen Dateistand zurück (gab es die Datei damals nicht, wird sie entfernt), mit demselben Ablauf aus `nginx -t` und Reconfigure.

## Bilder verlustfrei optimieren

`optimize_images(domain, path="", dry_run=true, confirm=false, max_files=500, recursive=true, keep_backup=true, weiter_ab="", hintergrund=false, dateien=[])` verkleinert JPG- und PNG-Dateien ohne Qualitätsverlust (gleiche Pixel). `domain` ist die Hauptdomain der Subscription, `path` ein Ordner oder eine Datei relativ zu `/var/www/vhosts/<domain>/` innerhalb eines Docroots; leer = Docroot der Hauptdomain (die Rückgabe nennt ihn unter `pfad`). Ein ungültiger Pfad wird mit der Liste aller gültigen Docroots abgelehnt.
Statt `path` kann `dateien` eine Liste einzelner Bilder sein (relative Pfade wie `path`, höchstens 200, `path` dann leer), z.B. die Timeout-Dateien eines früheren Laufs; jeder Eintrag wird gleich streng geprüft (relativ, kein `..`, keine Wildcards, in einem Docroot), ein ungültiger Eintrag lehnt den ganzen Aufruf ab. Symlinks in einem Pfadteil, nicht vorhandene oder nicht-Bild-Dateien stehen unter `uebersprungen_details` (`symlink`, `nicht_gefunden`, `kein_bild`), der Lauf geht mit den übrigen Dateien weiter.

- Pro Bild wird eine Arbeitskopie unter `.plesk-mcp/tmp/<run_id>/` im Vhost bearbeitet (ausserhalb der Webordner, nicht per Web erreichbar; nach dem Lauf gelöscht, Reste abgebrochener Läufe nach einem Tag): zuerst werden GPS-Daten, Kamera- und Objektiv-Seriennummern, Besitzername und Herstellerdaten (MakerNote) aus EXIF und XMP entfernt (Datenschutz), Farbprofil, Ausrichtung und alle übrigen Metadaten bleiben. Danach `jpegoptim -p` (ohne Qualitätsstufe, nur verlustfreie Optimierung) bzw. `optipng -o2 -preserve` (PNGs ab 1 MB mit `-o1`, ebenfalls verlustfrei, aber deutlich schneller - sonst laufen grosse PNGs oft ins Timeout). Ersetzt wird nur, wenn die Datei kleiner wird oder Datenschutzdaten entfernt wurden; Änderungsdatum und Rechte bleiben.
- Der Worker läuft per `runuser` als System-User der Subscription (nie root), folgt keinen Symlinks, überspringt Hardlinks, Dateien über 50 MB, den Papierkorb und `.plesk-mcp`. Programme: `IMAGE_JPEGOPTIM` (Standard `/usr/bin/jpegoptim`), `IMAGE_OPTIPNG` (Standard `/usr/bin/optipng`); fehlt eines, wird der Dateityp übersprungen. Timeout ohne Fortschritt: `IMAGE_OPT_TIMEOUT` (Standard 300 Sekunden).
- `dry_run=true` (Standard) rechnet die Ersparnis aus und ändert nichts. Ausführen mit `dry_run=false` und `confirm=true`.
- Auch der Probelauf führt `jpegoptim`/`optipng` tatsächlich aus (auf Kopien) und braucht ähnlich viel Zeit wie ein echter Lauf.
- Damit kein Aufruf den 60-Sekunden-Timeout des MCP-Clients überschreitet: Zeitbudget von 30 Sekunden ab Start des Workers inkl. Einlesen (`IMAGE_OPT_TIME_BUDGET`), geprüft vor jeder Datei; die laufende Datei wird noch fertig bearbeitet. Jeder Aufruf von `jpegoptim`/`optipng` hat ein eigenes Timeout von 15 Sekunden (`IMAGE_OPT_TOOL_TIMEOUT`), danach bleibt die Datei unverändert und wird unter `uebersprungen.timeout` gezählt; `timeout_hinweis` nennt die betroffenen Dateien als fertige `dateien=[...]`-Liste für einen erneuten Versuch mit `hintergrund=true`.
- Ist nach Zeitbudget oder `max_files` noch nicht alles bearbeitet, enthält die Rückgabe `"unvollstaendig": true`, `"weiter_ab": "<letzte bearbeitete Datei>"` und einen Hinweis. Mit `weiter_ab=<Wert>` und sonst gleichen Parametern erneut aufrufen, bis `"unvollstaendig": false`. Die Bilder werden in fester, alphabetischer Pfad-Reihenfolge bearbeitet, jedes genau einmal; echte Teilläufe bekommen je eine eigene `backup_run_id`.
- Der Probelauf legt in den Webordnern nichts an und ändert dort nichts (auch kein Änderungsdatum eines Ordners). Arbeitskopien liegen unter `.plesk-mcp/tmp/` (ist das nicht beschreibbar: privates Temp-Verzeichnis des Users). Nur im echten Lauf und nur wenn die Arbeitskopie auf einem anderen Dateisystem liegt, entsteht für den Moment des atomaren Ersetzens eine Kopie im Zielordner, die bei jedem Fehler wieder entfernt wird.
- Nicht als `fehler`, sondern unter `uebersprungen` gezählt und mit Datei und Meldung unter `uebersprungen_details` aufgeführt: `format_passt_nicht_zur_endung` (z.B. JPEG oder ICO mit Endung `.png`), `werkzeug_abgelehnt` (z.B. abgeschnittene Datei oder ungültiger PNG-Chunk; Meldung mit Werkzeug, Exit-Code und dessen Ausgabe), `timeout`. `fehler` enthält nur noch echte Fehler, immer mit Text (bei Python-Ausnahmen mit Typ).
- Ordner, in denen der System-User der Subscription nicht schreiben darf, werden übersprungen (`uebersprungen.keine_schreibrechte`, Ordner und Besitzer unter `ohne_schreibrechte`) - schon im Probelauf, damit er den echten Lauf vorhersagt.
- Verwaiste Arbeitskopien früherer Versionen im Zielpfad (`.<name>.mcp-xxxxxxxx.<ext>`, von optipng auch `….bak`) stehen unter `verwaiste_temp_dateien`. Gelöscht werden sie nur im echten Lauf, nur bei genau diesem Namensschema, als normale Datei des Subscription-Users und wenn sie älter als 1 Stunde sind (die Sperre pro Domain verhindert parallele Läufe, ein Lauf dauert nur Minuten). Andere `.mcp-`-Dateien werden nur gemeldet.
- Die Listen `groesste_ersparnisse`, `uebersprungen_details` und `fehler` zeigen im Vordergrund höchstens 50 Einträge; `listen_gesamt` nennt die Gesamtzahl je Liste und `gekuerzt` sagt, ob gekürzt wurde (vollständige Listen liefert ein Hintergrundjob über `image_job`).
- Läuft für eine Domain bereits eine Bildoptimierung (Vordergrund oder Hintergrundjob), wird ein zweiter Aufruf mit "Für <domain> läuft bereits eine Bildoptimierung - bitte warten und erneut versuchen." abgelehnt; das gilt auch für `image_backups` mit `restore`/`delete`.
- Mit `keep_backup=true` (Standard) bleiben die Originale per Hardlink unter `.plesk-mcp/image-backups/<run_id>/` im Vhost (ausserhalb jedes Docroots). Der Speicher wird erst frei, wenn das Backup gelöscht ist: `image_backups(domain, action="list"|"restore"|"delete", run_id, confirm=true)`.

### Hintergrundmodus

Grosse Dateien laufen im Vordergrund in den 15-Sekunden-Timeout, weil ein Aufruf unter dem 60-Sekunden-Limit des MCP-Clients bleiben muss. `optimize_images(domain, path, dry_run, confirm, hintergrund=true)` kehrt deshalb sofort mit einer `job_id` zurück (= `run_id` des Backups), der Worker läuft entkoppelt weiter:

- Der ganze Pfad (oder die `dateien`-Liste) ist EIN Job: kein Zeitbudget, kein `max_files`, kein `weiter_ab`, EIN Backup für den ganzen Lauf (`backup_run_id` = `job_id`). Pro Aufruf von `jpegoptim`/`optipng` gilt `IMAGE_OPT_BG_TOOL_TIMEOUT` (Standard 300 Sekunden). `dry_run`/`confirm` wie im Vordergrund.
- `image_job(domain, action="status", job_id)` zeigt während des Laufs `fortschritt` (`bearbeitet`/`gesamt`, `aktuelle_datei`, `optimiert`, `ersparnis_bytes`, `uebersprungen`, `fehler`) und `laufzeit_s`. Nach Abschluss (`status` = `fertig`, `abgebrochen` oder `fehler`) liefert derselbe Aufruf das Ergebnis im Format von `optimize_images`. Die drei Listen zeigen die ersten 200 Einträge (`gekuerzt`, Gesamtzahl unter `listen_gesamt`); blättern mit `liste="groesste_ersparnisse"|"uebersprungen_details"|"fehler"`, `ab=<Start>`, `limit=<1..500>`.
- `image_job(domain, action="cancel", job_id)` ohne `confirm=true` nur Vorschau. Mit `confirm=true` schickt der MCP-Server dem Worker über dessen stdin die Zeile `cancel`: das laufende Werkzeug wird beendet, die Arbeitskopie verworfen, bereits ersetzte Bilder bleiben optimiert (Backup unter `backup_run_id`), nicht mehr bearbeitete Dateien stehen unter `uebersprungen.abgebrochen_rest`. Es wird nie ein Prozess anhand einer PID beendet, und es liegt keine Status- oder PID-Datei im Vhost.
- Der Job läuft als Thread im MCP-Container, der die SSH-Sitzung (Keepalive alle 30 Sekunden) bis zum Ende hält; Status und Ergebnis bleiben 24 Stunden im Speicher. Nach einem Neustart des Containers ist die `job_id` unbekannt (`image_job` sagt das ausdrücklich und verweist auf `image_backups(action="list")`); der Worker auf dem Plesk-Server sieht dann EOF auf stdin und bricht auf demselben Weg sauber ab wie bei `cancel`, ebenso bei SIGTERM/SIGHUP. Halb geschriebene Bilder gibt es in keinem Fall: Ersetzt wird atomar, Arbeitskopien liegen unter `.plesk-mcp/tmp/<job_id>/` und werden beim Abbruch entfernt (Reste nach einem harten Kill räumt der nächste Lauf nach einem Tag weg).
- Solange der Job läuft, gilt die Sperre pro Domain: ein Vordergrundaufruf, ein zweiter Job oder `image_backups` mit `restore`/`delete` für dieselbe Domain werden abgelehnt. Insgesamt laufen höchstens 4 Hintergrundjobs gleichzeitig.
- Fortschrittswerte kommen vom Worker, der als Subscription-User läuft; der MCP-Server prüft Typen und Grössen, bevor er sie übernimmt. `job_id` wird wie `run_id` nur nach dem festen Muster akzeptiert und nie in ein Kommando eingesetzt.
- Jeder Jobstart (`image_job_start`), Abschluss (`image_job_done` bzw. `image_job_failed`) und Abbruch (`image_job_cancel`) steht im Audit-Log; `image_job_done` entfällt beim Probelauf.
- Im Hintergrund setzt der Worker sich selbst auf `nice 10` (vererbt an `jpegoptim`/`optipng`) und startet die Werkzeuge mit `ionice -c2 -n7` (wenn `/usr/bin/ionice` vorhanden ist; `prioritaet` in der Rückgabe zeigt beides). **CloudLinux LVE**: Der Worker startet per `runuser`, und `pam_lve` ist auf dem Server nur an `su` und `sshd` gebunden (`grep -l pam_lve /etc/pam.d/runuser /etc/pam.d/su /etc/pam.d/sshd` liefert nur `su` und `sshd`). Der Worker läuft deshalb **ausserhalb** der LVE-Limits des Accounts; nice/ionice sind die einzige Bremse, die Last zählt nicht gegen das Kundenkonto. Prüfen lässt sich das während eines Jobs mit `lveps -p` (der Prozess erscheint dann unter keiner LVE-ID).

Beispiel: `optimize_images(domain="example.com", path="httpdocs/wp-content/uploads", dry_run=false, confirm=true, hintergrund=true)` liefert `job_id` `20260101120000-1a2b3c4d`; `image_job(domain="example.com", action="status", job_id="20260101120000-1a2b3c4d")` zeigt den Fortschritt und später das Ergebnis; Timeouts eines Vordergrundlaufs gezielt nachholen: `optimize_images(domain="example.com", dateien=["httpdocs/wp-content/uploads/2024/gross.png"], dry_run=false, confirm=true, hintergrund=true)`.

## Beispiel: WordPress-Option gezielt ändern

1. Vorschau: `wp_option_update(domain="example.com", wp_path="httpdocs", option_name="wp_rocket_settings", keys={"minify_css": 1, "defer_all_js": 1})` - zeigt alt/neu je Schlüssel, schreibt nichts
2. Ausführen: dasselbe mit `confirm=true` - Rückgabe mit `backup_id` (z.B. `20260101120000-1a2b3c4d@example.com`) und alt/neu je Schlüssel
3. Einfache Option: `wp_option_update(domain="example.com", wp_path="httpdocs", option_name="elementor_font_display", value="swap", confirm=true)`
4. Rückgängig: `wp_option_rollback(backup_id="20260101120000-1a2b3c4d@example.com")` für die Vorschau, dann mit `confirm=true`

Customizer-CSS (Design > Customizer > Zusätzliches CSS):

1. Anzeigen: `wp_custom_css_show(domain="example.com", wp_path="httpdocs")`
2. Vorschau: `wp_custom_css_update(domain="example.com", wp_path="httpdocs", css=".tml .tml-button { background: #ff6600; }", mode="append")` - zeigt den Diff, schreibt nichts
3. Ausführen: dasselbe mit `confirm=true` - Rückgabe enthält `backup_id`
4. Rückgängig: `wp_custom_css_rollback(backup_id="...", confirm=true)`

Spam-Kommentar aus einer "Bitte moderiere"-Mail (`c=295`):

1. Vorschau: `wp_comment_moderate(domain="example.com", wp_path="httpdocs", comment_ids=[295], action="spam")` - zeigt Autor, E-Mail und Inhalt
2. Ausführen: dasselbe mit `confirm=true`
3. Alle wartenden Kommentare sehen: `wp_comment_list(domain="example.com", wp_path="httpdocs", status="hold")`

Plugin-Updates:

1. Verfügbare Updates: `wp_plugin_list(domain="example.com", wp_path="httpdocs", only_updates=true)`
2. Vorschau: `wp_plugin_update(domain="example.com", wp_path="httpdocs", plugins=["contact-form-7"])` bzw. `all_updates=true` für alle - zeigt alt/neu, ändert nichts
3. Ausführen: dasselbe mit `confirm=true` - Rückgabe enthält `backup_id`
4. Rückgängig: `wp_plugin_rollback(backup_id="...", confirm=true)`

Theme-Updates:

1. Verfügbare Updates: `wp_theme_list(domain="example.com", wp_path="httpdocs", only_updates=true)`
2. Vorschau: `wp_theme_update(domain="example.com", wp_path="httpdocs", themes=["twentytwentyfive"])` - zeigt alt/neu, ändert nichts
3. Ausführen: dasselbe mit `confirm=true` - Rückgabe enthält `backup_id`
4. Rückgängig: `wp_theme_rollback(backup_id="20260101120000-1a2b3c4d@example.com", confirm=true)`

Bestellung anlegen (z.B. Musterlieferung):

1. Variations-ID ermitteln: `wc_stock_get(domain="example.com", wp_path="httpdocs", product="<ID des Hauptprodukts>")`
2. Vorschau: `wc_order_create(domain="example.com", billing={"company": "Muster AG", "first_name": "Max", "last_name": "Muster", "address_1": "Musterstrasse 1", "postcode": "3000", "city": "Bern", "country": "CH", "email": "info@example.com"}, items=[{"product_id": 123, "quantity": 1, "price": "0"}], suppress_customer_emails=true, order_note="Muster leihweise")` - listet die Versandarten mit ID
3. Mit `shipping_method="flat_rate:3"`, `shipping_cost="0"` erneut als Vorschau, dann mit `confirm=true` - Rückgabe mit Bestellnummer, Status, Summe und ob Mails unterdrückt wurden

WooCommerce-Gutscheine:

1. Anlegen (Vorschau): `wc_coupon_create(domain="example.com", wp_path="httpdocs", code="SOMMER10", discount_type="percent", amount="10", fields={"date_expires": "2026-12-31", "usage_limit_per_user": 1})`, dann mit `confirm=true`
2. Ändern: `wc_coupon_update(domain="example.com", wp_path="httpdocs", coupon="SOMMER10", fields={"amount": "15", "status": "draft"}, confirm=true)` - Rückgabe mit `backup_id`
3. Entfernen: `wc_coupon_delete(domain="example.com", wp_path="httpdocs", coupon="SOMMER10", confirm=true)` (Papierkorb)

## Beispiel: Imunify-False-Positive in rotierten Logs

Imunify360 meldet gelegentlich "Malware" in rotierten Logdateien, weil dort protokollierte Angriffsversuche stehen (kein ausführbarer Code):

1. `imunify_malware_list(domain="example.com")` - Treffer mit Pfad und Signatur prüfen
2. `imunify_ignore_add(path="/var/www/vhosts/example.com/logs/error_log.1.gz", confirm=true)`
3. `imunify_ignore_list(search="/var/www/vhosts/example.com/")` - Eintrag kontrollieren
4. Rückgängig: `imunify_ignore_remove(path="/var/www/vhosts/example.com/logs/error_log.1.gz", confirm=true)`

## Tests

Die Positiv-Listen von `run_diagnostic` (`tests/test_diagnostic.py`), Bearer-/Startprüfungen (`tests/test_http_auth.py`), SSH-Ausgabegrenze, Deadline und Host-Key-Pinning gegen einen lokalen paramiko-Mini-SSH-Server (`tests/test_ssh_exec.py`), die Pfadvalidierung der Imunify-Tools, URL-Validierung und Auswertung von `pagespeed_insights`, die Validierung von `wp_option_update`, `wp_custom_css_update`, `wp_comment_moderate` und der `wc_coupon_*`-Tools sowie Upload, Fetch (SSRF), Dateiverwaltung, Textersetzung, die PHP-Handler-, PHP-Einstellungs- und Webserver-Tools (mit simuliertem SSH bzw. Plesk) sowie die Bildoptimierung (Worker lokal, Testbilder per Pillow; auch Hintergrundjobs, Abbruch und Verbindungsverlust) sind ohne Plesk-Server testbar. `tests/test_vhost_files.py` und `tests/test_replace.py` starten den Datei-Helper lokal gegen ein temporäres Verzeichnis und brauchen dafür Linux und root (`chown`), sonst werden diese Tests übersprungen:

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest
```

Die Tests laufen nicht in GitHub Actions und landen nicht im Docker-Image (dort wird nur `server.py` kopiert).
