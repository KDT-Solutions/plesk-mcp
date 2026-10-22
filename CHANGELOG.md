# Changelog

## 0.29

- Neue Tools `wc_order_get` und `wc_order_mark_shipped` für WooCommerce-Bestellungen. `wc_order_get` zeigt Status, Betrag, Adressen, Positionen, eingetragene Sendungsverfolgung und die Sendungsnummer eines Label-Plugins (`_spap_tracking`). `wc_order_mark_shipped` trägt die Sendungsnummer über das Shipment-Tracking-Plugin ein (`wc_st_add_tracking_number`, Provider-Standard `swiss-post`, leer = Nummer aus dem Label-Plugin) und setzt den Status auf "Abgeschlossen" (`set_completed=false`: nur Tracking). Fester PHP-Code per `wp eval`, Werte als JSON per stdin, ohne `confirm=true` nur Vorschau, danach Kontrolle. Ersetzt Einmal-PHP-Skripte im Webroot.
- Tests: `tests/test_wc_order.py`.

## 0.28

- WP-CLI startet mit dem `memory_limit` der Domain: Alle WP-CLI-Aufrufe (`wp_option_*`, `wp_custom_css_*`, `wp_comment_*`, `wp_plugin_*`, `wp_theme_*`, `wc_coupon_*`) laufen über eine gemeinsame Funktion und rufen die WP-CLI-Phar als `<php der Domain> -d memory_limit=<wert> -d display_errors=stderr <wp-cli> ...` auf. Vorher lief WP-CLI mit dem PHP-CLI-Standard (128M) und brach bei grösseren Installationen (z.B. WooCommerce plus Jetpack) mit "Allowed memory size ... exhausted" ab.
  - Wert aus `plesk bin site --show-php-settings` ("Additional directives" vor den Standardfeldern), mindestens 256M, `-1` bzw. über 2048M wird zu 2048M. Strikte Prüfung (Zahl plus optional K/M/G), kein Shell-Interpolieren.
  - Nicht ermittelbar: 512M, im Ergebnis als `hinweis_memory_limit` vermerkt. Der verwendete Wert steht als `php_memory_limit` im Ergebnis.
  - Massgebend ist der Exit-Code: PHP-/WP-CLI-Warnungen (Warning, Notice, Deprecated) werden aus der Ausgabe entfernt, machen einen erfolgreichen Aufruf nicht mehr zum Fehler und stehen separat unter `warnungen`. Fehlermeldungen enthalten die letzten Zeilen ohne diese Warnungen.
- Neue Tools `wp_theme_list`, `wp_theme_update` und `wp_theme_rollback` analog zu den Plugin-Tools: Vorschau ohne `confirm`, tar.gz-Backup je Theme unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-theme-backups/<backup_id>/`, Kontrolle von Versionen und Startseite, automatisches Zurückstellen bei neuem 5xx (deutlich gemeldet), Hinweis bei aktivem Child-Theme mit aktualisiertem Parent-Theme. Plugin- und Theme-Tools nutzen denselben Unterbau.
- Version: Der Patch-Teil zählt jetzt die Commits seit der letzten Änderung von `_VERSION_BASE` (lokal und im GitHub-Workflow). Eine neue Basis beginnt damit bei `.0`, diese Version ist `0.28.0`.
- Tests: `tests/test_wp_cli_memory.py`, `tests/test_wp_theme.py`.

## 0.27

- Neue Tools `wp_plugin_list`, `wp_plugin_update` und `wp_plugin_rollback` für WordPress-Plugin-Updates per WP-CLI (als System-User der Subscription, nie root).
  - `wp_plugin_list` zeigt Status, installierte Version und verfügbares Update je Plugin (`only_updates=true` nur Plugins mit Update).
  - `wp_plugin_update`: einzelne Plugins (`plugins`, Slugs) oder `all_updates=true` für alle mit verfügbarem Update; optional `version` für genau ein Plugin (auch Downgrade). Must-use-Plugins und Drop-ins werden übersprungen. Ohne `confirm=true` nur Vorschau alt/neu.
  - Mit `confirm=true` vorher Backup jedes Plugins als tar.gz unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-plugin-backups/<backup_id>/` (0600, mit Manifest), dann `wp plugin update`, danach Kontrolle der Versionen und der Startseite. Liefert die Startseite vorher keinen 5xx-Fehler und danach schon, werden die Plugins automatisch zurückgestellt.
  - `wp_plugin_rollback(backup_id)` stellt alle oder einzelne Plugins aus dem Backup wieder her und sichert vorher den aktuellen Stand. Datenbank-Änderungen eines Updates werden nicht zurückgestellt.
  - Neue Umgebungsvariable `WP_PLUGIN_UPDATE_TIMEOUT` (Standard 600 Sekunden).
