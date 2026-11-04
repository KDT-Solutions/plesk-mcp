<?php
/**
 * Elementor: Seiten, Vorlagen und Popups bis auf Widget-Ebene lesen und ändern.
 *
 * Die Abilities werden nur registriert, wenn Elementor aktiv ist. Die Hilfen für Elementor-Daten
 * (IDs, Sicherung, Wiederherstellung) funktionieren auch ohne Elementor, damit Kopien und Rollbacks
 * die Daten nie beschädigen.
 *
 * Gespeichert wird über die Elementor-Dokument-API (Revisionen, CSS, Caches macht Elementor selbst). Nur
 * für Dokumenttypen, die Elementor ohne Pro nicht kennt (z.B. Popups), direkt ins Meta mit wp_slash().
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Elementor {

	const DATA_KEY      = '_elementor_data';
	const SETTINGS_KEY  = '_elementor_page_settings';
	const MAX_BYTES     = 5242880;
	const MAX_FULL      = 300000;
	const MAX_HITS      = 200;
	const CACHE_META    = array( '_elementor_css', '_elementor_element_cache', '_elementor_page_assets', '_elementor_inline_svg' );
	// Vorlagentypen mit Anzeigebedingungen (Elementor Pro).
	const CONDITION_TYPES = array( 'popup', 'header', 'footer', 'single', 'single-post', 'single-page', 'archive', 'search-results', 'error-404', 'product', 'product-archive', 'loop-item' );
	// Seiteneinstellungen, die Elementor in Beitragsfelder umschreibt: nur über post-update.
	const POST_FIELD_SETTINGS = array( 'post_title', 'post_status', 'post_excerpt', 'post_featured_image', 'template' );

	/**
	 * Steuerelemente pro Element-Typ (Name => Tab), Cache für Zusammenfassungen.
	 *
	 * @var array<string, array<string, string>>
	 */
	private static $control_tabs = array();

	public static function active() {
		return did_action( 'elementor/loaded' ) && class_exists( '\Elementor\Plugin' ) && defined( 'ELEMENTOR_VERSION' );
	}

	public static function pro_active() {
		return defined( 'ELEMENTOR_PRO_VERSION' );
	}

	/**
	 * Beitragstypen, die Elementor bearbeitet.
	 *
	 * @return string[]
	 */
	public static function supported_types() {
		if ( ! self::active() ) {
			return array();
		}
		$types   = (array) get_option( 'elementor_cpt_support', array( 'page', 'post' ) );
		$types[] = 'elementor_library';
		return array_values( array_unique( array_map( 'strval', $types ) ) );
	}

	/* ------------------------------------------------------------------ Definitionen */

	/**
	 * @return array<string, array<string, mixed>>
	 */
	public static function definitions() {
		if ( ! self::active() ) {
			return array();
		}
		$confirm  = Tabside_MCP_Abilities::confirm_schema();
		$post_id  = Tabside_MCP_Abilities::id_schema( 'ID der Seite, Vorlage oder des Popups.' );
		$element  = array(
			'type'        => 'string',
			'description' => 'Element-ID aus elementor-get.',
		);
		$id_map   = array(
			'type'                 => 'object',
			'additionalProperties' => array( 'type' => array( 'integer', 'string' ) ),
		);
		$settings = array(
			'type'                 => 'object',
			'additionalProperties' => true,
		);
		$can_edit = static function ( $input ) {
			return Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['post_id'] ?? 0 ), 'edit_post' );
		};
		$edit     = static function () {
			return Tabside_MCP_Plugin::check_user( 'edit_posts' );
		};

		return array(
			'elementor-info'                 => array(
				'label'       => 'Elementor-Info',
				'description' => 'Elementor- und Pro-Version, aktive Experimente (Container), Kit mit globalen Farben und Schriften, Breakpoints.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $edit,
				'execute'     => array( __CLASS__, 'info' ),
			),
			'elementor-get'                  => array(
				'label'       => 'Elementor-Struktur',
				'description' => 'Struktur einer Elementor-Seite/-Vorlage als Baum: pro Element id, elType, widgetType, Pfad und eine lesbare Zusammenfassung (Texte, Links, Bilder, Icons, verknüpfte Popups und Vorlagen). full=true liefert alle Settings, element_id nur einen Teilbaum.',
				'readonly'    => true,
				'properties'  => array(
					'post_id'    => $post_id,
					'element_id' => $element,
					'full'       => array(
						'type'    => 'boolean',
						'default' => false,
					),
				),
				'required'    => array( 'post_id' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'get' ),
			),
			'elementor-find'                 => array(
				'label'       => 'In Elementor suchen',
				'description' => 'Volltextsuche in den Elementor-Inhalten einer Seite (post_id) oder aller Seiten, Vorlagen und Popups. Liefert Beitrag, Element-ID, Feld und Fundstelle mit Kontext.',
				'readonly'    => true,
				'properties'  => array(
					'search'         => array( 'type' => 'string' ),
					'post_id'        => array_merge( $post_id, array( 'description' => 'Nur in diesem Beitrag suchen, leer = alle.' ) ),
					'case_sensitive' => array(
						'type'    => 'boolean',
						'default' => false,
					),
				),
				'required'    => array( 'search' ),
				'permission'  => static function ( $input ) {
					if ( ! empty( $input['post_id'] ) ) {
						return Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['post_id'] ), 'edit_post' );
					}
					return Tabside_MCP_Plugin::check_user( 'edit_posts' );
				},
				'execute'     => array( __CLASS__, 'find' ),
			),
			'elementor-references'           => array(
				'label'       => 'Verwendung einer Vorlage',
				'description' => 'Wo eine Vorlage oder ein Popup verwendet wird: Vorlagen-Widgets, Shortcodes [elementor-template], Popup-Links und dynamische Popup-Tags, dazu die eigenen Anzeigebedingungen.',
				'readonly'    => true,
				'properties'  => array( 'post_id' => $post_id ),
				'required'    => array( 'post_id' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'references' ),
			),
			'elementor-template-list'        => array(
				'label'       => 'Elementor-Vorlagen',
				'description' => 'Vorlagen (elementor_library) nach Vorlagentyp gefiltert (page, section, container, popup, header, footer, single, archive, loop-item ...), mit Bedingungen und Anzahl Verwendungen.',
				'readonly'    => true,
				'properties'  => array(
					'template_type' => array( 'type' => 'string' ),
					'search'        => array( 'type' => 'string' ),
				),
				'permission'  => $edit,
				'execute'     => array( __CLASS__, 'template_list' ),
			),
			'elementor-update-element'       => array(
				'label'       => 'Element ändern',
				'description' => 'Setzt Settings eines Elements (nur die übergebenen Keys, null entfernt einen Key). Vorschau zeigt alt/neu pro Key. Sicherung vorher, Rückholen mit post-rollback. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'post_id'    => $post_id,
					'element_id' => $element,
					'settings'   => $settings,
					'confirm'    => $confirm,
				),
				'required'    => array( 'post_id', 'element_id', 'settings' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'update_element' ),
			),
			'elementor-replace-text'         => array(
				'label'       => 'Text ersetzen',
				'description' => 'Ersetzt einen Text exakt in den Settings eines Elements, einer Seite oder mehrerer Seiten. Weicht die Trefferzahl von expected_count ab, wird nichts geändert und die Fundstellen kommen zurück. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'search'         => array( 'type' => 'string' ),
					'replace'        => array( 'type' => 'string' ),
					'expected_count' => array(
						'type'    => 'integer',
						'minimum' => 1,
					),
					'post_id'        => array_merge( $post_id, array( 'description' => 'Eine Seite. Alternativ post_ids.' ) ),
					'post_ids'       => array(
						'type'  => 'array',
						'items' => array(
							'type'    => 'integer',
							'minimum' => 1,
						),
					),
					'element_id'     => array_merge( $element, array( 'description' => 'Nur in diesem Element (mit post_id).' ) ),
					'confirm'        => $confirm,
				),
				'required'    => array( 'search', 'replace', 'expected_count' ),
				'permission'  => static function ( $input ) {
					$ids = self::target_ids( $input );
					if ( is_wp_error( $ids ) ) {
						return $ids;
					}
					foreach ( $ids as $id ) {
						$ok = Tabside_MCP_Abilities::check_post_permission( array( 'id' => $id ), 'edit_post' );
						if ( true !== $ok ) {
							return $ok;
						}
					}
					return true;
				},
				'execute'     => array( __CLASS__, 'replace_text' ),
			),
			'elementor-insert-element'       => array(
				'label'       => 'Element einfügen',
				'description' => 'Fügt ein Element ein, aus JSON (element) oder als Kopie eines bestehenden Elements (source_post_id + source_element_id), unter parent_id (leer = oberste Ebene) an Position index. IDs werden neu vergeben. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'post_id'           => $post_id,
					'parent_id'         => array_merge( $element, array( 'description' => 'Eltern-Element, leer = oberste Ebene.' ) ),
					'index'             => array(
						'type'        => 'integer',
						'minimum'     => 0,
						'description' => 'Position unter dem Eltern-Element, leer = am Ende.',
					),
					'element'           => array(
						'type'                 => 'object',
						'additionalProperties' => true,
						'description'          => 'Element im Elementor-Format: {"elType":"widget","widgetType":"heading","settings":{...},"elements":[]}.',
					),
					'source_post_id'    => array(
						'type'    => 'integer',
						'minimum' => 1,
					),
					'source_element_id' => array( 'type' => 'string' ),
					'confirm'           => $confirm,
				),
				'required'    => array( 'post_id' ),
				'permission'  => static function ( $input ) use ( $can_edit ) {
					$ok = $can_edit( $input );
					if ( true === $ok && ! empty( $input['source_post_id'] ) ) {
						$ok = Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['source_post_id'] ), 'edit_post' );
					}
					return $ok;
				},
				'execute'     => array( __CLASS__, 'insert_element' ),
			),
			'elementor-move-element'         => array(
				'label'       => 'Element verschieben',
				'description' => 'Verschiebt ein Element unter parent_id (leer = oberste Ebene) an Position index. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'post_id'    => $post_id,
					'element_id' => $element,
					'parent_id'  => array_merge( $element, array( 'description' => 'Neues Eltern-Element, leer = oberste Ebene.' ) ),
					'index'      => array(
						'type'    => 'integer',
						'minimum' => 0,
					),
					'confirm'    => $confirm,
				),
				'required'    => array( 'post_id', 'element_id' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'move_element' ),
			),
			'elementor-remove-element'       => array(
				'label'       => 'Element entfernen',
				'description' => 'Entfernt ein Element samt Inhalt aus der Struktur. Die alte Fassung steckt in der Sicherung (post-rollback). Ohne confirm=true nur Vorschau.',
				'destructive' => true,
				'flush'       => 'post',
				'properties'  => array(
					'post_id'    => $post_id,
					'element_id' => $element,
					'confirm'    => $confirm,
				),
				'required'    => array( 'post_id', 'element_id' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'remove_element' ),
			),
			'elementor-copy-layout'          => array(
				'label'       => 'Layout übernehmen',
				'description' => 'Übernimmt die komplette Elementor-Struktur von source_post_id in target_post_id (ersetzt den bisherigen Aufbau, Sicherung vorher). replace_popup_ids und replace_template_ids ordnen alte IDs neuen zu, damit Links auf die neuen Popups bzw. Vorlagen zeigen. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'source_post_id'       => Tabside_MCP_Abilities::id_schema( 'Vorlage (Seite, deren Aufbau übernommen wird).' ),
					'target_post_id'       => Tabside_MCP_Abilities::id_schema( 'Ziel (wird überschrieben).' ),
					'replace_popup_ids'    => $id_map + array( 'description' => '{"alte Popup-ID": neue Popup-ID}.' ),
					'replace_template_ids' => $id_map + array( 'description' => '{"alte Vorlagen-ID": neue Vorlagen-ID}.' ),
					'copy_page_settings'   => array(
						'type'        => 'boolean',
						'default'     => true,
						'description' => 'Seiteneinstellungen und Template mit übernehmen.',
					),
					'confirm'              => $confirm,
				),
				'required'    => array( 'source_post_id', 'target_post_id' ),
				'permission'  => static function ( $input ) {
					$ok = Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['source_post_id'] ?? 0 ), 'edit_post' );
					return true === $ok ? Tabside_MCP_Abilities::check_post_permission( array( 'id' => $input['target_post_id'] ?? 0 ), 'edit_post' ) : $ok;
				},
				'execute'     => array( __CLASS__, 'copy_layout' ),
			),
			'elementor-set-conditions'       => array(
				'label'       => 'Anzeigebedingungen setzen',
				'description' => 'Setzt Anzeigebedingungen (z.B. include/general, include/singular/page/12, exclude/...) und bei Popups Auslöser (triggers) und erweiterte Regeln (timing). Nur mit Elementor Pro. Ohne confirm=true nur Vorschau.',
				'flush'       => 'site',
				'properties'  => array(
					'post_id'    => $post_id,
					'conditions' => array(
						'type'  => 'array',
						'items' => array( 'type' => 'string' ),
					),
					'triggers'   => $settings + array( 'description' => 'Nur Popups: Auslöser, z.B. {"page_load":"yes","page_load_delay":3}. Ersetzt die bisherigen.' ),
					'timing'     => $settings + array( 'description' => 'Nur Popups: erweiterte Regeln. Ersetzt die bisherigen.' ),
					'confirm'    => $confirm,
				),
				'required'    => array( 'post_id' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'set_conditions' ),
			),
			'elementor-page-settings-update' => array(
				'label'       => 'Elementor-Seiteneinstellungen',
				'description' => 'Ändert _elementor_page_settings (z.B. hide_title, Seitenlayout, Abstände, eigene CSS), nur die übergebenen Keys, null entfernt einen Key. Titel, Status und Template über post-update. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'post_id'  => $post_id,
					'settings' => $settings,
					'confirm'  => $confirm,
				),
				'required'    => array( 'post_id', 'settings' ),
				'permission'  => $can_edit,
				'execute'     => array( __CLASS__, 'page_settings_update' ),
			),
		);
	}

	/* ------------------------------------------------------------------ Lesen */

	public static function info() {
		$plugin      = \Elementor\Plugin::$instance;
		$experiments = array();
		foreach ( $plugin->experiments->get_features() as $name => $feature ) {
			if ( $plugin->experiments->is_feature_active( $name ) ) {
				$experiments[] = $name;
			}
		}
		$kit_id = (int) $plugin->kits_manager->get_active_id();
		$kit    = $kit_id ? $plugin->documents->get( $kit_id ) : null;
		$colors = array();
		$fonts  = array();
		if ( $kit ) {
			$kit_settings = (array) $kit->get_settings();
			foreach ( array( 'system_colors', 'custom_colors' ) as $group ) {
				foreach ( (array) ( $kit_settings[ $group ] ?? array() ) as $color ) {
					$colors[] = array(
						'id'    => $color['_id'] ?? '',
						'title' => $color['title'] ?? '',
						'color' => $color['color'] ?? '',
						'group' => $group,
					);
				}
			}
			foreach ( array( 'system_typography', 'custom_typography' ) as $group ) {
				foreach ( (array) ( $kit_settings[ $group ] ?? array() ) as $font ) {
					$fonts[] = array(
						'id'     => $font['_id'] ?? '',
						'title'  => $font['title'] ?? '',
						'family' => $font['typography_font_family'] ?? '',
						'weight' => $font['typography_font_weight'] ?? '',
						'group'  => $group,
					);
				}
			}
		}
		$breakpoints = array();
		foreach ( $plugin->breakpoints->get_active_breakpoints() as $name => $breakpoint ) {
			$breakpoints[] = array(
				'name'      => $name,
				'label'     => $breakpoint->get_label(),
				'value'     => $breakpoint->get_value(),
				'direction' => $breakpoint->get_direction(),
			);
		}
		return array(
			'elementor'        => ELEMENTOR_VERSION,
			'elementor_pro'    => self::pro_active() ? ELEMENTOR_PRO_VERSION : null,
			'container'        => $plugin->experiments->is_feature_active( 'container' ),
			'experiments'      => $experiments,
			'kit_id'           => $kit_id,
			'colors'           => $colors,
			'fonts'            => $fonts,
			'breakpoints'      => $breakpoints,
			'css_print_method' => get_option( 'elementor_css_print_method', 'external' ),
			'post_types'       => self::supported_types(),
		);
	}

	public static function get( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$tree = self::tree( $post->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		$full  = ! empty( $input['full'] );
		$nodes = $tree;
		$path  = array();
		if ( ! empty( $input['element_id'] ) ) {
			$found = self::locate( $tree, (string) $input['element_id'] );
			if ( null === $found ) {
				return self::element_missing( $input['element_id'] );
			}
			$nodes = array( $found['element'] );
			$path  = $found['path'];
		}
		$out   = array(
			'post'          => Tabside_MCP_Abilities::summary( $post ),
			'elements'      => self::summarize_list( $nodes, $full, $path ),
			'element_count' => self::count_elements( $tree ),
			'page_settings' => self::page_settings( $post->ID ),
		);
		$notes = array();
		if ( $full && strlen( (string) wp_json_encode( $out ) ) > self::MAX_FULL ) {
			$out['elements']  = self::summarize_list( $nodes, false, $path );
			$out['truncated'] = true;
			$notes[]          = 'Für full=true zu gross, darum nur die Zusammenfassung. Mit element_id einen Teilbaum abfragen.';
		}
		$out['notes'] = $notes;
		return $out;
	}

	public static function find( $input ) {
		$search = (string) $input['search'];
		if ( '' === $search ) {
			return new WP_Error( 'tabside_mcp_search_empty', 'search ist leer.' );
		}
		$ids  = ! empty( $input['post_id'] ) ? array( (int) $input['post_id'] ) : self::elementor_post_ids();
		$hits = array();
		$more = false;
		foreach ( $ids as $id ) {
			$tree = self::tree( $id );
			if ( is_wp_error( $tree ) ) {
				continue;
			}
			foreach ( self::search_tree( $tree, $search, ! empty( $input['case_sensitive'] ) ) as $hit ) {
				if ( count( $hits ) >= self::MAX_HITS ) {
					$more = true;
					break 2;
				}
				$hits[] = array_merge(
					array(
						'post_id' => $id,
						'title'   => get_the_title( $id ),
						'type'    => get_post_type( $id ),
					),
					$hit
				);
			}
		}
		return array(
			'search'    => $search,
			'count'     => count( $hits ),
			'truncated' => $more,
			'hits'      => $hits,
		);
	}

	public static function references( $input ) {
		$post = self::load( (int) $input['post_id'], false );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$used = array();
		foreach ( self::elementor_post_ids() as $id ) {
			$tree = self::tree( $id );
			if ( is_wp_error( $tree ) ) {
				continue;
			}
			foreach ( self::collect_refs( $tree ) as $ref ) {
				if ( (int) $ref['id'] === $post->ID ) {
					$used[] = array(
						'post_id'    => $id,
						'title'      => get_the_title( $id ),
						'type'       => get_post_type( $id ),
						'element_id' => $ref['element_id'],
						'kind'       => $ref['kind'],
					);
				}
			}
		}
		return array(
			'post'       => Tabside_MCP_Abilities::summary( $post ),
			'conditions' => self::conditions( $post->ID ),
			'count'      => count( $used ),
			'used_in'    => $used,
		);
	}

	public static function template_list( $input ) {
		$args = array(
			'post_type'      => 'elementor_library',
			'post_status'    => array( 'publish', 'draft', 'pending', 'private', 'future' ),
			'posts_per_page' => 500,
			'orderby'        => 'title',
			'order'          => 'ASC',
		);
		if ( ! empty( $input['template_type'] ) ) {
			$args['meta_query'] = array( // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_query
				array(
					'key'   => '_elementor_template_type',
					'value' => (string) $input['template_type'],
				),
			);
		}
		if ( ! empty( $input['search'] ) ) {
			$args['s'] = (string) $input['search'];
		}
		$templates = ( new WP_Query( $args ) )->posts;
		$usage     = array();
		foreach ( self::elementor_post_ids() as $id ) {
			$tree = self::tree( $id );
			if ( is_wp_error( $tree ) ) {
				continue;
			}
			foreach ( self::collect_refs( $tree ) as $ref ) {
				$usage[ (int) $ref['id'] ][ $id ] = true;
			}
		}
		$out = array();
		foreach ( $templates as $tpl ) {
			$out[] = array(
				'id'            => $tpl->ID,
				'title'         => $tpl->post_title,
				'status'        => $tpl->post_status,
				'template_type' => (string) get_post_meta( $tpl->ID, '_elementor_template_type', true ),
				'conditions'    => self::conditions( $tpl->ID ),
				'used_in_posts' => count( $usage[ $tpl->ID ] ?? array() ),
				'modified'      => $tpl->post_modified,
			);
		}
		return array(
			'count'     => count( $out ),
			'templates' => $out,
		);
	}

	/* ------------------------------------------------------------------ Schreiben */

	public static function update_element( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$tree = self::tree( $post->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		$element_id = (string) $input['element_id'];
		$found      = self::locate( $tree, $element_id );
		if ( null === $found ) {
			return self::element_missing( $element_id );
		}
		$old     = (array) ( $found['element']['settings'] ?? array() );
		$new     = $old;
		$changes = array();
		foreach ( (array) $input['settings'] as $key => $value ) {
			if ( null === $value ) {
				unset( $new[ $key ] );
			} else {
				$new[ $key ] = $value;
			}
			$changes[ $key ] = array(
				'old' => $old[ $key ] ?? null,
				'new' => $value,
			);
		}
		$tree = self::modify( $tree, $element_id, static function ( $el ) use ( $new ) {
			$el['settings'] = $new;
			return $el;
		} );
		return self::commit(
			$post,
			$tree,
			$input,
			array(
				'element' => self::summarize( $found['element'], false, $found['path'] ),
				'changes' => $changes,
			)
		);
	}

	public static function replace_text( $input ) {
		$ids = self::target_ids( $input );
		if ( is_wp_error( $ids ) ) {
			return $ids;
		}
		$search   = (string) $input['search'];
		$replace  = (string) $input['replace'];
		$expected = (int) $input['expected_count'];
		if ( '' === $search ) {
			return new WP_Error( 'tabside_mcp_search_empty', 'search ist leer.' );
		}
		if ( ! empty( $input['element_id'] ) && 1 !== count( $ids ) ) {
			return new WP_Error( 'tabside_mcp_element_scope', 'element_id nur zusammen mit genau einer Seite (post_id).' );
		}
		$plan  = array();
		$total = 0;
		$hits  = array();
		foreach ( $ids as $id ) {
			$post = self::load( $id );
			if ( is_wp_error( $post ) ) {
				return $post;
			}
			$tree = self::tree( $id );
			if ( is_wp_error( $tree ) ) {
				return $tree;
			}
			$scope = $tree;
			if ( ! empty( $input['element_id'] ) ) {
				$found = self::locate( $tree, (string) $input['element_id'] );
				if ( null === $found ) {
					return self::element_missing( $input['element_id'] );
				}
				$scope = array( $found['element'] );
			}
			$found_hits = self::search_tree( $scope, $search, true );
			foreach ( $found_hits as $hit ) {
				$hits[] = array_merge( array( 'post_id' => $id ), $hit );
			}
			$count  = array_sum( wp_list_pluck( $found_hits, 'occurrences' ) );
			$total += $count;
			if ( $count > 0 ) {
				$plan[ $id ] = array(
					'post'  => $post,
					'tree'  => $tree,
					'count' => $count,
				);
			}
		}
		if ( $total !== $expected ) {
			return new WP_Error(
				'tabside_mcp_count_mismatch',
				sprintf( 'Erwartet %d Treffer, gefunden %d. Nichts geändert.', $expected, $total ),
				array(
					'status' => 409,
					'hits'   => $hits,
				)
			);
		}
		$replace_fn = static function ( $value ) use ( $search, $replace ) {
			return str_replace( $search, $replace, $value );
		};
		$results    = array();
		$previews   = array();
		$backups    = array();
		foreach ( $plan as $id => $item ) {
			if ( ! empty( $input['element_id'] ) ) {
				$tree = self::modify( $item['tree'], (string) $input['element_id'], static function ( $el ) use ( $replace_fn ) {
					return self::map_strings_element( $el, $replace_fn );
				} );
			} else {
				$tree = self::map_strings_tree( $item['tree'], $replace_fn );
			}
			$result = self::commit(
				$item['post'],
				$tree,
				$input,
				array( 'occurrences' => $item['count'] )
			);
			if ( is_wp_error( $result ) ) {
				return $result;
			}
			if ( empty( $input['confirm'] ) ) {
				$previews[ $id ] = $result['preview'];
			} else {
				$results[ $id ] = $result;
				$backups[]      = $result['backup_id'];
			}
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'search'  => $search,
					'replace' => $replace,
					'total'   => $total,
					'hits'    => $hits,
					'posts'   => $previews,
				),
				array()
			);
		}
		return array(
			'confirmed'      => true,
			'total'          => $total,
			'backup_ids'     => $backups,
			'affected_posts' => array_keys( $results ),
			'results'        => array_values( $results ),
		);
	}

	public static function insert_element( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$tree = self::tree( $post->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		if ( ! empty( $input['element'] ) === ! empty( $input['source_post_id'] ) ) {
			return new WP_Error( 'tabside_mcp_insert_source', 'Genau eines angeben: element oder source_post_id mit source_element_id.' );
		}
		if ( ! empty( $input['source_post_id'] ) ) {
			$source_tree = self::tree( (int) $input['source_post_id'] );
			if ( is_wp_error( $source_tree ) ) {
				return $source_tree;
			}
			$found = self::locate( $source_tree, (string) ( $input['source_element_id'] ?? '' ) );
			if ( null === $found ) {
				return self::element_missing( $input['source_element_id'] ?? '' );
			}
			$element = $found['element'];
		} else {
			$element = self::normalize_element( (array) $input['element'] );
			if ( is_wp_error( $element ) ) {
				return $element;
			}
		}
		$copy   = self::regenerate_ids( array( $element ), self::ids( $tree ) );
		$copy   = $copy[0];
		$parent = (string) ( $input['parent_id'] ?? '' );
		$index  = isset( $input['index'] ) ? (int) $input['index'] : null;
		$tree   = self::place( $tree, $copy, $parent, $index );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		return self::commit(
			$post,
			$tree,
			$input,
			array(
				'insert'    => self::summarize( $copy, false, array() ),
				'parent_id' => '' === $parent ? null : $parent,
				'index'     => $index,
			),
			array(),
			array( 'element_id' => $copy['id'] )
		);
	}

	public static function move_element( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$tree = self::tree( $post->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		$element_id = (string) $input['element_id'];
		$found      = self::locate( $tree, $element_id );
		if ( null === $found ) {
			return self::element_missing( $element_id );
		}
		$parent = (string) ( $input['parent_id'] ?? '' );
		if ( '' !== $parent && ( $parent === $element_id || in_array( $parent, self::ids( array( $found['element'] ) ), true ) ) ) {
			return new WP_Error( 'tabside_mcp_move_into_self', 'Ein Element lässt sich nicht in sich selbst verschieben.' );
		}
		$index = isset( $input['index'] ) ? (int) $input['index'] : null;
		$tree  = self::place( self::cut( $tree, $element_id ), $found['element'], $parent, $index );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		return self::commit(
			$post,
			$tree,
			$input,
			array(
				'element' => self::summarize( $found['element'], false, $found['path'] ),
				'from'    => array(
					'parent_id' => $found['parent_id'],
					'index'     => $found['index'],
				),
				'to'      => array(
					'parent_id' => '' === $parent ? null : $parent,
					'index'     => $index,
				),
			)
		);
	}

	public static function remove_element( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$tree = self::tree( $post->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		$element_id = (string) $input['element_id'];
		$found      = self::locate( $tree, $element_id );
		if ( null === $found ) {
			return self::element_missing( $element_id );
		}
		return self::commit(
			$post,
			self::cut( $tree, $element_id ),
			$input,
			array(
				'remove'             => self::summarize( $found['element'], false, $found['path'] ),
				'elements_removed'   => self::count_elements( array( $found['element'] ) ),
			),
			array( 'Die alte Fassung bleibt in der Sicherung (post-rollback).' )
		);
	}

	public static function copy_layout( $input ) {
		$source = self::load( (int) $input['source_post_id'] );
		$target = self::load( (int) $input['target_post_id'] );
		if ( is_wp_error( $source ) ) {
			return $source;
		}
		if ( is_wp_error( $target ) ) {
			return $target;
		}
		if ( $source->ID === $target->ID ) {
			return new WP_Error( 'tabside_mcp_same_post', 'Quelle und Ziel sind derselbe Beitrag.' );
		}
		$tree = self::tree( $source->ID );
		if ( is_wp_error( $tree ) ) {
			return $tree;
		}
		if ( empty( $tree ) ) {
			return new WP_Error( 'tabside_mcp_elementor_empty', sprintf( 'Beitrag %d hat keinen Elementor-Aufbau.', $source->ID ) );
		}
		$popups    = self::id_map( $input['replace_popup_ids'] ?? array() );
		$templates = self::id_map( $input['replace_template_ids'] ?? array() );
		foreach ( array_merge( array_values( $popups ), array_values( $templates ) ) as $new_id ) {
			if ( ! self::library_exists( $new_id ) ) {
				return new WP_Error( 'tabside_mcp_reference_missing', sprintf( 'Zuordnung zeigt auf %d, das ist keine vorhandene Elementor-Vorlage.', $new_id ) );
			}
		}
		$notes = array();
		foreach ( self::collect_refs( $tree ) as $ref ) {
			$is_popup = 0 === strpos( $ref['kind'], 'popup' );
			$map      = $is_popup ? $popups : $templates;
			if ( ! isset( $map[ (int) $ref['id'] ] ) ) {
				$notes[ $ref['kind'] . $ref['id'] ] = sprintf( '%s %d bleibt unverändert verknüpft (Element %s).', $is_popup ? 'Popup' : 'Vorlage', $ref['id'], $ref['element_id'] );
			}
		}
		$tree     = self::replace_refs( self::regenerate_ids( $tree ), $popups, $templates );
		$settings = null;
		$meta     = array();
		if ( ! array_key_exists( 'copy_page_settings', $input ) || $input['copy_page_settings'] ) {
			$settings = self::page_settings( $source->ID );
			$template = (string) get_post_meta( $source->ID, '_wp_page_template', true );
			if ( '' !== $template ) {
				$meta['_wp_page_template'] = $template;
			}
		}
		$current = self::tree( $target->ID );
		return self::commit(
			$target,
			$tree,
			$input,
			array(
				'source'            => Tabside_MCP_Abilities::summary( $source ),
				'target'            => Tabside_MCP_Abilities::summary( $target ),
				'elements_before'   => is_wp_error( $current ) ? null : self::count_elements( $current ),
				'elements_after'    => self::count_elements( $tree ),
				'popups_replaced'   => $popups,
				'templates_replaced' => $templates,
				'page_settings'     => null !== $settings,
			),
			array_values( $notes ),
			array(),
			$settings,
			$meta
		);
	}

	public static function set_conditions( $input ) {
		if ( ! self::pro_active() ) {
			return new WP_Error( 'tabside_mcp_pro_required', 'Anzeigebedingungen und Auslöser gibt es nur mit Elementor Pro.' );
		}
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$type = (string) get_post_meta( $post->ID, '_elementor_template_type', true );
		if ( 'elementor_library' !== $post->post_type || ! in_array( $type, self::CONDITION_TYPES, true ) ) {
			return new WP_Error( 'tabside_mcp_conditions', sprintf( 'Beitrag %d ist kein Popup und keine Theme-Vorlage.', $post->ID ) );
		}
		if ( ( isset( $input['triggers'] ) || isset( $input['timing'] ) ) && 'popup' !== $type ) {
			return new WP_Error( 'tabside_mcp_conditions', 'triggers und timing gibt es nur bei Popups.' );
		}
		if ( ! isset( $input['conditions'] ) && ! isset( $input['triggers'] ) && ! isset( $input['timing'] ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'conditions, triggers oder timing übergeben.' );
		}
		$conditions = isset( $input['conditions'] ) ? array_values( array_map( 'strval', (array) $input['conditions'] ) ) : null;
		foreach ( (array) $conditions as $condition ) {
			if ( ! preg_match( '#^(include|exclude)/[a-z0-9_\-]+(/[a-z0-9_\-]+)*$#i', $condition ) ) {
				return new WP_Error( 'tabside_mcp_conditions', sprintf( 'Bedingung "%s" ungültig. Format: include/general, include/singular/page/12, exclude/... .', $condition ) );
			}
		}
		$display = get_post_meta( $post->ID, '_elementor_popup_display_settings', true );
		$display = is_array( $display ) ? $display : array();
		$changes = array();
		if ( null !== $conditions ) {
			$changes['conditions'] = array(
				'old' => self::conditions( $post->ID ),
				'new' => $conditions,
			);
		}
		$new_display = $display;
		foreach ( array( 'triggers', 'timing' ) as $key ) {
			if ( isset( $input[ $key ] ) ) {
				$new_display[ $key ] = (array) $input[ $key ];
				$changes[ $key ]     = array(
					'old' => $display[ $key ] ?? array(),
					'new' => $new_display[ $key ],
				);
			}
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'post'    => Tabside_MCP_Abilities::summary( $post ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Backups::create( $post, 'elementor' );
		if ( null !== $conditions ) {
			self::save_conditions( $post->ID, $conditions );
		}
		if ( $new_display !== $display ) {
			update_post_meta( $post->ID, '_elementor_popup_display_settings', wp_slash( $new_display ) );
		}
		$stored  = get_post_meta( $post->ID, '_elementor_popup_display_settings', true );
		$verify  = array( 'conditions' => null === $conditions || self::conditions( $post->ID ) === $conditions );
		if ( $new_display !== $display ) {
			$verify['display_settings'] = $stored === $new_display;
		}
		return array(
			'confirmed'  => true,
			'backup_id'  => $backup,
			'verify'     => $verify,
			'conditions' => self::conditions( $post->ID ),
			'post'       => Tabside_MCP_Abilities::summary( get_post( $post->ID ) ),
		);
	}

	public static function page_settings_update( $input ) {
		$post = self::load( (int) $input['post_id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$old     = self::page_settings( $post->ID );
		$new     = $old;
		$changes = array();
		foreach ( (array) $input['settings'] as $key => $value ) {
			if ( in_array( $key, self::POST_FIELD_SETTINGS, true ) ) {
				return new WP_Error( 'tabside_mcp_page_setting', sprintf( '%s bitte über post-update ändern.', $key ) );
			}
			if ( null === $value ) {
				unset( $new[ $key ] );
			} else {
				$new[ $key ] = $value;
			}
			$changes[ $key ] = array(
				'old' => $old[ $key ] ?? null,
				'new' => $value,
			);
		}
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result(
				array(
					'post'    => Tabside_MCP_Abilities::summary( $post ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Backups::create( $post, 'elementor' );
		update_post_meta( $post->ID, self::SETTINGS_KEY, wp_slash( $new ) );
		return array(
			'confirmed'     => true,
			'backup_id'     => $backup,
			'verify'        => array( 'page_settings' => self::page_settings( $post->ID ) === $new ),
			'page_settings' => self::page_settings( $post->ID ),
			'post'          => Tabside_MCP_Abilities::summary( get_post( $post->ID ) ),
		);
	}

	/* ------------------------------------------------------------------ Speichern */

	/**
	 * Gemeinsamer Ablauf aller Struktur-Änderungen: prüfen, Vorschau oder Sicherung + Speichern + Kontrolle.
	 *
	 * @param WP_Post    $post     Ziel.
	 * @param array      $tree     Neue Elementor-Struktur.
	 * @param array      $input    Eingabe (confirm).
	 * @param array      $preview  Vorschau-Angaben.
	 * @param string[]   $notes    Hinweise.
	 * @param array      $extra    Zusätzliche Rückgabe-Felder.
	 * @param array|null $settings Neue Seiteneinstellungen oder null.
	 * @param array      $meta     Weitere Meta-Felder (nur _wp_page_template).
	 * @return array|WP_Error
	 */
	private static function commit( WP_Post $post, array $tree, array $input, array $preview, array $notes = array(), array $extra = array(), $settings = null, array $meta = array() ) {
		$errors = self::validate( $tree );
		if ( $errors ) {
			return new WP_Error( 'tabside_mcp_elementor_invalid', 'Elementor-Daten ungültig, nichts geändert: ' . implode( ' ', $errors ), array( 'status' => 400 ) );
		}
		$preview['post'] = $preview['post'] ?? Tabside_MCP_Abilities::summary( $post );
		if ( empty( $input['confirm'] ) ) {
			return Tabside_MCP_Abilities::preview_result( $preview, $notes );
		}
		$backup = Tabside_MCP_Backups::create( $post, 'elementor' );
		$method = self::save( $post, $tree, $settings );
		foreach ( $meta as $key => $value ) {
			update_post_meta( $post->ID, $key, wp_slash( $value ) );
		}
		$stored = json_decode( self::raw_data( $post->ID ), true );
		$verify = array(
			'saved_via' => $method,
			'equal'     => is_array( $stored ) && self::normalize( $stored ) === self::normalize( $tree ),
			'elements'  => is_array( $stored ) ? self::count_elements( $stored ) : 0,
		);
		if ( null !== $settings ) {
			$verify['page_settings'] = self::page_settings( $post->ID ) == $settings; // phpcs:ignore Universal.Operators.StrictComparisons.LooseEqual
		}
		return array_merge(
			array(
				'confirmed' => true,
				'backup_id' => $backup,
				'notes'     => $notes,
				'verify'    => $verify,
				'post'      => Tabside_MCP_Abilities::summary( get_post( $post->ID ) ),
			),
			$extra
		);
	}

	/**
	 * @return string document_api | meta
	 */
	private static function save( WP_Post $post, array $tree, $settings ) {
		$stored_type = (string) get_post_meta( $post->ID, '_elementor_template_type', true );
		$document    = \Elementor\Plugin::$instance->documents->get( $post->ID, false );
		$use_api     = $document && $document->is_editable_by_current_user()
			&& ( '' === $stored_type || $document->get_name() === $stored_type );
		if ( '' === (string) get_post_meta( $post->ID, '_elementor_edit_mode', true ) ) {
			update_post_meta( $post->ID, '_elementor_edit_mode', 'builder' );
		}
		if ( $use_api && $document->save( array( 'elements' => $tree ) ) ) {
			if ( null !== $settings ) {
				// Seiteneinstellungen exakt ins Meta (Elementor würde nur Einstellungen mit Steuerelement übernehmen).
				update_post_meta( $post->ID, self::SETTINGS_KEY, wp_slash( $settings ) );
			}
			return 'document_api';
		}
		update_post_meta( $post->ID, self::DATA_KEY, wp_slash( wp_json_encode( $tree ) ) );
		if ( null !== $settings ) {
			update_post_meta( $post->ID, self::SETTINGS_KEY, wp_slash( $settings ) );
		}
		if ( '' === (string) get_post_meta( $post->ID, '_elementor_version', true ) ) {
			update_post_meta( $post->ID, '_elementor_version', ELEMENTOR_VERSION );
		}
		foreach ( self::CACHE_META as $key ) {
			delete_post_meta( $post->ID, $key );
		}
		return 'meta';
	}

	/**
	 * Prüft Struktur, eindeutige IDs, registrierte Element-/Widget-Typen, Verweise und Grösse.
	 *
	 * @return string[] Fehler.
	 */
	public static function validate( array $tree ) {
		$errors = array();
		$json   = wp_json_encode( $tree );
		if ( false === $json ) {
			return array( 'Nicht als JSON speicherbar.' );
		}
		$max = (int) apply_filters( 'tabside_mcp_elementor_max_bytes', self::MAX_BYTES );
		if ( strlen( $json ) > $max ) {
			$errors[] = sprintf( 'Elementor-Daten sind %s gross, erlaubt sind %s.', size_format( strlen( $json ) ), size_format( $max ) );
		}
		$seen = array();
		self::walk(
			$tree,
			static function ( $el, $path, $parent ) use ( &$errors, &$seen ) {
				$id = isset( $el['id'] ) ? (string) $el['id'] : '';
				if ( '' === $id || ! preg_match( '/^[a-z0-9]+$/i', $id ) ) {
					$errors[] = sprintf( 'Element an Position %s hat keine gültige id.', implode( '/', $path ) );
				} elseif ( isset( $seen[ $id ] ) ) {
					$errors[] = sprintf( 'id %s kommt mehrfach vor.', $id );
				}
				$seen[ $id ] = true;
				$type        = (string) ( $el['elType'] ?? '' );
				if ( '' === $type || ! \Elementor\Plugin::$instance->elements_manager->get_element_types( $type ) ) {
					if ( 'widget' !== $type ) {
						$errors[] = sprintf( 'elType "%s" (Element %s) ist nicht registriert.', $type, $id );
					}
				}
				if ( 'widget' === $type ) {
					$widget = (string) ( $el['widgetType'] ?? '' );
					if ( '' === $widget || ! \Elementor\Plugin::$instance->widgets_manager->get_widget_types( $widget ) ) {
						$errors[] = sprintf( 'Widget-Typ "%s" (Element %s) ist nicht registriert.', $widget, $id );
					}
					if ( ! empty( $el['elements'] ) ) {
						$errors[] = sprintf( 'Widget %s darf keine Unterelemente haben.', $id );
					}
				}
				if ( null === $parent && in_array( $type, array( 'widget', 'column' ), true ) ) {
					$errors[] = sprintf( '%s %s darf nicht auf der obersten Ebene stehen.', $type, $id );
				}
				if ( null !== $parent && 'section' === ( $parent['elType'] ?? '' ) && 'column' !== $type ) {
					$errors[] = sprintf( 'In einer Section (%s) sind nur Spalten erlaubt, nicht %s %s.', $parent['id'] ?? '', $type, $id );
				}
				if ( 'column' === $type && ( null === $parent || 'section' !== ( $parent['elType'] ?? '' ) ) ) {
					$errors[] = sprintf( 'Spalte %s muss in einer Section stehen.', $id );
				}
				if ( isset( $el['settings'] ) && ! is_array( $el['settings'] ) ) {
					$errors[] = sprintf( 'settings von Element %s ist kein Objekt.', $id );
				}
			}
		);
		foreach ( self::collect_refs( $tree ) as $ref ) {
			if ( ! self::library_exists( (int) $ref['id'] ) ) {
				$errors[] = sprintf( '%s (Element %s) zeigt auf %d, das gibt es nicht als Elementor-Vorlage.', $ref['kind'], $ref['element_id'], $ref['id'] );
			}
		}
		return array_values( array_unique( $errors ) );
	}

	/* ------------------------------------------------------------------ Daten-Hilfen (auch ohne Elementor) */

	public static function raw_data( $post_id ) {
		$value = get_post_meta( $post_id, self::DATA_KEY, true );
		if ( is_array( $value ) ) {
			return (string) wp_json_encode( $value );
		}
		return (string) $value;
	}

	/**
	 * @return array|WP_Error
	 */
	public static function tree( $post_id ) {
		$raw = self::raw_data( $post_id );
		if ( '' === $raw ) {
			return array();
		}
		$tree = json_decode( $raw, true );
		if ( ! is_array( $tree ) ) {
			return new WP_Error( 'tabside_mcp_elementor_json', sprintf( 'Die Elementor-Daten von Beitrag %d sind kein gültiges JSON.', $post_id ) );
		}
		return $tree;
	}

	public static function page_settings( $post_id ) {
		$settings = get_post_meta( $post_id, self::SETTINGS_KEY, true );
		return is_array( $settings ) ? $settings : array();
	}

	public static function conditions( $post_id ) {
		$conditions = get_post_meta( $post_id, '_elementor_conditions', true );
		return is_array( $conditions ) ? array_values( $conditions ) : array();
	}

	/**
	 * Schreibt Anzeigebedingungen. Mit Elementor Pro über dessen Bedingungs-Manager (inkl. Cache).
	 */
	public static function save_conditions( $post_id, array $conditions ) {
		$manager = null;
		if ( class_exists( '\ElementorPro\Modules\ThemeBuilder\Module' ) ) {
			$module = \ElementorPro\Modules\ThemeBuilder\Module::instance();
			if ( method_exists( $module, 'get_conditions_manager' ) ) {
				$manager = $module->get_conditions_manager();
			}
		}
		update_post_meta( $post_id, '_elementor_conditions', wp_slash( $conditions ) );
		if ( $manager && method_exists( $manager, 'get_cache' ) ) {
			$cache = $manager->get_cache();
			if ( is_object( $cache ) && method_exists( $cache, 'regenerate' ) ) {
				$cache->regenerate();
			}
		}
	}

	public static function is_cache_meta( $key ) {
		return in_array( $key, self::CACHE_META, true ) || 0 === strpos( $key, '_elementor_screenshot' );
	}

	/**
	 * Alle Elementor-Meta-Felder eines Beitrags (ohne Caches) für die Sicherung.
	 *
	 * @return array<string, mixed>
	 */
	public static function meta_snapshot( $post_id ) {
		$out = array();
		foreach ( array_keys( (array) get_post_meta( $post_id ) ) as $key ) {
			if ( 0 === strpos( $key, '_elementor_' ) && ! self::is_cache_meta( $key ) ) {
				$out[ $key ] = get_post_meta( $post_id, $key, true );
			}
		}
		ksort( $out );
		return $out;
	}

	/**
	 * Stellt die Elementor-Meta-Felder exakt wieder her (auch den JSON-String von _elementor_data).
	 */
	public static function restore_meta( $post_id, array $snapshot ) {
		foreach ( array_keys( self::meta_snapshot( $post_id ) ) as $key ) {
			if ( ! array_key_exists( $key, $snapshot ) ) {
				delete_post_meta( $post_id, $key );
			}
		}
		foreach ( $snapshot as $key => $value ) {
			update_post_meta( $post_id, $key, wp_slash( $value ) );
		}
		foreach ( self::CACHE_META as $key ) {
			delete_post_meta( $post_id, $key );
		}
	}

	public static function describe_meta( array $snapshot ) {
		if ( empty( $snapshot ) ) {
			return null;
		}
		$data = (string) ( $snapshot[ self::DATA_KEY ] ?? '' );
		$tree = '' === $data ? array() : json_decode( $data, true );
		return array(
			'keys'     => array_keys( $snapshot ),
			'elements' => is_array( $tree ) ? self::count_elements( $tree ) : null,
			'bytes'    => strlen( $data ),
		);
	}

	/**
	 * Elementor-Angaben für post-get, null wenn der Beitrag keine Elementor-Daten hat.
	 */
	public static function post_info( WP_Post $post ) {
		$mode = (string) get_post_meta( $post->ID, '_elementor_edit_mode', true );
		$raw  = self::raw_data( $post->ID );
		if ( '' === $mode && '' === $raw ) {
			return null;
		}
		$tree = '' === $raw ? array() : json_decode( $raw, true );
		$out  = array(
			'built_with_elementor' => 'builder' === $mode,
			'version'              => (string) get_post_meta( $post->ID, '_elementor_version', true ),
			'pro_version'          => (string) get_post_meta( $post->ID, '_elementor_pro_version', true ),
			'template_type'        => (string) get_post_meta( $post->ID, '_elementor_template_type', true ),
			'elements'             => is_array( $tree ) ? self::count_elements( $tree ) : null,
			'data_bytes'           => strlen( $raw ),
			'edit_url'             => admin_url( 'post.php?post=' . $post->ID . '&action=elementor' ),
		);
		if ( in_array( $out['template_type'], self::CONDITION_TYPES, true ) ) {
			$out['conditions'] = self::conditions( $post->ID );
		}
		if ( 'popup' === $out['template_type'] ) {
			$out['popup_display_settings'] = get_post_meta( $post->ID, '_elementor_popup_display_settings', true );
		}
		return $out;
	}

	/**
	 * Elementor-CSS eines Beitrags neu erzeugen.
	 *
	 * @return array{status: string, url: string|null}|null
	 */
	public static function rebuild_css( $post_id ) {
		if ( ! self::active() || '' === self::raw_data( $post_id ) ) {
			return null;
		}
		$css = \Elementor\Core\Files\CSS\Post::create( $post_id );
		$css->update();
		$status = (string) $css->get_meta( 'status' );
		return array(
			'status' => $status,
			'url'    => 'file' === $status ? $css->get_url() : null,
		);
	}

	public static function count_elements( array $tree ) {
		$count = 0;
		self::walk(
			$tree,
			static function () use ( &$count ) {
				++$count;
			}
		);
		return $count;
	}

	/**
	 * @return string[]
	 */
	public static function ids( array $tree ) {
		$ids = array();
		self::walk(
			$tree,
			static function ( $el ) use ( &$ids ) {
				$ids[] = (string) ( $el['id'] ?? '' );
			}
		);
		return $ids;
	}

	/**
	 * Neue Element-IDs (8 Stellen hex, eindeutig), Struktur und Settings bleiben unverändert.
	 *
	 * @param array    $tree Elemente.
	 * @param string[] $used Bereits vergebene IDs.
	 */
	public static function regenerate_ids( array $tree, array $used = array() ) {
		$taken = array_fill_keys( array_merge( $used, self::ids( $tree ) ), true );
		$fn    = static function ( array $elements ) use ( &$fn, &$taken ) {
			foreach ( $elements as $i => $el ) {
				if ( ! is_array( $el ) ) {
					continue;
				}
				do {
					$id = bin2hex( random_bytes( 4 ) );
				} while ( isset( $taken[ $id ] ) || ctype_digit( $id ) );
				$taken[ $id ]       = true;
				$elements[ $i ]['id'] = $id;
				if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
					$elements[ $i ]['elements'] = $fn( $el['elements'] );
				}
			}
			return $elements;
		};
		return $fn( $tree );
	}

	/**
	 * Struktur ohne IDs, für den Vergleich einer Kopie mit dem Original.
	 */
	public static function strip_ids( array $tree ) {
		$out = array();
		foreach ( $tree as $el ) {
			unset( $el['id'] );
			if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
				$el['elements'] = self::strip_ids( $el['elements'] );
			}
			$out[] = $el;
		}
		return $out;
	}

	/* ------------------------------------------------------------------ Baum-Hilfen */

	private static function walk( array $elements, callable $fn, array $path = array(), $parent = null ) {
		foreach ( array_values( $elements ) as $i => $el ) {
			if ( ! is_array( $el ) ) {
				continue;
			}
			$here = array_merge( $path, array( $i ) );
			$fn( $el, $here, $parent );
			if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
				self::walk( $el['elements'], $fn, $here, $el );
			}
		}
	}

	/**
	 * @return array{element: array, path: int[], parent_id: string|null, index: int}|null
	 */
	private static function locate( array $tree, $id ) {
		$found = null;
		self::walk(
			$tree,
			static function ( $el, $path, $parent ) use ( $id, &$found ) {
				if ( null === $found && (string) ( $el['id'] ?? '' ) === (string) $id ) {
					$found = array(
						'element'   => $el,
						'path'      => $path,
						'parent_id' => $parent ? (string) $parent['id'] : null,
						'index'     => (int) end( $path ),
					);
				}
			}
		);
		return $found;
	}

	private static function modify( array $elements, $id, callable $fn ) {
		foreach ( $elements as $i => $el ) {
			if ( (string) ( $el['id'] ?? '' ) === (string) $id ) {
				$elements[ $i ] = $fn( $el );
			} elseif ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
				$elements[ $i ]['elements'] = self::modify( $el['elements'], $id, $fn );
			}
		}
		return $elements;
	}

	private static function cut( array $elements, $id ) {
		$out = array();
		foreach ( $elements as $el ) {
			if ( (string) ( $el['id'] ?? '' ) === (string) $id ) {
				continue;
			}
			if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
				$el['elements'] = self::cut( $el['elements'], $id );
			}
			$out[] = $el;
		}
		return $out;
	}

	/**
	 * @return array|WP_Error
	 */
	private static function place( array $tree, array $element, $parent_id, $index ) {
		if ( '' === $parent_id ) {
			$pos = null === $index ? count( $tree ) : min( $index, count( $tree ) );
			array_splice( $tree, $pos, 0, array( $element ) );
			return $tree;
		}
		$parent = self::locate( $tree, $parent_id );
		if ( null === $parent ) {
			return self::element_missing( $parent_id );
		}
		if ( 'widget' === ( $parent['element']['elType'] ?? '' ) ) {
			return new WP_Error( 'tabside_mcp_parent_widget', sprintf( 'Element %s ist ein Widget und kann keine Unterelemente haben.', $parent_id ) );
		}
		return self::modify(
			$tree,
			$parent_id,
			static function ( $el ) use ( $element, $index ) {
				$children = isset( $el['elements'] ) && is_array( $el['elements'] ) ? array_values( $el['elements'] ) : array();
				$pos      = null === $index ? count( $children ) : min( $index, count( $children ) );
				array_splice( $children, $pos, 0, array( $element ) );
				$el['elements'] = $children;
				return $el;
			}
		);
	}

	/**
	 * @return array|WP_Error
	 */
	private static function normalize_element( array $el ) {
		if ( empty( $el['elType'] ) ) {
			return new WP_Error( 'tabside_mcp_element_type', 'element braucht elType (container, section, column oder widget).' );
		}
		$el['id']       = isset( $el['id'] ) ? (string) $el['id'] : 'tmp';
		$el['settings'] = isset( $el['settings'] ) && is_array( $el['settings'] ) ? $el['settings'] : array();
		$el['elements'] = isset( $el['elements'] ) && is_array( $el['elements'] ) ? array_values( $el['elements'] ) : array();
		if ( ! isset( $el['isInner'] ) ) {
			$el['isInner'] = false;
		}
		foreach ( $el['elements'] as $i => $child ) {
			$child = self::normalize_element( (array) $child );
			if ( is_wp_error( $child ) ) {
				return $child;
			}
			$el['elements'][ $i ] = $child;
		}
		return $el;
	}

	/**
	 * Vergleichsform: Reihenfolge und Inhalte der Elemente, leere Settings einheitlich.
	 */
	private static function normalize( array $tree ) {
		$out = array();
		foreach ( $tree as $el ) {
			$out[] = array(
				'id'         => (string) ( $el['id'] ?? '' ),
				'elType'     => (string) ( $el['elType'] ?? '' ),
				'widgetType' => (string) ( $el['widgetType'] ?? '' ),
				'settings'   => wp_json_encode( empty( $el['settings'] ) ? array() : $el['settings'] ),
				'elements'   => self::normalize( isset( $el['elements'] ) && is_array( $el['elements'] ) ? $el['elements'] : array() ),
			);
		}
		return $out;
	}

	private static function element_missing( $id ) {
		return new WP_Error( 'tabside_mcp_element_not_found', sprintf( 'Element %s nicht gefunden (IDs liefert elementor-get).', $id ), array( 'status' => 404 ) );
	}

	/**
	 * @return WP_Post|WP_Error
	 */
	private static function load( $id, $need_elementor_type = true ) {
		$post = Tabside_MCP_Abilities::load_post( $id );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		if ( $need_elementor_type && ! in_array( $post->post_type, self::supported_types(), true ) ) {
			return new WP_Error( 'tabside_mcp_elementor_type', sprintf( 'Elementor bearbeitet den Beitragstyp %s nicht (Elementor > Einstellungen > Beitragstypen).', $post->post_type ) );
		}
		return $post;
	}

	/**
	 * @return int[]|WP_Error
	 */
	private static function target_ids( $input ) {
		$ids = array();
		if ( ! empty( $input['post_id'] ) ) {
			$ids[] = (int) $input['post_id'];
		}
		foreach ( (array) ( $input['post_ids'] ?? array() ) as $id ) {
			$ids[] = (int) $id;
		}
		$ids = array_values( array_unique( array_filter( $ids ) ) );
		if ( empty( $ids ) ) {
			return new WP_Error( 'tabside_mcp_no_target', 'post_id oder post_ids angeben.', array( 'status' => 400 ) );
		}
		return $ids;
	}

	private static function id_map( $map ) {
		$out = array();
		foreach ( (array) $map as $old => $new ) {
			$out[ (int) $old ] = (int) $new;
		}
		return $out;
	}

	private static function library_exists( $id ) {
		$post = $id > 0 ? get_post( $id ) : null;
		return $post && 'elementor_library' === $post->post_type && 'trash' !== $post->post_status;
	}

	/**
	 * IDs aller Beiträge mit Elementor-Daten, die der Benutzer bearbeiten darf (ohne Papierkorb).
	 *
	 * @return int[]
	 */
	private static function elementor_post_ids() {
		$ids = get_posts(
			array(
				'post_type'      => self::supported_types(),
				'post_status'    => array( 'publish', 'draft', 'pending', 'private', 'future' ),
				'posts_per_page' => -1,
				'fields'         => 'ids',
				'meta_key'       => self::DATA_KEY, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_key
				'orderby'        => 'ID',
				'order'          => 'ASC',
			)
		);
		return array_values(
			array_filter(
				array_map( 'intval', $ids ),
				static function ( $id ) {
					return current_user_can( 'edit_post', $id );
				}
			)
		);
	}

	/* ------------------------------------------------------------------ Zusammenfassung */

	private static function summarize_list( array $elements, $full, array $path ) {
		$out = array();
		foreach ( array_values( $elements ) as $i => $el ) {
			if ( is_array( $el ) ) {
				$out[] = self::summarize( $el, $full, empty( $path ) ? array( $i ) : $path );
			}
		}
		return $out;
	}

	private static function summarize( array $el, $full, array $path ) {
		$settings = isset( $el['settings'] ) && is_array( $el['settings'] ) ? $el['settings'] : array();
		$out      = array(
			'id'     => (string) ( $el['id'] ?? '' ),
			'elType' => (string) ( $el['elType'] ?? '' ),
			'path'   => implode( '/', $path ),
		);
		if ( ! empty( $el['widgetType'] ) ) {
			$out['widgetType'] = (string) $el['widgetType'];
		}
		if ( $full ) {
			$out['settings'] = (object) $settings;
		} else {
			$content = self::content_settings( $el, $settings );
			if ( $content ) {
				$out['content'] = $content;
			}
		}
		$refs = self::collect_refs( array( $el ), false );
		foreach ( $refs as $ref ) {
			$key           = 0 === strpos( $ref['kind'], 'popup' ) ? 'popups' : 'templates';
			$out[ $key ][] = (int) $ref['id'];
		}
		if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
			$children = array();
			foreach ( array_values( $el['elements'] ) as $i => $child ) {
				if ( is_array( $child ) ) {
					$children[] = self::summarize( $child, $full, array_merge( $path, array( $i ) ) );
				}
			}
			$out['elements'] = $children;
		}
		return $out;
	}

	/**
	 * Inhalts-Settings eines Elements (Tab "Inhalt" laut Elementor-Steuerelementen), ohne Styles.
	 */
	private static function content_settings( array $el, array $settings ) {
		$tabs = self::control_tabs( $el );
		$out  = array();
		foreach ( $settings as $key => $value ) {
			if ( '__dynamic__' === $key ) {
				$out[ $key ] = $value;
				continue;
			}
			if ( null !== $tabs ) {
				if ( ( $tabs[ $key ] ?? '' ) !== 'content' ) {
					continue;
				}
			} elseif ( 0 === strpos( (string) $key, '_' ) ) {
				continue;
			}
			$short = self::shorten( $value );
			if ( null !== $short && '' !== $short && array() !== $short ) {
				$out[ $key ] = $short;
			}
		}
		return $out;
	}

	private static function shorten( $value, $depth = 0 ) {
		if ( is_string( $value ) ) {
			$text = trim( $value );
			return mb_strlen( $text ) > 300 ? mb_substr( $text, 0, 300 ) . ' …' : $text;
		}
		if ( is_array( $value ) ) {
			if ( $depth > 3 ) {
				return '…';
			}
			$out = array();
			foreach ( $value as $k => $v ) {
				$short = self::shorten( $v, $depth + 1 );
				if ( null !== $short && '' !== $short && array() !== $short ) {
					$out[ $k ] = $short;
				}
			}
			return $out;
		}
		return $value;
	}

	/**
	 * Steuerelement-Name => Tab für den Element-Typ, null wenn unbekannt.
	 */
	private static function control_tabs( array $el ) {
		$type = 'widget' === ( $el['elType'] ?? '' ) ? 'w:' . ( $el['widgetType'] ?? '' ) : 'e:' . ( $el['elType'] ?? '' );
		if ( ! array_key_exists( $type, self::$control_tabs ) ) {
			$stack = 0 === strpos( $type, 'w:' )
				? \Elementor\Plugin::$instance->widgets_manager->get_widget_types( substr( $type, 2 ) )
				: \Elementor\Plugin::$instance->elements_manager->get_element_types( substr( $type, 2 ) );
			$tabs  = null;
			if ( $stack && method_exists( $stack, 'get_controls' ) ) {
				$tabs = array();
				foreach ( (array) $stack->get_controls() as $name => $control ) {
					$tabs[ $name ] = (string) ( $control['tab'] ?? '' );
				}
				if ( empty( $tabs ) ) {
					$tabs = null;
				}
			}
			self::$control_tabs[ $type ] = $tabs;
		}
		return self::$control_tabs[ $type ];
	}

	/* ------------------------------------------------------------------ Verweise und Text */

	/**
	 * Verweise auf Popups und Vorlagen: Popup-Links, dynamische Popup-Tags, Vorlagen-Widgets,
	 * globale Widgets und Shortcodes [elementor-template id="..."].
	 *
	 * @return array<int, array{id: int, kind: string, element_id: string}>
	 */
	private static function collect_refs( array $tree, $deep = true ) {
		$refs = array();
		$fn   = static function ( $el ) use ( &$refs ) {
			$element_id = (string) ( $el['id'] ?? '' );
			$settings   = isset( $el['settings'] ) && is_array( $el['settings'] ) ? $el['settings'] : array();
			if ( 'template' === ( $el['widgetType'] ?? '' ) && ! empty( $settings['template_id'] ) ) {
				$refs[] = array(
					'id'         => (int) $settings['template_id'],
					'kind'       => 'template_widget',
					'element_id' => $element_id,
				);
			}
			if ( 'global' === ( $el['widgetType'] ?? '' ) && ! empty( $el['templateID'] ) ) {
				$refs[] = array(
					'id'         => (int) $el['templateID'],
					'kind'       => 'global_widget',
					'element_id' => $element_id,
				);
			}
			array_walk_recursive(
				$settings,
				static function ( $value ) use ( &$refs, $element_id ) {
					if ( ! is_string( $value ) ) {
						return;
					}
					foreach ( self::string_refs( $value ) as $ref ) {
						$refs[] = array_merge( $ref, array( 'element_id' => $element_id ) );
					}
				}
			);
		};
		if ( $deep ) {
			self::walk( $tree, $fn );
		} else {
			foreach ( $tree as $el ) {
				$fn( $el );
			}
		}
		return $refs;
	}

	/**
	 * @return array<int, array{id: int, kind: string}>
	 */
	private static function string_refs( $value ) {
		$refs = array();
		if ( false !== stripos( $value, 'elementor-action' ) ) {
			preg_match_all( '/#elementor-action(?::|%3A)[^"\'\s<>]+/i', $value, $m );
			foreach ( $m[0] as $hash ) {
				$action = self::parse_action( $hash );
				if ( $action && 'popup:open' === $action['action'] && ! empty( $action['settings']['id'] ) ) {
					$refs[] = array(
						'id'   => (int) $action['settings']['id'],
						'kind' => 'popup_link',
					);
				}
			}
		}
		if ( false !== strpos( $value, '[elementor-tag' ) ) {
			preg_match_all( '/\[elementor-tag[^\]]*\]/', $value, $m );
			foreach ( $m[0] as $tag ) {
				$data = self::parse_tag( $tag );
				if ( $data && 'popup' === $data['name'] && ! empty( $data['settings']['popup'] ) ) {
					$refs[] = array(
						'id'   => (int) $data['settings']['popup'],
						'kind' => 'popup_dynamic',
					);
				}
			}
		}
		if ( false !== strpos( $value, '[elementor-template' ) ) {
			preg_match_all( '/\[elementor-template\s+id=["\']?(\d+)["\']?[^\]]*\]/', $value, $m );
			foreach ( $m[1] as $id ) {
				$refs[] = array(
					'id'   => (int) $id,
					'kind' => 'template_shortcode',
				);
			}
		}
		return $refs;
	}

	/**
	 * #elementor-action:action=popup:open&settings=<base64 JSON>, auch URL-kodiert.
	 *
	 * @return array{action: string, settings: array, encoded: bool}|null
	 */
	private static function parse_action( $hash ) {
		$raw     = substr( $hash, 1 );
		$encoded = false !== stripos( $raw, '%3A' );
		$decoded = $encoded ? rawurldecode( $raw ) : html_entity_decode( $raw );
		if ( ! preg_match( '/^elementor-action:action=([^&]+)&settings=(.*)$/', $decoded, $m ) ) {
			return null;
		}
		$json = base64_decode( rawurldecode( $m[2] ), true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode
		$data = false === $json ? null : json_decode( $json, true );
		return array(
			'action'   => $m[1],
			'settings' => is_array( $data ) ? $data : array(),
			'encoded'  => $encoded,
		);
	}

	private static function build_action( $action, array $settings, $encoded ) {
		$text = sprintf( 'elementor-action:action=%1$s&settings=%2$s', $action, base64_encode( wp_json_encode( $settings ) ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode
		return '#' . ( $encoded ? rawurlencode( $text ) : $text );
	}

	/**
	 * [elementor-tag id="…" name="popup" settings="<urlencoded JSON>"].
	 *
	 * @return array{name: string, settings: array}|null
	 */
	private static function parse_tag( $tag ) {
		if ( ! preg_match( '/name="([^"]*)"/', $tag, $name ) || ! preg_match( '/settings="([^"]*)"/', $tag, $settings ) ) {
			return null;
		}
		$data = json_decode( urldecode( $settings[1] ), true );
		return array(
			'name'     => $name[1],
			'settings' => is_array( $data ) ? $data : array(),
		);
	}

	private static function replace_refs( array $tree, array $popups, array $templates ) {
		if ( empty( $popups ) && empty( $templates ) ) {
			return $tree;
		}
		$string_fn = static function ( $value ) use ( $popups, $templates ) {
			if ( $popups && false !== stripos( $value, 'elementor-action' ) ) {
				$value = preg_replace_callback(
					'/#elementor-action(?::|%3A)[^"\'\s<>]+/i',
					static function ( $m ) use ( $popups ) {
						$action = self::parse_action( $m[0] );
						if ( ! $action || 'popup:open' !== $action['action'] || ! isset( $popups[ (int) ( $action['settings']['id'] ?? 0 ) ] ) ) {
							return $m[0];
						}
						$settings       = $action['settings'];
						$settings['id'] = (string) $popups[ (int) $settings['id'] ];
						return self::build_action( $action['action'], $settings, $action['encoded'] );
					},
					$value
				);
			}
			if ( $popups && false !== strpos( $value, '[elementor-tag' ) ) {
				$value = preg_replace_callback(
					'/\[elementor-tag[^\]]*\]/',
					static function ( $m ) use ( $popups ) {
						$data = self::parse_tag( $m[0] );
						if ( ! $data || 'popup' !== $data['name'] || ! isset( $popups[ (int) ( $data['settings']['popup'] ?? 0 ) ] ) ) {
							return $m[0];
						}
						$settings          = $data['settings'];
						$settings['popup'] = (string) $popups[ (int) $settings['popup'] ];
						$encoded           = urlencode( wp_json_encode( $settings, JSON_FORCE_OBJECT ) );
						return preg_replace( '/settings="[^"]*"/', 'settings="' . $encoded . '"', $m[0] );
					},
					$value
				);
			}
			if ( $templates && false !== strpos( $value, '[elementor-template' ) ) {
				$value = preg_replace_callback(
					'/(\[elementor-template\s+id=["\']?)(\d+)(["\']?)/',
					static function ( $m ) use ( $templates ) {
						return isset( $templates[ (int) $m[2] ] ) ? $m[1] . $templates[ (int) $m[2] ] . $m[3] : $m[0];
					},
					$value
				);
			}
			return $value;
		};
		$tree = self::map_strings_tree( $tree, $string_fn, true );
		$fix  = static function ( array $elements ) use ( &$fix, $templates ) {
			foreach ( $elements as $i => $el ) {
				if ( 'template' === ( $el['widgetType'] ?? '' ) && isset( $el['settings']['template_id'], $templates[ (int) $el['settings']['template_id'] ] ) ) {
					$elements[ $i ]['settings']['template_id'] = (string) $templates[ (int) $el['settings']['template_id'] ];
				}
				if ( 'global' === ( $el['widgetType'] ?? '' ) && isset( $el['templateID'], $templates[ (int) $el['templateID'] ] ) ) {
					$elements[ $i ]['templateID'] = $templates[ (int) $el['templateID'] ];
				}
				if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
					$elements[ $i ]['elements'] = $fix( $el['elements'] );
				}
			}
			return $elements;
		};
		return $fix( $tree );
	}

	/**
	 * Wendet $fn auf alle Text-Werte der Settings an. Ohne $with_dynamic bleiben __dynamic__ und
	 * __globals__ unberührt (dort stehen Tags und Verweise, kein Text).
	 */
	private static function map_strings_tree( array $tree, callable $fn, $with_dynamic = false ) {
		foreach ( $tree as $i => $el ) {
			if ( ! is_array( $el ) ) {
				continue;
			}
			$tree[ $i ] = self::map_strings_element( $el, $fn, $with_dynamic );
			if ( ! empty( $el['elements'] ) && is_array( $el['elements'] ) ) {
				$tree[ $i ]['elements'] = self::map_strings_tree( $el['elements'], $fn, $with_dynamic );
			}
		}
		return $tree;
	}

	private static function map_strings_element( array $el, callable $fn, $with_dynamic = false ) {
		if ( empty( $el['settings'] ) || ! is_array( $el['settings'] ) ) {
			return $el;
		}
		foreach ( $el['settings'] as $key => $value ) {
			if ( ! $with_dynamic && in_array( $key, array( '__dynamic__', '__globals__' ), true ) ) {
				continue;
			}
			$el['settings'][ $key ] = self::map_strings( $value, $fn );
		}
		return $el;
	}

	private static function map_strings( $value, callable $fn ) {
		if ( is_string( $value ) ) {
			return $fn( $value );
		}
		if ( is_array( $value ) ) {
			foreach ( $value as $k => $v ) {
				$value[ $k ] = self::map_strings( $v, $fn );
			}
		}
		return $value;
	}

	/**
	 * Fundstellen in Text-Werten der Settings (ohne __dynamic__/__globals__).
	 *
	 * @return array<int, array<string, mixed>>
	 */
	private static function search_tree( array $tree, $search, $case_sensitive ) {
		$hits = array();
		self::walk(
			$tree,
			static function ( $el ) use ( &$hits, $search, $case_sensitive ) {
				$settings = isset( $el['settings'] ) && is_array( $el['settings'] ) ? $el['settings'] : array();
				foreach ( $settings as $key => $value ) {
					if ( in_array( $key, array( '__dynamic__', '__globals__' ), true ) ) {
						continue;
					}
					self::search_value(
						$value,
						(string) $key,
						static function ( $field, $text ) use ( &$hits, $el, $search, $case_sensitive ) {
							$count = $case_sensitive ? substr_count( $text, $search ) : substr_count( mb_strtolower( $text ), mb_strtolower( $search ) );
							if ( 0 === $count ) {
								return;
							}
							$pos = $case_sensitive ? mb_strpos( $text, $search ) : mb_stripos( $text, $search );
							$hit = array(
								'element_id'  => (string) ( $el['id'] ?? '' ),
								'widgetType'  => (string) ( $el['widgetType'] ?? $el['elType'] ?? '' ),
								'field'       => $field,
								'occurrences' => $count,
							);
							$start          = max( 0, (int) $pos - 60 );
							$hit['context'] = ( $start > 0 ? '…' : '' ) . mb_substr( $text, $start, mb_strlen( $search ) + 120 ) . ( mb_strlen( $text ) > $start + mb_strlen( $search ) + 120 ? '…' : '' );
							$hits[]         = $hit;
						}
					);
				}
			}
		);
		return $hits;
	}

	private static function search_value( $value, $field, callable $fn ) {
		if ( is_string( $value ) ) {
			$fn( $field, $value );
		} elseif ( is_array( $value ) ) {
			foreach ( $value as $k => $v ) {
				self::search_value( $v, $field . '.' . $k, $fn );
			}
		}
	}
}
