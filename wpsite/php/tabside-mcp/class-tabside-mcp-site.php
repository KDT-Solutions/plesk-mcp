<?php
/**
 * Navigation, Einstellungen, Erscheinungsbild: Menüs, Optionen (feste Whitelist), Customizer-CSS, klassische
 * Widgets und Weiterleitungen (nur mit dem Plugin Redirection).
 *
 * Vor jeder Änderung wird der alte Stand gesichert (Tabside_MCP_Store), zurückholen mit den *-rollback-Abilities.
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Site {

	/**
	 * Optionen, die gelesen und geändert werden dürfen (erweiterbar mit dem Filter tabside_mcp_option_whitelist).
	 */
	const OPTIONS = array(
		// Allgemein.
		'blogname', 'blogdescription', 'timezone_string', 'gmt_offset', 'date_format', 'time_format', 'start_of_week', 'WPLANG',
		// Lesen.
		'show_on_front', 'page_on_front', 'page_for_posts', 'posts_per_page', 'posts_per_rss', 'rss_use_excerpt', 'blog_public',
		// Diskussion.
		'default_pingback_flag', 'default_ping_status', 'default_comment_status', 'require_name_email', 'comment_registration',
		'close_comments_for_old_posts', 'close_comments_days_old', 'thread_comments', 'thread_comments_depth', 'page_comments',
		'comments_per_page', 'default_comments_page', 'comment_order', 'comments_notify', 'moderation_notify', 'comment_moderation',
		'comment_previously_approved', 'comment_max_links', 'moderation_keys', 'disallowed_keys', 'show_avatars', 'avatar_rating', 'avatar_default',
		// Medien.
		'thumbnail_size_w', 'thumbnail_size_h', 'thumbnail_crop', 'medium_size_w', 'medium_size_h', 'large_size_w', 'large_size_h',
		'uploads_use_yearmonth_folders',
		// Permalinks.
		'permalink_structure', 'category_base', 'tag_base',
	);

	/**
	 * Nie lesbar oder änderbar, auch nicht per Filter.
	 */
	const DENIED = array( 'siteurl', 'home', 'admin_email', 'new_admin_email', 'users_can_register', 'default_role', 'active_plugins', 'template', 'stylesheet', 'auto_update_plugins', 'auto_update_themes', 'recently_activated', 'cron', 'wp_user_roles' );

	const PERMALINK_OPTIONS = array( 'permalink_structure', 'category_base', 'tag_base' );

	/**
	 * @return array<string, array<string, mixed>>
	 */
	public static function definitions() {
		$confirm = Tabside_MCP_Abilities::confirm_schema();
		$menus   = static function () {
			return Tabside_MCP_Plugin::check_user( 'edit_theme_options' );
		};
		$options = static function () {
			return Tabside_MCP_Plugin::check_user( 'manage_options' );
		};
		$backup  = array(
			'type'        => 'string',
			'description' => 'ID der Sicherung, leer = neueste.',
		);
		$item    = array(
			'title'       => array( 'type' => 'string' ),
			'type'        => array(
				'type'        => 'string',
				'enum'        => array( 'custom', 'post_type', 'taxonomy' ),
				'description' => 'custom (eigene URL), post_type (Seite/Beitrag, object + object_id) oder taxonomy (Kategorie, object + object_id).',
			),
			'object'      => array(
				'type'        => 'string',
				'description' => 'Beitragstyp oder Taxonomie, z.B. page oder category.',
			),
			'object_id'   => array(
				'type'    => 'integer',
				'minimum' => 1,
			),
			'url'         => array( 'type' => 'string' ),
			'parent_id'   => array(
				'type'        => 'integer',
				'minimum'     => 0,
				'description' => 'Eltern-Eintrag, 0 = oberste Ebene.',
			),
			'position'    => array(
				'type'        => 'integer',
				'minimum'     => 1,
				'description' => 'Position im Menü (1 = zuerst), leer = am Ende.',
			),
			'target'      => array(
				'type' => 'string',
				'enum' => array( '', '_blank' ),
			),
			'classes'     => array( 'type' => 'string' ),
			'attr_title'  => array( 'type' => 'string' ),
			'description' => array( 'type' => 'string' ),
		);

		$defs = array(
			'menu-list'           => array(
				'label'       => 'Menüs auflisten',
				'description' => 'Menüs mit ID, Name, Anzahl Einträge und zugewiesenen Positionen, dazu die Menü-Positionen des Themes.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_list' ),
			),
			'menu-get'            => array(
				'label'       => 'Menü abrufen',
				'description' => 'Einträge eines Menüs als Baum (ID, Titel, Ziel, URL, Reihenfolge) und die vorhandenen Sicherungen.',
				'readonly'    => true,
				'properties'  => array( 'menu_id' => Tabside_MCP_Abilities::id_schema( 'ID des Menüs aus menu-list.' ) ),
				'required'    => array( 'menu_id' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_get' ),
			),
			'menu-item-create'    => array(
				'label'       => 'Menüeintrag anlegen',
				'description' => 'Legt einen Menüeintrag an (eigene URL, Seite/Beitrag oder Kategorie). Sicherung des Menüs vorher. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array_merge( array( 'menu_id' => Tabside_MCP_Abilities::id_schema( 'ID des Menüs.' ) ), $item, array( 'confirm' => $confirm ) ),
				'required'    => array( 'menu_id', 'type' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_item_create' ),
			),
			'menu-item-update'    => array(
				'label'       => 'Menüeintrag ändern',
				'description' => 'Ändert die übergebenen Felder eines Menüeintrags. Sicherung des Menüs vorher. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array_merge( array( 'item_id' => Tabside_MCP_Abilities::id_schema( 'ID des Eintrags aus menu-get.' ) ), $item, array( 'confirm' => $confirm ) ),
				'required'    => array( 'item_id' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_item_update' ),
			),
			'menu-item-move'      => array(
				'label'       => 'Menüeintrag verschieben',
				'description' => 'Verschiebt einen Menüeintrag unter parent_id (0 = oberste Ebene) an position. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'item_id'   => Tabside_MCP_Abilities::id_schema( 'ID des Eintrags.' ),
					'parent_id' => $item['parent_id'],
					'position'  => $item['position'],
					'confirm'   => $confirm,
				),
				'required'    => array( 'item_id' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_item_move' ),
			),
			'menu-item-remove'    => array(
				'label'       => 'Menüeintrag entfernen',
				'description' => 'Entfernt einen Menüeintrag (Papierkorb, Untereinträge rücken nach oben). Sicherung des Menüs vorher, zurückholen mit menu-rollback. Ohne confirm=true nur Vorschau.',
				'destructive' => true,
				'flush'       => 'site',
				'properties'  => array(
					'item_id' => Tabside_MCP_Abilities::id_schema( 'ID des Eintrags.' ),
					'confirm' => $confirm,
				),
				'required'    => array( 'item_id' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_item_remove' ),
			),
			'menu-locations-set'  => array(
				'label'       => 'Menü-Positionen setzen',
				'description' => 'Weist Menü-Positionen des Themes ein Menü zu ({"primary": 12}, 0 = keines). Nur die übergebenen Positionen. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'locations' => array(
						'type'                 => 'object',
						'additionalProperties' => array(
							'type'    => 'integer',
							'minimum' => 0,
						),
					),
					'confirm'   => $confirm,
				),
				'required'    => array( 'locations' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_locations_set' ),
			),
			'menu-rollback'       => array(
				'label'       => 'Menü zurückholen',
				'description' => 'Stellt ein Menü (Einträge) oder die Menü-Positionen aus einer Sicherung wieder her. Der aktuelle Stand wird vorher gesichert. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'backup_id' => $backup,
					'menu_id'   => array(
						'type'        => 'integer',
						'minimum'     => 1,
						'description' => 'Ohne backup_id: neueste Sicherung dieses Menüs.',
					),
					'confirm'   => $confirm,
				),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'menu_rollback' ),
			),
			'option-get'          => array(
				'label'       => 'Einstellungen lesen',
				'description' => 'Liest Einstellungen aus der Whitelist (Allgemein, Lesen, Diskussion, Medien, Permalinks). Ohne names alle erlaubten.',
				'readonly'    => true,
				'properties'  => array(
					'names' => array(
						'type'  => 'array',
						'items' => array( 'type' => 'string' ),
					),
				),
				'permission'  => $options,
				'execute'     => array( __CLASS__, 'option_get' ),
			),
			'option-update'       => array(
				'label'       => 'Einstellungen ändern',
				'description' => 'Ändert Einstellungen aus der Whitelist ({"blogname": "…", "posts_per_page": 10}). Werte werden wie im wp-admin geprüft. Sicherung vorher, zurückholen mit option-rollback. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'values'  => array(
						'type'                 => 'object',
						'additionalProperties' => array( 'type' => array( 'string', 'integer', 'number', 'boolean' ) ),
					),
					'confirm' => $confirm,
				),
				'required'    => array( 'values' ),
				'permission'  => $options,
				'execute'     => array( __CLASS__, 'option_update' ),
			),
			'option-rollback'     => array(
				'label'       => 'Einstellungen zurückholen',
				'description' => 'Stellt Einstellungen aus einer Sicherung wieder her (Standard: neueste). Mit der backup_id von wptc-set-revision-limit auch die WPTC-Speicherdauer, von wptc-repair die WPTC-Pfade. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'backup_id' => $backup,
					'confirm'   => $confirm,
				),
				'permission'  => $options,
				'execute'     => array( __CLASS__, 'option_rollback' ),
			),
			'custom-css-get'      => array(
				'label'       => 'Customizer-CSS lesen',
				'description' => 'Zusätzliches CSS des aktiven Themes (Customizer) und die vorhandenen Sicherungen.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'edit_css' );
				},
				'execute'     => array( __CLASS__, 'custom_css_get' ),
			),
			'custom-css-update'   => array(
				'label'       => 'Customizer-CSS ändern',
				'description' => 'Ersetzt das zusätzliche CSS des aktiven Themes. Vorschau mit geänderten Zeilen. Sicherung vorher, zurückholen mit custom-css-rollback. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'css'     => array( 'type' => 'string' ),
					'confirm' => $confirm,
				),
				'required'    => array( 'css' ),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'edit_css' );
				},
				'execute'     => array( __CLASS__, 'custom_css_update' ),
			),
			'custom-css-rollback' => array(
				'label'       => 'Customizer-CSS zurückholen',
				'description' => 'Stellt das zusätzliche CSS aus einer Sicherung wieder her (Standard: neueste). Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'backup_id' => $backup,
					'confirm'   => $confirm,
				),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'edit_css' );
				},
				'execute'     => array( __CLASS__, 'custom_css_rollback' ),
			),
			'widget-list'         => array(
				'label'       => 'Widgets auflisten',
				'description' => 'Seitenleisten des Themes mit ihren klassischen Widgets und deren Einstellungen, dazu inaktive Widgets.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'widget_list' ),
			),
			'widget-update'       => array(
				'label'       => 'Widget ändern',
				'description' => 'Ändert Einstellungen eines Widgets (nur übergebene Keys, null entfernt) und verschiebt es optional in eine andere Seitenleiste (sidebar, position). Sicherung vorher, zurückholen mit widget-rollback. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'widget_id' => array(
						'type'        => 'string',
						'description' => 'Widget-ID aus widget-list, z.B. text-2 oder block-3.',
					),
					'settings'  => array(
						'type'                 => 'object',
						'additionalProperties' => true,
					),
					'sidebar'   => array(
						'type'        => 'string',
						'description' => 'Seitenleiste, wp_inactive_widgets = deaktivieren.',
					),
					'position'  => array(
						'type'    => 'integer',
						'minimum' => 1,
					),
					'confirm'   => $confirm,
				),
				'required'    => array( 'widget_id' ),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'widget_update' ),
			),
			'widget-rollback'     => array(
				'label'       => 'Widgets zurückholen',
				'description' => 'Stellt Widget-Einstellungen und Seitenleisten aus einer Sicherung wieder her (Standard: neueste). Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'backup_id' => $backup,
					'confirm'   => $confirm,
				),
				'permission'  => $menus,
				'execute'     => array( __CLASS__, 'widget_rollback' ),
			),
		);

		if ( 'redirection' === self::redirect_plugin() ) {
			$redirect = array(
				'source' => array(
					'type'        => 'string',
					'description' => 'Quell-Pfad, z.B. /alte-seite/ (bei regex=true ein regulärer Ausdruck).',
				),
				'target' => array(
					'type'        => 'string',
					'description' => 'Ziel-URL oder -Pfad.',
				),
				'code'   => array(
					'type' => 'integer',
					'enum' => array( 301, 302, 307, 308 ),
				),
				'regex'  => array( 'type' => 'boolean' ),
				'title'  => array( 'type' => 'string' ),
			);
			$defs    += array(
				'redirect-list'   => array(
					'label'       => 'Weiterleitungen auflisten',
					'description' => 'Weiterleitungen des Plugins Redirection, Suche in Quelle oder Ziel.',
					'readonly'    => true,
					'properties'  => array(
						'search' => array( 'type' => 'string' ),
						'limit'  => array(
							'type'    => 'integer',
							'minimum' => 5,
							'maximum' => 200,
							'default' => 50,
						),
						'page'   => array(
							'type'    => 'integer',
							'minimum' => 1,
							'default' => 1,
						),
					),
					'permission'  => $options,
					'execute'     => array( __CLASS__, 'redirect_list' ),
				),
				'redirect-create' => array(
					'label'       => 'Weiterleitung anlegen',
					'description' => 'Legt eine Weiterleitung im Plugin Redirection an (Standard 301). Ohne confirm=true nur Vorschau.',
					'properties'  => array_merge(
						$redirect,
						array(
							'group_id' => array(
								'type'        => 'integer',
								'minimum'     => 1,
								'description' => 'Gruppe, leer = erste Gruppe.',
							),
							'confirm'  => $confirm,
						)
					),
					'required'    => array( 'source', 'target' ),
					'permission'  => $options,
					'execute'     => array( __CLASS__, 'redirect_create' ),
				),
				'redirect-update' => array(
					'label'       => 'Weiterleitung ändern',
					'description' => 'Ändert eine Weiterleitung oder schaltet sie ab (enabled=false, kein Löschen). Alter Stand wird gesichert. Ohne confirm=true nur Vorschau.',
					'properties'  => array_merge(
						array( 'redirect_id' => Tabside_MCP_Abilities::id_schema( 'ID aus redirect-list.' ) ),
						$redirect,
						array(
							'enabled' => array( 'type' => 'boolean' ),
							'confirm' => $confirm,
						)
					),
					'required'    => array( 'redirect_id' ),
					'permission'  => $options,
					'execute'     => array( __CLASS__, 'redirect_update' ),
				),
			);
		}
		return $defs;
	}

	/**
	 * Erkanntes Weiterleitungs-Plugin, das unterstützt wird (derzeit nur Redirection).
	 */
	public static function redirect_plugin() {
		return class_exists( 'Red_Item' ) && class_exists( 'Red_Group' ) ? 'redirection' : null;
	}

	/* ------------------------------------------------------------------ Menüs */

	public static function menu_list() {
		$locations  = get_nav_menu_locations();
		$registered = get_registered_nav_menus();
		$menus      = array();
		foreach ( wp_get_nav_menus() as $menu ) {
			$menus[] = array(
				'id'        => (int) $menu->term_id,
				'name'      => $menu->name,
				'slug'      => $menu->slug,
				'count'     => (int) $menu->count,
				'locations' => array_keys( array_filter( $locations, static function ( $id ) use ( $menu ) {
					return (int) $id === (int) $menu->term_id;
				} ) ),
			);
		}
		$out_locations = array();
		foreach ( $registered as $location => $label ) {
			$out_locations[] = array(
				'location' => $location,
				'label'    => $label,
				'menu_id'  => (int) ( $locations[ $location ] ?? 0 ),
			);
		}
		return array(
			'menus'     => $menus,
			'locations' => $out_locations,
		);
	}

	public static function menu_get( $input ) {
		$menu = wp_get_nav_menu_object( (int) $input['menu_id'] );
		if ( ! $menu ) {
			return self::menu_missing( $input['menu_id'] );
		}
		return array(
			'menu'    => array(
				'id'   => (int) $menu->term_id,
				'name' => $menu->name,
			),
			'items'   => self::item_tree( self::menu_items( $menu->term_id ) ),
			'backups' => Tabside_MCP_Store::backup_summary( array( 'menu' ), (string) $menu->term_id ),
		);
	}

	public static function menu_item_create( $input ) {
		$menu = wp_get_nav_menu_object( (int) $input['menu_id'] );
		if ( ! $menu ) {
			return self::menu_missing( $input['menu_id'] );
		}
		$args = self::item_args( $input, null );
		if ( is_wp_error( $args ) ) {
			return $args;
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'menu' => $menu->name,
					'item' => self::describe_args( $args ),
				),
				array()
			);
		}
		$backup = self::backup_menu( $menu->term_id );
		$id     = wp_update_nav_menu_item( $menu->term_id, 0, wp_slash( $args ) );
		if ( is_wp_error( $id ) ) {
			return $id;
		}
		return self::menu_result( $menu->term_id, $backup, (int) $id );
	}

	public static function menu_item_update( $input ) {
		$item = self::load_item( (int) $input['item_id'] );
		if ( is_wp_error( $item ) ) {
			return $item;
		}
		$args = self::item_args( $input, $item );
		if ( is_wp_error( $args ) ) {
			return $args;
		}
		return self::save_item( $item, $args, $input );
	}

	public static function menu_item_move( $input ) {
		$item = self::load_item( (int) $input['item_id'] );
		if ( is_wp_error( $item ) ) {
			return $item;
		}
		$args = self::item_args( array_intersect_key( $input, array_flip( array( 'parent_id', 'position' ) ) ), $item );
		if ( is_wp_error( $args ) ) {
			return $args;
		}
		return self::save_item( $item, $args, $input );
	}

	public static function menu_item_remove( $input ) {
		$item = self::load_item( (int) $input['item_id'] );
		if ( is_wp_error( $item ) ) {
			return $item;
		}
		$menu_id  = self::item_menu( $item->ID );
		$children = array_filter( self::menu_items( $menu_id ), static function ( $child ) use ( $item ) {
			return (int) $child->menu_item_parent === $item->ID;
		} );
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'item'     => self::item_summary( $item ),
					'children' => count( $children ),
				),
				array( 'Untereinträge rücken eine Ebene nach oben. Zurückholen mit menu-rollback.' )
			);
		}
		$backup = self::backup_menu( $menu_id );
		foreach ( $children as $child ) {
			update_post_meta( $child->ID, '_menu_item_menu_item_parent', (int) $item->menu_item_parent );
		}
		wp_update_post(
			array(
				'ID'          => $item->ID,
				'post_status' => 'trash',
			)
		);
		return self::menu_result( $menu_id, $backup );
	}

	public static function menu_locations_set( $input ) {
		$registered = get_registered_nav_menus();
		$current    = get_nav_menu_locations();
		$new        = $current;
		$changes    = array();
		foreach ( (array) $input['locations'] as $location => $menu_id ) {
			if ( ! isset( $registered[ $location ] ) ) {
				return new WP_Error( 'tabside_mcp_menu_location', sprintf( 'Menü-Position "%s" gibt es im Theme nicht. Vorhanden: %s.', $location, implode( ', ', array_keys( $registered ) ) ) );
			}
			$menu_id = (int) $menu_id;
			if ( $menu_id > 0 && ! wp_get_nav_menu_object( $menu_id ) ) {
				return self::menu_missing( $menu_id );
			}
			$new[ $location ]     = $menu_id;
			$changes[ $location ] = array(
				'old' => (int) ( $current[ $location ] ?? 0 ),
				'new' => $menu_id,
			);
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( array( 'changes' => $changes ), array() );
		}
		$backup = Tabside_MCP_Store::backup( 'menu_locations', 'locations', $current );
		set_theme_mod( 'nav_menu_locations', array_map( 'intval', $new ) );
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'verify'    => array( 'locations' => get_nav_menu_locations() == array_map( 'intval', $new ) ), // phpcs:ignore Universal.Operators.StrictComparisons.LooseEqual
			'locations' => get_nav_menu_locations(),
		);
	}

	public static function menu_rollback( $input ) {
		$target = empty( $input['backup_id'] ) && ! empty( $input['menu_id'] ) ? (string) (int) $input['menu_id'] : null;
		$backup = Tabside_MCP_Store::find_backup( array( 'menu', 'menu_locations' ), $input['backup_id'] ?? null, $target );
		if ( null === $backup ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Keine passende Sicherung vorhanden.' );
		}
		if ( 'menu_locations' === $backup['kind'] ) {
			if ( empty( $input['confirm'] ) ) {
				return Tabside_MCP_Abilities::preview_result(
					array(
						'backup_id' => $backup['id'],
						'locations' => array(
							'old' => get_nav_menu_locations(),
							'new' => $backup['data'],
						),
					),
					array()
				);
			}
			$safety = Tabside_MCP_Store::backup( 'menu_locations', 'locations', get_nav_menu_locations() );
			set_theme_mod( 'nav_menu_locations', array_map( 'intval', (array) $backup['data'] ) );
			return array(
				'confirmed'        => true,
				'restored_backup'  => $backup['id'],
				'safety_backup_id' => $safety,
				'locations'        => get_nav_menu_locations(),
			);
		}
		$menu_id = (int) $backup['target'];
		if ( ! wp_get_nav_menu_object( $menu_id ) ) {
			return self::menu_missing( $menu_id );
		}
		$items = (array) $backup['data']['items'];
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'backup_id'   => $backup['id'],
					'backup_time' => $backup['created_gmt'] . ' UTC',
					'menu_id'     => $menu_id,
					'items_now'   => count( self::menu_items( $menu_id ) ),
					'items_after' => count( $items ),
				),
				array()
			);
		}
		$safety  = self::backup_menu( $menu_id );
		$keep    = array();
		$map     = array();
		$pending = $items;
		// Eltern zuerst, damit neu angelegte Einträge die neue Eltern-ID bekommen.
		usort( $pending, static function ( $a, $b ) {
			return (int) $a['position'] - (int) $b['position'];
		} );
		for ( $round = 0; $pending && $round < 20; $round++ ) {
			$next = array();
			foreach ( $pending as $data ) {
				$parent = (int) $data['parent_id'];
				if ( $parent > 0 && ! isset( $map[ $parent ] ) && in_array( $parent, wp_list_pluck( $items, 'id' ), true ) ) {
					$next[] = $data;
					continue;
				}
				$existing = get_post( (int) $data['id'] );
				$item_id  = $existing && 'nav_menu_item' === $existing->post_type ? $existing->ID : 0;
				$args     = array(
					'menu-item-object-id'   => (int) $data['object_id'],
					'menu-item-object'      => $data['object'],
					'menu-item-parent-id'   => $parent > 0 ? ( $map[ $parent ] ?? 0 ) : 0,
					'menu-item-position'    => (int) $data['position'],
					'menu-item-type'        => $data['type'],
					'menu-item-title'       => $data['title'],
					'menu-item-url'         => $data['url'],
					'menu-item-description' => $data['description'],
					'menu-item-attr-title'  => $data['attr_title'],
					'menu-item-target'      => $data['target'],
					'menu-item-classes'     => $data['classes'],
					'menu-item-xfn'         => $data['xfn'],
					'menu-item-status'      => 'publish',
				);
				$new_id   = wp_update_nav_menu_item( $menu_id, $item_id, wp_slash( $args ) );
				if ( is_wp_error( $new_id ) ) {
					return $new_id;
				}
				$map[ (int) $data['id'] ] = (int) $new_id;
				$keep[]                   = (int) $new_id;
			}
			$pending = $next;
		}
		foreach ( self::menu_items( $menu_id ) as $item ) {
			if ( ! in_array( $item->ID, $keep, true ) ) {
				wp_update_post(
					array(
						'ID'          => $item->ID,
						'post_status' => 'trash',
					)
				);
			}
		}
		return array_merge(
			self::menu_result( $menu_id, '' ),
			array(
				'restored_backup'  => $backup['id'],
				'safety_backup_id' => $safety,
				'backup_id'        => '',
			)
		);
	}

	private static function menu_items( $menu_id ) {
		$items = wp_get_nav_menu_items( $menu_id, array( 'post_status' => 'publish' ) );
		return is_array( $items ) ? $items : array();
	}

	private static function item_menu( $item_id ) {
		$terms = wp_get_object_terms( $item_id, 'nav_menu', array( 'fields' => 'ids' ) );
		return is_wp_error( $terms ) || empty( $terms ) ? 0 : (int) $terms[0];
	}

	/**
	 * @return WP_Post|WP_Error Eintrag mit den Feldern von wp_setup_nav_menu_item().
	 */
	private static function load_item( $id ) {
		$post = get_post( $id );
		if ( ! $post || 'nav_menu_item' !== $post->post_type || 'trash' === $post->post_status ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Menüeintrag %d nicht gefunden.', $id ), array( 'status' => 404 ) );
		}
		return wp_setup_nav_menu_item( $post );
	}

	private static function menu_missing( $id ) {
		return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Menü %d nicht gefunden (menu-list).', $id ), array( 'status' => 404 ) );
	}

	/**
	 * Vollständige Argumente für wp_update_nav_menu_item() (die Funktion setzt fehlende Felder sonst zurück).
	 *
	 * @return array<string, mixed>|WP_Error
	 */
	private static function item_args( $input, $item ) {
		$args = array(
			'menu-item-object-id'   => $item ? (int) $item->object_id : 0,
			'menu-item-object'      => $item ? $item->object : '',
			'menu-item-parent-id'   => $item ? (int) $item->menu_item_parent : 0,
			'menu-item-position'    => $item ? (int) $item->menu_order : 0,
			'menu-item-type'        => $item ? $item->type : 'custom',
			'menu-item-title'       => $item ? $item->post_title : '',
			'menu-item-url'         => $item ? $item->url : '',
			'menu-item-description' => $item ? $item->post_content : '',
			'menu-item-attr-title'  => $item ? $item->post_excerpt : '',
			'menu-item-target'      => $item ? $item->target : '',
			'menu-item-classes'     => $item ? implode( ' ', array_filter( (array) $item->classes ) ) : '',
			'menu-item-xfn'         => $item ? $item->xfn : '',
			'menu-item-status'      => 'publish',
		);
		$map  = array(
			'title'       => 'menu-item-title',
			'type'        => 'menu-item-type',
			'object'      => 'menu-item-object',
			'object_id'   => 'menu-item-object-id',
			'url'         => 'menu-item-url',
			'parent_id'   => 'menu-item-parent-id',
			'position'    => 'menu-item-position',
			'target'      => 'menu-item-target',
			'classes'     => 'menu-item-classes',
			'attr_title'  => 'menu-item-attr-title',
			'description' => 'menu-item-description',
		);
		foreach ( $map as $key => $arg ) {
			if ( isset( $input[ $key ] ) ) {
				$args[ $arg ] = $input[ $key ];
			}
		}
		$type = $args['menu-item-type'];
		if ( 'custom' === $type ) {
			if ( '' === trim( (string) $args['menu-item-url'] ) ) {
				return new WP_Error( 'tabside_mcp_menu_item', 'Eigener Link (type custom) braucht url.' );
			}
			$args['menu-item-object']    = 'custom';
			$args['menu-item-object-id'] = $item && 'custom' === $item->type ? (int) $item->object_id : 0;
		} elseif ( 'post_type' === $type ) {
			$post = get_post( (int) $args['menu-item-object-id'] );
			if ( ! $post || 'trash' === $post->post_status ) {
				return new WP_Error( 'tabside_mcp_menu_item', sprintf( 'Beitrag %d für den Menüeintrag nicht gefunden.', $args['menu-item-object-id'] ) );
			}
			$args['menu-item-object'] = $post->post_type;
			$args['menu-item-url']    = '';
		} elseif ( 'taxonomy' === $type ) {
			$term = get_term( (int) $args['menu-item-object-id'] );
			if ( ! $term instanceof WP_Term ) {
				return new WP_Error( 'tabside_mcp_menu_item', sprintf( 'Begriff %d für den Menüeintrag nicht gefunden.', $args['menu-item-object-id'] ) );
			}
			$args['menu-item-object'] = $term->taxonomy;
			$args['menu-item-url']    = '';
		}
		$parent = (int) $args['menu-item-parent-id'];
		if ( $parent > 0 ) {
			$parent_post = get_post( $parent );
			if ( ! $parent_post || 'nav_menu_item' !== $parent_post->post_type || ( $item && $item->ID === $parent ) ) {
				return new WP_Error( 'tabside_mcp_menu_item', sprintf( 'Eltern-Eintrag %d ungültig.', $parent ) );
			}
		}
		return $args;
	}

	private static function save_item( $item, array $args, array $input ) {
		$menu_id = self::item_menu( $item->ID );
		$old     = self::item_args( array(), $item );
		$changes = array();
		foreach ( $args as $key => $value ) {
			if ( (string) $old[ $key ] !== (string) $value ) {
				$changes[ str_replace( 'menu-item-', '', $key ) ] = array(
					'old' => $old[ $key ],
					'new' => $value,
				);
			}
		}
		if ( empty( $changes ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Änderung gegenüber dem aktuellen Stand.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'item'    => self::item_summary( $item ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = self::backup_menu( $menu_id );
		$id     = wp_update_nav_menu_item( $menu_id, $item->ID, wp_slash( $args ) );
		if ( is_wp_error( $id ) ) {
			return $id;
		}
		return self::menu_result( $menu_id, $backup, $item->ID );
	}

	private static function backup_menu( $menu_id ) {
		$items = array();
		foreach ( self::menu_items( $menu_id ) as $item ) {
			$items[] = array(
				'id'          => (int) $item->ID,
				'title'       => $item->post_title,
				'type'        => $item->type,
				'object'      => $item->object,
				'object_id'   => (int) $item->object_id,
				'url'         => $item->url,
				'parent_id'   => (int) $item->menu_item_parent,
				'position'    => (int) $item->menu_order,
				'target'      => $item->target,
				'classes'     => implode( ' ', array_filter( (array) $item->classes ) ),
				'attr_title'  => $item->post_excerpt,
				'description' => $item->post_content,
				'xfn'         => $item->xfn,
			);
		}
		return Tabside_MCP_Store::backup( 'menu', (string) $menu_id, array( 'items' => $items ) );
	}

	private static function menu_result( $menu_id, $backup, $item_id = 0 ) {
		$out = array(
			'confirmed' => true,
			'backup_id' => $backup,
			'menu_id'   => (int) $menu_id,
			'items'     => self::item_tree( self::menu_items( $menu_id ) ),
		);
		if ( $item_id ) {
			$item        = get_post( $item_id );
			$out['item'] = $item ? self::item_summary( wp_setup_nav_menu_item( $item ) ) : null;
		}
		return $out;
	}

	private static function item_summary( $item ) {
		return array(
			'id'        => (int) $item->ID,
			'title'     => $item->title,
			'type'      => $item->type,
			'object'    => $item->object,
			'object_id' => (int) $item->object_id,
			'url'       => $item->url,
			'parent_id' => (int) $item->menu_item_parent,
			'position'  => (int) $item->menu_order,
			'target'    => $item->target,
			'classes'   => implode( ' ', array_filter( (array) $item->classes ) ),
		);
	}

	private static function item_tree( array $items ) {
		$by_parent = array();
		foreach ( $items as $item ) {
			$by_parent[ (int) $item->menu_item_parent ][] = self::item_summary( $item );
		}
		$build = static function ( $parent ) use ( &$build, $by_parent ) {
			$out = array();
			foreach ( $by_parent[ $parent ] ?? array() as $entry ) {
				$children = $build( $entry['id'] );
				if ( $children ) {
					$entry['children'] = $children;
				}
				$out[] = $entry;
			}
			return $out;
		};
		return $build( 0 );
	}

	private static function describe_args( array $args ) {
		$out = array();
		foreach ( $args as $key => $value ) {
			$out[ str_replace( 'menu-item-', '', $key ) ] = $value;
		}
		return $out;
	}

	/* ------------------------------------------------------------------ Optionen */

	/**
	 * @return string[]
	 */
	public static function allowed_options() {
		$list = (array) apply_filters( 'tabside_mcp_option_whitelist', self::OPTIONS );
		return array_values( array_filter( array_unique( array_map( 'strval', $list ) ), array( __CLASS__, 'option_allowed' ) ) );
	}

	/**
	 * Sperrliste gilt immer, auch für per Filter ergänzte Optionen.
	 */
	public static function option_allowed( $name ) {
		if ( in_array( $name, self::DENIED, true ) ) {
			return false;
		}
		return ! preg_match( '/(key|token|secret|pass|auth|salt|nonce|licen[cs]e|credential|api)/i', $name );
	}

	public static function option_get( $input ) {
		$allowed = self::allowed_options();
		$names   = empty( $input['names'] ) ? $allowed : array_map( 'strval', (array) $input['names'] );
		$values  = array();
		foreach ( $names as $name ) {
			if ( ! in_array( $name, $allowed, true ) ) {
				return self::option_denied( $name );
			}
			$values[ $name ] = get_option( $name );
		}
		return array(
			'values'  => $values,
			'backups' => Tabside_MCP_Store::backup_summary( array( 'option' ) ),
		);
	}

	public static function option_update( $input ) {
		$allowed = self::allowed_options();
		$values  = array();
		$changes = array();
		foreach ( (array) $input['values'] as $name => $value ) {
			$name = (string) $name;
			if ( ! in_array( $name, $allowed, true ) ) {
				return self::option_denied( $name );
			}
			$value = is_bool( $value ) ? (int) $value : $value;
			$check = self::check_option( $name, $value );
			if ( is_wp_error( $check ) ) {
				return $check;
			}
			$clean = sanitize_option( $name, $value );
			if ( '' === $clean && '' !== (string) $value && ! in_array( $name, array( 'timezone_string' ), true ) ) {
				return new WP_Error( 'tabside_mcp_option_value', sprintf( 'Wert für %s ungültig.', $name ) );
			}
			$values[ $name ]  = $clean;
			$changes[ $name ] = array(
				'old' => get_option( $name ),
				'new' => $clean,
			);
		}
		if ( empty( $values ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Einstellungen übergeben.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( array( 'changes' => $changes ), array() );
		}
		$old = array();
		foreach ( array_keys( $values ) as $name ) {
			$old[ $name ] = array(
				'exists' => false !== get_option( $name, false ),
				'value'  => get_option( $name ),
			);
		}
		$backup = Tabside_MCP_Store::backup( 'option', implode( ',', array_keys( $values ) ), $old );
		foreach ( $values as $name => $value ) {
			update_option( $name, $value );
		}
		if ( array_intersect( array_keys( $values ), self::PERMALINK_OPTIONS ) ) {
			flush_rewrite_rules( false );
		}
		$verify = array();
		foreach ( $values as $name => $value ) {
			$verify[ $name ] = (string) get_option( $name ) === (string) $value;
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'verify'    => $verify,
			'values'    => array_combine( array_keys( $values ), array_map( 'get_option', array_keys( $values ) ) ),
		);
	}

	public static function option_rollback( $input ) {
		// WPTC-Sicherungen (wptc-set-revision-limit, wptc-repair) nur mit ausdrücklicher backup_id, ohne ID gilt die neueste Einstellungs-Sicherung.
		$kinds  = empty( $input['backup_id'] ) ? array( 'option' ) : array( 'option', 'wptc' );
		$backup = Tabside_MCP_Store::find_backup( $kinds, $input['backup_id'] ?? null );
		if ( null === $backup ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Keine passende Sicherung vorhanden.' );
		}
		if ( 'wptc' === $backup['kind'] ) {
			return Tabside_MCP_Admin::wptc_rollback( $backup, ! empty( $input['confirm'] ) );
		}
		$changes = array();
		foreach ( (array) $backup['data'] as $name => $old ) {
			$changes[ $name ] = array(
				'old' => get_option( $name ),
				'new' => $old['exists'] ? $old['value'] : '(nicht gesetzt)',
			);
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'backup_id'   => $backup['id'],
					'backup_time' => $backup['created_gmt'] . ' UTC',
					'changes'     => $changes,
				),
				array()
			);
		}
		$current = array();
		foreach ( array_keys( (array) $backup['data'] ) as $name ) {
			$current[ $name ] = array(
				'exists' => false !== get_option( $name, false ),
				'value'  => get_option( $name ),
			);
		}
		$safety = Tabside_MCP_Store::backup( 'option', $backup['target'], $current );
		foreach ( (array) $backup['data'] as $name => $old ) {
			if ( ! self::option_allowed( $name ) ) {
				continue;
			}
			if ( $old['exists'] ) {
				update_option( $name, $old['value'] );
			} else {
				delete_option( $name );
			}
		}
		if ( array_intersect( array_keys( (array) $backup['data'] ), self::PERMALINK_OPTIONS ) ) {
			flush_rewrite_rules( false );
		}
		return array(
			'confirmed'        => true,
			'restored_backup'  => $backup['id'],
			'safety_backup_id' => $safety,
			'values'           => array_combine( array_keys( (array) $backup['data'] ), array_map( 'get_option', array_keys( (array) $backup['data'] ) ) ),
		);
	}

	private static function option_denied( $name ) {
		return new WP_Error( 'tabside_mcp_option_denied', sprintf( 'Die Einstellung "%s" ist über tabside MCP nicht freigegeben.', $name ), array( 'status' => 403 ) );
	}

	/**
	 * Prüfungen, die sanitize_option() nicht macht.
	 *
	 * @return true|WP_Error
	 */
	private static function check_option( $name, $value ) {
		switch ( $name ) {
			case 'show_on_front':
				return in_array( $value, array( 'posts', 'page' ), true ) ? true : new WP_Error( 'tabside_mcp_option_value', 'show_on_front: posts oder page.' );
			case 'page_on_front':
			case 'page_for_posts':
				$id   = (int) $value;
				$post = $id > 0 ? get_post( $id ) : null;
				return 0 === $id || ( $post && 'page' === $post->post_type && 'trash' !== $post->post_status ) ? true : new WP_Error( 'tabside_mcp_option_value', sprintf( '%s: Seite %d nicht gefunden.', $name, $id ) );
			case 'timezone_string':
				return '' === $value || in_array( $value, timezone_identifiers_list(), true ) ? true : new WP_Error( 'tabside_mcp_option_value', sprintf( 'Zeitzone "%s" unbekannt, z.B. Europe/Zurich.', $value ) );
			case 'default_comment_status':
			case 'default_ping_status':
				return in_array( $value, array( 'open', 'closed' ), true ) ? true : new WP_Error( 'tabside_mcp_option_value', $name . ': open oder closed.' );
		}
		return true;
	}

	/* ------------------------------------------------------------------ Customizer-CSS */

	public static function custom_css_get() {
		$post = wp_get_custom_css_post();
		return array(
			'theme'   => get_stylesheet(),
			'post_id' => $post ? $post->ID : null,
			'css'     => wp_get_custom_css(),
			'backups' => Tabside_MCP_Store::backup_summary( array( 'custom_css' ), get_stylesheet() ),
		);
	}

	public static function custom_css_update( $input ) {
		return self::write_css( (string) $input['css'], ! empty( $input['confirm'] ), null );
	}

	public static function custom_css_rollback( $input ) {
		$backup = Tabside_MCP_Store::find_backup( array( 'custom_css' ), $input['backup_id'] ?? null, empty( $input['backup_id'] ) ? get_stylesheet() : null );
		if ( null === $backup ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Keine passende Sicherung vorhanden.' );
		}
		if ( $backup['target'] !== get_stylesheet() ) {
			return new WP_Error( 'tabside_mcp_css_theme', sprintf( 'Die Sicherung gehört zum Theme %s, aktiv ist %s.', $backup['target'], get_stylesheet() ) );
		}
		return self::write_css( (string) $backup['data'], ! empty( $input['confirm'] ), $backup['id'] );
	}

	private static function write_css( $css, $confirm, $restored ) {
		$old  = wp_get_custom_css();
		$diff = self::line_diff( $old, $css );
		if ( ! $confirm ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'theme'     => get_stylesheet(),
					'chars_old' => strlen( $old ),
					'chars_new' => strlen( $css ),
					'diff'      => $diff,
				),
				$old === $css ? array( 'Das CSS ist bereits so gesetzt.' ) : array()
			);
		}
		$backup = Tabside_MCP_Store::backup( 'custom_css', get_stylesheet(), $old );
		$result = wp_update_custom_css_post( $css );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		$out = array(
			'confirmed' => true,
			'backup_id' => $backup,
			'verify'    => array( 'css' => wp_get_custom_css() === $css ),
			'diff'      => $diff,
		);
		if ( $restored ) {
			$out['restored_backup']  = $restored;
			$out['safety_backup_id'] = $backup;
		}
		return $out;
	}

	/**
	 * Entfernte und neue Zeilen (höchstens je 50).
	 */
	private static function line_diff( $old, $new ) {
		$a = '' === (string) $old ? array() : preg_split( '/\r\n|\r|\n/', (string) $old );
		$b = '' === (string) $new ? array() : preg_split( '/\r\n|\r|\n/', (string) $new );
		return array(
			'removed' => array_slice( array_values( array_diff( $a, $b ) ), 0, 50 ),
			'added'   => array_slice( array_values( array_diff( $b, $a ) ), 0, 50 ),
		);
	}

	/* ------------------------------------------------------------------ Widgets */

	public static function widget_list() {
		global $wp_registered_sidebars;
		$sidebars = wp_get_sidebars_widgets();
		$out      = array();
		foreach ( (array) $wp_registered_sidebars as $id => $sidebar ) {
			$out[] = array(
				'id'          => $id,
				'name'        => $sidebar['name'],
				'description' => $sidebar['description'],
				'widgets'     => array_map( array( __CLASS__, 'widget_info' ), (array) ( $sidebars[ $id ] ?? array() ) ),
			);
		}
		return array(
			'sidebars' => $out,
			'inactive' => array_map( array( __CLASS__, 'widget_info' ), (array) ( $sidebars['wp_inactive_widgets'] ?? array() ) ),
			'notes'    => empty( $out ) ? array( 'Das aktive Theme registriert keine Seitenleisten (z.B. Block-Theme oder Elementor-Theme-Builder).' ) : array(),
		);
	}

	public static function widget_update( $input ) {
		$widget_id = (string) $input['widget_id'];
		$parsed    = self::parse_widget_id( $widget_id );
		if ( null === $parsed ) {
			return new WP_Error( 'tabside_mcp_widget', sprintf( 'Widget-ID "%s" ungültig (Format name-zahl).', $widget_id ) );
		}
		list( $base, $number ) = $parsed;
		$option                = 'widget_' . $base;
		$instances             = get_option( $option );
		if ( ! is_array( $instances ) || ! isset( $instances[ $number ] ) ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Widget %s nicht gefunden (widget-list).', $widget_id ), array( 'status' => 404 ) );
		}
		$sidebars = wp_get_sidebars_widgets();
		$current  = self::widget_sidebar( $sidebars, $widget_id );
		$changes  = array();
		$new      = (array) $instances[ $number ];
		foreach ( (array) ( $input['settings'] ?? array() ) as $key => $value ) {
			$changes[ 'settings.' . $key ] = array(
				'old' => $new[ $key ] ?? null,
				'new' => $value,
			);
			if ( null === $value ) {
				unset( $new[ $key ] );
			} else {
				$new[ $key ] = $value;
			}
		}
		$new_sidebars = $sidebars;
		if ( isset( $input['sidebar'] ) ) {
			global $wp_registered_sidebars;
			$target = (string) $input['sidebar'];
			if ( 'wp_inactive_widgets' !== $target && ! isset( $wp_registered_sidebars[ $target ] ) ) {
				return new WP_Error( 'tabside_mcp_widget', sprintf( 'Seitenleiste "%s" gibt es nicht.', $target ) );
			}
			foreach ( $new_sidebars as $id => $list ) {
				if ( is_array( $list ) ) {
					$new_sidebars[ $id ] = array_values( array_diff( $list, array( $widget_id ) ) );
				}
			}
			$list = (array) ( $new_sidebars[ $target ] ?? array() );
			$pos  = isset( $input['position'] ) ? min( (int) $input['position'] - 1, count( $list ) ) : count( $list );
			array_splice( $list, $pos, 0, array( $widget_id ) );
			$new_sidebars[ $target ] = $list;
			$changes['sidebar']      = array(
				'old' => $current,
				'new' => $target . ' #' . ( $pos + 1 ),
			);
		}
		if ( empty( $changes ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'settings oder sidebar übergeben.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'widget'  => self::widget_info( $widget_id ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Store::backup(
			'widget',
			$widget_id,
			array(
				'sidebars' => $sidebars,
				'option'   => $option,
				'value'    => $instances,
			)
		);
		$instances[ $number ] = $new;
		update_option( $option, $instances );
		if ( $new_sidebars !== $sidebars ) {
			wp_set_sidebars_widgets( $new_sidebars );
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'verify'    => array( 'settings' => get_option( $option )[ $number ] === $new ),
			'widget'    => self::widget_info( $widget_id ),
		);
	}

	public static function widget_rollback( $input ) {
		$backup = Tabside_MCP_Store::find_backup( array( 'widget' ), $input['backup_id'] ?? null );
		if ( null === $backup ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Keine passende Sicherung vorhanden.' );
		}
		$data = $backup['data'];
		if ( 0 !== strpos( (string) $data['option'], 'widget_' ) ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Sicherung ungültig.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'backup_id'   => $backup['id'],
					'backup_time' => $backup['created_gmt'] . ' UTC',
					'widget'      => $backup['target'],
				),
				array( 'Stellt die Einstellungen aller Widgets dieses Typs und die Belegung der Seitenleisten zum Zeitpunkt der Sicherung wieder her.' )
			);
		}
		$safety = Tabside_MCP_Store::backup(
			'widget',
			$backup['target'],
			array(
				'sidebars' => wp_get_sidebars_widgets(),
				'option'   => $data['option'],
				'value'    => get_option( $data['option'] ),
			)
		);
		update_option( $data['option'], $data['value'] );
		wp_set_sidebars_widgets( $data['sidebars'] );
		return array(
			'confirmed'        => true,
			'restored_backup'  => $backup['id'],
			'safety_backup_id' => $safety,
			'widget'           => self::widget_info( $backup['target'] ),
		);
	}

	private static function parse_widget_id( $widget_id ) {
		if ( ! preg_match( '/^(.+)-(\d+)$/', $widget_id, $m ) ) {
			return null;
		}
		return array( $m[1], (int) $m[2] );
	}

	private static function widget_sidebar( array $sidebars, $widget_id ) {
		foreach ( $sidebars as $id => $list ) {
			if ( is_array( $list ) && in_array( $widget_id, $list, true ) ) {
				return $id;
			}
		}
		return null;
	}

	public static function widget_info( $widget_id ) {
		$parsed = self::parse_widget_id( (string) $widget_id );
		$out    = array(
			'id'      => (string) $widget_id,
			'sidebar' => self::widget_sidebar( wp_get_sidebars_widgets(), (string) $widget_id ),
		);
		if ( $parsed ) {
			$instances       = get_option( 'widget_' . $parsed[0] );
			$out['type']     = $parsed[0];
			$out['settings'] = is_array( $instances ) && isset( $instances[ $parsed[1] ] ) ? $instances[ $parsed[1] ] : null;
		}
		return $out;
	}

	/* ------------------------------------------------------------------ Weiterleitungen (Redirection) */

	public static function redirect_list( $input ) {
		$params = array(
			'per_page' => (int) ( $input['limit'] ?? 50 ),
			'page'     => max( 0, (int) ( $input['page'] ?? 1 ) - 1 ),
		);
		if ( ! empty( $input['search'] ) ) {
			$params['filterBy'] = array( 'url' => (string) $input['search'] );
		}
		$result = Red_Item::get_filtered( $params );
		return array(
			'total'     => (int) $result['total'],
			'redirects' => array_map( array( __CLASS__, 'redirect_summary' ), $result['items'] ),
			'groups'    => array_map(
				static function ( $group ) {
					return array(
						'id'   => (int) $group['id'],
						'name' => $group['name'],
					);
				},
				Red_Group::get_all()
			),
		);
	}

	public static function redirect_create( $input ) {
		$group = (int) ( $input['group_id'] ?? 0 );
		if ( 0 === $group ) {
			$groups = Red_Group::get_all();
			$group  = $groups ? (int) $groups[0]['id'] : 0;
		}
		if ( 0 === $group ) {
			return new WP_Error( 'tabside_mcp_redirect_group', 'Redirection ist noch nicht eingerichtet (keine Gruppe vorhanden). Einmal Werkzeuge > Redirection im wp-admin öffnen.' );
		}
		$details = array(
			'url'         => (string) $input['source'],
			'match_type'  => 'url',
			'action_type' => 'url',
			'action_code' => (int) ( $input['code'] ?? 301 ),
			'action_data' => array( 'url' => (string) $input['target'] ),
			'regex'       => ! empty( $input['regex'] ) ? 1 : 0,
			'group_id'    => $group,
			'title'       => (string) ( $input['title'] ?? '' ),
		);
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'source'   => $details['url'],
					'target'   => $input['target'],
					'code'     => $details['action_code'],
					'regex'    => (bool) $details['regex'],
					'group_id' => $group,
				),
				array()
			);
		}
		$item = Red_Item::create( $details );
		if ( is_wp_error( $item ) ) {
			return $item;
		}
		return array(
			'confirmed' => true,
			'redirect'  => self::redirect_summary( $item->to_json() ),
		);
	}

	public static function redirect_update( $input ) {
		$item = Red_Item::get_by_id( (int) $input['redirect_id'] );
		if ( ! $item ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Weiterleitung %d nicht gefunden.', $input['redirect_id'] ), array( 'status' => 404 ) );
		}
		$old     = $item->to_json();
		$details = array(
			'url'         => $input['source'] ?? $old['url'],
			'match_type'  => $old['match_type'],
			'action_type' => $old['action_type'],
			'action_code' => (int) ( $input['code'] ?? $old['action_code'] ),
			'action_data' => isset( $input['target'] ) ? array( 'url' => (string) $input['target'] ) : ( is_array( $old['action_data'] ) ? $old['action_data'] : array( 'url' => (string) $old['action_data'] ) ),
			'regex'       => isset( $input['regex'] ) ? ( $input['regex'] ? 1 : 0 ) : ( $old['regex'] ? 1 : 0 ),
			'group_id'    => $old['group_id'],
			'title'       => $input['title'] ?? $old['title'],
			'position'    => $old['position'],
			'match_data'  => $old['match_data'],
		);
		$changes = array();
		foreach ( array( 'source' => 'url', 'code' => 'action_code', 'regex' => 'regex', 'title' => 'title' ) as $key => $field ) {
			if ( isset( $input[ $key ] ) ) {
				$changes[ $key ] = array(
					'old' => $old[ $field ],
					'new' => $input[ $key ],
				);
			}
		}
		if ( isset( $input['target'] ) ) {
			$changes['target'] = array(
				'old' => self::redirect_summary( $old )['target'],
				'new' => $input['target'],
			);
		}
		if ( isset( $input['enabled'] ) ) {
			$changes['enabled'] = array(
				'old' => (bool) $old['enabled'],
				'new' => (bool) $input['enabled'],
			);
		}
		if ( empty( $changes ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Felder zum Ändern übergeben.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'redirect' => self::redirect_summary( $old ),
					'changes'  => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Store::backup( 'redirect', (string) $item->get_id(), $old );
		if ( array_diff_key( $changes, array( 'enabled' => true ) ) ) {
			$result = $item->update( $details );
			if ( is_wp_error( $result ) ) {
				return $result;
			}
		}
		if ( isset( $input['enabled'] ) ) {
			if ( $input['enabled'] ) {
				$item->enable();
			} else {
				$item->disable();
			}
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'redirect'  => self::redirect_summary( Red_Item::get_by_id( $item->get_id() )->to_json() ),
		);
	}

	private static function redirect_summary( array $json ) {
		$target = $json['action_data'] ?? null;
		if ( is_array( $target ) ) {
			$target = $target['url'] ?? wp_json_encode( $target );
		}
		return array(
			'id'          => (int) $json['id'],
			'source'      => $json['url'],
			'target'      => $target,
			'code'        => (int) $json['action_code'],
			'type'        => $json['action_type'],
			'regex'       => (bool) $json['regex'],
			'enabled'     => (bool) $json['enabled'],
			'title'       => $json['title'],
			'group_id'    => (int) $json['group_id'],
			'hits'        => (int) $json['hits'],
			'last_access' => $json['last_access'],
		);
	}
}
