<?php
/**
 * Administration: Plugins, Themes, Core-Updates, Website-Zustand, Cache, Benutzer, Anwendungspasswörter, Protokoll.
 *
 * Jede Ability prüft die passende Capability (activate_plugins, install_plugins, update_core, list_users ...),
 * die in WordPress standardmässig nur Administratoren haben. Kein Löschen von Plugins, Themes oder Benutzern,
 * Installation nur aus dem WordPress-Verzeichnis per Slug.
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Admin {

	const SELF_PLUGIN = 'tabside-mcp/tabside-mcp.php';

	/**
	 * Admin-Abilities verfügbar (Rolle Administrator bzw. manage_options)?
	 */
	public static function available() {
		return current_user_can( 'manage_options' );
	}

	/**
	 * @return array<string, array<string, mixed>>
	 */
	public static function definitions() {
		$confirm = Tabside_MCP_Abilities::confirm_schema();
		$cap     = static function ( $capability ) {
			return static function () use ( $capability ) {
				return Tabside_MCP_Plugin::check_user( $capability );
			};
		};
		$plugin  = array(
			'type'        => 'string',
			'description' => 'Plugin-Datei aus plugin-list (z.B. redirection/redirection.php) oder Verzeichnis-Slug (redirection).',
		);
		$theme   = array(
			'type'        => 'string',
			'description' => 'Theme-Verzeichnis (stylesheet) aus theme-list.',
		);
		$refresh = array(
			'type'        => 'boolean',
			'default'     => false,
			'description' => 'Vorher bei wordpress.org nach Updates fragen.',
		);
		$admin_note = ' Admin-Ability: Vorschau zuerst dem Benutzer zeigen, confirm=true erst nach Bestätigung.';

		return array(
			'plugin-list'             => array(
				'label'       => 'Plugins auflisten',
				'description' => 'Installierte Plugins mit Version, aktiv, verfügbarem Update und Auto-Update.',
				'readonly'    => true,
				'properties'  => array( 'refresh' => $refresh ),
				'permission'  => $cap( 'activate_plugins' ),
				'execute'     => array( __CLASS__, 'plugin_list' ),
			),
			'plugin-activate'         => array(
				'label'       => 'Plugin aktivieren',
				'description' => 'Aktiviert ein installiertes Plugin. Ohne confirm=true nur Vorschau.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array(
					'plugin'  => $plugin,
					'confirm' => $confirm,
				),
				'required'    => array( 'plugin' ),
				'permission'  => $cap( 'activate_plugins' ),
				'execute'     => array( __CLASS__, 'plugin_activate' ),
			),
			'plugin-deactivate'       => array(
				'label'       => 'Plugin deaktivieren',
				'description' => 'Deaktiviert ein Plugin (kein Löschen). tabside-mcp selbst lässt sich nicht deaktivieren. Ohne confirm=true nur Vorschau.' . $admin_note,
				'destructive' => true,
				'flush'       => 'site',
				'properties'  => array(
					'plugin'  => $plugin,
					'confirm' => $confirm,
				),
				'required'    => array( 'plugin' ),
				'permission'  => $cap( 'activate_plugins' ),
				'execute'     => array( __CLASS__, 'plugin_deactivate' ),
			),
			'plugin-update'           => array(
				'label'       => 'Plugin aktualisieren',
				'description' => 'Aktualisiert ein Plugin auf die verfügbare Version (aktive Plugins bleiben aktiv). Ohne confirm=true nur Vorschau.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array(
					'plugin'  => $plugin,
					'confirm' => $confirm,
				),
				'required'    => array( 'plugin' ),
				'permission'  => $cap( 'update_plugins' ),
				'execute'     => array( __CLASS__, 'plugin_update' ),
			),
			'plugin-auto-update-set'  => array(
				'label'       => 'Plugin-Auto-Update',
				'description' => 'Schaltet automatische Updates eines Plugins ein oder aus. Ohne confirm=true nur Vorschau.' . $admin_note,
				'properties'  => array(
					'plugin'  => $plugin,
					'enabled' => array( 'type' => 'boolean' ),
					'confirm' => $confirm,
				),
				'required'    => array( 'plugin', 'enabled' ),
				'permission'  => $cap( 'update_plugins' ),
				'execute'     => array( __CLASS__, 'plugin_auto_update_set' ),
			),
			'plugin-install'          => array(
				'label'       => 'Plugin installieren',
				'description' => 'Installiert ein Plugin aus dem WordPress-Verzeichnis (wordpress.org/plugins) per Slug, nie aus anderen Quellen. Optional gleich aktivieren. Ohne confirm=true nur Vorschau mit Angaben aus dem Verzeichnis.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array(
					'slug'     => array(
						'type'        => 'string',
						'pattern'     => '^[a-z0-9][a-z0-9-]*$',
						'description' => 'Slug aus der Adresse wordpress.org/plugins/<slug>/.',
					),
					'activate' => array(
						'type'    => 'boolean',
						'default' => false,
					),
					'confirm'  => $confirm,
				),
				'required'    => array( 'slug' ),
				'permission'  => static function ( $input ) {
					$ok = Tabside_MCP_Plugin::check_user( 'install_plugins' );
					if ( true === $ok && ! empty( $input['activate'] ) ) {
						$ok = Tabside_MCP_Plugin::check_user( 'activate_plugins' );
					}
					return $ok;
				},
				'execute'     => array( __CLASS__, 'plugin_install' ),
			),
			'theme-list'              => array(
				'label'       => 'Themes auflisten',
				'description' => 'Installierte Themes mit Version, aktiv, Eltern-Theme, verfügbarem Update und Auto-Update.',
				'readonly'    => true,
				'properties'  => array( 'refresh' => $refresh ),
				'permission'  => $cap( 'switch_themes' ),
				'execute'     => array( __CLASS__, 'theme_list' ),
			),
			'theme-activate'          => array(
				'label'       => 'Theme aktivieren',
				'description' => 'Wechselt das aktive Theme. Achtung: die ganze Seite ändert ihr Aussehen, Menü-Positionen und Widgets können sich verschieben. Ohne confirm=true nur Vorschau.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array(
					'theme'   => $theme,
					'confirm' => $confirm,
				),
				'required'    => array( 'theme' ),
				'permission'  => $cap( 'switch_themes' ),
				'execute'     => array( __CLASS__, 'theme_activate' ),
			),
			'theme-update'            => array(
				'label'       => 'Theme aktualisieren',
				'description' => 'Aktualisiert ein Theme auf die verfügbare Version. Ohne confirm=true nur Vorschau.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array(
					'theme'   => $theme,
					'confirm' => $confirm,
				),
				'required'    => array( 'theme' ),
				'permission'  => $cap( 'update_themes' ),
				'execute'     => array( __CLASS__, 'theme_update' ),
			),
			'core-update-check'       => array(
				'label'       => 'WordPress-Updates prüfen',
				'description' => 'Installierte WordPress-Version und verfügbare Updates. core-update führt nur Minor-Updates (gleicher Zweig, z.B. 6.9.1 -> 6.9.2) aus.',
				'readonly'    => true,
				'properties'  => array( 'refresh' => $refresh ),
				'permission'  => $cap( 'update_core' ),
				'execute'     => array( __CLASS__, 'core_update_check' ),
			),
			'core-update'             => array(
				'label'       => 'WordPress-Minor-Update',
				'description' => 'Installiert das verfügbare Minor-Update von WordPress (nur gleicher Zweig, keine neue Hauptversion). Ohne confirm=true nur Vorschau.' . $admin_note,
				'flush'       => 'site',
				'properties'  => array( 'confirm' => $confirm ),
				'permission'  => $cap( 'update_core' ),
				'execute'     => array( __CLASS__, 'core_update' ),
			),
			'site-health'             => array(
				'label'       => 'Website-Zustand',
				'description' => 'Ergebnisse der direkten Website-Zustand-Tests, PHP-Version, Speicherlimit, Cron-Status, Debug-Modus, Objekt-Cache.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $cap( 'view_site_health_checks' ),
				'execute'     => array( __CLASS__, 'site_health' ),
			),
			'cache-flush'             => array(
				'label'       => 'Cache leeren',
				'description' => 'Leert Objekt-Cache, Elementor-CSS und erkannte Cache-Plugins (WP Super Cache, W3 Total Cache, WP Rocket, LiteSpeed, Autoptimize, SG Optimizer, Breeze). Mit post_id nur für diesen Beitrag. Läuft nach Änderungen ohnehin automatisch. Ohne confirm=true nur Vorschau.',
				'properties'  => array(
					'post_id' => array(
						'type'    => 'integer',
						'minimum' => 1,
					),
					'confirm' => $confirm,
				),
				'permission'  => static function ( $input ) {
					if ( ! empty( $input['post_id'] ) ) {
						return Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['post_id'] ), 'edit_post' );
					}
					return Tabside_MCP_Plugin::check_user( 'manage_options' );
				},
				'execute'     => array( __CLASS__, 'cache_flush' ),
			),
			'wptc-set-revision-limit' => array(
				'label'       => 'WPTC-Speicherdauer setzen',
				'description' => 'Setzt bei WP Time Capsule die Speicherdauer der Sicherungen (revision_limit, in Tagen) in der WPTC-Tabelle. Nur wenn WPTC aktiv ist. Alter Wert wird gesichert (option-rollback mit backup_id). Ohne confirm=true nur Vorschau.' . $admin_note,
				'properties'  => array(
					'days'    => array(
						'type'        => 'integer',
						'minimum'     => 1,
						'maximum'     => 365,
						'default'     => 30,
						'description' => 'Speicherdauer in Tagen, 1 bis 365.',
					),
					'confirm' => $confirm,
				),
				'permission'  => $cap( 'manage_options' ),
				'execute'     => array( __CLASS__, 'wptc_set_revision_limit' ),
			),
			'wptc-status'             => array(
				'label'       => 'WPTC-Status',
				'description' => 'Detaillierter Stand von WP Time Capsule: Konto und Verbindung zum WPTC-Dienst, Backup-Ziel, Zeitplan, letzte erfolgreiche Sicherung, laufende Sicherung mit letztem Lebenszeichen (hanging = über 60 Minuten still), Warteschlange, letzte Sicherungen und die neuesten Einträge des WPTC-Aktivitätsprotokolls. Nur lesend.',
				'readonly'    => true,
				'properties'  => array(
					'log_limit' => array(
						'type'    => 'integer',
						'minimum' => 0,
						'maximum' => 100,
						'default' => 20,
					),
				),
				'permission'  => $cap( 'manage_options' ),
				'execute'     => array( __CLASS__, 'wptc_backup_status' ),
			),
			'wptc-backup-start'       => array(
				'label'       => 'WPTC-Sicherung starten',
				'description' => 'Startet bei WP Time Capsule eine Sicherung (wie "Backup now" bzw. BACKUP_START des WPTC-Dienstes). Eine hängende Sicherung (letztes Lebenszeichen über 60 Minuten alt) wird vorher abgebrochen und zurückgesetzt; eine aktive nur mit abort_running=true. Nie während einer Wiederherstellung. Die Vorschau zeigt den aktuellen Stand und was passiert. Ohne confirm=true nur Vorschau.' . $admin_note,
				'properties'  => array(
					'abort_running' => array(
						'type'        => 'boolean',
						'default'     => false,
						'description' => 'Auch eine nicht hängende, laufende Sicherung abbrechen und neu starten.',
					),
					'confirm'       => $confirm,
				),
				'permission'  => $cap( 'manage_options' ),
				'execute'     => array( __CLASS__, 'wptc_backup_start' ),
			),
			'wptc-repair'             => array(
				'label'       => 'WPTC reparieren',
				'description' => 'Repariert WP Time Capsule, wenn das Temp-Verzeichnis fehlt oder die Sicherung hängt: Diagnose (Temp-Verzeichnis, Pfad-Einstellung backup_db_path, laufende/hängende Sicherung, Konto), dann mit den WPTC-eigenen Funktionen wp-content/uploads/tCapsule/backups samt index.php anlegen und den Pfad neu setzen, hängende Sicherung abbrechen und zurücksetzen, optional (start_backup, Vorgabe true) frische Sicherung starten. Der Pfad wird immer aus der WordPress-Konfiguration abgeleitet, nie übergeben. Alte Pfad-Werte werden gesichert (option-rollback mit backup_id). Eine aktive, nicht hängende Sicherung nur mit abort_running=true, nie während einer Wiederherstellung. Ohne confirm=true nur Vorschau.' . $admin_note,
				'properties'  => array(
					'start_backup'  => array(
						'type'        => 'boolean',
						'default'     => true,
						'description' => 'Nach der Reparatur eine frische Sicherung starten.',
					),
					'abort_running' => array(
						'type'        => 'boolean',
						'default'     => false,
						'description' => 'Auch eine nicht hängende, laufende Sicherung abbrechen.',
					),
					'confirm'       => $confirm,
				),
				'permission'  => $cap( 'manage_options' ),
				'execute'     => array( __CLASS__, 'wptc_repair' ),
			),
			'user-list'               => array(
				'label'       => 'Benutzer auflisten',
				'description' => 'Benutzer mit Login, Name, E-Mail, Rolle, Registrierung, Anzahl Anwendungspasswörter und letzter Nutzung eines Anwendungspassworts.',
				'readonly'    => true,
				'properties'  => array(
					'role'   => array( 'type' => 'string' ),
					'search' => array( 'type' => 'string' ),
					'limit'  => array(
						'type'    => 'integer',
						'minimum' => 1,
						'maximum' => Tabside_MCP_Abilities::MAX_LIMIT,
						'default' => 50,
					),
					'page'   => array(
						'type'    => 'integer',
						'minimum' => 1,
						'default' => 1,
					),
				),
				'permission'  => $cap( 'list_users' ),
				'execute'     => array( __CLASS__, 'user_list' ),
			),
			'user-get'                => array(
				'label'       => 'Benutzer abrufen',
				'description' => 'Ein Benutzer mit Namen, Biografie, Rollen und Anwendungspasswörtern (ohne Passwort).',
				'readonly'    => true,
				'properties'  => array( 'user_id' => Tabside_MCP_Abilities::id_schema( 'ID des Benutzers.' ) ),
				'required'    => array( 'user_id' ),
				'permission'  => static function ( $input ) {
					if ( (int) ( $input['user_id'] ?? 0 ) === get_current_user_id() ) {
						return Tabside_MCP_Plugin::check_user();
					}
					return Tabside_MCP_Plugin::check_user( 'list_users' );
				},
				'execute'     => array( __CLASS__, 'user_get' ),
			),
			'user-update'             => array(
				'label'       => 'Benutzer ändern',
				'description' => 'Ändert nur Anzeigename, Vor-/Nachname, Biografie oder Rolle. Kein Passwort, keine E-Mail, kein Anlegen oder Löschen. Rolle administrator und Änderungen an Administratoren nur durch Administratoren, die eigene Rolle nie. Ohne confirm=true nur Vorschau.' . $admin_note,
				'properties'  => array(
					'user_id'      => Tabside_MCP_Abilities::id_schema( 'ID des Benutzers.' ),
					'display_name' => array( 'type' => 'string' ),
					'first_name'   => array( 'type' => 'string' ),
					'last_name'    => array( 'type' => 'string' ),
					'description'  => array( 'type' => 'string' ),
					'role'         => array( 'type' => 'string' ),
					'confirm'      => $confirm,
				),
				'required'    => array( 'user_id' ),
				'permission'  => static function ( $input ) {
					$ok = Tabside_MCP_Plugin::check_user( 'edit_user', (int) ( $input['user_id'] ?? 0 ) );
					if ( true === $ok && isset( $input['role'] ) ) {
						$ok = Tabside_MCP_Plugin::check_user( 'promote_user', (int) $input['user_id'] );
					}
					return $ok;
				},
				'execute'     => array( __CLASS__, 'user_update' ),
			),
			'application-password-list' => array(
				'label'       => 'Eigene Anwendungspasswörter',
				'description' => 'Anwendungspasswörter des angemeldeten Benutzers: Name, erstellt, zuletzt benutzt, letzte IP. Nie das Passwort.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'read' );
				},
				'execute'     => array( __CLASS__, 'application_password_list' ),
			),
			'audit-log'               => array(
				'label'       => 'Protokoll',
				'description' => 'Protokoll der ausgeführten Änderungen über tabside MCP (Zeit, Benutzer, Ability, Ziel, Sicherungs-ID), neueste zuerst, höchstens 500. Administratoren sehen alle Einträge, andere nur die eigenen.',
				'readonly'    => true,
				'properties'  => array(
					'limit' => array(
						'type'    => 'integer',
						'minimum' => 1,
						'maximum' => Tabside_MCP_Store::MAX_AUDIT,
						'default' => 50,
					),
					'user'  => array( 'type' => 'string' ),
				),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'edit_posts' );
				},
				'execute'     => array( __CLASS__, 'audit_log' ),
			),
		);
	}

	private static function admin_includes() {
		require_once ABSPATH . 'wp-admin/includes/admin.php';
		require_once ABSPATH . 'wp-admin/includes/plugin-install.php';
		require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';
	}

	/* ------------------------------------------------------------------ Plugins */

	public static function plugin_list( $input ) {
		self::admin_includes();
		if ( ! empty( $input['refresh'] ) ) {
			// Sonst liefert der eigene Updater bis zu 6 Stunden alte Release-Angaben von GitHub.
			Tabside_MCP_Updater::flush();
			// Ohne geleerten Update-Cache fragt wp_update_plugins() höchstens alle 12 Stunden neu an.
			wp_clean_plugins_cache();
			wp_update_plugins();
		}
		$updates = get_site_transient( 'update_plugins' );
		$auto    = (array) get_site_option( 'auto_update_plugins', array() );
		$out     = array();
		foreach ( get_plugins() as $file => $data ) {
			$update = isset( $updates->response[ $file ] ) ? $updates->response[ $file ] : null;
			$out[]  = array(
				'plugin'      => $file,
				'name'        => $data['Name'],
				'version'     => $data['Version'],
				'author'      => wp_strip_all_tags( $data['Author'] ),
				'active'      => is_plugin_active( $file ),
				'update'      => $update ? $update->new_version : null,
				'auto_update' => in_array( $file, $auto, true ) || ( self::SELF_PLUGIN === $file && ! defined( 'TABSIDE_MCP_DISABLE_AUTO_UPDATE' ) ),
			);
		}
		return array(
			'auto_updates_possible' => wp_is_auto_update_enabled_for_type( 'plugin' ),
			'plugins'               => $out,
		);
	}

	public static function plugin_activate( $input ) {
		$file = self::plugin_file( $input['plugin'] );
		if ( is_wp_error( $file ) ) {
			return $file;
		}
		if ( ! current_user_can( 'activate_plugin', $file ) ) {
			return self::forbidden( 'activate_plugin' );
		}
		if ( is_plugin_active( $file ) ) {
			return new WP_Error( 'tabside_mcp_plugin_active', sprintf( '%s ist bereits aktiv.', $file ) );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( array( 'plugin' => self::plugin_info( $file ) ), array( 'Wird aktiviert.' ) );
		}
		$result = activate_plugin( $file );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		return array(
			'confirmed' => true,
			'plugin'    => self::plugin_info( $file ),
		);
	}

	public static function plugin_deactivate( $input ) {
		$file = self::plugin_file( $input['plugin'] );
		if ( is_wp_error( $file ) ) {
			return $file;
		}
		if ( self::SELF_PLUGIN === $file || plugin_basename( TABSIDE_MCP_DIR . 'tabside-mcp.php' ) === $file ) {
			return new WP_Error( 'tabside_mcp_self', 'tabside-mcp lässt sich über tabside MCP nicht deaktivieren.', array( 'status' => 403 ) );
		}
		if ( ! current_user_can( 'deactivate_plugin', $file ) ) {
			return self::forbidden( 'deactivate_plugin' );
		}
		if ( ! is_plugin_active( $file ) ) {
			return new WP_Error( 'tabside_mcp_plugin_inactive', sprintf( '%s ist nicht aktiv.', $file ) );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( array( 'plugin' => self::plugin_info( $file ) ), array( 'Wird deaktiviert, nicht gelöscht. Wieder einschalten mit plugin-activate.' ) );
		}
		deactivate_plugins( $file );
		return array(
			'confirmed' => true,
			'plugin'    => self::plugin_info( $file ),
		);
	}

	public static function plugin_update( $input ) {
		$file = self::plugin_file( $input['plugin'] );
		if ( is_wp_error( $file ) ) {
			return $file;
		}
		if ( self::SELF_PLUGIN === $file ) {
			Tabside_MCP_Updater::flush();
		}
		// Ohne geleerten Update-Cache fragt wp_update_plugins() höchstens alle 12 Stunden neu an.
		wp_clean_plugins_cache();
		wp_update_plugins();
		$updates = get_site_transient( 'update_plugins' );
		$update  = $updates->response[ $file ] ?? null;
		if ( ! $update ) {
			return new WP_Error( 'tabside_mcp_no_update', sprintf( 'Für %s ist kein Update verfügbar.', $file ) );
		}
		$info = self::plugin_info( $file );
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'plugin'      => $info,
					'new_version' => $update->new_version,
				),
				array()
			);
		}
		$was_active = is_plugin_active( $file );
		$skin       = new WP_Ajax_Upgrader_Skin();
		$result     = ( new Plugin_Upgrader( $skin ) )->upgrade( $file );
		$error      = self::upgrader_error( $result, $skin );
		if ( $error ) {
			return $error;
		}
		wp_clean_plugins_cache();
		// Über die REST-API deaktiviert WordPress das Plugin vor dem Update und aktiviert es nicht wieder.
		if ( $was_active && ! is_plugin_active( $file ) ) {
			activate_plugin( $file, '', false, true );
		}
		return array(
			'confirmed'   => true,
			'old_version' => $info['version'],
			'plugin'      => self::plugin_info( $file ),
		);
	}

	public static function plugin_auto_update_set( $input ) {
		$file = self::plugin_file( $input['plugin'] );
		if ( is_wp_error( $file ) ) {
			return $file;
		}
		if ( ! wp_is_auto_update_enabled_for_type( 'plugin' ) ) {
			return new WP_Error( 'tabside_mcp_auto_updates_off', 'Automatische Plugin-Updates sind auf dieser Seite abgeschaltet.' );
		}
		$enabled = (bool) $input['enabled'];
		$list    = (array) get_site_option( 'auto_update_plugins', array() );
		$old     = in_array( $file, $list, true );
		$notes   = self::SELF_PLUGIN === $file ? array( 'tabside-mcp aktualisiert sich unabhängig davon selbst (abschaltbar mit TABSIDE_MCP_DISABLE_AUTO_UPDATE).' ) : array();
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'plugin'      => $file,
					'auto_update' => array(
						'old' => $old,
						'new' => $enabled,
					),
				),
				$notes
			);
		}
		$list = $enabled ? array_values( array_unique( array_merge( $list, array( $file ) ) ) ) : array_values( array_diff( $list, array( $file ) ) );
		update_site_option( 'auto_update_plugins', $list );
		return array(
			'confirmed'   => true,
			'plugin'      => $file,
			'auto_update' => in_array( $file, (array) get_site_option( 'auto_update_plugins', array() ), true ),
			'notes'       => $notes,
		);
	}

	public static function plugin_install( $input ) {
		self::admin_includes();
		$slug = (string) $input['slug'];
		if ( ! preg_match( '/^[a-z0-9][a-z0-9-]*$/', $slug ) ) {
			return new WP_Error( 'tabside_mcp_plugin_slug', 'Ungültiger Slug.' );
		}
		foreach ( array_keys( get_plugins() ) as $file ) {
			if ( dirname( $file ) === $slug ) {
				return new WP_Error( 'tabside_mcp_plugin_installed', sprintf( 'Plugin %s ist bereits installiert (%s).', $slug, $file ) );
			}
		}
		$api = plugins_api(
			'plugin_information',
			array(
				'slug'   => $slug,
				'fields' => array(
					'sections'    => false,
					'short_description' => true,
					'icons'       => false,
					'banners'     => false,
					'reviews'     => false,
					'downloaded'  => false,
					'active_installs' => true,
				),
			)
		);
		if ( is_wp_error( $api ) ) {
			return new WP_Error( 'tabside_mcp_plugin_unknown', sprintf( 'Plugin "%s" im WordPress-Verzeichnis nicht gefunden: %s', $slug, $api->get_error_message() ) );
		}
		$link = (string) ( $api->download_link ?? '' );
		if ( 0 !== strpos( $link, 'https://downloads.wordpress.org/' ) ) {
			return new WP_Error( 'tabside_mcp_plugin_source', 'Download stammt nicht aus dem WordPress-Verzeichnis, Installation abgebrochen.' );
		}
		$preview = array(
			'slug'            => $slug,
			'name'            => $api->name,
			'version'         => $api->version,
			'author'          => wp_strip_all_tags( (string) $api->author ),
			'requires_wp'     => $api->requires ?? null,
			'tested_wp'       => $api->tested ?? null,
			'requires_php'    => $api->requires_php ?? null,
			'active_installs' => $api->active_installs ?? null,
			'rating'          => $api->rating ?? null,
			'last_updated'    => $api->last_updated ?? null,
			'description'     => wp_strip_all_tags( (string) ( $api->short_description ?? '' ) ),
			'activate'        => ! empty( $input['activate'] ),
		);
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( $preview, array( 'Quelle: wordpress.org/plugins/' . $slug . '/' ) );
		}
		$skin   = new WP_Ajax_Upgrader_Skin();
		$upgr   = new Plugin_Upgrader( $skin );
		$result = $upgr->install( $link );
		$error  = self::upgrader_error( $result, $skin );
		if ( $error ) {
			return $error;
		}
		wp_clean_plugins_cache();
		$file = $upgr->plugin_info();
		if ( ! $file ) {
			return new WP_Error( 'tabside_mcp_plugin_install', 'Installiert, aber die Plugin-Datei wurde nicht gefunden.' );
		}
		$notes = array();
		if ( ! empty( $input['activate'] ) ) {
			$activated = activate_plugin( $file );
			if ( is_wp_error( $activated ) ) {
				$notes[] = 'Installiert, aber nicht aktiviert: ' . $activated->get_error_message();
			}
		}
		return array(
			'confirmed' => true,
			'notes'     => $notes,
			'plugin'    => self::plugin_info( $file ),
		);
	}

	/**
	 * Datei oder Verzeichnis-Slug -> Plugin-Datei.
	 *
	 * @return string|WP_Error
	 */
	private static function plugin_file( $plugin ) {
		self::admin_includes();
		$plugin  = trim( (string) $plugin );
		$plugins = get_plugins();
		if ( isset( $plugins[ $plugin ] ) ) {
			return $plugin;
		}
		$matches = array();
		foreach ( array_keys( $plugins ) as $file ) {
			if ( dirname( $file ) === $plugin || basename( $file, '.php' ) === $plugin ) {
				$matches[] = $file;
			}
		}
		if ( 1 === count( $matches ) ) {
			return $matches[0];
		}
		return new WP_Error( 'tabside_mcp_plugin_not_found', sprintf( 'Plugin "%s" nicht eindeutig gefunden (plugin-list).', $plugin ), array( 'status' => 404 ) );
	}

	private static function plugin_info( $file ) {
		self::admin_includes();
		wp_clean_plugins_cache( false );
		$plugins = get_plugins();
		$data    = $plugins[ $file ] ?? array();
		return array(
			'plugin'  => $file,
			'name'    => $data['Name'] ?? $file,
			'version' => $data['Version'] ?? null,
			'active'  => is_plugin_active( $file ),
		);
	}

	/**
	 * @return WP_Error|null
	 */
	private static function upgrader_error( $result, $skin ) {
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		if ( $skin->get_errors()->has_errors() ) {
			return new WP_Error( 'tabside_mcp_upgrade', implode( ' ', $skin->get_error_messages() ? (array) $skin->get_error_messages() : $skin->get_errors()->get_error_messages() ) );
		}
		if ( false === $result || null === $result ) {
			return new WP_Error( 'tabside_mcp_upgrade', 'Fehlgeschlagen. Möglicherweise fehlt der direkte Dateizugriff (FS_METHOD) auf diesem Server.' );
		}
		return null;
	}

	/* ------------------------------------------------------------------ Themes */

	public static function theme_list( $input ) {
		self::admin_includes();
		if ( ! empty( $input['refresh'] ) ) {
			wp_clean_themes_cache();
			wp_update_themes();
		}
		$updates = get_site_transient( 'update_themes' );
		$auto    = (array) get_site_option( 'auto_update_themes', array() );
		$out     = array();
		foreach ( wp_get_themes() as $stylesheet => $theme ) {
			$out[] = array(
				'theme'       => $stylesheet,
				'name'        => $theme->get( 'Name' ),
				'version'     => $theme->get( 'Version' ),
				'active'      => get_stylesheet() === $stylesheet,
				'parent'      => $theme->parent() ? $theme->get_template() : null,
				'block_theme' => $theme->is_block_theme(),
				'update'      => isset( $updates->response[ $stylesheet ]['new_version'] ) ? $updates->response[ $stylesheet ]['new_version'] : null,
				'auto_update' => in_array( $stylesheet, $auto, true ),
			);
		}
		return array( 'themes' => $out );
	}

	public static function theme_activate( $input ) {
		$theme = wp_get_theme( (string) $input['theme'] );
		if ( ! $theme->exists() || $theme->errors() ) {
			return new WP_Error( 'tabside_mcp_theme_not_found', sprintf( 'Theme "%s" nicht gefunden oder fehlerhaft (theme-list).', $input['theme'] ), array( 'status' => 404 ) );
		}
		if ( get_stylesheet() === $theme->get_stylesheet() ) {
			return new WP_Error( 'tabside_mcp_theme_active', 'Dieses Theme ist bereits aktiv.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'current' => get_stylesheet(),
					'new'     => $theme->get_stylesheet(),
					'name'    => $theme->get( 'Name' ),
				),
				array( 'ACHTUNG: Die ganze Seite ändert ihr Aussehen. Menü-Positionen, Widgets und Customizer-Einstellungen gehören zum Theme und können sich verschieben. Zurück mit theme-activate und dem bisherigen Theme.' )
			);
		}
		$old = get_stylesheet();
		switch_theme( $theme->get_stylesheet() );
		return array(
			'confirmed' => true,
			'previous'  => $old,
			'active'    => get_stylesheet(),
		);
	}

	public static function theme_update( $input ) {
		self::admin_includes();
		$stylesheet = (string) $input['theme'];
		$theme      = wp_get_theme( $stylesheet );
		if ( ! $theme->exists() ) {
			return new WP_Error( 'tabside_mcp_theme_not_found', sprintf( 'Theme "%s" nicht gefunden.', $stylesheet ), array( 'status' => 404 ) );
		}
		wp_clean_themes_cache();
		wp_update_themes();
		$updates = get_site_transient( 'update_themes' );
		if ( empty( $updates->response[ $stylesheet ] ) ) {
			return new WP_Error( 'tabside_mcp_no_update', sprintf( 'Für %s ist kein Update verfügbar.', $stylesheet ) );
		}
		$old = $theme->get( 'Version' );
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'theme'       => $stylesheet,
					'version'     => $old,
					'new_version' => $updates->response[ $stylesheet ]['new_version'],
				),
				array()
			);
		}
		$skin   = new WP_Ajax_Upgrader_Skin();
		$result = ( new Theme_Upgrader( $skin ) )->upgrade( $stylesheet );
		$error  = self::upgrader_error( $result, $skin );
		if ( $error ) {
			return $error;
		}
		wp_clean_themes_cache();
		return array(
			'confirmed'   => true,
			'old_version' => $old,
			'version'     => wp_get_theme( $stylesheet )->get( 'Version' ),
		);
	}

	/* ------------------------------------------------------------------ Core */

	public static function core_update_check( $input ) {
		self::admin_includes();
		if ( ! empty( $input['refresh'] ) ) {
			wp_version_check( array(), true );
		}
		$offers = array();
		foreach ( self::core_offers() as $offer ) {
			$offers[] = array(
				'response' => $offer->response,
				'version'  => $offer->version ?? $offer->current ?? null,
				'locale'   => $offer->locale ?? null,
				'minor'    => self::is_minor( $offer ),
			);
		}
		$minor = self::minor_offer();
		return array(
			'wordpress'       => get_bloginfo( 'version' ),
			'offers'          => $offers,
			'minor_available' => $minor ? $minor->version : null,
		);
	}

	public static function core_update( $input ) {
		self::admin_includes();
		wp_version_check( array(), true );
		$offer = self::minor_offer();
		if ( ! $offer ) {
			return new WP_Error( 'tabside_mcp_no_update', 'Kein Minor-Update verfügbar (neue Hauptversionen nur über wp-admin).' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'wordpress'   => get_bloginfo( 'version' ),
					'new_version' => $offer->version,
				),
				array( 'Die Seite ist während des Updates kurz im Wartungsmodus.' )
			);
		}
		$old    = get_bloginfo( 'version' );
		$skin   = new WP_Ajax_Upgrader_Skin();
		$result = ( new Core_Upgrader( $skin ) )->upgrade( $offer );
		$error  = self::upgrader_error( $result, $skin );
		if ( $error ) {
			return $error;
		}
		return array(
			'confirmed'   => true,
			'old_version' => $old,
			'version'     => is_string( $result ) ? $result : $offer->version,
		);
	}

	private static function is_minor( $offer ) {
		$current = explode( '.', preg_replace( '/-.*$/', '', get_bloginfo( 'version' ) ) );
		$new     = explode( '.', (string) ( $offer->version ?? '' ) );
		return isset( $new[1] ) && 'upgrade' === ( $offer->response ?? '' )
			&& $current[0] === $new[0] && ( $current[1] ?? '0' ) === $new[1]
			&& version_compare( $offer->version, get_bloginfo( 'version' ), '>' );
	}

	/**
	 * Update-Angebote von wordpress.org (leer, solange WordPress noch nicht nachgefragt hat).
	 *
	 * @return object[]
	 */
	private static function core_offers() {
		$offers = get_core_updates();
		return is_array( $offers ) ? array_filter( $offers, 'is_object' ) : array();
	}

	private static function minor_offer() {
		foreach ( self::core_offers() as $offer ) {
			if ( self::is_minor( $offer ) ) {
				return $offer;
			}
		}
		return null;
	}

	/* ------------------------------------------------------------------ Wartung */

	public static function site_health() {
		self::admin_includes();
		require_once ABSPATH . 'wp-admin/includes/class-wp-site-health.php';
		$health  = WP_Site_Health::get_instance();
		$results = array();
		foreach ( (array) ( $health::get_tests()['direct'] ?? array() ) as $key => $test ) {
			$callback = null;
			if ( is_string( $test['test'] ) && method_exists( $health, 'get_test_' . $test['test'] ) ) {
				$callback = array( $health, 'get_test_' . $test['test'] );
			} elseif ( is_callable( $test['test'] ) ) {
				$callback = $test['test'];
			}
			if ( ! $callback ) {
				continue;
			}
			$result = call_user_func( $callback );
			if ( ! is_array( $result ) ) {
				continue;
			}
			$results[] = array(
				'test'        => $key,
				'label'       => wp_strip_all_tags( (string) ( $result['label'] ?? $test['label'] ?? $key ) ),
				'status'      => $result['status'] ?? null,
				'category'    => $result['badge']['label'] ?? null,
				'description' => trim( preg_replace( '/\s+/', ' ', html_entity_decode( wp_strip_all_tags( str_replace( '</p>', '</p> ', (string) ( $result['description'] ?? '' ) ) ), ENT_QUOTES ) ) ),
			);
		}
		$crons   = _get_cron_array();
		$overdue = 0;
		$next    = null;
		foreach ( (array) $crons as $timestamp => $events ) {
			if ( $timestamp < time() - 600 ) {
				$overdue += count( $events );
			}
			if ( null === $next || $timestamp < $next ) {
				$next = $timestamp;
			}
		}
		$counts = array_count_values( wp_list_pluck( $results, 'status' ) );
		return array(
			'summary'      => $counts,
			'php'          => PHP_VERSION,
			'memory_limit' => ini_get( 'memory_limit' ),
			'wp_memory'    => WP_MEMORY_LIMIT,
			'debug'        => array(
				'WP_DEBUG'         => defined( 'WP_DEBUG' ) && WP_DEBUG,
				'WP_DEBUG_LOG'     => defined( 'WP_DEBUG_LOG' ) && WP_DEBUG_LOG,
				'WP_DEBUG_DISPLAY' => defined( 'WP_DEBUG_DISPLAY' ) && WP_DEBUG_DISPLAY,
			),
			'cron'         => array(
				'disabled'       => defined( 'DISABLE_WP_CRON' ) && DISABLE_WP_CRON,
				'next_event_gmt' => $next ? gmdate( 'Y-m-d H:i:s', $next ) : null,
				'overdue_events' => $overdue,
			),
			'object_cache' => (bool) wp_using_ext_object_cache(),
			'https'        => 0 === strpos( home_url(), 'https://' ),
			'wptc'         => self::wptc_status(),
			'tests'        => $results,
		);
	}

	/**
	 * Stand von WP Time Capsule (Speicherdauer, Ziel, letzte erfolgreiche Sicherung), nur lesend.
	 * null, wenn WPTC nicht installiert ist.
	 */
	public static function wptc_status() {
		global $wpdb;
		$table = $wpdb->prefix . 'wptc_options';
		if ( $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $table ) ) ) !== $table ) {
			return null;
		}
		$rows = $wpdb->get_results(
			"SELECT name, value FROM `{$table}` WHERE name IN ('revision_limit','default_repo','last_backup_success_complete_time')", // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			OBJECT_K
		);
		$last = self::wptc_last_success( isset( $rows['last_backup_success_complete_time'] ) ? $rows['last_backup_success_complete_time']->value : '' );
		return array(
			'active'                 => is_plugin_active( 'wp-time-capsule/wp-time-capsule.php' ),
			'revision_limit_days'    => isset( $rows['revision_limit'] ) ? (int) $rows['revision_limit']->value : null,
			'repo'                   => isset( $rows['default_repo'] ) ? $rows['default_repo']->value : null,
			'last_success_gmt'       => $last['gmt'],
			'last_success_age_hours' => $last['age_hours'],
			'last_success_source'    => $last['source'],
		);
	}

	/**
	 * Speicherdauer von WP Time Capsule setzen (wie wptc_set_revision_limit im plesk-mcp).
	 */
	public static function wptc_set_revision_limit( $input ) {
		$table = self::wptc_table();
		if ( is_wp_error( $table ) ) {
			return $table;
		}
		$days = (int) ( $input['days'] ?? 30 );
		if ( $days < 1 || $days > 365 ) {
			return new WP_Error( 'tabside_mcp_wptc_days', 'days muss zwischen 1 und 365 liegen.' );
		}
		$old = self::wptc_revision_limit( $table );
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'revision_limit_days' => array(
						'old' => $old,
						'new' => $days,
					),
				),
				$old === $days ? array( 'Die Speicherdauer ist bereits so gesetzt.' ) : array()
			);
		}
		$backup = Tabside_MCP_Store::backup(
			'wptc',
			'revision_limit',
			array(
				'exists' => null !== $old,
				'value'  => $old,
			)
		);
		$written = self::wptc_write_revision_limit( $table, $days );
		if ( is_wp_error( $written ) ) {
			return $written;
		}
		$now = self::wptc_revision_limit( $table );
		return array(
			'confirmed'           => true,
			'backup_id'           => $backup,
			'revision_limit_days' => array(
				'old' => $old,
				'new' => $now,
			),
			'verify'              => array( 'revision_limit' => $now === $days ),
			'audit_target'        => 'wptc revision_limit ' . $days,
		);
	}

	/**
	 * Stellt revision_limit aus einer Sicherung (Art wptc) wieder her, aufgerufen von option-rollback.
	 *
	 * @param array $backup Eintrag aus Tabside_MCP_Store.
	 * @param bool  $confirm Ausführen oder nur Vorschau.
	 * @return array|WP_Error
	 */
	public static function wptc_rollback( array $backup, $confirm ) {
		$table = self::wptc_table();
		if ( is_wp_error( $table ) ) {
			return $table;
		}
		if ( 'paths' === $backup['target'] ) {
			return self::wptc_paths_rollback( $table, $backup, $confirm );
		}
		$target = $backup['data']['exists'] ? (int) $backup['data']['value'] : null;
		$old    = self::wptc_revision_limit( $table );
		if ( ! $confirm ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'backup_id'           => $backup['id'],
					'backup_time'         => $backup['created_gmt'] . ' UTC',
					'revision_limit_days' => array(
						'old' => $old,
						'new' => $target,
					),
				),
				null === $target ? array( 'revision_limit war vorher nicht gesetzt und wird entfernt (WPTC nimmt dann seinen Standard).' ) : array()
			);
		}
		$safety = Tabside_MCP_Store::backup(
			'wptc',
			'revision_limit',
			array(
				'exists' => null !== $old,
				'value'  => $old,
			)
		);
		global $wpdb;
		if ( null === $target ) {
			$wpdb->delete( $table, array( 'name' => 'revision_limit' ) );
		} else {
			$written = self::wptc_write_revision_limit( $table, $target );
			if ( is_wp_error( $written ) ) {
				return $written;
			}
		}
		$now = self::wptc_revision_limit( $table );
		return array(
			'confirmed'           => true,
			'restored_backup'     => $backup['id'],
			'safety_backup_id'    => $safety,
			'revision_limit_days' => array(
				'old' => $old,
				'new' => $now,
			),
			'verify'              => array( 'revision_limit' => $now === $target ),
			'audit_target'        => 'wptc revision_limit ' . ( null === $target ? '(entfernt)' : $target ),
		);
	}

	/** Sekunden ohne Lebenszeichen, ab denen eine Sicherung als hängend gilt (wie wptc_reset_backup_if_long_time_no_ping). */
	const WPTC_HANG_SECONDS = 3600;

	/**
	 * Detaillierter WPTC-Stand, nur lesend.
	 */
	public static function wptc_backup_status( $input ) {
		$table = self::wptc_table();
		if ( is_wp_error( $table ) ) {
			return $table;
		}
		global $wpdb;
		$o    = self::wptc_options( $table );
		$last = self::wptc_last_success( $o['last_backup_success_complete_time'] ?? '' );
		$cron = self::wptc_unserialize( $o['wptc_own_cron_status'] ?? '' );
		$tmp  = self::wptc_tmp_dir( $o );

		$warnings = array();
		if ( is_wp_error( $tmp ) ) {
			$warnings[] = 'Temp-Verzeichnis ungültig: ' . $tmp->get_error_message();
		}
		if ( null === $last['gmt'] ) {
			$warnings[] = 'Keine erfolgreiche Sicherung gefunden (weder in WPTC noch im Aktivitätsprotokoll).';
		} elseif ( $last['age_hours'] >= self::WPTC_STALE_HOURS ) {
			$warnings[] = sprintf( 'Letzte erfolgreiche Sicherung ist %d Stunden alt (%s UTC), älter als %d Stunden.', $last['age_hours'], $last['gmt'], self::WPTC_STALE_HOURS );
		}

		$queue  = array();
		$ptable = $wpdb->base_prefix . 'wptc_current_process';
		if ( self::table_exists( $ptable ) ) {
			foreach ( (array) $wpdb->get_results( "SELECT status, COUNT(*) AS n FROM `{$ptable}` GROUP BY status" ) as $row ) { // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				$label           = array(
					'P' => 'processed',
					'Q' => 'queued',
					'S' => 'skipped',
				)[ $row->status ] ?? $row->status;
				$queue[ $label ] = (int) $row->n;
			}
		}

		$backups = array();
		$btable  = $wpdb->base_prefix . 'wptc_backups';
		if ( self::table_exists( $btable ) ) {
			foreach ( (array) $wpdb->get_results( "SELECT backup_id, backup_name, backup_type, files_count FROM `{$btable}` ORDER BY id DESC LIMIT 5" ) as $row ) { // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
				$backups[] = array(
					'started_gmt' => is_numeric( $row->backup_id ) ? gmdate( 'Y-m-d H:i:s', (int) $row->backup_id ) : null,
					'name'        => $row->backup_name,
					'type'        => $row->backup_type,
					'files'       => (int) $row->files_count,
				);
			}
		}

		$log    = array();
		$limit  = max( 0, min( 100, (int) ( $input['log_limit'] ?? 20 ) ) );
		$ltable = $wpdb->base_prefix . 'wptc_activity_log';
		if ( $limit && self::table_exists( $ltable ) ) {
			$rows = $wpdb->get_results( $wpdb->prepare( "SELECT time, type, log_data FROM `{$ltable}` ORDER BY id DESC LIMIT %d", $limit ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			foreach ( (array) $rows as $row ) {
				$data  = self::wptc_unserialize( $row->log_data );
				$msg   = is_array( $data ) && isset( $data['msg'] ) && is_scalar( $data['msg'] ) ? (string) $data['msg'] : '';
				$log[] = array(
					'time_gmt' => is_array( $data ) && ! empty( $data['log_time'] ) ? gmdate( 'Y-m-d H:i:s', (int) $data['log_time'] ) : $row->time,
					'type'     => $row->type,
					'msg'      => function_exists( 'mb_substr' ) ? mb_substr( wp_strip_all_tags( $msg ), 0, 500 ) : substr( wp_strip_all_tags( $msg ), 0, 500 ),
				);
			}
		}

		return array(
			'active'          => true,
			'version'         => defined( 'WPTC_VERSION' ) ? WPTC_VERSION : null,
			'account'         => array(
				'email'             => $o['main_account_email'] ?? null,
				'logged_in'         => ! empty( $o['is_user_logged_in'] ),
				'server_connected'  => ! empty( $o['wptc_server_connected'] ),
				'service_requested' => ( $o['wptc_service_request'] ?? '' ) === 'yes',
				'cron_status'       => is_array( $cron ) ? $cron : null,
			),
			'warnings'        => $warnings,
			'repo'            => $o['default_repo'] ?? null,
			'tmp_dir'         => is_wp_error( $tmp ) ? null : $tmp,
			'tmp_dir_ok'      => ! is_wp_error( $tmp ),
			'tmp_dir_error'   => is_wp_error( $tmp ) ? $tmp->get_error_message() : null,
			'revision_limit'  => array(
				'days'     => isset( $o['revision_limit'] ) && '' !== $o['revision_limit'] ? (int) $o['revision_limit'] : null,
				'eligible' => isset( $o['eligible_revision_limit'] ) && '' !== $o['eligible_revision_limit'] ? (int) $o['eligible_revision_limit'] : null,
			),
			'schedule'        => array(
				'slot'     => $o['backup_slot'] ?? null,
				'time'     => $o['schedule_time_str'] ?? null,
				'timezone' => $o['wptc_timezone'] ?? null,
			),
			'last_success'    => $last + array( 'stale' => null === $last['gmt'] || $last['age_hours'] >= self::WPTC_STALE_HOURS ),
			'running'         => self::wptc_running( $o ),
			'queue'           => $queue,
			'recent_backups'  => $backups,
			'log'             => $log,
		);
	}

	/**
	 * WPTC-Sicherung starten, hängende vorher abbrechen.
	 */
	public static function wptc_backup_start( $input ) {
		$table = self::wptc_table();
		if ( is_wp_error( $table ) ) {
			return $table;
		}
		foreach ( array( 'start_fresh_backup_tc_callback_wptc', 'stop_fresh_backup_tc_callback_wptc', 'reset_backup_related_settings_wptc' ) as $fn ) {
			if ( ! function_exists( $fn ) ) {
				return new WP_Error( 'tabside_mcp_wptc_api', sprintf( 'WPTC-Funktion %s fehlt (andere WPTC-Version?).', $fn ) );
			}
		}
		$o       = self::wptc_options( $table );
		$running = self::wptc_running( $o );
		$abort   = ! empty( $input['abort_running'] );
		$notes   = array();

		if ( $running['restore'] ) {
			return new WP_Error( 'tabside_mcp_wptc_restore', 'Bei WPTC läuft eine Wiederherstellung, keine Sicherung gestartet.' );
		}
		if ( empty( $o['main_account_email'] ) || empty( $o['default_repo'] ) ) {
			return new WP_Error( 'tabside_mcp_wptc_setup', 'WPTC ist nicht mit einem Konto bzw. Backup-Ziel verbunden. Das geht nur im WPTC-Assistenten im wp-admin.' );
		}
		// Schutz vor einem WPTC-Fehler: Ist das Temp-Verzeichnis nicht gesetzt, leert WPTC beim Abbrechen
		// (force_complete -> make_folders_empty) die ganze WordPress-Installation. Dann gar nichts tun.
		$tmp = self::wptc_tmp_dir( $o );
		if ( is_wp_error( $tmp ) ) {
			return $tmp;
		}
		if ( $running['backup'] && ! $running['hanging'] && ! $abort ) {
			return new WP_Error(
				'tabside_mcp_wptc_running',
				sprintf( 'Es läuft bereits eine Sicherung (letztes Lebenszeichen vor %s Minuten), sie hängt nicht. Neu starten nur mit abort_running=true.', null === $running['last_ping_minutes'] ? '?' : $running['last_ping_minutes'] )
			);
		}
		$will_abort = $running['backup'];
		if ( $will_abort ) {
			$notes[] = $running['hanging'] ? 'Die laufende Sicherung hängt und wird abgebrochen und zurückgesetzt.' : 'Die laufende Sicherung wird auf Wunsch (abort_running) abgebrochen.';
		}
		if ( empty( $o['wptc_server_connected'] ) ) {
			$notes[] = 'WPTC meldet keine Verbindung zum WPTC-Dienst (wptc_server_connected leer). Der Start wird versucht, die Sicherung läuft aber nur, wenn der Dienst die Seite erreicht.';
		}

		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'running'      => $running,
					'abort_first'  => $will_abort,
					'last_success' => self::wptc_last_success( $o['last_backup_success_complete_time'] ?? '' )['gmt'],
				),
				$notes
			);
		}

		$errors = array();
		ob_start();
		try {
			self::wptc_bootstrap();
			if ( $will_abort ) {
				try {
					stop_fresh_backup_tc_callback_wptc( null, false );
				} catch ( Throwable $e ) {
					$errors[] = 'Abbrechen: ' . $e->getMessage();
				}
			}
			// Wie WPTC selbst bei BACKUP_START (wptc-cron-functions.php): zurücksetzen, dann manuell starten mit Verbindungstest.
			reset_backup_related_settings_wptc();
			if ( empty( $errors ) ) {
				start_fresh_backup_tc_callback_wptc( 'manual', array( 'type' => 'manual' ), true, false );
			}
		} catch ( Throwable $e ) {
			$errors[] = $e->getMessage();
		}
		$output = trim( (string) ob_get_clean() );

		$after = self::wptc_options( $table );
		$cron  = self::wptc_unserialize( $after['wptc_own_cron_status'] ?? '' );
		$state = self::wptc_running( $after );
		$ok    = $state['backup'] && empty( $errors );
		if ( $ok && empty( $after['wptc_server_connected'] ) ) {
			$ok       = false;
			$errors[] = 'Gestartet, aber WPTC ist nicht mit dem WPTC-Dienst verbunden (wptc_server_connected leer): ohne Dienst wird die Sicherung nicht abgearbeitet. WPTC im wp-admin neu anmelden.';
		}
		if ( is_array( $cron ) && 'success' !== ( $cron['status'] ?? '' ) ) {
			$ok       = false;
			$errors[] = 'Verbindungstest zum WPTC-Dienst fehlgeschlagen' . ( ! empty( $cron['body'] ) && is_scalar( $cron['body'] ) ? ': ' . substr( (string) $cron['body'], 0, 300 ) : '.' );
			// WPTC startet trotz fehlgeschlagenem Test weiter; ohne Dienst bliebe nur eine Leiche zurück.
			if ( $state['backup'] ) {
				reset_backup_related_settings_wptc();
				$state = self::wptc_running( self::wptc_options( $table ) );
			}
		}

		return array(
			'confirmed'    => true,
			'aborted'      => $will_abort,
			'started'      => $ok,
			'running'      => $state,
			'cron_status'  => is_array( $cron ) ? $cron : null,
			'errors'       => $errors,
			'output'       => '' === $output ? null : substr( wp_strip_all_tags( $output ), 0, 500 ),
			'hint'         => $ok ? 'Die Sicherung läuft über den WPTC-Dienst weiter. Fortschritt mit wptc_status prüfen.' : 'Nicht gestartet, siehe errors und cron_status.',
			'audit_target' => 'wptc backup start' . ( $will_abort ? ' (abgebrochen: ' . ( $running['hanging'] ? 'hängend' : 'laufend' ) . ')' : '' ),
		);
	}

	/** Stunden, ab denen die letzte erfolgreiche Sicherung als zu alt gemeldet wird. */
	const WPTC_STALE_HOURS = 48;

	/** Pfad-Einstellungen, die WPTC in Config::set_paths_flags() zusammen mit backup_db_path setzt. */
	const WPTC_PATH_OPTIONS = array( 'backup_db_path', 'site_abspath', 'site_db_name', 'wp_content_dir', 'is_wp_content_dir_moved_outside_root', 'site_db_prefix' );

	/** Inhalt der index.php, die WPTC (Config::create_silence_file) ins Temp-Verzeichnis legt. */
	const WPTC_SILENCE = "<?php\n// Silence is golden.\n";

	/**
	 * Letzte erfolgreiche Sicherung. WPTC setzt last_backup_success_complete_time selbst auf leer, wenn
	 * 72 Stunden lang keine Sicherung lief (check_and_send_no_backup_email). Dann gilt der neueste
	 * Eintrag "Backup complete." im Aktivitätsprotokoll (BackupController::add_completed_log).
	 *
	 * @param string $option Wert von last_backup_success_complete_time.
	 * @return array{gmt: ?string, age_hours: ?int, source: ?string}
	 */
	private static function wptc_last_success( $option ) {
		global $wpdb;
		$time   = is_numeric( $option ) ? (int) $option : 0;
		$source = $time ? 'option' : null;
		// WPTC 1.22.6 setzt den Wert beim Update einmalig auf "jetzt + 24 Stunden"; Zeiten in der Zukunft zählen nicht.
		if ( $time > time() ) {
			$time   = 0;
			$source = null;
		}
		$ltable = $wpdb->base_prefix . 'wptc_activity_log';
		if ( self::table_exists( $ltable ) ) {
			// LIKE vergleicht in MySQL ohne Gross-/Kleinschreibung und trifft auch "SQL backup complete.",
			// darum nur vorfiltern und die Meldung danach exakt prüfen.
			$rows = $wpdb->get_results( "SELECT log_data FROM `{$ltable}` WHERE type = 'backups' AND log_data LIKE '%ackup complete.%' ORDER BY id DESC LIMIT 50" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			foreach ( (array) $rows as $row ) {
				$data = self::wptc_unserialize( $row->log_data );
				if ( ! is_array( $data ) || 'Backup complete.' !== ( $data['msg'] ?? '' ) ) {
					continue;
				}
				$log = ! empty( $data['log_time'] ) ? (int) $data['log_time'] : 0;
				if ( $log > $time && $log <= time() ) {
					$time   = $log;
					$source = 'activity_log';
				}
				break;
			}
		}
		return array(
			'gmt'       => $time ? gmdate( 'Y-m-d H:i:s', $time ) : null,
			'age_hours' => $time ? (int) floor( ( time() - $time ) / 3600 ) : null,
			'source'    => $source,
		);
	}

	/**
	 * Gespeicherten WPTC-Pfad in einen vollen Pfad umwandeln, wie wptc_add_fullpath() in WPTC 1.22.x:
	 * WPTC speichert backup_db_path relativ (wptc_remove_fullpath), z.B. "/wp-content/uploads".
	 * Pfade unter wp-content werden an WP_CONTENT_DIR gehängt, andere an ABSPATH.
	 */
	private static function wptc_full_path( $file ) {
		$file        = wp_normalize_path( (string) $file );
		$content_dir = untrailingslashit( wp_normalize_path( WP_CONTENT_DIR ) );
		$abspath     = trailingslashit( wp_normalize_path( ABSPATH ) );
		$relative    = '/' . basename( $content_dir );
		if ( 0 === stripos( $file, $relative ) || 0 === stripos( $file, $content_dir ) ) {
			$temp = $file;
			if ( 0 === stripos( $file, $relative ) ) {
				$temp = (string) substr( $file, strlen( $relative ) );
				if ( '' === $temp || '/' === $temp ) {
					$temp = $content_dir;
				}
			}
			return false !== stripos( trailingslashit( $temp ), $content_dir . '/' ) ? $temp : $content_dir . '/' . ltrim( $temp, '/' );
		}
		return false !== stripos( trailingslashit( $file ), $abspath ) ? $file : $abspath . ltrim( $file, '/' );
	}

	/** Basis, die WPTC selbst wählt (Config::get_default_backup_dir): WP_CONTENT_DIR/uploads. */
	private static function wptc_default_base() {
		return untrailingslashit( wp_normalize_path( WP_CONTENT_DIR ) ) . '/uploads';
	}

	private static function wptc_temp_basename() {
		return defined( 'WPTC_TEMP_DIR_BASENAME' ) ? WPTC_TEMP_DIR_BASENAME : 'tCapsule';
	}

	/**
	 * Temp-Verzeichnis von WPTC ({backup_db_path}/tCapsule/backups), das beim Abbrechen geleert wird.
	 * Gültig nur, wenn es existiert und innerhalb von wp-content/uploads liegt.
	 *
	 * @param array<string, string> $o Optionen.
	 * @return string|WP_Error
	 */
	private static function wptc_tmp_dir( array $o ) {
		$stored = trim( (string) ( $o['backup_db_path'] ?? '' ) );
		if ( '' === $stored ) {
			return new WP_Error( 'tabside_mcp_wptc_tmp', 'Das Temp-Verzeichnis von WPTC (backup_db_path) ist nicht gesetzt. Aus Sicherheitsgründen nichts gestartet oder abgebrochen: WPTC würde sonst beim Abbrechen Dateien der Installation löschen. Reparieren mit wptc_repair.' );
		}
		$dir     = untrailingslashit( self::wptc_full_path( $stored ) ) . '/' . self::wptc_temp_basename() . '/backups';
		$real    = realpath( $dir );
		$uploads = realpath( self::wptc_default_base() );
		if ( false === $real || false === $uploads || ! is_dir( $real ) || 0 !== strpos( trailingslashit( wp_normalize_path( $real ) ), trailingslashit( wp_normalize_path( $uploads ) ) ) || wp_normalize_path( $real ) === wp_normalize_path( $uploads ) ) {
			return new WP_Error( 'tabside_mcp_wptc_tmp', sprintf( 'Das Temp-Verzeichnis von WPTC (%s) fehlt oder ist ungültig (nicht unter wp-content/uploads). Aus Sicherheitsgründen nichts gestartet oder abgebrochen. Reparieren mit wptc_repair.', $dir ) );
		}
		return wp_normalize_path( $real );
	}

	/**
	 * Zustand für wptc-repair, nur lesend.
	 *
	 * @param array<string, string> $o Optionen.
	 */
	private static function wptc_diagnose( array $o ) {
		clearstatcache();
		$base     = self::wptc_default_base();
		$target   = $base . '/' . self::wptc_temp_basename() . '/backups';
		$stored   = trim( (string) ( $o['backup_db_path'] ?? '' ) );
		$resolved = '' === $stored ? null : untrailingslashit( self::wptc_full_path( $stored ) );
		$exists   = is_dir( $target );
		$owner    = $exists ? @fileowner( $target ) : false; // phpcs:ignore
		$php_uid  = function_exists( 'posix_geteuid' ) ? posix_geteuid() : null;
		$name     = static function ( $uid ) {
			if ( false === $uid || null === $uid ) {
				return null;
			}
			$info = function_exists( 'posix_getpwuid' ) ? @posix_getpwuid( $uid ) : false; // phpcs:ignore
			return is_array( $info ) ? $info['name'] : (string) $uid;
		};
		$tmp      = self::wptc_tmp_dir( $o );
		$running  = self::wptc_running( $o );
		$links    = array();
		foreach ( array( $base . '/' . self::wptc_temp_basename(), $target ) as $path ) {
			if ( is_link( $path ) ) {
				$links[] = $path;
			}
		}
		return array(
			'tmp_dir'     => array(
				'path'       => $target,
				'exists'     => $exists,
				'writable'   => $exists && is_writable( $target ),
				'index_php'  => file_exists( $target . '/index.php' ),
				'owner'      => $name( $owner ),
				'php_user'   => $name( $php_uid ),
				'owner_ok'   => null === $php_uid || false === $owner ? null : $owner === $php_uid,
				'mode'       => $exists ? substr( sprintf( '%o', fileperms( $target ) ), -4 ) : null,
				'symlinks'   => $links,
				'uploads_ok' => is_dir( $base ) && is_writable( $base ),
				'valid'      => ! is_wp_error( $tmp ),
				'error'      => is_wp_error( $tmp ) ? $tmp->get_error_message() : null,
			),
			'path_option' => array(
				'backup_db_path' => '' === $stored ? null : $stored,
				'resolved'       => $resolved,
				'expected'       => $base,
				'ok'             => null !== $resolved && wp_normalize_path( $resolved ) === $base,
			),
			'running'     => $running,
			'account'     => array(
				'email'            => $o['main_account_email'] ?? null,
				'repo'             => $o['default_repo'] ?? null,
				'connected'        => ! empty( $o['main_account_email'] ) && ! empty( $o['default_repo'] ),
				'server_connected' => ! empty( $o['wptc_server_connected'] ),
			),
		);
	}

	/**
	 * WPTC reparieren: Temp-Verzeichnis und Pfad wie WPTC selbst anlegen, hängende Sicherung zurücksetzen, optional neu starten.
	 */
	public static function wptc_repair( $input ) {
		$table = self::wptc_table();
		if ( is_wp_error( $table ) ) {
			return $table;
		}
		foreach ( array( 'stop_fresh_backup_tc_callback_wptc', 'reset_backup_related_settings_wptc', 'start_fresh_backup_tc_callback_wptc' ) as $fn ) {
			if ( ! function_exists( $fn ) ) {
				return new WP_Error( 'tabside_mcp_wptc_api', sprintf( 'WPTC-Funktion %s fehlt (andere WPTC-Version?).', $fn ) );
			}
		}
		$start = ! array_key_exists( 'start_backup', (array) $input ) || ! empty( $input['start_backup'] );
		$abort = ! empty( $input['abort_running'] );
		$o     = self::wptc_options( $table );
		$diag  = self::wptc_diagnose( $o );
		$run   = $diag['running'];

		if ( $run['restore'] ) {
			return new WP_Error( 'tabside_mcp_wptc_restore', 'Bei WPTC läuft eine Wiederherstellung, es wird nichts repariert.' );
		}
		if ( $run['backup'] && ! $run['hanging'] && ! $abort ) {
			return new WP_Error(
				'tabside_mcp_wptc_running',
				sprintf( 'Es läuft eine Sicherung (letztes Lebenszeichen vor %s Minuten), sie hängt nicht. Reparieren nur mit abort_running=true.', null === $run['last_ping_minutes'] ? '?' : $run['last_ping_minutes'] )
			);
		}
		if ( ! empty( $diag['tmp_dir']['symlinks'] ) ) {
			return new WP_Error( 'tabside_mcp_wptc_symlink', 'Im Temp-Pfad von WPTC liegt ein symbolischer Link (' . implode( ', ', $diag['tmp_dir']['symlinks'] ) . '). Aus Sicherheitsgründen nichts geändert.' );
		}
		if ( ! $diag['tmp_dir']['uploads_ok'] ) {
			return new WP_Error( 'tabside_mcp_wptc_uploads', sprintf( 'Das Verzeichnis %s fehlt oder ist für PHP nicht beschreibbar, das Temp-Verzeichnis kann dort nicht angelegt werden.', self::wptc_default_base() ) );
		}
		if ( $diag['tmp_dir']['exists'] && ! $diag['tmp_dir']['writable'] ) {
			return new WP_Error( 'tabside_mcp_wptc_perm', sprintf( 'Das Temp-Verzeichnis %s existiert, ist aber für den PHP-Benutzer (%s) nicht beschreibbar (Besitzer %s, Rechte %s). Das lässt sich nur über das Hosting (Dateirechte) beheben.', $diag['tmp_dir']['path'], $diag['tmp_dir']['php_user'] ?? '?', $diag['tmp_dir']['owner'] ?? '?', $diag['tmp_dir']['mode'] ?? '?' ) );
		}

		$actions = array();
		if ( ! $diag['tmp_dir']['exists'] ) {
			$actions[] = 'create_tmp_dir';
		}
		if ( ! $diag['path_option']['ok'] ) {
			$actions[] = 'set_path_option';
		}
		if ( ! $diag['tmp_dir']['index_php'] ) {
			$actions[] = 'create_index_php';
		}
		if ( $run['backup'] ) {
			$actions[] = 'abort_backup';
		}
		$notes = array();
		if ( $start && ! $diag['account']['connected'] ) {
			$notes[] = 'WPTC ist nicht mit einem Konto bzw. Backup-Ziel verbunden: es wird nur repariert, keine Sicherung gestartet (Verbinden geht nur im WPTC-Assistenten im wp-admin).';
		} elseif ( $start ) {
			$actions[] = 'start_backup';
			if ( ! $diag['account']['server_connected'] ) {
				$notes[] = 'WPTC meldet keine Verbindung zum WPTC-Dienst (wptc_server_connected leer). Der Start wird versucht, die Sicherung läuft aber nur, wenn der Dienst die Seite erreicht.';
			}
		}
		if ( false === $diag['tmp_dir']['owner_ok'] ) {
			$notes[] = sprintf( 'Das Temp-Verzeichnis gehört %s, nicht dem PHP-Benutzer %s. Es ist beschreibbar und bleibt so.', $diag['tmp_dir']['owner'], $diag['tmp_dir']['php_user'] );
		}
		if ( empty( $actions ) ) {
			$notes[] = 'Nichts zu reparieren: Temp-Verzeichnis und Pfad sind in Ordnung, es läuft keine Sicherung.';
		}

		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'diagnosis' => $diag,
					'actions'   => $actions,
				),
				$notes
			);
		}
		if ( empty( $actions ) ) {
			return array(
				'confirmed' => true,
				'actions'   => array(),
				'diagnosis' => $diag,
				'notes'     => $notes,
				'verify'    => array( 'tmp_dir' => $diag['tmp_dir']['valid'] ),
			);
		}

		$saved = array();
		foreach ( self::wptc_raw_options( $table, self::WPTC_PATH_OPTIONS ) as $name => $value ) {
			$saved[ $name ] = array(
				'exists' => null !== $value,
				'value'  => $value,
			);
		}
		$backup = Tabside_MCP_Store::backup( 'wptc', 'paths', $saved );

		$errors = array();
		$done   = array();
		ob_start();
		try {
			self::wptc_bootstrap();
			if ( ! class_exists( 'WPTC_Factory' ) ) {
				throw new RuntimeException( 'WPTC_Factory fehlt (andere WPTC-Version?).' );
			}
			$config = WPTC_Factory::get( 'config' );
			if ( ! is_object( $config ) || ! method_exists( $config, 'choose_db_backup_path' ) ) {
				throw new RuntimeException( 'WPTC-Funktion Config::choose_db_backup_path fehlt (andere WPTC-Version?).' );
			}
			if ( in_array( 'create_tmp_dir', $actions, true ) || in_array( 'set_path_option', $actions, true ) || in_array( 'create_index_php', $actions, true ) ) {
				self::wptc_with_direct_fs(
					static function ( $fs ) use ( $config, $diag ) {
						// WPTC selbst: Ordner mit 0755 anlegen (createRecursiveFileSystemFolder) und backup_db_path samt Pfad-Flags setzen.
						$config->choose_db_backup_path();
						$index = $diag['tmp_dir']['path'] . '/index.php';
						if ( is_dir( $diag['tmp_dir']['path'] ) && ! $fs->exists( $index ) ) {
							// Wie Config::create_silence_file(), die im REST-Kontext nicht aufgerufen wird.
							$fs->put_contents( $index, self::WPTC_SILENCE );
						}
					}
				);
				$check = self::wptc_diagnose( self::wptc_options( $table ) );
				if ( ! $check['tmp_dir']['valid'] || ! $check['path_option']['ok'] ) {
					throw new RuntimeException( 'Temp-Verzeichnis bzw. Pfad danach nicht in Ordnung: ' . ( $check['tmp_dir']['error'] ?? 'backup_db_path zeigt auf ' . ( $check['path_option']['resolved'] ?? '(leer)' ) ) );
				}
				$done = array_values( array_intersect( $actions, array( 'create_tmp_dir', 'set_path_option', 'create_index_php' ) ) );
			}
			if ( in_array( 'abort_backup', $actions, true ) ) {
				// Erst jetzt, mit gültigem Temp-Verzeichnis: WPTC leert beim Abbrechen dieses Verzeichnis (force_complete).
				try {
					stop_fresh_backup_tc_callback_wptc( null, false );
				} catch ( Throwable $e ) {
					$errors[] = 'Abbrechen: ' . $e->getMessage();
				}
				reset_backup_related_settings_wptc();
				$done[] = 'abort_backup';
			}
		} catch ( Throwable $e ) {
			$errors[] = $e->getMessage();
		}
		$output = trim( (string) ob_get_clean() );

		$start_result = null;
		if ( empty( $errors ) && in_array( 'start_backup', $actions, true ) ) {
			$start_result = self::wptc_backup_start( array( 'confirm' => true ) );
			if ( is_wp_error( $start_result ) ) {
				$errors[]     = 'Start: ' . $start_result->get_error_message();
				$start_result = null;
			} else {
				unset( $start_result['audit_target'], $start_result['confirmed'] );
				if ( ! empty( $start_result['started'] ) ) {
					$done[] = 'start_backup';
				} else {
					$errors = array_merge( $errors, array_map( static fn( $e ) => 'Start: ' . $e, (array) $start_result['errors'] ) );
				}
			}
		}

		$after  = self::wptc_diagnose( self::wptc_options( $table ) );
		$verify = array(
			'tmp_dir'     => $after['tmp_dir']['valid'],
			'path_option' => $after['path_option']['ok'],
			'index_php'   => $after['tmp_dir']['index_php'],
			'owner'       => $after['tmp_dir']['owner_ok'],
		);
		if ( in_array( 'abort_backup', $actions, true ) || in_array( 'start_backup', $actions, true ) ) {
			$verify['backup'] = in_array( 'start_backup', $actions, true ) ? $after['running']['backup'] : ! $after['running']['backup'];
		}

		return array(
			'confirmed'    => true,
			'backup_id'    => $backup,
			'actions'      => $actions,
			'done'         => $done,
			'before'       => $diag,
			'after'        => $after,
			'backup_start' => $start_result,
			'verify'       => $verify,
			'errors'       => $errors,
			'notes'        => $notes,
			'output'       => '' === $output ? null : substr( wp_strip_all_tags( $output ), 0, 500 ),
			'hint'         => 'Pfad-Einstellungen zurück mit option_rollback und backup_id. Der angelegte Ordner bleibt bestehen, eine abgebrochene Sicherung lässt sich nicht wiederherstellen.',
			'audit_target' => 'wptc repair: ' . ( empty( $done ) ? '(nichts)' : implode( ', ', $done ) ),
		);
	}

	/**
	 * Führt $callback mit einem WP_Filesystem_Direct als globalem $wp_filesystem aus: WPTC legt Ordner und
	 * Dateien über $wp_filesystem an, so gehören sie dem PHP-Benutzer der Seite (nicht einem FTP-Benutzer).
	 */
	private static function wptc_with_direct_fs( callable $callback ) {
		global $wp_filesystem;
		require_once ABSPATH . 'wp-admin/includes/file.php';
		require_once ABSPATH . 'wp-admin/includes/class-wp-filesystem-base.php';
		require_once ABSPATH . 'wp-admin/includes/class-wp-filesystem-direct.php';
		// Wie WP_Filesystem() in wp-admin/includes/file.php.
		if ( ! defined( 'FS_CHMOD_DIR' ) ) {
			define( 'FS_CHMOD_DIR', ( fileperms( ABSPATH ) & 0777 | 0755 ) );
		}
		if ( ! defined( 'FS_CHMOD_FILE' ) ) {
			define( 'FS_CHMOD_FILE', ( fileperms( ABSPATH . 'index.php' ) & 0777 | 0644 ) );
		}
		$previous      = $wp_filesystem;
		$wp_filesystem = new WP_Filesystem_Direct( null ); // phpcs:ignore WordPress.WP.GlobalVariablesOverride.Prohibited
		try {
			$callback( $wp_filesystem );
		} finally {
			$wp_filesystem = $previous; // phpcs:ignore WordPress.WP.GlobalVariablesOverride.Prohibited
		}
	}

	/**
	 * Stellt die Pfad-Einstellungen aus einer Sicherung von wptc-repair wieder her.
	 */
	private static function wptc_paths_rollback( $table, array $backup, $confirm ) {
		global $wpdb;
		$saved   = (array) $backup['data'];
		$names   = array_values( array_intersect( array_keys( $saved ), self::WPTC_PATH_OPTIONS ) );
		$current = self::wptc_raw_options( $table, $names );
		$changes = array();
		foreach ( $names as $name ) {
			$changes[ $name ] = array(
				'old' => $current[ $name ],
				'new' => $saved[ $name ]['exists'] ? $saved[ $name ]['value'] : null,
			);
		}
		if ( ! $confirm ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'backup_id'   => $backup['id'],
					'backup_time' => $backup['created_gmt'] . ' UTC',
					'changes'     => $changes,
				),
				array( 'Nur die Pfad-Einstellungen von WPTC werden zurückgestellt. Der Ordner bleibt bestehen, eine abgebrochene oder gestartete Sicherung wird nicht rückgängig gemacht.' )
			);
		}
		$safety = array();
		foreach ( $current as $name => $value ) {
			$safety[ $name ] = array(
				'exists' => null !== $value,
				'value'  => $value,
			);
		}
		$safety_id = Tabside_MCP_Store::backup( 'wptc', 'paths', $safety );
		foreach ( $changes as $name => $change ) {
			if ( null === $change['new'] ) {
				$wpdb->delete( $table, array( 'name' => $name ) );
			} elseif ( null === $change['old'] ) {
				$wpdb->insert(
					$table,
					array(
						'name'  => $name,
						'value' => (string) $change['new'],
					)
				);
			} else {
				$wpdb->update( $table, array( 'value' => (string) $change['new'] ), array( 'name' => $name ) );
			}
		}
		$now    = self::wptc_raw_options( $table, $names );
		$verify = array();
		foreach ( $changes as $name => $change ) {
			$verify[ $name ] = $now[ $name ] === ( null === $change['new'] ? null : (string) $change['new'] );
		}
		return array(
			'confirmed'        => true,
			'restored_backup'  => $backup['id'],
			'safety_backup_id' => $safety_id,
			'changes'          => $changes,
			'verify'           => $verify,
			'audit_target'     => 'wptc paths rollback',
		);
	}

	/**
	 * Rohwerte aus der WPTC-Tabelle, null für nicht vorhandene.
	 *
	 * @param string[] $names Namen.
	 * @return array<string, ?string>
	 */
	private static function wptc_raw_options( $table, array $names ) {
		global $wpdb;
		$out = array_fill_keys( $names, null );
		if ( empty( $names ) ) {
			return $out;
		}
		$in   = implode( ',', array_fill( 0, count( $names ), '%s' ) );
		$rows = $wpdb->get_results( $wpdb->prepare( "SELECT name, value FROM `{$table}` WHERE name IN ({$in})", $names ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		foreach ( (array) $rows as $row ) {
			$out[ $row->name ] = (string) $row->value;
		}
		return $out;
	}

	/**
	 * WPTC lädt seine Klassen nur im wp-admin und bei Anfragen des WPTC-Dienstes (wptc_load_files).
	 * Für REST-Aufrufe dieselben Schritte ohne diese Bedingung, ohne die Admin-Hooks.
	 */
	private static function wptc_bootstrap() {
		if ( defined( 'DEFAULT_REPO' ) ) {
			return;
		}
		foreach ( array( 'include_primary_files_wptc', 'include_spl_files_wptc', 'store_bridge_compatibile_values_wptc' ) as $fn ) {
			if ( function_exists( $fn ) ) {
				$fn();
			}
		}
		do_action( 'just_initialized_wptc_h', '' );
		foreach ( array( 'wptc_set_fallback_db_search_1_14_0', 'define_default_repo_const_wptc' ) as $fn ) {
			if ( function_exists( $fn ) ) {
				$fn();
			}
		}
	}

	/**
	 * Laufzustand aus den WPTC-Optionen.
	 *
	 * @param array<string, string> $o Optionen.
	 */
	private static function wptc_running( array $o ) {
		$backup  = ! empty( $o['in_progress'] ) || ! empty( $o['is_running'] ) || ! empty( $o['wptc_main_cycle_running'] ) || ! empty( $o['auto_backup_running'] );
		$restore = ! empty( $o['in_progress_restore'] ) || ! empty( $o['is_bridge_process'] ) || ! empty( $o['is_running_restore'] ) || ( $o['wptc_update_progress'] ?? '' ) === 'start';
		$ping    = (int) ( $o['recent_backup_ping'] ?? 0 );
		$age     = $ping ? time() - $ping : null;
		return array(
			'backup'            => $backup,
			'restore'           => $restore,
			'current_action'    => ( $o['backup_current_action'] ?? '' ) ?: null,
			'last_ping_gmt'     => $ping ? gmdate( 'Y-m-d H:i:s', $ping ) : null,
			'last_ping_minutes' => null === $age ? null : (int) floor( $age / 60 ),
			'hanging'           => $backup && ( null === $age || $age > self::WPTC_HANG_SECONDS ),
		);
	}

	/**
	 * Die für Status und Start nötigen WPTC-Optionen.
	 *
	 * @return array<string, string>
	 */
	private static function wptc_options( $table ) {
		global $wpdb;
		$names = array(
			'main_account_email', 'is_user_logged_in', 'wptc_server_connected', 'wptc_service_request', 'wptc_own_cron_status',
			'default_repo', 'revision_limit', 'eligible_revision_limit', 'backup_slot', 'schedule_time_str', 'wptc_timezone',
			'last_backup_success_complete_time', 'in_progress', 'is_running', 'wptc_main_cycle_running', 'auto_backup_running',
			'in_progress_restore', 'is_bridge_process', 'is_running_restore', 'wptc_update_progress', 'recent_backup_ping',
			'backup_current_action', 'backup_db_path',
		);
		$in    = implode( ',', array_fill( 0, count( $names ), '%s' ) );
		$rows  = $wpdb->get_results( $wpdb->prepare( "SELECT name, value FROM `{$table}` WHERE name IN ({$in})", $names ) ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$out   = array();
		foreach ( (array) $rows as $row ) {
			$out[ $row->name ] = (string) $row->value;
		}
		return $out;
	}

	/**
	 * Serialisierte WPTC-Werte ohne Objekte lesen.
	 *
	 * @return mixed
	 */
	private static function wptc_unserialize( $value ) {
		if ( ! is_string( $value ) || '' === $value || ! is_serialized( $value ) ) {
			return null;
		}
		return @unserialize( $value, array( 'allowed_classes' => false ) ); // phpcs:ignore
	}

	private static function table_exists( $table ) {
		global $wpdb;
		return $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $table ) ) ) === $table;
	}

	/**
	 * Name der WPTC-Tabelle, wenn WPTC aktiv ist und die Tabelle existiert.
	 *
	 * @return string|WP_Error
	 */
	private static function wptc_table() {
		global $wpdb;
		self::admin_includes();
		if ( ! is_plugin_active( 'wp-time-capsule/wp-time-capsule.php' ) ) {
			return new WP_Error( 'tabside_mcp_wptc_inactive', 'WP Time Capsule ist auf dieser Seite nicht aktiv.' );
		}
		$table = $wpdb->prefix . 'wptc_options';
		if ( $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $table ) ) ) !== $table ) {
			return new WP_Error( 'tabside_mcp_wptc_table', sprintf( 'WPTC ist aktiv, aber die Tabelle %s fehlt.', $table ) );
		}
		return $table;
	}

	/**
	 * @return int|null Tage oder null, wenn nicht gesetzt.
	 */
	private static function wptc_revision_limit( $table ) {
		global $wpdb;
		$value = $wpdb->get_var( "SELECT value FROM `{$table}` WHERE name = 'revision_limit' LIMIT 1" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		return null === $value || '' === $value ? null : (int) $value;
	}

	/**
	 * @return true|WP_Error
	 */
	private static function wptc_write_revision_limit( $table, $days ) {
		global $wpdb;
		$exists = null !== $wpdb->get_var( "SELECT name FROM `{$table}` WHERE name = 'revision_limit' LIMIT 1" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		$result = $exists
			? $wpdb->update( $table, array( 'value' => (string) $days ), array( 'name' => 'revision_limit' ) )
			: $wpdb->insert(
				$table,
				array(
					'name'  => 'revision_limit',
					'value' => (string) $days,
				)
			);
		if ( false === $result ) {
			return new WP_Error( 'tabside_mcp_wptc_write', 'Schreiben in die WPTC-Tabelle fehlgeschlagen: ' . $wpdb->last_error );
		}
		return true;
	}

	public static function cache_flush( $input ) {
		$posts = ! empty( $input['post_id'] ) ? array( (int) $input['post_id'] ) : array();
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'scope'   => $posts ? 'posts' : 'site',
					'posts'   => $posts,
					'plugins' => array_keys( array_filter( Tabside_MCP_Cache::detected() ) ),
				),
				array()
			);
		}
		return array(
			'confirmed' => true,
			'cache'     => Tabside_MCP_Cache::flush( $posts ),
		);
	}

	/* ------------------------------------------------------------------ Benutzer */

	public static function user_list( $input ) {
		$limit = min( Tabside_MCP_Abilities::MAX_LIMIT, max( 1, (int) ( $input['limit'] ?? 50 ) ) );
		$args  = array(
			'number'      => $limit,
			'paged'       => max( 1, (int) ( $input['page'] ?? 1 ) ),
			'orderby'     => 'login',
			'count_total' => true,
		);
		if ( ! empty( $input['role'] ) ) {
			$args['role'] = (string) $input['role'];
		}
		if ( ! empty( $input['search'] ) ) {
			$args['search'] = '*' . (string) $input['search'] . '*';
		}
		$query = new WP_User_Query( $args );
		return array(
			'total' => (int) $query->get_total(),
			'users' => array_map( array( __CLASS__, 'user_summary' ), $query->get_results() ),
		);
	}

	public static function user_get( $input ) {
		$user = get_userdata( (int) $input['user_id'] );
		if ( ! $user ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Benutzer %d nicht gefunden.', $input['user_id'] ), array( 'status' => 404 ) );
		}
		$out                          = self::user_summary( $user );
		$out['first_name']            = $user->first_name;
		$out['last_name']             = $user->last_name;
		$out['description']           = $user->description;
		$out['url']                   = $user->user_url;
		$out['application_passwords'] = self::app_passwords( $user->ID );
		return $out;
	}

	public static function user_update( $input ) {
		$user = get_userdata( (int) $input['user_id'] );
		if ( ! $user ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Benutzer %d nicht gefunden.', $input['user_id'] ), array( 'status' => 404 ) );
		}
		$me       = wp_get_current_user();
		$is_admin = in_array( 'administrator', (array) $me->roles, true ) || is_super_admin();
		if ( in_array( 'administrator', (array) $user->roles, true ) && ! $is_admin ) {
			return new WP_Error( 'tabside_mcp_forbidden', 'Administratoren darf nur ein Administrator ändern.', array( 'status' => 403 ) );
		}
		$changes = array();
		$data    = array( 'ID' => $user->ID );
		foreach ( array( 'display_name', 'first_name', 'last_name', 'description' ) as $key ) {
			if ( isset( $input[ $key ] ) && (string) $input[ $key ] !== (string) $user->$key ) {
				$data[ $key ]    = (string) $input[ $key ];
				$changes[ $key ] = array(
					'old' => $user->$key,
					'new' => (string) $input[ $key ],
				);
			}
		}
		$role = null;
		if ( isset( $input['role'] ) ) {
			$role = (string) $input['role'];
			if ( $user->ID === $me->ID ) {
				return new WP_Error( 'tabside_mcp_own_role', 'Die eigene Rolle lässt sich über tabside MCP nicht ändern.', array( 'status' => 403 ) );
			}
			require_once ABSPATH . 'wp-admin/includes/user.php';
			$editable = get_editable_roles();
			if ( ! isset( $editable[ $role ] ) ) {
				return new WP_Error( 'tabside_mcp_role', sprintf( 'Rolle "%s" gibt es nicht oder darf nicht vergeben werden. Möglich: %s.', $role, implode( ', ', array_keys( $editable ) ) ) );
			}
			if ( 'administrator' === $role && ! $is_admin ) {
				return new WP_Error( 'tabside_mcp_forbidden', 'Die Rolle administrator darf nur ein Administrator vergeben.', array( 'status' => 403 ) );
			}
			if ( array( $role ) !== array_values( (array) $user->roles ) ) {
				$changes['role'] = array(
					'old' => array_values( (array) $user->roles ),
					'new' => $role,
				);
			} else {
				$role = null;
			}
		}
		if ( empty( $changes ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Änderung gegenüber dem aktuellen Stand.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'user'    => self::user_summary( $user ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Store::backup(
			'user',
			(string) $user->ID,
			array(
				'display_name' => $user->display_name,
				'first_name'   => $user->first_name,
				'last_name'    => $user->last_name,
				'description'  => $user->description,
				'roles'        => array_values( (array) $user->roles ),
			)
		);
		if ( count( $data ) > 1 ) {
			$result = wp_update_user( wp_slash( $data ) );
			if ( is_wp_error( $result ) ) {
				return $result;
			}
		}
		if ( null !== $role ) {
			$user->set_role( $role );
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'user'      => self::user_get( array( 'user_id' => $user->ID ) ),
		);
	}

	public static function application_password_list() {
		$user = wp_get_current_user();
		return array(
			'user'                  => $user->user_login,
			'application_passwords' => self::app_passwords( $user->ID ),
		);
	}

	public static function audit_log( $input ) {
		$limit = min( Tabside_MCP_Store::MAX_AUDIT, max( 1, (int) ( $input['limit'] ?? 50 ) ) );
		$user  = current_user_can( 'manage_options' ) ? ( empty( $input['user'] ) ? null : (string) $input['user'] ) : wp_get_current_user()->user_login;
		return array( 'entries' => Tabside_MCP_Store::audit( $limit, $user ) );
	}

	private static function app_passwords( $user_id ) {
		$out = array();
		foreach ( WP_Application_Passwords::get_user_application_passwords( $user_id ) as $item ) {
			$out[] = array(
				'uuid'      => $item['uuid'],
				'name'      => $item['name'],
				'created'   => $item['created'] ? gmdate( 'Y-m-d H:i:s', (int) $item['created'] ) : null,
				'last_used' => $item['last_used'] ? gmdate( 'Y-m-d H:i:s', (int) $item['last_used'] ) : null,
				'last_ip'   => $item['last_ip'] ?? null,
			);
		}
		return $out;
	}

	private static function user_summary( WP_User $user ) {
		$last = null;
		$apps = WP_Application_Passwords::get_user_application_passwords( $user->ID );
		foreach ( $apps as $item ) {
			if ( ! empty( $item['last_used'] ) && ( null === $last || $item['last_used'] > $last ) ) {
				$last = (int) $item['last_used'];
			}
		}
		return array(
			'id'                         => $user->ID,
			'login'                      => $user->user_login,
			'display_name'               => $user->display_name,
			'email'                      => $user->user_email,
			'roles'                      => array_values( (array) $user->roles ),
			'registered'                 => $user->user_registered,
			'application_passwords'      => count( $apps ),
			'last_application_login_gmt' => $last ? gmdate( 'Y-m-d H:i:s', $last ) : null,
		);
	}

	private static function forbidden( $capability ) {
		return new WP_Error( 'tabside_mcp_forbidden', sprintf( 'Fehlende Berechtigung: %s', $capability ), array( 'status' => 403 ) );
	}
}
