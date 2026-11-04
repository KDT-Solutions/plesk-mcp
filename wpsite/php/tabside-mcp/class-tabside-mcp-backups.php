<?php
/**
 * Sicherungen des alten Inhalts vor jedem Update (Post-Meta), Grundlage für post-rollback.
 *
 * Unabhängig von den WordPress-Revisionen, die auf manchen Hostings abgeschaltet sind. Die Liste
 * (_tabside_mcp_backups) enthält nur die Kurzangaben, der gesicherte Stand liegt pro Sicherung in einem
 * eigenen Meta-Feld (_tabside_mcp_backup_<id>), weil Elementor-Daten mehrere MB gross sein können.
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Backups {

	const META_KEY    = '_tabside_mcp_backups';
	const DATA_PREFIX = '_tabside_mcp_backup_';
	const MAX         = 20;

	/**
	 * Aktueller Zustand eines Beitrags (alle Felder, die post-update und die Elementor-Abilities ändern können).
	 *
	 * @param WP_Post $post Beitrag.
	 * @return array<string, mixed>
	 */
	public static function snapshot( WP_Post $post ) {
		$template = (string) get_post_meta( $post->ID, '_wp_page_template', true );
		$fields   = array(
			'title'                   => $post->post_title,
			'content'                 => $post->post_content,
			'excerpt'                 => $post->post_excerpt,
			'status'                  => $post->post_status,
			'date'                    => $post->post_date,
			'date_gmt'                => $post->post_date_gmt,
			'slug'                    => $post->post_name,
			'featured_media'          => (int) get_post_thumbnail_id( $post ),
			'hide_featured_on_single' => Tabside_MCP_Abilities::get_hide_featured( $post->ID ),
			'parent'                  => (int) $post->post_parent,
			'menu_order'              => (int) $post->menu_order,
			'template'                => '' === $template ? 'default' : $template,
			'comment_status'          => $post->comment_status,
		);
		if ( is_object_in_taxonomy( $post->post_type, 'category' ) ) {
			$fields['categories'] = array_map( 'intval', wp_get_post_categories( $post->ID ) );
		}
		if ( is_object_in_taxonomy( $post->post_type, 'post_tag' ) ) {
			$fields['tags'] = array_map( 'intval', wp_get_post_tags( $post->ID, array( 'fields' => 'ids' ) ) );
		}
		$terms = array();
		foreach ( get_object_taxonomies( $post->post_type ) as $taxonomy ) {
			if ( in_array( $taxonomy, array( 'category', 'post_tag' ), true ) ) {
				continue;
			}
			$ids = wp_get_object_terms( $post->ID, $taxonomy, array( 'fields' => 'ids' ) );
			if ( ! is_wp_error( $ids ) ) {
				$terms[ $taxonomy ] = array_map( 'intval', $ids );
			}
		}
		if ( $terms ) {
			$fields['terms'] = $terms;
		}
		$fields['elementor'] = Tabside_MCP_Elementor::meta_snapshot( $post->ID );
		return $fields;
	}

	/**
	 * @param int $post_id Beitrag.
	 * @return array<int, array<string, mixed>> Neueste zuerst.
	 */
	public static function all( $post_id ) {
		$list = get_post_meta( $post_id, self::META_KEY, true );
		return is_array( $list ) ? array_values( $list ) : array();
	}

	/**
	 * Sichert den aktuellen Zustand und gibt die ID der Sicherung zurück.
	 *
	 * @param WP_Post $post   Beitrag.
	 * @param string  $reason update | rollback | elementor.
	 * @return string
	 */
	public static function create( WP_Post $post, $reason ) {
		$id     = gmdate( 'Ymd-His' ) . '-' . strtolower( wp_generate_password( 6, false ) );
		$fields = self::snapshot( $post );
		$entry  = array(
			'id'          => $id,
			'created_gmt' => gmdate( 'Y-m-d H:i:s' ),
			'reason'      => $reason,
			'user'        => wp_get_current_user()->user_login,
			'title'       => $fields['title'],
			'status'      => $fields['status'],
		);
		// update_post_meta() entfernt Slashes, darum vorher wp_slash() (sonst gehen \ in JSON verloren).
		update_post_meta( $post->ID, self::DATA_PREFIX . $id, wp_slash( $fields ) );

		$list = self::all( $post->ID );
		array_unshift( $list, $entry );
		$max  = max( 1, (int) apply_filters( 'tabside_mcp_max_backups', self::MAX ) );
		foreach ( array_slice( $list, $max ) as $old ) {
			delete_post_meta( $post->ID, self::DATA_PREFIX . $old['id'] );
		}
		update_post_meta( $post->ID, self::META_KEY, wp_slash( array_slice( $list, 0, $max ) ) );
		return $id;
	}

	/**
	 * @param int         $post_id   Beitrag.
	 * @param string|null $backup_id ID oder null für die neueste Sicherung.
	 * @return array<string, mixed>|null Eintrag mit fields.
	 */
	public static function find( $post_id, $backup_id = null ) {
		foreach ( self::all( $post_id ) as $entry ) {
			if ( null === $backup_id || '' === $backup_id || $entry['id'] === $backup_id ) {
				if ( ! isset( $entry['fields'] ) ) {
					// Sicherungen ab 2.0.0: Daten im eigenen Meta-Feld. Ältere tragen fields direkt in der Liste.
					$fields          = get_post_meta( $post_id, self::DATA_PREFIX . $entry['id'], true );
					$entry['fields'] = is_array( $fields ) ? $fields : array();
				}
				return $entry;
			}
		}
		return null;
	}

	/**
	 * Kurzliste ohne Inhalt für post-get.
	 *
	 * @param int $post_id Beitrag.
	 * @return array<int, array<string, mixed>>
	 */
	public static function summary( $post_id ) {
		$out = array();
		foreach ( self::all( $post_id ) as $entry ) {
			$out[] = array(
				'id'          => $entry['id'],
				'created_gmt' => $entry['created_gmt'],
				'reason'      => $entry['reason'],
				'user'        => $entry['user'],
				'title'       => $entry['title'] ?? ( $entry['fields']['title'] ?? '' ),
				'status'      => $entry['status'] ?? ( $entry['fields']['status'] ?? '' ),
			);
		}
		return $out;
	}
}
