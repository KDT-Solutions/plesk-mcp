<?php
/**
 * Abilities der Kategorie "tabside": Beiträge aller Beitragstypen, Begriffe, Medien. Registriert auch die
 * Abilities der übrigen Klassen (Elementor, Navigation/Einstellungen, Administration).
 *
 * Schreibende Abilities führen nur mit confirm=true aus, sonst liefern sie eine Vorschau. Jede ausgeführte
 * Änderung wird protokolliert (audit-log), danach werden die betroffenen Caches geleert.
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Abilities {

	const CATEGORY  = 'tabside';
	const STATUSES  = array( 'draft', 'pending', 'publish', 'future', 'private' );
	const MAX_LIMIT = 100;
	// Filter für post-list (meta_key), keine beliebigen Meta-Abfragen.
	const META_FILTERS = array( '_elementor_edit_mode', '_elementor_template_type', '_wp_page_template' );

	/**
	 * Ability-Namen in der Reihenfolge der Registrierung.
	 *
	 * @return string[]
	 */
	public static function names() {
		return array_map(
			static function ( $slug ) {
				return self::CATEGORY . '/' . $slug;
			},
			array_keys( self::all_definitions() )
		);
	}

	public static function register_category() {
		wp_register_ability_category(
			self::CATEGORY,
			array(
				'label'       => __( 'tabside', 'tabside-mcp' ),
				'description' => __( 'Seite verwalten über tabside MCP (Inhalte, Elementor, Navigation, Einstellungen, Administration).', 'tabside-mcp' ),
			)
		);
	}

	/**
	 * @return array<string, array<string, mixed>>
	 */
	public static function all_definitions() {
		return array_merge(
			self::definitions(),
			Tabside_MCP_Elementor::definitions(),
			Tabside_MCP_Site::definitions(),
			Tabside_MCP_Admin::definitions()
		);
	}

	public static function register() {
		foreach ( self::all_definitions() as $slug => $def ) {
			$readonly = ! empty( $def['readonly'] );
			$schema   = array(
				'type'                 => 'object',
				// Der REST-Endpunkt des Kerns übergibt ohne Parameter null, der Standardwert macht daraus {}.
				'default'              => array(),
				'properties'           => $def['properties'],
				'required'             => $def['required'] ?? array(),
				'additionalProperties' => false,
			);
			wp_register_ability(
				self::CATEGORY . '/' . $slug,
				array(
					'label'               => $def['label'],
					'description'         => $def['description'],
					'category'            => self::CATEGORY,
					'input_schema'        => $schema,
					'execute_callback'    => static function ( $input = array() ) use ( $slug, $def, $readonly, $schema ) {
						// Lesende Abilities bekommen die Eingabe als Query-String (full=false ist dann der Text "false"),
						// der Kern prüft sie nur. Darum hier nach dem Schema in echte Typen umwandeln.
						$input = rest_sanitize_value_from_schema( is_array( $input ) ? $input : array(), $schema, 'input' );
						if ( is_wp_error( $input ) ) {
							$input->add_data( array( 'status' => 400 ) );
							return $input;
						}
						$result = call_user_func( $def['execute'], $input );
						if ( is_wp_error( $result ) ) {
							// Ohne Status liefert der REST-Endpunkt 500, gemeint ist aber eine abgelehnte Eingabe.
							$data = $result->get_error_data();
							if ( ! is_array( $data ) || ! isset( $data['status'] ) ) {
								$result->add_data( array_merge( is_array( $data ) ? $data : array(), array( 'status' => 400 ) ) );
							}
							return $result;
						}
						if ( ! $readonly && is_array( $result ) && ! empty( $result['confirmed'] ) ) {
							self::after_write( $slug, $def, $input, $result );
						}
						return $result;
					},
					'permission_callback' => $def['permission'],
					'meta'                => array(
						// meta.public gibt es im Core erst ab 7.1, der MCP Adapter liest zusätzlich meta.mcp.public.
						'public'       => true,
						'show_in_rest' => true,
						'mcp'          => array( 'public' => true ),
						'annotations'  => array(
							'readonly'    => $readonly,
							'destructive' => ! empty( $def['destructive'] ),
							'idempotent'  => $readonly,
						),
					),
				)
			);
		}
	}

	/**
	 * Nach jeder ausgeführten Änderung: Caches leeren (flush = post | site) und protokollieren.
	 */
	private static function after_write( $slug, $def, $input, &$result ) {
		$posts = array();
		if ( isset( $result['affected_posts'] ) ) {
			$posts = array_map( 'intval', (array) $result['affected_posts'] );
		} elseif ( isset( $result['post']['id'] ) ) {
			$posts = array( (int) $result['post']['id'] );
		} elseif ( ! empty( $input['id'] ) ) {
			$posts = array( (int) $input['id'] );
		}
		if ( 'post' === ( $def['flush'] ?? '' ) && $posts ) {
			$result['cache'] = Tabside_MCP_Cache::flush( $posts );
		} elseif ( 'site' === ( $def['flush'] ?? '' ) ) {
			$result['cache'] = Tabside_MCP_Cache::flush();
		}

		if ( isset( $result['audit_target'] ) ) {
			$target = (string) $result['audit_target'];
		} elseif ( $posts ) {
			$target = 'post ' . implode( ',', $posts );
		} else {
			$target = '';
			foreach ( array( 'plugin', 'slug', 'theme', 'user_id', 'menu_id', 'item_id', 'widget_id', 'term_id', 'redirect_id', 'source', 'name', 'values', 'locations' ) as $key ) {
				if ( isset( $input[ $key ] ) && '' !== $input[ $key ] ) {
					// Bei Objekten (values, locations) nur die Schlüssel, nie die Werte.
					$value  = is_array( $input[ $key ] ) ? implode( ',', array_keys( $input[ $key ] ) ) : $input[ $key ];
					$target = $key . ' ' . $value;
					break;
				}
			}
			if ( '' === $target && isset( $result['media']['id'] ) ) {
				$target = 'media ' . $result['media']['id'];
			}
		}
		$backup = $result['backup_id'] ?? ( $result['safety_backup_id'] ?? '' );
		if ( '' === $backup && ! empty( $result['backup_ids'] ) ) {
			$backup = implode( ',', (array) $result['backup_ids'] );
		}
		Tabside_MCP_Store::log( $slug, $target, $backup );
		unset( $result['audit_target'] );
	}

	/**
	 * @return array<string, array<string, mixed>>
	 */
	private static function definitions() {
		$confirm = self::confirm_schema();
		$terms   = array(
			'type'  => 'array',
			'items' => array( 'type' => array( 'integer', 'string' ) ),
		);
		$fields  = array(
			'title'                   => array(
				'type'        => 'string',
				'description' => 'Titel.',
			),
			'content'                 => array(
				'type'        => 'string',
				'description' => 'Inhalt als HTML oder als Gutenberg-Block-Markup (<!-- wp:paragraph -->...). Wird unverändert gespeichert. Elementor-Seiten über die elementor-Abilities bearbeiten.',
			),
			'status'                  => array(
				'type'        => 'string',
				'enum'        => self::STATUSES,
				'description' => 'draft, pending, publish, future oder private. publish mit Datum in der Zukunft wird zu future.',
			),
			'date'                    => array(
				'type'        => 'string',
				'description' => 'Veröffentlichungsdatum: YYYY-MM-DD, YYYY-MM-DD HH:MM[:SS] oder ISO 8601 mit Zeitzone. Ohne Zeitzone gilt die Zeitzone der Seite, ohne Uhrzeit 00:00.',
			),
			'categories'              => $terms + array( 'description' => 'Kategorien als ID, Slug oder Name; müssen existieren. Ersetzt die bisherigen.' ),
			'tags'                    => $terms + array( 'description' => 'Schlagwörter als ID oder Name; fehlende Namen werden angelegt. Ersetzt die bisherigen.' ),
			'terms'                   => array(
				'type'                 => 'object',
				'additionalProperties' => $terms,
				'description'          => 'Begriffe weiterer Taxonomien: {"taxonomie": [ID, Slug oder Name]}. Müssen existieren (term-create). Ersetzt die bisherigen dieser Taxonomie.',
			),
			'featured_media'          => array(
				'type'        => 'integer',
				'minimum'     => 0,
				'description' => 'ID eines Bildes aus der Mediathek als Beitragsbild, 0 entfernt es.',
			),
			'excerpt'                 => array(
				'type'        => 'string',
				'description' => 'Auszug.',
			),
			'slug'                    => array(
				'type'        => 'string',
				'description' => 'Titelform (URL-Teil).',
			),
			'hide_featured_on_single' => array(
				'type'        => 'boolean',
				'description' => 'Beitragsbild auf der Beitragsseite ausblenden (Theme-Option, z.B. Blockchain), true oder false.',
			),
			'parent'                  => array(
				'type'        => 'integer',
				'minimum'     => 0,
				'description' => 'Eltern-Element (nur hierarchische Beitragstypen wie page), 0 = keines.',
			),
			'menu_order'              => array(
				'type'        => 'integer',
				'description' => 'Reihenfolge.',
			),
			'template'                => array(
				'type'        => 'string',
				'description' => 'Seiten-Template, z.B. default, elementor_canvas, elementor_header_footer oder eine Template-Datei des Themes.',
			),
			'comment_status'          => array(
				'type' => 'string',
				'enum' => array( 'open', 'closed' ),
			),
		);
		$id      = self::id_schema( 'ID des Beitrags (beliebiger Beitragstyp).' );
		$limit   = array(
			'type'    => 'integer',
			'minimum' => 1,
			'maximum' => self::MAX_LIMIT,
			'default' => 20,
		);
		$page    = array(
			'type'    => 'integer',
			'minimum' => 1,
			'default' => 1,
		);
		$search  = array( 'type' => 'string' );
		$type    = array(
			'type'        => 'string',
			'default'     => 'post',
			'description' => 'Beitragstyp aus post-type-list, z.B. post, page, elementor_library oder ein eigener Typ.',
		);
		$edit    = static function () {
			return Tabside_MCP_Plugin::check_user( 'edit_posts' );
		};

		return array(
			'get-version'    => array(
				'label'       => 'Version',
				'description' => 'Version und Commit von tabside MCP, WordPress-, Elementor-, PHP-Version und ob Admin-Abilities für den Benutzer verfügbar sind.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $edit,
				'execute'     => array( __CLASS__, 'get_version' ),
			),
			'post-type-list' => array(
				'label'       => 'Beitragstypen auflisten',
				'description' => 'Alle Beitragstypen mit Verwaltungsoberfläche: Slug, Bezeichnung, URL-Präfix, Taxonomien, Elementor-Unterstützung, Anzahl.',
				'readonly'    => true,
				'properties'  => array(),
				'permission'  => $edit,
				'execute'     => array( __CLASS__, 'post_type_list' ),
			),
			'post-list'      => array(
				'label'       => 'Beiträge auflisten',
				'description' => 'Listet Beiträge eines Beitragstyps (oder any), neueste zuerst. Filter: Status, Suche, Kategorie, Eltern-ID, Meta-Feld (nur Elementor-Modus, Vorlagentyp, Template).',
				'readonly'    => true,
				'properties'  => array(
					'type'       => array_merge( $type, array( 'description' => 'Beitragstyp aus post-type-list oder any (alle).' ) ),
					'status'     => array(
						'type'    => 'string',
						'enum'    => array_merge( self::STATUSES, array( 'trash', 'any' ) ),
						'default' => 'any',
					),
					'search'     => $search,
					'category'   => array(
						'type'        => array( 'integer', 'string' ),
						'description' => 'Kategorie-ID oder Slug.',
					),
					'parent'     => array(
						'type'    => 'integer',
						'minimum' => 0,
					),
					'meta_key'   => array(
						'type' => 'string',
						'enum' => self::META_FILTERS,
					),
					'meta_value' => array(
						'type'        => 'string',
						'description' => 'Wert zu meta_key, z.B. popup bei _elementor_template_type. Leer = Feld vorhanden.',
					),
					'orderby'    => array(
						'type'    => 'string',
						'enum'    => array( 'date', 'modified', 'title', 'menu_order', 'ID' ),
						'default' => 'date',
					),
					'order'      => array(
						'type'    => 'string',
						'enum'    => array( 'ASC', 'DESC' ),
						'default' => 'DESC',
					),
					'limit'      => $limit,
					'page'       => $page,
				),
				'permission'  => static function ( $input ) {
					$type = $input['type'] ?? 'post';
					if ( 'any' === $type ) {
						return Tabside_MCP_Plugin::check_user( 'edit_posts' );
					}
					$obj = self::type_object( $type );
					return is_wp_error( $obj ) ? $obj : Tabside_MCP_Plugin::check_user( $obj->cap->edit_posts );
				},
				'execute'     => array( __CLASS__, 'post_list' ),
			),
			'post-get'       => array(
				'label'       => 'Beitrag abrufen',
				'description' => 'Ein Beitrag beliebigen Typs mit Inhalt (roh), Datum, Begriffen, Beitragsbild, Template, Eltern-Seite, Elementor-Angaben (Vorlagentyp, Bedingungen), Zusatzfeldern und den vorhandenen Sicherungen.',
				'readonly'    => true,
				'properties'  => array( 'id' => $id ),
				'required'    => array( 'id' ),
				'permission'  => static function ( $input ) {
					return self::check_post_permission( $input, 'edit_post' );
				},
				'execute'     => array( __CLASS__, 'post_get' ),
			),
			'category-list'  => array(
				'label'       => 'Kategorien auflisten',
				'description' => 'Kategorien mit ID, Name, Slug, Eltern-ID und Anzahl Beiträge.',
				'readonly'    => true,
				'properties'  => array(
					'search' => $search,
					'limit'  => array_merge( $limit, array( 'default' => 100 ) ),
				),
				'permission'  => $edit,
				'execute'     => static function ( $input ) {
					return self::term_list( array_merge( $input, array( 'taxonomy' => 'category' ) ) );
				},
			),
			'tag-list'       => array(
				'label'       => 'Schlagwörter auflisten',
				'description' => 'Schlagwörter mit ID, Name, Slug und Anzahl Beiträge.',
				'readonly'    => true,
				'properties'  => array(
					'search' => $search,
					'limit'  => array_merge( $limit, array( 'default' => 100 ) ),
				),
				'permission'  => $edit,
				'execute'     => static function ( $input ) {
					return self::term_list( array_merge( $input, array( 'taxonomy' => 'post_tag' ) ) );
				},
			),
			'term-list'      => array(
				'label'       => 'Begriffe auflisten',
				'description' => 'Begriffe einer beliebigen Taxonomie (category, post_tag, eigene). Ohne taxonomy: Liste der Taxonomien.',
				'readonly'    => true,
				'properties'  => array(
					'taxonomy' => array( 'type' => 'string' ),
					'search'   => $search,
					'parent'   => array(
						'type'    => 'integer',
						'minimum' => 0,
					),
					'limit'    => array_merge( $limit, array( 'default' => 100 ) ),
					'page'     => $page,
				),
				'permission'  => $edit,
				'execute'     => array( __CLASS__, 'term_list' ),
			),
			'media-list'     => array(
				'label'       => 'Medien auflisten',
				'description' => 'Dateien der Mediathek, neueste zuerst, mit URL zum Verlinken.',
				'readonly'    => true,
				'properties'  => array(
					'search'    => $search,
					'mime_type' => array(
						'type'        => 'string',
						'description' => 'z.B. application/pdf oder image.',
					),
					'limit'     => $limit,
					'page'      => $page,
				),
				'permission'  => static function () {
					return Tabside_MCP_Plugin::check_user( 'upload_files' );
				},
				'execute'     => array( __CLASS__, 'media_list' ),
			),
			'post-create'    => array(
				'label'       => 'Beitrag anlegen',
				'description' => 'Legt einen Beitrag beliebigen Typs an. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array_merge( array( 'type' => $type ), $fields, array( 'confirm' => $confirm ) ),
				'required'    => array( 'title' ),
				'permission'  => array( __CLASS__, 'permission_create' ),
				'execute'     => array( __CLASS__, 'post_create' ),
			),
			'post-update'    => array(
				'label'       => 'Beitrag ändern',
				'description' => 'Ändert nur die übergebenen Felder. Vorher wird der alte Stand gesichert (post-rollback). Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array_merge( array( 'id' => $id ), $fields, array( 'confirm' => $confirm ) ),
				'required'    => array( 'id' ),
				'permission'  => array( __CLASS__, 'permission_update' ),
				'execute'     => array( __CLASS__, 'post_update' ),
			),
			'post-duplicate' => array(
				'label'       => 'Beitrag duplizieren',
				'description' => 'Kopiert einen Beitrag beliebigen Typs mit allen Zusatzfeldern (auch Elementor-Daten mit neuen Element-IDs), Begriffen und Beitragsbild. Bei Popups und Theme-Vorlagen sind die Anzeigebedingungen der Kopie leer, ausser conditions wird übergeben. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'id'         => $id,
					'title'      => array(
						'type'        => 'string',
						'description' => 'Titel der Kopie, Standard "<Titel> (Kopie)".',
					),
					'slug'       => array( 'type' => 'string' ),
					'status'     => array_merge( $fields['status'], array( 'default' => 'draft' ) ),
					'conditions' => array(
						'type'        => 'array',
						'items'       => array( 'type' => 'string' ),
						'description' => 'Elementor-Anzeigebedingungen der Kopie (nur Popups/Theme-Vorlagen, Elementor Pro), z.B. ["include/singular/page/12"]. Standard: keine.',
					),
					'confirm'    => $confirm,
				),
				'required'    => array( 'id' ),
				'permission'  => static function ( $input ) {
					$ok = self::check_post_permission( $input, 'edit_post' );
					if ( true !== $ok ) {
						return $ok;
					}
					$obj = get_post_type_object( get_post( (int) $input['id'] )->post_type );
					$ok  = Tabside_MCP_Plugin::check_user( $obj->cap->create_posts );
					if ( true === $ok && in_array( $input['status'] ?? 'draft', array( 'publish', 'future', 'private' ), true ) ) {
						$ok = Tabside_MCP_Plugin::check_user( $obj->cap->publish_posts );
					}
					return $ok;
				},
				'execute'     => array( __CLASS__, 'post_duplicate' ),
			),
			'media-upload'   => array(
				'label'       => 'Datei hochladen',
				'description' => 'Lädt eine Datei (Base64) in die Mediathek, z.B. PDF oder Bild, und gibt die URL zum Verlinken zurück. Ohne confirm=true nur Vorschau.',
				'properties'  => array(
					'filename'    => array(
						'type'        => 'string',
						'description' => 'Dateiname mit Endung, z.B. merkblatt.pdf.',
					),
					'data_base64' => array(
						'type'        => 'string',
						'description' => 'Dateiinhalt als Base64 (optional mit data:-Präfix).',
					),
					'title'       => array( 'type' => 'string' ),
					'alt_text'    => array( 'type' => 'string' ),
					'caption'     => array( 'type' => 'string' ),
					'description' => array( 'type' => 'string' ),
					'post_id'     => array(
						'type'        => 'integer',
						'minimum'     => 1,
						'description' => 'Optional: Datei diesem Beitrag zuordnen.',
					),
					'confirm'     => $confirm,
				),
				'required'    => array( 'filename', 'data_base64' ),
				'permission'  => static function ( $input ) {
					$ok = Tabside_MCP_Plugin::check_user( 'upload_files' );
					if ( true === $ok && ! empty( $input['post_id'] ) ) {
						$ok = self::check_post_permission( array( 'id' => $input['post_id'] ), 'edit_post' );
					}
					return $ok;
				},
				'execute'     => array( __CLASS__, 'media_upload' ),
			),
			'post-trash'     => array(
				'label'       => 'In den Papierkorb',
				'description' => 'Verschiebt einen Beitrag beliebigen Typs in den Papierkorb (kein endgültiges Löschen). Ohne confirm=true nur Vorschau.',
				'destructive' => true,
				'flush'       => 'post',
				'properties'  => array(
					'id'      => $id,
					'confirm' => $confirm,
				),
				'required'    => array( 'id' ),
				'permission'  => static function ( $input ) {
					return self::check_post_permission( $input, 'delete_post' );
				},
				'execute'     => array( __CLASS__, 'post_trash' ),
			),
			'post-rollback'  => array(
				'label'       => 'Sicherung zurückholen',
				'description' => 'Stellt einen Beitrag aus einer Sicherung wieder her (Standard: neueste), auch Elementor-Daten exakt. Der aktuelle Stand wird vorher ebenfalls gesichert. Ohne confirm=true nur Vorschau.',
				'flush'       => 'post',
				'properties'  => array(
					'id'        => $id,
					'backup_id' => array(
						'type'        => 'string',
						'description' => 'ID aus post-get (backups). Leer = neueste Sicherung.',
					),
					'confirm'   => $confirm,
				),
				'required'    => array( 'id' ),
				'permission'  => static function ( $input ) {
					return self::check_post_permission( $input, 'edit_post' );
				},
				'execute'     => array( __CLASS__, 'post_rollback' ),
			),
			'term-create'    => array(
				'label'       => 'Begriff anlegen',
				'description' => 'Legt einen Begriff in einer Taxonomie an (Kategorie, Schlagwort, eigene). Ohne confirm=true nur Vorschau.',
				'properties'  => array(
					'taxonomy'    => array( 'type' => 'string' ),
					'name'        => array( 'type' => 'string' ),
					'slug'        => array( 'type' => 'string' ),
					'description' => array( 'type' => 'string' ),
					'parent'      => array(
						'type'    => 'integer',
						'minimum' => 0,
					),
					'confirm'     => $confirm,
				),
				'required'    => array( 'taxonomy', 'name' ),
				'permission'  => static function ( $input ) {
					return self::check_term_permission( $input, 'manage_terms' );
				},
				'execute'     => array( __CLASS__, 'term_create' ),
			),
			'term-update'    => array(
				'label'       => 'Begriff ändern',
				'description' => 'Ändert Name, Slug, Beschreibung oder Eltern-Begriff. Ohne confirm=true nur Vorschau.',
				'properties'  => array(
					'taxonomy'    => array( 'type' => 'string' ),
					'term_id'     => self::id_schema( 'ID des Begriffs.' ),
					'name'        => array( 'type' => 'string' ),
					'slug'        => array( 'type' => 'string' ),
					'description' => array( 'type' => 'string' ),
					'parent'      => array(
						'type'    => 'integer',
						'minimum' => 0,
					),
					'confirm'     => $confirm,
				),
				'required'    => array( 'taxonomy', 'term_id' ),
				'permission'  => static function ( $input ) {
					return self::check_term_permission( $input, 'edit_terms' );
				},
				'execute'     => array( __CLASS__, 'term_update' ),
			),
			'term-delete'    => array(
				'label'       => 'Begriff löschen',
				'description' => 'Löscht einen Begriff, aber nur, wenn ihm keine Beiträge zugeordnet sind. Name, Slug, Beschreibung und Zusatzfelder werden vorher gesichert. Ohne confirm=true nur Vorschau.',
				'destructive' => true,
				'properties'  => array(
					'taxonomy' => array( 'type' => 'string' ),
					'term_id'  => self::id_schema( 'ID des Begriffs.' ),
					'confirm'  => $confirm,
				),
				'required'    => array( 'taxonomy', 'term_id' ),
				'permission'  => static function ( $input ) {
					return self::check_term_permission( $input, 'delete_terms' );
				},
				'execute'     => array( __CLASS__, 'term_delete' ),
			),
		);
	}

	/* ------------------------------------------------------------------ Schemas */

	public static function confirm_schema() {
		return array(
			'type'        => 'boolean',
			'default'     => false,
			'description' => 'Ohne true wird nichts geändert, es kommt nur eine Vorschau.',
		);
	}

	public static function id_schema( $description ) {
		return array(
			'type'        => 'integer',
			'minimum'     => 1,
			'description' => $description,
		);
	}

	/* ------------------------------------------------------------------ Berechtigung */

	/**
	 * Beitragstypen, die über tabside MCP bearbeitet werden dürfen: alle mit Verwaltungsoberfläche ausser Medien.
	 *
	 * @return array<string, WP_Post_Type>
	 */
	public static function post_types() {
		$out = array();
		foreach ( get_post_types( array( 'show_ui' => true ), 'objects' ) as $name => $obj ) {
			if ( 'attachment' !== $name ) {
				$out[ $name ] = $obj;
			}
		}
		return $out;
	}

	/**
	 * @return WP_Post_Type|WP_Error
	 */
	public static function type_object( $type ) {
		$types = self::post_types();
		if ( ! isset( $types[ $type ] ) ) {
			return new WP_Error( 'tabside_mcp_invalid_type', sprintf( 'Unbekannter Beitragstyp "%s". Vorhandene liefert post-type-list.', $type ), array( 'status' => 400 ) );
		}
		return $types[ $type ];
	}

	/**
	 * @param array  $input      Eingabe mit id.
	 * @param string $capability edit_post | delete_post.
	 * @return true|WP_Error
	 */
	public static function check_post_permission( $input, $capability ) {
		$ok = Tabside_MCP_Plugin::check_user();
		if ( true !== $ok ) {
			return $ok;
		}
		$post = self::load_post( (int) ( $input['id'] ?? 0 ) );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		return Tabside_MCP_Plugin::check_user( $capability, $post->ID );
	}

	public static function permission_create( $input ) {
		$obj = self::type_object( $input['type'] ?? 'post' );
		if ( is_wp_error( $obj ) ) {
			return $obj;
		}
		$ok = Tabside_MCP_Plugin::check_user( $obj->cap->create_posts );
		if ( true === $ok && in_array( $input['status'] ?? 'draft', array( 'publish', 'future', 'private' ), true ) ) {
			$ok = Tabside_MCP_Plugin::check_user( $obj->cap->publish_posts );
		}
		return $ok;
	}

	public static function permission_update( $input ) {
		$ok = self::check_post_permission( $input, 'edit_post' );
		if ( true === $ok && in_array( $input['status'] ?? '', array( 'publish', 'future', 'private' ), true ) ) {
			$post = get_post( (int) $input['id'] );
			$ok   = Tabside_MCP_Plugin::check_user( get_post_type_object( $post->post_type )->cap->publish_posts );
		}
		return $ok;
	}

	private static function check_term_permission( $input, $cap ) {
		$tax = get_taxonomy( (string) ( $input['taxonomy'] ?? '' ) );
		if ( ! $tax || ! $tax->show_ui ) {
			return new WP_Error( 'tabside_mcp_invalid_taxonomy', sprintf( 'Unbekannte Taxonomie "%s". Vorhandene liefert term-list ohne taxonomy.', $input['taxonomy'] ?? '' ), array( 'status' => 400 ) );
		}
		if ( ! empty( $input['term_id'] ) ) {
			return Tabside_MCP_Plugin::check_user( 'delete_terms' === $cap ? 'delete_term' : 'edit_term', (int) $input['term_id'] );
		}
		return Tabside_MCP_Plugin::check_user( $tax->cap->$cap );
	}

	/* ------------------------------------------------------------------ Lesen */

	public static function get_version() {
		$elementor = Tabside_MCP_Elementor::active();
		return array(
			'plugin'           => 'tabside-mcp',
			'version'          => TABSIDE_MCP_VERSION,
			'commit'           => TABSIDE_MCP_COMMIT,
			'wordpress'        => get_bloginfo( 'version' ),
			'mcp_adapter'      => defined( 'WP_MCP_VERSION' ) ? WP_MCP_VERSION : null,
			'php'              => PHP_VERSION,
			'site_url'         => home_url( '/' ),
			'timezone'         => wp_timezone_string(),
			'user'             => wp_get_current_user()->user_login,
			'roles'            => array_values( wp_get_current_user()->roles ),
			'allowed_user'     => Tabside_MCP_Plugin::allowed_login() ? Tabside_MCP_Plugin::allowed_login() : null,
			'admin_tools'      => Tabside_MCP_Admin::available() ? 'enabled' : 'disabled',
			'elementor'        => $elementor ? ELEMENTOR_VERSION : null,
			'elementor_pro'    => Tabside_MCP_Elementor::pro_active() ? ELEMENTOR_PRO_VERSION : null,
			'redirect_plugin'  => Tabside_MCP_Site::redirect_plugin(),
			'cache_plugins'    => array_keys( array_filter( Tabside_MCP_Cache::detected() ) ),
			'max_upload_mb'    => round( wp_max_upload_size() / 1048576, 1 ),
		);
	}

	public static function post_type_list() {
		$elementor = Tabside_MCP_Elementor::supported_types();
		$out       = array();
		foreach ( self::post_types() as $name => $obj ) {
			$counts = wp_count_posts( $name );
			$total  = 0;
			foreach ( (array) $counts as $status => $n ) {
				if ( ! in_array( $status, array( 'trash', 'auto-draft', 'inherit' ), true ) ) {
					$total += (int) $n;
				}
			}
			$out[] = array(
				'name'         => $name,
				'label'        => $obj->label,
				'singular'     => $obj->labels->singular_name,
				'public'       => (bool) $obj->public,
				'hierarchical' => (bool) $obj->hierarchical,
				'rewrite_slug' => is_array( $obj->rewrite ) && isset( $obj->rewrite['slug'] ) ? $obj->rewrite['slug'] : null,
				'taxonomies'   => array_values( get_object_taxonomies( $name ) ),
				'supports'     => array_keys( get_all_post_type_supports( $name ) ),
				'elementor'    => in_array( $name, $elementor, true ),
				'count'        => $total,
				'can_create'   => current_user_can( $obj->cap->create_posts ),
			);
		}
		return array( 'post_types' => $out );
	}

	public static function post_list( $input ) {
		$type  = $input['type'] ?? 'post';
		$limit = min( self::MAX_LIMIT, max( 1, (int) ( $input['limit'] ?? 20 ) ) );
		$page  = max( 1, (int) ( $input['page'] ?? 1 ) );
		if ( 'any' === $type ) {
			$types = array();
			foreach ( self::post_types() as $name => $obj ) {
				if ( current_user_can( $obj->cap->edit_posts ) ) {
					$types[] = $name;
				}
			}
		} else {
			$types = $type;
		}
		$args = array(
			'post_type'           => $types,
			'post_status'         => $input['status'] ?? 'any',
			'posts_per_page'      => $limit,
			'paged'               => $page,
			'orderby'             => $input['orderby'] ?? 'date',
			'order'               => $input['order'] ?? 'DESC',
			'ignore_sticky_posts' => true,
		);
		if ( ! empty( $input['search'] ) ) {
			$args['s'] = (string) $input['search'];
		}
		if ( isset( $input['category'] ) && '' !== $input['category'] ) {
			if ( is_numeric( $input['category'] ) ) {
				$args['cat'] = (int) $input['category'];
			} else {
				$args['category_name'] = sanitize_title( (string) $input['category'] );
			}
		}
		if ( isset( $input['parent'] ) ) {
			$args['post_parent'] = (int) $input['parent'];
		}
		if ( ! empty( $input['meta_key'] ) && in_array( $input['meta_key'], self::META_FILTERS, true ) ) {
			$meta = array( 'key' => $input['meta_key'] );
			if ( isset( $input['meta_value'] ) && '' !== $input['meta_value'] ) {
				$meta['value'] = (string) $input['meta_value'];
			} else {
				$meta['compare'] = 'EXISTS';
			}
			$args['meta_query'] = array( $meta ); // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_query
		}
		$query = new WP_Query( $args );
		return array(
			'total' => (int) $query->found_posts,
			'page'  => $page,
			'pages' => (int) $query->max_num_pages,
			'posts' => array_map( array( __CLASS__, 'summary' ), $query->posts ),
		);
	}

	public static function post_get( $input ) {
		$post = self::load_post( (int) $input['id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		return self::details( $post );
	}

	public static function term_list( $input ) {
		if ( empty( $input['taxonomy'] ) ) {
			$out = array();
			foreach ( get_taxonomies( array( 'show_ui' => true ), 'objects' ) as $name => $tax ) {
				$out[] = array(
					'name'         => $name,
					'label'        => $tax->label,
					'hierarchical' => (bool) $tax->hierarchical,
					'post_types'   => array_values( (array) $tax->object_type ),
				);
			}
			return array( 'taxonomies' => $out );
		}
		$tax = get_taxonomy( (string) $input['taxonomy'] );
		if ( ! $tax || ! $tax->show_ui ) {
			return new WP_Error( 'tabside_mcp_invalid_taxonomy', sprintf( 'Unbekannte Taxonomie "%s".', $input['taxonomy'] ), array( 'status' => 400 ) );
		}
		$limit = min( self::MAX_LIMIT, max( 1, (int) ( $input['limit'] ?? 100 ) ) );
		$args  = array(
			'taxonomy'   => $tax->name,
			'hide_empty' => false,
			'number'     => $limit,
			'offset'     => ( max( 1, (int) ( $input['page'] ?? 1 ) ) - 1 ) * $limit,
			'orderby'    => 'name',
		);
		if ( ! empty( $input['search'] ) ) {
			$args['search'] = (string) $input['search'];
		}
		if ( isset( $input['parent'] ) ) {
			$args['parent'] = (int) $input['parent'];
		}
		$terms = get_terms( $args );
		if ( is_wp_error( $terms ) ) {
			return $terms;
		}
		return array(
			'taxonomy' => $tax->name,
			'terms'    => array_map( array( __CLASS__, 'term_summary' ), $terms ),
		);
	}

	public static function media_list( $input ) {
		$limit = min( self::MAX_LIMIT, max( 1, (int) ( $input['limit'] ?? 20 ) ) );
		$page  = max( 1, (int) ( $input['page'] ?? 1 ) );
		$args  = array(
			'post_type'      => 'attachment',
			'post_status'    => 'inherit',
			'posts_per_page' => $limit,
			'paged'          => $page,
			'orderby'        => 'date',
			'order'          => 'DESC',
		);
		if ( ! empty( $input['search'] ) ) {
			$args['s'] = (string) $input['search'];
		}
		if ( ! empty( $input['mime_type'] ) ) {
			$args['post_mime_type'] = (string) $input['mime_type'];
		}
		$query = new WP_Query( $args );
		return array(
			'total' => (int) $query->found_posts,
			'page'  => $page,
			'pages' => (int) $query->max_num_pages,
			'media' => array_map( array( __CLASS__, 'media_summary' ), $query->posts ),
		);
	}

	/* ------------------------------------------------------------------ Schreiben */

	public static function post_create( $input ) {
		$type    = $input['type'] ?? 'post';
		$status  = $input['status'] ?? 'draft';
		$notes   = array();
		$changes = self::prepare_fields( $input, $type, $notes );
		if ( is_wp_error( $changes ) ) {
			return $changes;
		}
		$ts        = isset( $changes['date'] ) ? $changes['date']['ts'] : null;
		$effective = self::effective_status( $status, $ts );
		if ( is_wp_error( $effective ) ) {
			return $effective;
		}
		if ( $effective !== $status ) {
			$notes[] = sprintf( 'Status %s wird wegen des Datums zu %s.', $status, $effective );
		}

		$preview = array(
			'type'    => $type,
			'title'   => $input['title'],
			'status'  => $effective,
			'date'    => isset( $changes['date'] ) ? $changes['date']['local'] : 'jetzt',
			'content' => self::text_preview( $input['content'] ?? '' ),
		);
		foreach ( $changes as $key => $value ) {
			if ( 'date' !== $key ) {
				$preview[ $key ] = self::describe( $key, $value );
			}
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( $preview, $notes );
		}

		$postarr = array(
			'post_type'    => $type,
			'post_title'   => (string) $input['title'],
			'post_content' => (string) ( $input['content'] ?? '' ),
			'post_status'  => $effective,
			'post_author'  => get_current_user_id(),
		);
		$postarr = array_merge( $postarr, self::postarr_from_changes( $changes ) );
		// wp_insert_post() erwartet Daten mit Slashes.
		$post_id = wp_insert_post( wp_slash( $postarr ), true );
		if ( is_wp_error( $post_id ) ) {
			return $post_id;
		}
		$applied = self::apply_terms_and_thumbnail( $post_id, $changes );
		if ( is_wp_error( $applied ) ) {
			return $applied;
		}
		return array(
			'confirmed' => true,
			'notes'     => $notes,
			'post'      => self::details( get_post( $post_id ) ),
		);
	}

	public static function post_update( $input ) {
		$post = self::load_post( (int) $input['id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$notes   = array();
		$changes = self::prepare_fields( $input, $post->post_type, $notes, $post );
		if ( is_wp_error( $changes ) ) {
			return $changes;
		}
		foreach ( array( 'title', 'content', 'status' ) as $key ) {
			if ( array_key_exists( $key, $input ) ) {
				$changes[ $key ] = (string) $input[ $key ];
			}
		}
		if ( empty( $changes ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Felder zum Ändern übergeben.' );
		}

		// Status neu bestimmen, wenn Status oder Datum sich ändern.
		$status = $changes['status'] ?? $post->post_status;
		if ( isset( $changes['status'] ) || isset( $changes['date'] ) ) {
			$ts = null;
			if ( isset( $changes['date'] ) ) {
				$ts = $changes['date']['ts'];
			} elseif ( '0000-00-00 00:00:00' !== $post->post_date_gmt ) {
				$ts = strtotime( $post->post_date_gmt . ' UTC' );
			}
			$effective = self::effective_status( $status, $ts );
			if ( is_wp_error( $effective ) ) {
				return $effective;
			}
			if ( $effective !== $status ) {
				$notes[] = sprintf( 'Status %s wird wegen des Datums zu %s.', $status, $effective );
			}
			if ( $effective !== $post->post_status ) {
				$changes['status'] = $effective;
				if ( in_array( $effective, array( 'publish', 'future', 'private' ), true )
					&& ! current_user_can( get_post_type_object( $post->post_type )->cap->publish_posts ) ) {
					return new WP_Error( 'tabside_mcp_forbidden', 'Fehlende Berechtigung zum Veröffentlichen.' );
				}
			} else {
				unset( $changes['status'] );
			}
		}

		$before  = Tabside_MCP_Backups::snapshot( $post );
		$preview = array();
		foreach ( $changes as $key => $value ) {
			$preview[ $key ] = array(
				'old' => self::describe( $key, $before[ $key ] ?? null, $post ),
				'new' => self::describe( $key, $value, $post ),
			);
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result(
				array(
					'post'    => self::summary( $post ),
					'changes' => $preview,
				),
				$notes
			);
		}

		$backup_id = Tabside_MCP_Backups::create( $post, 'update' );
		$postarr   = array_merge( array( 'ID' => $post->ID ), self::postarr_from_changes( $changes ) );
		if ( isset( $changes['title'] ) ) {
			$postarr['post_title'] = $changes['title'];
		}
		if ( isset( $changes['content'] ) ) {
			$postarr['post_content'] = $changes['content'];
		}
		if ( isset( $changes['status'] ) ) {
			$postarr['post_status'] = $changes['status'];
		}
		$result = wp_update_post( wp_slash( $postarr ), true );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		$applied = self::apply_terms_and_thumbnail( $post->ID, $changes );
		if ( is_wp_error( $applied ) ) {
			return $applied;
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup_id,
			'notes'     => $notes,
			'post'      => self::details( get_post( $post->ID ) ),
		);
	}

	public static function post_duplicate( $input ) {
		$source = self::load_post( (int) $input['id'] );
		if ( is_wp_error( $source ) ) {
			return $source;
		}
		$title         = isset( $input['title'] ) && '' !== $input['title'] ? (string) $input['title'] : $source->post_title . ' (Kopie)';
		$status        = $input['status'] ?? 'draft';
		$template_type = (string) get_post_meta( $source->ID, '_elementor_template_type', true );
		$is_theme      = in_array( $template_type, Tabside_MCP_Elementor::CONDITION_TYPES, true );
		$conditions    = isset( $input['conditions'] ) ? array_values( array_map( 'strval', (array) $input['conditions'] ) ) : array();
		$notes         = array();
		if ( 'future' === $status ) {
			return new WP_Error( 'tabside_mcp_invalid_status', 'Für Kopien draft, pending, publish oder private verwenden.' );
		}
		if ( ! empty( $conditions ) && ! $is_theme ) {
			return new WP_Error( 'tabside_mcp_conditions', 'conditions gibt es nur bei Popups und Theme-Vorlagen.' );
		}
		if ( ! empty( $conditions ) && ! Tabside_MCP_Elementor::pro_active() ) {
			return new WP_Error( 'tabside_mcp_conditions', 'Anzeigebedingungen setzen braucht Elementor Pro.' );
		}

		$data = Tabside_MCP_Elementor::raw_data( $source->ID );
		$tree = null;
		if ( '' !== $data ) {
			$tree = json_decode( $data, true );
			if ( ! is_array( $tree ) ) {
				return new WP_Error( 'tabside_mcp_elementor_json', 'Die Elementor-Daten der Vorlage sind kein gültiges JSON, Kopieren abgebrochen.' );
			}
		}
		$meta = self::copyable_meta( $source->ID );
		if ( $is_theme && isset( $meta['_elementor_conditions'] ) ) {
			$notes[] = empty( $conditions ) ? 'Die Kopie erhält keine Anzeigebedingungen, damit sie nicht sofort erscheint.' : 'Die Kopie erhält die übergebenen Anzeigebedingungen.';
		}
		$preview = array(
			'source'          => self::summary( $source ),
			'title'           => $title,
			'slug'            => $input['slug'] ?? null,
			'status'          => $status,
			'meta_keys'       => array_keys( $meta ),
			'elementor'       => null === $tree ? null : array(
				'elements' => Tabside_MCP_Elementor::count_elements( $tree ),
				'bytes'    => strlen( $data ),
			),
			'template_type'   => '' === $template_type ? null : $template_type,
			'conditions'      => $is_theme ? $conditions : null,
			'featured_media'  => self::describe( 'featured_media', (int) get_post_thumbnail_id( $source ) ),
		);
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( $preview, $notes );
		}

		$postarr = array(
			'post_type'      => $source->post_type,
			'post_title'     => $title,
			'post_content'   => $source->post_content,
			'post_excerpt'   => $source->post_excerpt,
			'post_status'    => $status,
			'post_parent'    => $source->post_parent,
			'menu_order'     => $source->menu_order,
			'comment_status' => $source->comment_status,
			'ping_status'    => $source->ping_status,
			'post_password'  => $source->post_password,
			'post_author'    => get_current_user_id(),
		);
		if ( ! empty( $input['slug'] ) ) {
			$postarr['post_name'] = sanitize_title( (string) $input['slug'] );
		}
		$new_id = wp_insert_post( wp_slash( $postarr ), true );
		if ( is_wp_error( $new_id ) ) {
			return $new_id;
		}
		foreach ( $meta as $key => $values ) {
			if ( '_elementor_conditions' === $key && $is_theme ) {
				continue;
			}
			foreach ( $values as $value ) {
				if ( Tabside_MCP_Elementor::DATA_KEY === $key && null !== $tree ) {
					continue;
				}
				// add_post_meta() entfernt Slashes, darum wp_slash() (Elementor-JSON enthält \).
				add_post_meta( $new_id, $key, wp_slash( $value ) );
			}
		}
		$copied = null;
		if ( null !== $tree ) {
			$copied = Tabside_MCP_Elementor::regenerate_ids( $tree );
			update_post_meta( $new_id, Tabside_MCP_Elementor::DATA_KEY, wp_slash( wp_json_encode( $copied ) ) );
		}
		if ( $is_theme && ! empty( $conditions ) ) {
			Tabside_MCP_Elementor::save_conditions( $new_id, $conditions );
		}
		foreach ( get_object_taxonomies( $source->post_type ) as $taxonomy ) {
			$ids = wp_get_object_terms( $source->ID, $taxonomy, array( 'fields' => 'ids' ) );
			if ( ! is_wp_error( $ids ) && $ids ) {
				wp_set_object_terms( $new_id, array_map( 'intval', $ids ), $taxonomy );
			}
		}

		// Prüfen: Elementor-Struktur gleich bis auf die IDs, Zusatzfelder vollständig.
		$verify = array( 'meta_complete' => true );
		foreach ( array_keys( $meta ) as $key ) {
			if ( ! metadata_exists( 'post', $new_id, $key ) && ! ( '_elementor_conditions' === $key && $is_theme ) ) {
				$verify['meta_complete'] = false;
				$verify['missing_meta'][] = $key;
			}
		}
		if ( null !== $tree ) {
			$stored                     = json_decode( Tabside_MCP_Elementor::raw_data( $new_id ), true );
			$verify['elementor_equal']  = is_array( $stored ) && Tabside_MCP_Elementor::strip_ids( $stored ) === Tabside_MCP_Elementor::strip_ids( $tree );
			$verify['elementor_new_ids'] = is_array( $stored ) && ! array_intersect( Tabside_MCP_Elementor::ids( $stored ), Tabside_MCP_Elementor::ids( $tree ) );
		}
		return array(
			'confirmed' => true,
			'source_id' => $source->ID,
			'notes'     => $notes,
			'verify'    => $verify,
			'post'      => self::details( get_post( $new_id ) ),
		);
	}

	public static function media_upload( $input ) {
		$filename = sanitize_file_name( (string) $input['filename'] );
		$check    = wp_check_filetype( $filename );
		if ( '' === $filename || empty( $check['ext'] ) || empty( $check['type'] ) ) {
			return new WP_Error( 'tabside_mcp_filetype', 'Dateityp ist auf dieser Seite nicht erlaubt oder der Dateiname hat keine Endung.' );
		}
		$data = (string) $input['data_base64'];
		if ( 0 === strpos( $data, 'data:' ) && false !== strpos( $data, ',' ) ) {
			$data = substr( $data, strpos( $data, ',' ) + 1 );
		}
		$data   = preg_replace( '/\s+/', '', $data );
		$max    = (int) wp_max_upload_size();
		$binary = base64_decode( $data, true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode
		if ( false === $binary || '' === $binary ) {
			return new WP_Error( 'tabside_mcp_base64', 'data_base64 ist kein gültiges Base64.' );
		}
		$size = strlen( $binary );
		if ( $max > 0 && $size > $max ) {
			return new WP_Error( 'tabside_mcp_too_large', sprintf( 'Datei ist %s gross, erlaubt sind %s.', size_format( $size ), size_format( $max ) ) );
		}
		$preview = array(
			'filename'  => $filename,
			'mime_type' => $check['type'],
			'size'      => size_format( $size ),
			'title'     => $input['title'] ?? preg_replace( '/\.[^.]+$/', '', $filename ),
			'post_id'   => $input['post_id'] ?? null,
		);
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( $preview, array() );
		}

		require_once ABSPATH . 'wp-admin/includes/file.php';
		require_once ABSPATH . 'wp-admin/includes/media.php';
		require_once ABSPATH . 'wp-admin/includes/image.php';

		$tmp = wp_tempnam( $filename );
		if ( ! $tmp || false === file_put_contents( $tmp, $binary ) ) { // phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
			return new WP_Error( 'tabside_mcp_tmp', 'Temporäre Datei konnte nicht geschrieben werden.' );
		}
		$file_array = array(
			'name'     => $filename,
			'tmp_name' => $tmp,
		);
		$post_data  = array();
		if ( isset( $input['caption'] ) ) {
			$post_data['post_excerpt'] = (string) $input['caption'];
		}
		if ( isset( $input['description'] ) ) {
			$post_data['post_content'] = (string) $input['description'];
		}
		$title = isset( $input['title'] ) ? (string) $input['title'] : null;
		$id    = media_handle_sideload( $file_array, (int) ( $input['post_id'] ?? 0 ), $title, $post_data );
		if ( file_exists( $tmp ) ) {
			wp_delete_file( $tmp );
		}
		if ( is_wp_error( $id ) ) {
			return $id;
		}
		if ( isset( $input['alt_text'] ) ) {
			update_post_meta( $id, '_wp_attachment_image_alt', wp_slash( sanitize_text_field( (string) $input['alt_text'] ) ) );
		}
		return array(
			'confirmed' => true,
			'media'     => self::media_summary( get_post( $id ) ),
		);
	}

	public static function post_trash( $input ) {
		$post = self::load_post( (int) $input['id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		if ( 'trash' === $post->post_status ) {
			return new WP_Error( 'tabside_mcp_already_trashed', 'Liegt bereits im Papierkorb.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( array( 'post' => self::summary( $post ) ), array( 'Wird in den Papierkorb verschoben und kann dort wiederhergestellt werden.' ) );
		}
		if ( ! wp_trash_post( $post->ID ) ) {
			return new WP_Error( 'tabside_mcp_trash_failed', 'Verschieben in den Papierkorb fehlgeschlagen.' );
		}
		return array(
			'confirmed' => true,
			'post'      => self::summary( get_post( $post->ID ) ),
		);
	}

	public static function post_rollback( $input ) {
		$post = self::load_post( (int) $input['id'] );
		if ( is_wp_error( $post ) ) {
			return $post;
		}
		$backup = Tabside_MCP_Backups::find( $post->ID, $input['backup_id'] ?? null );
		if ( null === $backup || empty( $backup['fields'] ) ) {
			return new WP_Error( 'tabside_mcp_no_backup', 'Keine passende Sicherung vorhanden.' );
		}
		$target  = $backup['fields'];
		$current = Tabside_MCP_Backups::snapshot( $post );
		$changes = array();
		foreach ( $target as $key => $value ) {
			if ( 'date_gmt' === $key ) {
				continue;
			}
			if ( self::comparable( $current[ $key ] ?? null, $key ) !== self::comparable( $value, $key ) ) {
				$changes[ $key ] = array(
					'old' => self::describe( $key, $current[ $key ] ?? null, $post ),
					'new' => self::describe( $key, $value, $post ),
				);
			}
		}
		$notes = array();
		if ( empty( $changes ) ) {
			$notes[] = 'Der aktuelle Stand entspricht bereits dieser Sicherung.';
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result(
				array(
					'post'        => self::summary( $post ),
					'backup_id'   => $backup['id'],
					'backup_time' => $backup['created_gmt'] . ' UTC',
					'changes'     => $changes,
				),
				$notes
			);
		}
		if ( in_array( $target['status'] ?? '', array( 'publish', 'future', 'private' ), true )
			&& ! current_user_can( get_post_type_object( $post->post_type )->cap->publish_posts ) ) {
			return new WP_Error( 'tabside_mcp_forbidden', 'Fehlende Berechtigung zum Veröffentlichen.' );
		}

		$safety  = Tabside_MCP_Backups::create( $post, 'rollback' );
		$postarr = array(
			'ID'        => $post->ID,
			'edit_date' => true,
		);
		$map     = array(
			'title'          => 'post_title',
			'content'        => 'post_content',
			'excerpt'        => 'post_excerpt',
			'status'         => 'post_status',
			'slug'           => 'post_name',
			'date'           => 'post_date',
			'date_gmt'       => 'post_date_gmt',
			'parent'         => 'post_parent',
			'menu_order'     => 'menu_order',
			'comment_status' => 'comment_status',
		);
		foreach ( $map as $key => $field ) {
			if ( array_key_exists( $key, $target ) ) {
				$postarr[ $field ] = $target[ $key ];
			}
		}
		$result = wp_update_post( wp_slash( $postarr ), true );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		if ( isset( $target['categories'] ) ) {
			wp_set_post_categories( $post->ID, $target['categories'] );
		}
		if ( isset( $target['tags'] ) ) {
			wp_set_post_terms( $post->ID, $target['tags'], 'post_tag' );
		}
		foreach ( (array) ( $target['terms'] ?? array() ) as $taxonomy => $ids ) {
			if ( taxonomy_exists( $taxonomy ) ) {
				wp_set_object_terms( $post->ID, array_map( 'intval', (array) $ids ), $taxonomy );
			}
		}
		if ( ! empty( $target['featured_media'] ) ) {
			set_post_thumbnail( $post->ID, (int) $target['featured_media'] );
		} else {
			delete_post_thumbnail( $post->ID );
		}
		if ( isset( $target['hide_featured_on_single'] ) ) {
			self::set_hide_featured( $post->ID, (bool) $target['hide_featured_on_single'] );
		}
		if ( isset( $target['template'] ) ) {
			update_post_meta( $post->ID, '_wp_page_template', $target['template'] );
		}
		$verify = array();
		if ( isset( $target['elementor'] ) && is_array( $target['elementor'] ) ) {
			Tabside_MCP_Elementor::restore_meta( $post->ID, $target['elementor'] );
			$verify['elementor_exact'] = Tabside_MCP_Elementor::meta_snapshot( $post->ID ) === $target['elementor'];
		}
		return array(
			'confirmed'        => true,
			'restored_backup'  => $backup['id'],
			'safety_backup_id' => $safety,
			'verify'           => $verify,
			'post'             => self::details( get_post( $post->ID ) ),
		);
	}

	public static function term_create( $input ) {
		$taxonomy = (string) $input['taxonomy'];
		$args     = self::term_args( $input, $taxonomy );
		if ( is_wp_error( $args ) ) {
			return $args;
		}
		$preview = array_merge(
			array(
				'taxonomy' => $taxonomy,
				'name'     => (string) $input['name'],
			),
			$args
		);
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( $preview, array() );
		}
		$term = wp_insert_term( wp_slash( (string) $input['name'] ), $taxonomy, wp_slash( $args ) );
		if ( is_wp_error( $term ) ) {
			return $term;
		}
		return array(
			'confirmed' => true,
			'term'      => self::term_summary( get_term( (int) $term['term_id'], $taxonomy ) ),
		);
	}

	public static function term_update( $input ) {
		$taxonomy = (string) $input['taxonomy'];
		$term     = get_term( (int) $input['term_id'], $taxonomy );
		if ( ! $term instanceof WP_Term ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Begriff %d nicht gefunden.', $input['term_id'] ), array( 'status' => 404 ) );
		}
		$args = self::term_args( $input, $taxonomy, $term );
		if ( is_wp_error( $args ) ) {
			return $args;
		}
		if ( isset( $input['name'] ) ) {
			$args['name'] = (string) $input['name'];
		}
		if ( empty( $args ) ) {
			return new WP_Error( 'tabside_mcp_nothing_to_update', 'Keine Felder zum Ändern übergeben.' );
		}
		$changes = array();
		foreach ( $args as $key => $value ) {
			$changes[ $key ] = array(
				'old' => $term->$key,
				'new' => $value,
			);
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result(
				array(
					'term'    => self::term_summary( $term ),
					'changes' => $changes,
				),
				array()
			);
		}
		$backup = Tabside_MCP_Store::backup( 'term', $taxonomy . ':' . $term->term_id, self::term_summary( $term ) );
		$result = wp_update_term( $term->term_id, $taxonomy, wp_slash( $args ) );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'term'      => self::term_summary( get_term( $term->term_id, $taxonomy ) ),
		);
	}

	public static function term_delete( $input ) {
		$taxonomy = (string) $input['taxonomy'];
		$term     = get_term( (int) $input['term_id'], $taxonomy );
		if ( ! $term instanceof WP_Term ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Begriff %d nicht gefunden.', $input['term_id'] ), array( 'status' => 404 ) );
		}
		$assigned = get_objects_in_term( $term->term_id, $taxonomy );
		if ( ( is_array( $assigned ) && $assigned ) || $term->count > 0 ) {
			return new WP_Error( 'tabside_mcp_term_in_use', sprintf( 'Begriff "%s" ist noch %d Einträgen zugeordnet, darum wird er nicht gelöscht.', $term->name, is_array( $assigned ) ? count( $assigned ) : $term->count ) );
		}
		if ( (int) get_option( 'default_' . $taxonomy ) === $term->term_id ) {
			return new WP_Error( 'tabside_mcp_term_default', 'Der Standard-Begriff dieser Taxonomie lässt sich nicht löschen.' );
		}
		if ( empty( $input['confirm'] ) ) {
			return self::preview_result( array( 'term' => self::term_summary( $term ) ), array( 'Wird gelöscht, Name, Slug, Beschreibung und Zusatzfelder werden vorher gesichert.' ) );
		}
		$data           = self::term_summary( $term );
		$data['meta']   = get_term_meta( $term->term_id );
		$backup         = Tabside_MCP_Store::backup( 'term', $taxonomy . ':' . $term->term_id, $data );
		$result         = wp_delete_term( $term->term_id, $taxonomy );
		if ( is_wp_error( $result ) || ! $result ) {
			return is_wp_error( $result ) ? $result : new WP_Error( 'tabside_mcp_term_delete', 'Löschen fehlgeschlagen.' );
		}
		return array(
			'confirmed' => true,
			'backup_id' => $backup,
			'deleted'   => $data,
		);
	}

	/* ------------------------------------------------------------------ Hilfen */

	/**
	 * @param int $id ID.
	 * @return WP_Post|WP_Error
	 */
	public static function load_post( $id ) {
		$post = $id > 0 ? get_post( $id ) : null;
		if ( ! $post || ! array_key_exists( $post->post_type, self::post_types() ) || 'auto-draft' === $post->post_status ) {
			return new WP_Error( 'tabside_mcp_not_found', sprintf( 'Beitrag %d nicht gefunden.', $id ), array( 'status' => 404 ) );
		}
		return $post;
	}

	/**
	 * @return array<string, mixed>|WP_Error
	 */
	private static function term_args( $input, $taxonomy, $term = null ) {
		$args = array();
		foreach ( array( 'slug', 'description' ) as $key ) {
			if ( isset( $input[ $key ] ) ) {
				$args[ $key ] = (string) $input[ $key ];
			}
		}
		if ( isset( $input['parent'] ) ) {
			$parent = (int) $input['parent'];
			if ( $parent > 0 ) {
				if ( ! is_taxonomy_hierarchical( $taxonomy ) ) {
					return new WP_Error( 'tabside_mcp_term_parent', 'Diese Taxonomie hat keine Eltern-Begriffe.' );
				}
				if ( ! get_term( $parent, $taxonomy ) instanceof WP_Term || ( $term && $term->term_id === $parent ) ) {
					return new WP_Error( 'tabside_mcp_term_parent', sprintf( 'Eltern-Begriff %d ungültig.', $parent ) );
				}
			}
			$args['parent'] = $parent;
		}
		return $args;
	}

	/**
	 * Zusatzfelder, die beim Duplizieren kopiert werden (roh, ohne Sperren, Sicherungen und Caches).
	 *
	 * @return array<string, array<int, mixed>>
	 */
	private static function copyable_meta( $post_id ) {
		$out = array();
		foreach ( (array) get_post_meta( $post_id ) as $key => $values ) {
			if ( self::skip_meta( $key ) || in_array( $key, array( '_wp_old_slug', '_wp_old_date', '_wp_trash_meta_status', '_wp_trash_meta_time', '_wp_desired_post_slug' ), true ) ) {
				continue;
			}
			$out[ $key ] = array_map( 'maybe_unserialize', (array) $values );
		}
		return $out;
	}

	/**
	 * Interne Felder: Sperren, Sicherungen, Elementor-Caches.
	 */
	public static function skip_meta( $key ) {
		return in_array( $key, array( '_edit_lock', '_edit_last', Tabside_MCP_Backups::META_KEY ), true )
			|| 0 === strpos( $key, Tabside_MCP_Backups::DATA_PREFIX )
			|| Tabside_MCP_Elementor::is_cache_meta( $key );
	}

	/**
	 * Prüft und normalisiert date, categories, tags, terms, featured_media, excerpt, slug, parent, menu_order,
	 * template, comment_status.
	 *
	 * @param array        $input Eingabe.
	 * @param string       $type  Beitragstyp.
	 * @param string[]     $notes Hinweise (Ausgabe).
	 * @param WP_Post|null $post  Bestehender Beitrag beim Update.
	 * @return array<string, mixed>|WP_Error
	 */
	private static function prepare_fields( $input, $type, &$notes, $post = null ) {
		$changes = array();
		if ( isset( $input['date'] ) && '' !== $input['date'] ) {
			$date = self::parse_date( (string) $input['date'] );
			if ( is_wp_error( $date ) ) {
				return $date;
			}
			if ( ! $date['has_time'] ) {
				$notes[] = 'Keine Uhrzeit angegeben, es gilt 00:00 (' . wp_timezone_string() . ').';
			}
			$changes['date'] = $date;
		}
		if ( ! empty( $input['categories'] ) && ! is_object_in_taxonomy( $type, 'category' ) ) {
			return new WP_Error( 'tabside_mcp_terms_on_type', sprintf( 'Kategorien gibt es beim Beitragstyp %s nicht.', $type ) );
		}
		if ( ! empty( $input['tags'] ) && ! is_object_in_taxonomy( $type, 'post_tag' ) ) {
			return new WP_Error( 'tabside_mcp_terms_on_type', sprintf( 'Schlagwörter gibt es beim Beitragstyp %s nicht.', $type ) );
		}
		if ( isset( $input['categories'] ) ) {
			$ids = self::resolve_categories( (array) $input['categories'] );
			if ( is_wp_error( $ids ) ) {
				return $ids;
			}
			$changes['categories'] = $ids;
		}
		if ( isset( $input['tags'] ) ) {
			$tags = self::resolve_tags( (array) $input['tags'] );
			if ( is_wp_error( $tags ) ) {
				return $tags;
			}
			if ( ! empty( $tags['new'] ) ) {
				$notes[] = 'Neue Schlagwörter werden angelegt: ' . implode( ', ', $tags['new'] );
			}
			$changes['tags'] = $tags;
		}
		if ( isset( $input['terms'] ) && is_array( $input['terms'] ) ) {
			$terms = array();
			foreach ( $input['terms'] as $taxonomy => $items ) {
				$tax = get_taxonomy( (string) $taxonomy );
				if ( ! $tax || ! is_object_in_taxonomy( $type, $tax->name ) ) {
					return new WP_Error( 'tabside_mcp_terms_on_type', sprintf( 'Taxonomie %s gibt es beim Beitragstyp %s nicht.', $taxonomy, $type ) );
				}
				if ( ! current_user_can( $tax->cap->assign_terms ) ) {
					return new WP_Error( 'tabside_mcp_forbidden', sprintf( 'Fehlende Berechtigung: Begriffe der Taxonomie %s zuordnen.', $tax->name ) );
				}
				$ids     = array();
				$missing = array();
				foreach ( (array) $items as $item ) {
					$term = self::find_term( $item, $tax->name );
					if ( $term ) {
						$ids[] = (int) $term->term_id;
					} else {
						$missing[] = (string) $item;
					}
				}
				if ( $missing ) {
					return new WP_Error( 'tabside_mcp_term_missing', sprintf( 'Begriff nicht gefunden in %s: %s. Anlegen mit term-create.', $tax->name, implode( ', ', $missing ) ) );
				}
				$terms[ $tax->name ] = array_values( array_unique( $ids ) );
			}
			$changes['terms'] = $terms;
		}
		if ( isset( $input['featured_media'] ) ) {
			$media_id = (int) $input['featured_media'];
			if ( $media_id > 0 ) {
				$att = get_post( $media_id );
				if ( ! $att || 'attachment' !== $att->post_type || ! wp_attachment_is_image( $att ) ) {
					return new WP_Error( 'tabside_mcp_featured_media', sprintf( 'Medium %d ist kein Bild aus der Mediathek.', $media_id ) );
				}
			}
			$changes['featured_media'] = $media_id;
		}
		foreach ( array( 'excerpt', 'slug' ) as $key ) {
			if ( isset( $input[ $key ] ) ) {
				$changes[ $key ] = (string) $input[ $key ];
			}
		}
		if ( isset( $input['hide_featured_on_single'] ) ) {
			$changes['hide_featured_on_single'] = (bool) $input['hide_featured_on_single'];
		}
		if ( isset( $input['parent'] ) ) {
			$parent = (int) $input['parent'];
			if ( $parent > 0 ) {
				$parent_post = get_post( $parent );
				if ( ! is_post_type_hierarchical( $type ) ) {
					return new WP_Error( 'tabside_mcp_parent', sprintf( 'Beitragstyp %s hat keine Eltern-Elemente.', $type ) );
				}
				if ( ! $parent_post || $parent_post->post_type !== $type || ( $post && $post->ID === $parent ) ) {
					return new WP_Error( 'tabside_mcp_parent', sprintf( 'Eltern-Element %d ungültig (muss vom selben Typ sein).', $parent ) );
				}
			}
			$changes['parent'] = $parent;
		}
		if ( isset( $input['menu_order'] ) ) {
			$changes['menu_order'] = (int) $input['menu_order'];
		}
		if ( isset( $input['template'] ) && '' !== $input['template'] ) {
			$template  = (string) $input['template'];
			$templates = wp_get_theme()->get_page_templates( $post, $type );
			if ( 'default' !== $template && ! isset( $templates[ $template ] ) ) {
				return new WP_Error(
					'tabside_mcp_template',
					sprintf( 'Template "%s" gibt es für %s nicht. Verfügbar: default, %s.', $template, $type, implode( ', ', array_keys( $templates ) ) )
				);
			}
			$changes['template'] = $template;
		}
		if ( isset( $input['comment_status'] ) ) {
			$changes['comment_status'] = (string) $input['comment_status'];
		}
		return $changes;
	}

	private static function postarr_from_changes( $changes ) {
		$postarr = array();
		if ( isset( $changes['date'] ) ) {
			$postarr['post_date']     = $changes['date']['local'];
			$postarr['post_date_gmt'] = $changes['date']['gmt'];
			// Sonst setzt wp_update_post() das Datum von Entwürfen zurück.
			$postarr['edit_date'] = true;
		}
		if ( isset( $changes['excerpt'] ) ) {
			$postarr['post_excerpt'] = $changes['excerpt'];
		}
		if ( isset( $changes['slug'] ) ) {
			$postarr['post_name'] = sanitize_title( $changes['slug'] );
		}
		if ( isset( $changes['parent'] ) ) {
			$postarr['post_parent'] = $changes['parent'];
		}
		if ( isset( $changes['menu_order'] ) ) {
			$postarr['menu_order'] = $changes['menu_order'];
		}
		if ( isset( $changes['comment_status'] ) ) {
			$postarr['comment_status'] = $changes['comment_status'];
		}
		return $postarr;
	}

	/**
	 * @return true|WP_Error
	 */
	private static function apply_terms_and_thumbnail( $post_id, $changes ) {
		if ( isset( $changes['categories'] ) ) {
			wp_set_post_categories( $post_id, $changes['categories'] );
		}
		if ( isset( $changes['tags'] ) ) {
			$ids = $changes['tags']['ids'];
			foreach ( $changes['tags']['new'] as $name ) {
				$term = wp_insert_term( $name, 'post_tag' );
				if ( is_wp_error( $term ) ) {
					$existing = $term->get_error_data( 'term_exists' );
					if ( ! $existing ) {
						return $term;
					}
					$ids[] = (int) $existing;
				} else {
					$ids[] = (int) $term['term_id'];
				}
			}
			wp_set_post_terms( $post_id, $ids, 'post_tag' );
		}
		foreach ( $changes['terms'] ?? array() as $taxonomy => $ids ) {
			wp_set_object_terms( $post_id, $ids, $taxonomy );
		}
		if ( isset( $changes['featured_media'] ) ) {
			if ( $changes['featured_media'] > 0 ) {
				set_post_thumbnail( $post_id, $changes['featured_media'] );
			} else {
				delete_post_thumbnail( $post_id );
			}
		}
		if ( isset( $changes['hide_featured_on_single'] ) ) {
			self::set_hide_featured( $post_id, $changes['hide_featured_on_single'] );
		}
		if ( isset( $changes['template'] ) ) {
			update_post_meta( $post_id, '_wp_page_template', $changes['template'] );
		}
		return true;
	}

	/**
	 * Meta-Schlüssel der Theme-Option "Beitragsbild auf der Beitragsseite ausblenden".
	 * Standard: Theme Blockchain (CSSIgniter), anpassbar mit dem Filter tabside_mcp_hide_featured_meta_key.
	 */
	public static function hide_featured_key() {
		return (string) apply_filters( 'tabside_mcp_hide_featured_meta_key', 'blockchain_hide_single_featured' );
	}

	public static function get_hide_featured( $post_id ) {
		return '1' === (string) get_post_meta( $post_id, self::hide_featured_key(), true );
	}

	private static function set_hide_featured( $post_id, $hide ) {
		if ( $hide ) {
			update_post_meta( $post_id, self::hide_featured_key(), '1' );
		} else {
			delete_post_meta( $post_id, self::hide_featured_key() );
		}
	}

	/**
	 * @return array{local: string, gmt: string, ts: int, has_time: bool}|WP_Error
	 */
	private static function parse_date( $value ) {
		$value = trim( $value );
		$re    = '/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$/i';
		if ( ! preg_match( $re, $value, $m ) || ! checkdate( (int) $m[2], (int) $m[3], (int) $m[1] ) ) {
			return new WP_Error( 'tabside_mcp_date', sprintf( 'Datum "%s" nicht lesbar. Erwartet YYYY-MM-DD oder YYYY-MM-DD HH:MM.', $value ) );
		}
		$has_time = isset( $m[4] ) && '' !== $m[4];
		$hour     = $has_time ? (int) $m[4] : 0;
		$minute   = $has_time ? (int) $m[5] : 0;
		$second   = isset( $m[6] ) && '' !== $m[6] ? (int) $m[6] : 0;
		if ( $hour > 23 || $minute > 59 || $second > 59 ) {
			return new WP_Error( 'tabside_mcp_date', sprintf( 'Uhrzeit in "%s" ungültig.', $value ) );
		}
		if ( isset( $m[7] ) && '' !== $m[7] ) {
			$tz = new DateTimeZone( 'Z' === strtoupper( $m[7] ) ? 'UTC' : $m[7] );
		} else {
			$tz = wp_timezone();
		}
		$dt = DateTimeImmutable::createFromFormat(
			'Y-m-d H:i:s',
			sprintf( '%04d-%02d-%02d %02d:%02d:%02d', $m[1], $m[2], $m[3], $hour, $minute, $second ),
			$tz
		);
		if ( ! $dt ) {
			return new WP_Error( 'tabside_mcp_date', sprintf( 'Datum "%s" nicht lesbar.', $value ) );
		}
		return array(
			'local'    => $dt->setTimezone( wp_timezone() )->format( 'Y-m-d H:i:s' ),
			'gmt'      => $dt->setTimezone( new DateTimeZone( 'UTC' ) )->format( 'Y-m-d H:i:s' ),
			'ts'       => $dt->getTimestamp(),
			'has_time' => $has_time,
		);
	}

	/**
	 * publish mit Datum in der Zukunft wird future, future braucht ein Datum in der Zukunft.
	 *
	 * @return string|WP_Error
	 */
	private static function effective_status( $status, $ts ) {
		if ( ! in_array( $status, array( 'publish', 'future' ), true ) ) {
			return $status;
		}
		if ( null === $ts ) {
			if ( 'future' === $status ) {
				return new WP_Error( 'tabside_mcp_future_needs_date', 'Status future braucht ein Datum in der Zukunft.' );
			}
			return 'publish';
		}
		if ( $ts > time() ) {
			return 'future';
		}
		if ( 'future' === $status ) {
			return new WP_Error( 'tabside_mcp_date_past', 'Status future, aber das Datum liegt nicht in der Zukunft.' );
		}
		return 'publish';
	}

	/**
	 * @return int[]|WP_Error
	 */
	private static function resolve_categories( array $items ) {
		$ids     = array();
		$missing = array();
		foreach ( $items as $item ) {
			$term = self::find_term( $item, 'category' );
			if ( $term ) {
				$ids[] = (int) $term->term_id;
			} else {
				$missing[] = (string) $item;
			}
		}
		if ( $missing ) {
			return new WP_Error( 'tabside_mcp_category_missing', 'Kategorie nicht gefunden: ' . implode( ', ', $missing ) . '. Vorhandene Kategorien liefert category-list.' );
		}
		return array_values( array_unique( $ids ) );
	}

	/**
	 * @return array{ids: int[], new: string[]}|WP_Error
	 */
	private static function resolve_tags( array $items ) {
		$ids = array();
		$new = array();
		foreach ( $items as $item ) {
			$term = self::find_term( $item, 'post_tag' );
			if ( $term ) {
				$ids[] = (int) $term->term_id;
			} elseif ( is_numeric( $item ) ) {
				return new WP_Error( 'tabside_mcp_tag_missing', sprintf( 'Schlagwort-ID %s nicht gefunden.', $item ) );
			} elseif ( '' !== trim( (string) $item ) ) {
				$new[] = trim( (string) $item );
			}
		}
		return array(
			'ids' => array_values( array_unique( $ids ) ),
			'new' => array_values( array_unique( $new ) ),
		);
	}

	/**
	 * @return WP_Term|null
	 */
	private static function find_term( $item, $taxonomy ) {
		if ( is_numeric( $item ) ) {
			$term = get_term( (int) $item, $taxonomy );
			return $term instanceof WP_Term ? $term : null;
		}
		$item = trim( (string) $item );
		if ( '' === $item ) {
			return null;
		}
		$term = get_term_by( 'slug', sanitize_title( $item ), $taxonomy );
		if ( ! $term ) {
			$term = get_term_by( 'name', $item, $taxonomy );
		}
		return $term instanceof WP_Term ? $term : null;
	}

	/**
	 * Vergleichswert: Listen von IDs sortiert, Elementor-Daten als JSON.
	 */
	private static function comparable( $value, $key = null ) {
		if ( 'elementor' === $key ) {
			return wp_json_encode( $value );
		}
		if ( 'terms' === $key && is_array( $value ) ) {
			$out = array();
			foreach ( $value as $taxonomy => $ids ) {
				$ids = array_map( 'intval', (array) $ids );
				sort( $ids );
				if ( $ids ) {
					$out[ $taxonomy ] = $ids;
				}
			}
			ksort( $out );
			return wp_json_encode( $out );
		}
		if ( is_array( $value ) ) {
			$value = array_map( 'intval', $value );
			sort( $value );
			return $value;
		}
		return is_int( $value ) ? $value : (string) $value;
	}

	public static function preview_result( $preview, $notes ) {
		return array(
			'confirmed' => false,
			'preview'   => $preview,
			'notes'     => $notes,
			'hint'      => 'Nichts geändert. Zum Ausführen denselben Aufruf mit confirm=true wiederholen.',
		);
	}

	public static function text_preview( $html, $length = 300 ) {
		$text = trim( preg_replace( '/\s+/', ' ', wp_strip_all_tags( (string) $html ) ) );
		return array(
			'chars'  => strlen( (string) $html ),
			'blocks' => has_blocks( (string) $html ),
			'start'  => mb_substr( $text, 0, $length ) . ( mb_strlen( $text ) > $length ? ' …' : '' ),
		);
	}

	/**
	 * Lesbare Darstellung eines Feldwerts für Vorschauen.
	 */
	public static function describe( $key, $value, $post = null ) {
		switch ( $key ) {
			case 'content':
				return self::text_preview( $value );
			case 'date':
				return is_array( $value ) ? $value['local'] : $value;
			case 'categories':
				return self::term_names( (array) $value, 'category' );
			case 'tags':
				if ( is_array( $value ) && isset( $value['ids'] ) ) {
					return array_merge( self::term_names( $value['ids'], 'post_tag' ), array_map( static function ( $n ) {
						return $n . ' (neu)';
					}, $value['new'] ) );
				}
				return self::term_names( (array) $value, 'post_tag' );
			case 'terms':
				$out = array();
				foreach ( (array) $value as $taxonomy => $ids ) {
					$out[ $taxonomy ] = self::term_names( (array) $ids, $taxonomy );
				}
				return $out;
			case 'featured_media':
				$id = (int) $value;
				return $id > 0 ? array(
					'id'  => $id,
					'url' => wp_get_attachment_url( $id ),
				) : null;
			case 'parent':
				$id = (int) $value;
				return $id > 0 ? array(
					'id'    => $id,
					'title' => get_the_title( $id ),
				) : 0;
			case 'elementor':
				return Tabside_MCP_Elementor::describe_meta( (array) $value );
			default:
				return $value;
		}
	}

	private static function term_names( array $ids, $taxonomy ) {
		$out = array();
		foreach ( $ids as $id ) {
			$term  = get_term( (int) $id, $taxonomy );
			$out[] = $term instanceof WP_Term ? $term->name : '#' . (int) $id;
		}
		return $out;
	}

	public static function term_summary( WP_Term $term ) {
		return array(
			'id'          => (int) $term->term_id,
			'taxonomy'    => $term->taxonomy,
			'name'        => $term->name,
			'slug'        => $term->slug,
			'description' => $term->description,
			'parent'      => (int) $term->parent,
			'count'       => (int) $term->count,
		);
	}

	public static function summary( WP_Post $post ) {
		$out = array(
			'id'       => $post->ID,
			'type'     => $post->post_type,
			'title'    => $post->post_title,
			'status'   => $post->post_status,
			'date'     => $post->post_date,
			'date_gmt' => '0000-00-00 00:00:00' === $post->post_date_gmt ? null : $post->post_date_gmt,
			'modified' => $post->post_modified,
			'slug'     => $post->post_name,
			'link'     => get_permalink( $post ),
			'author'   => get_the_author_meta( 'display_name', (int) $post->post_author ),
			'parent'   => (int) $post->post_parent,
		);
		if ( '' !== (string) get_post_meta( $post->ID, '_elementor_edit_mode', true ) ) {
			$out['elementor'] = true;
		}
		$template_type = (string) get_post_meta( $post->ID, '_elementor_template_type', true );
		if ( 'elementor_library' === $post->post_type && '' !== $template_type ) {
			$out['template_type'] = $template_type;
		}
		return $out;
	}

	public static function details( WP_Post $post ) {
		$out                   = self::summary( $post );
		$out['content']        = $post->post_content;
		$out['format']         = has_blocks( $post->post_content ) ? 'blocks' : 'html';
		$out['excerpt']        = $post->post_excerpt;
		$out['menu_order']     = (int) $post->menu_order;
		$out['comment_status'] = $post->comment_status;
		$template              = (string) get_post_meta( $post->ID, '_wp_page_template', true );
		$out['template']       = '' === $template ? 'default' : $template;
		$out['edit_url']       = admin_url( 'post.php?post=' . $post->ID . '&action=edit' );
		if ( in_array( $post->post_status, array( 'draft', 'pending', 'future' ), true ) ) {
			$out['preview_url'] = get_preview_post_link( $post );
		}
		if ( is_object_in_taxonomy( $post->post_type, 'category' ) ) {
			$out['categories'] = self::terms_of( $post->ID, 'category' );
		}
		if ( is_object_in_taxonomy( $post->post_type, 'post_tag' ) ) {
			$out['tags'] = self::terms_of( $post->ID, 'post_tag' );
		}
		$terms = array();
		foreach ( get_object_taxonomies( $post->post_type ) as $taxonomy ) {
			if ( ! in_array( $taxonomy, array( 'category', 'post_tag' ), true ) ) {
				$terms[ $taxonomy ] = self::terms_of( $post->ID, $taxonomy );
			}
		}
		if ( $terms ) {
			$out['terms'] = $terms;
		}
		$thumb                          = (int) get_post_thumbnail_id( $post );
		$out['featured_media']          = $thumb ? array(
			'id'  => $thumb,
			'url' => wp_get_attachment_url( $thumb ),
		) : null;
		$out['hide_featured_on_single'] = self::get_hide_featured( $post->ID );
		$elementor                      = Tabside_MCP_Elementor::post_info( $post );
		if ( $elementor ) {
			$out['elementor_info'] = $elementor;
			$out['format']         = 'elementor';
		}
		$out['backups'] = Tabside_MCP_Backups::summary( $post->ID );
		$out['meta']    = self::meta_of( $post->ID );
		return $out;
	}

	/**
	 * Zusatzfelder (Post-Meta) eines Beitrags, nur lesend, z.B. Theme-Optionen.
	 * Interne und grosse Felder werden ausgelassen, Werte auf 200 Zeichen gekürzt.
	 */
	private static function meta_of( $post_id ) {
		$out = array();
		foreach ( (array) get_post_meta( $post_id ) as $key => $values ) {
			if ( self::skip_meta( $key ) || in_array( $key, array( '_thumbnail_id', Tabside_MCP_Elementor::DATA_KEY ), true ) || 0 === strpos( $key, '_wp_old_' ) ) {
				continue;
			}
			$value       = isset( $values[0] ) ? maybe_unserialize( $values[0] ) : '';
			$text        = is_scalar( $value ) ? (string) $value : wp_json_encode( $value );
			$out[ $key ] = mb_strlen( (string) $text ) > 200 ? mb_substr( (string) $text, 0, 200 ) . ' …' : $text;
		}
		ksort( $out );
		return $out;
	}

	private static function terms_of( $post_id, $taxonomy ) {
		$terms = wp_get_post_terms( $post_id, $taxonomy );
		if ( is_wp_error( $terms ) ) {
			return array();
		}
		$out = array();
		foreach ( $terms as $term ) {
			$out[] = array(
				'id'   => (int) $term->term_id,
				'name' => $term->name,
				'slug' => $term->slug,
			);
		}
		return $out;
	}

	public static function media_summary( WP_Post $att ) {
		$file = get_attached_file( $att->ID );
		$meta = wp_get_attachment_metadata( $att->ID );
		$size = is_array( $meta ) && ! empty( $meta['filesize'] ) ? (int) $meta['filesize'] : ( $file && file_exists( $file ) ? (int) filesize( $file ) : null );
		return array(
			'id'        => $att->ID,
			'title'     => $att->post_title,
			'filename'  => $file ? wp_basename( $file ) : null,
			'url'       => wp_get_attachment_url( $att->ID ),
			'mime_type' => $att->post_mime_type,
			'size'      => $size,
			'date'      => $att->post_date,
			'alt_text'  => (string) get_post_meta( $att->ID, '_wp_attachment_image_alt', true ),
			'post_id'   => (int) $att->post_parent,
		);
	}
}