- Tests: `tests/test_wp_plugin.py` (Validierung ohne Serverzugriff).

## 0.26

- Neue Tools `wp_custom_css_show`, `wp_custom_css_update` und `wp_custom_css_rollback` für das "Zusätzliche CSS" des WordPress-Customizers (Design > Customizer > Zusätzliches CSS, `custom_css`-Post je Theme). Damit landet CSS dort, wo es auch im Backend sichtbar und editierbar ist, statt als mu-Plugin oder Theme-Datei.
  - Läuft wie `wp_option_update` über WP-CLI als System-User der Subscription (nie root): `wp eval` mit festem PHP-Code (`wp_get_custom_css`/`wp_update_custom_css_post`), das CSS wird per stdin übergeben und steht nie im Code; der Theme-Name nur nach Regex-Prüfung als Literal. WordPress legt dabei wie im Customizer eine Revision an.
  - `mode`: `replace` (Standard) oder `append`; `stylesheet` leer = aktives Theme, sonst Theme-Verzeichnis (Hinweis, wenn es nicht das aktive ist).
  - Ohne `confirm=true` nur Vorschau mit Unified-Diff alt/neu; mit `confirm=true` vorher JSON-Backup des alten CSS unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-custom-css-backups/<backup_id>.json` (0600, Subscription-User), dann Schreiben, erneut Lesen und Kontrolle, danach `wp cache flush` (best effort). Grenzen: 512 KB, kein Nullbyte, kein `</style`.
  - `wp_custom_css_rollback(backup_id)` stellt das damalige komplette CSS wieder her und legt vorher selbst ein Backup an.
- Neue Tools `wp_comment_list` und `wp_comment_moderate` zur Kommentar-Moderation per WP-CLI (z.B. Spam-Kommentare aus "Bitte moderiere"-Mails erledigen). `wp_comment_list` listet Kommentare nach Status (`hold`/`approve`/`spam`/`trash`/`all`, max. 200, Textsuche, Inhalt auf 300 Zeichen gekürzt) plus Zähler. `wp_comment_moderate` führt `spam`, `trash`, `approve`, `unapprove`, `unspam`, `untrash` oder `delete` (endgültig, `--force`) für 1-200 IDs aus - ohne `confirm=true` nur Vorschau mit Autor/E-Mail/Inhalt/Status, mit `confirm=true` Ausführung als Subscription-User und Statuskontrolle je Kommentar danach; WordPress-Hooks (Akismet) laufen mit.
- Neue Tools `wc_coupon_list`, `wc_coupon_get`, `wc_coupon_create`, `wc_coupon_update`, `wc_coupon_rollback` und `wc_coupon_delete` für WooCommerce-Gutscheine über die WooCommerce-CLI (`wp wc shop_coupon`, mit `--user` = erster Administrator der Installation, damit die WooCommerce-Validierung und -Hooks greifen). Felder gegen eine Whitelist geprüft (Code, Typ `percent`/`fixed_cart`/`fixed_product`, Betrag, Ablaufdatum, Limits, Produkt-/Kategorie-IDs, E-Mail-Einschränkungen, Status). `create` prüft vorher, ob der Code schon existiert; `update` zeigt alt/neu je Feld, legt ein JSON-Backup unter `/var/www/vhosts/<domain>/.plesk-mcp/wc-coupon-backups/` an und kontrolliert danach; `delete` verschiebt in den Papierkorb, `permanent=true` löscht endgültig (`--force`), jeweils mit Backup. Alle schreibenden Tools ohne `confirm=true` nur Vorschau.
- Neues Tool `customer_update_email(login, email, confirm)`: setzt die Kontakt-E-Mail eines Plesk-Kundenaccounts per `plesk bin customer --update <login> -email <mail>`. Login und Adresse werden strikt validiert, `admin` ist ausgeschlossen, ohne `confirm=true` nur Vorschau (aktuelle vs. neue Adresse).
- Neue Tools `subscription_suspend(domain, confirm)` und `subscription_activate(domain, confirm)`: sperren bzw. aktivieren eine komplette Subscription per `plesk bin subscription --webspace-off`/`--webspace-on` (z.B. nach Kündigung). Ohne `confirm=true` nur Vorschau mit Inhaber und Status, danach wird der neue Status gelesen und zurückgegeben.
- Neues Tool `subscription_delete(domain, confirm_domain, confirm)`: löscht eine Subscription endgültig (`plesk bin subscription --remove`). Ohne `confirm=true` Vorschau mit Inhaber, Status, Grösse und Mailboxen; ausgeführt wird nur mit `confirm=true` und `confirm_domain` = exakt derselbe Domainname. Danach Kontrolle, ob die Subscription weg ist. Zusatzdomains ohne eigene Subscription werden abgelehnt.
- Neues Tool `customer_delete(login, confirm_login, confirm)`: löscht einen Kundenaccount ohne Domains (`plesk bin customer --remove`), z.B. alte Kontakte. Kunden mit Domains und `admin` werden abgelehnt; ausgeführt wird nur mit `confirm=true` und `confirm_login` = exakt derselbe Login, danach Kontrolle.
- Tests: `tests/test_wp_custom_css.py`, `tests/test_wp_comment.py`, `tests/test_wc_coupon.py`, `tests/test_customer_email.py`, `tests/test_subscription.py` (Validierung ohne Serverzugriff).

## 0.25

- Neuer Endpunkt `GET /healthz` ohne Auth, liefert nur `{"status": "ok"}`. Prüft nur, ob der Prozess antwortet, nicht Plesk oder die SSH-Verbindung. Alle anderen Pfade brauchen weiterhin `MCP_API_KEY` (bzw. `/upload/{id}` das Einmal-Token).
- `HEALTHCHECK` im Dockerfile (Python/urllib auf `/healthz`, interval 30s, timeout 5s, start_period 15s, retries 3).

## 0.24

- Fix `web_server_settings_rollback` für `nginx-directives`, wenn die Datei vor dem Update nicht existierte: Nach dem Entfernen von `vhost_nginx.conf` lief `nginx -t` noch vor dem Reconfigure und schlug fehl, weil die Domain-Konfiguration die Datei noch per `include` einbindet ("No such file or directory"). Der Rollback wurde dadurch abgebrochen und der alte Stand wiederhergestellt. Beim Entfernen entfällt jetzt der Test vor dem Reconfigure; `nginx -t` läuft danach.

## 0.23

- Fix `nginx-directives` in `web_server_settings_update`: `plesk bin domain --update-web-server-settings` kennt keine Option `-nginx-directives` ("Unrecognized option"), Plesk kann die zusätzlichen nginx-Anweisungen per CLI nur anzeigen. Das Tool schreibt jetzt direkt in `/var/www/vhosts/system/<domain>/conf/vhost_nginx.conf`:
  - atomar (Temp-Datei im selben Ordner + rename), Besitzer/Rechte wie die bestehende Datei, neue Datei `root:nginx` `0600`
  - danach `nginx -t`; bei Fehler alte Datei zurück, nichts neu geladen
  - erst dann `plesk sbin httpdmng --reconfigure-domain <domain>`, danach Datei, `nginx -t` und Startseite (5xx) prüfen; bei Fehler alte Datei zurück und erneut Reconfigure
  - Domainname strikt geprüft, Pfad nur unter `/var/www/vhosts/system/<domain>/conf/`, keine Symlinks
  - `nginx-directives` muss allein gesetzt werden
- Rollback-Logik: Wurde nichts geschrieben, läuft kein Rollback mehr und es wird "FEHLER - nichts geändert" gemeldet statt "Zurückstellen fehlgeschlagen" (gilt auch für die übrigen Optionen).
- `web_server_settings_rollback` schreibt für `nginx-directives`-Backups den damaligen Dateistand zurück (bzw. entfernt die Datei, wenn es sie damals nicht gab), mit demselben Ablauf aus Test und Reconfigure.

## 0.22

- `web_server_settings_update`: neue Option `nginx-directives` für die zusätzlichen nginx-Anweisungen einer Domain (Plesk "Zusätzliche nginx-Anweisungen", `vhost_nginx.conf`).
  - Whitelist: nur `add_header`, `expires` und `location`-Blöcke, die ausschliesslich `add_header`/`expires` enthalten; Kommentare und Leerzeilen erlaubt, alles andere wird abgelehnt.
  - Neuer Parameter `mode`: `replace` (Standard, kompletter Inhalt) oder `append` (an den bestehenden Inhalt anhängen).
  - Vorschau und Ergebnis zeigen alt/neu als `diff`, mit Hinweis, wenn bestehende Zeilen ersetzt werden.
  - Mit `confirm=true`: `nginx -t` vorher, Backup, Schreiben per `-nginx-directives <datei>`, danach `nginx -t` und HTTP-Prüfung der Startseite; bei Fehler automatisch zurück auf den alten Stand.
- `web_server_settings_rollback` stellt die nginx-Anweisungen nur zurück, wenn das zugehörige Update sie geändert hat.
- `web_server_settings_show` listet `nginx-directives` unter `aenderbar`; der Inhalt steht weiterhin unter `additional nginx settings`.
