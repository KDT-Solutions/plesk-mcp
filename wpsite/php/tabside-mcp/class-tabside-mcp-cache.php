<?php
/**
 * Caches leeren: Objekt-Cache, Elementor-CSS und erkannte Cache-Plugins, jeweils über deren eigene Funktionen.
 *
 * Läuft automatisch nach jeder ausgeführten Änderung an Inhalten oder Darstellung (Tabside_MCP_Abilities::register).
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Cache {

	/**
	 * Erkannte Cache-Plugins (Name => aktiv).
	 *
	 * @return array<string, bool>
	 */
	public static function detected() {
		return array(
			'wp_super_cache'   => function_exists( 'wp_cache_clear_cache' ),
			'w3_total_cache'   => function_exists( 'w3tc_flush_all' ),
			'wp_rocket'        => function_exists( 'rocket_clean_domain' ),
			'litespeed_cache'  => defined( 'LSCWP_V' ),
			'autoptimize'      => class_exists( 'autoptimizeCache' ),
			'sg_optimizer'     => function_exists( 'sg_cachepress_purge_everything' ),
			'breeze'           => defined( 'BREEZE_VERSION' ),
			'elementor'        => Tabside_MCP_Elementor::active(),
		);
	}

	/**
	 * Leert die Caches für einzelne Beiträge (post_ids) oder für die ganze Seite (leer).
	 *
	 * @param int[] $post_ids Beiträge oder leer für alles.
	 * @return array{scope: string, flushed: string[], posts?: int[], elementor_css?: array}
	 */
	public static function flush( array $post_ids = array() ) {
		$post_ids = array_values( array_unique( array_filter( array_map( 'intval', $post_ids ) ) ) );
		$all      = empty( $post_ids );
		$done     = array();
		$plugins  = self::detected();

		if ( $all ) {
			wp_cache_flush();
			$done[] = 'object_cache';
		} else {
			foreach ( $post_ids as $id ) {
				clean_post_cache( $id );
			}
			$done[] = 'post_cache';
		}

		$css = array();
		if ( $plugins['elementor'] ) {
			if ( $all ) {
				\Elementor\Plugin::$instance->files_manager->clear_cache();
				$done[] = 'elementor_css_all';
			} else {
				foreach ( $post_ids as $id ) {
					$rebuilt = Tabside_MCP_Elementor::rebuild_css( $id );
					if ( $rebuilt ) {
						$css[ $id ] = $rebuilt;
					}
				}
				$done[] = 'elementor_css';
			}
		}
		if ( $plugins['wp_super_cache'] ) {
			if ( $all || ! function_exists( 'wp_cache_post_change' ) ) {
				wp_cache_clear_cache();
			} else {
				array_map( 'wp_cache_post_change', $post_ids );
			}
			$done[] = 'wp_super_cache';
		}
		if ( $plugins['w3_total_cache'] ) {
			if ( $all || ! function_exists( 'w3tc_flush_post' ) ) {
				w3tc_flush_all();
			} else {
				array_map( 'w3tc_flush_post', $post_ids );
			}
			$done[] = 'w3_total_cache';
		}
		if ( $plugins['wp_rocket'] ) {
			if ( $all || ! function_exists( 'rocket_clean_post' ) ) {
				rocket_clean_domain();
			} else {
				array_map( 'rocket_clean_post', $post_ids );
			}
			$done[] = 'wp_rocket';
		}
		if ( $plugins['litespeed_cache'] ) {
			if ( $all ) {
				do_action( 'litespeed_purge_all' );
			} else {
				foreach ( $post_ids as $id ) {
					do_action( 'litespeed_purge_post', $id );
				}
			}
			$done[] = 'litespeed_cache';
		}
		if ( $plugins['autoptimize'] && $all ) {
			autoptimizeCache::clearall();
			$done[] = 'autoptimize';
		}
		if ( $plugins['sg_optimizer'] ) {
			if ( $all || ! function_exists( 'sg_cachepress_purge_cache' ) ) {
				sg_cachepress_purge_everything();
			} else {
				foreach ( $post_ids as $id ) {
					sg_cachepress_purge_cache( get_permalink( $id ) );
				}
			}
			$done[] = 'sg_optimizer';
		}
		if ( $plugins['breeze'] ) {
			do_action( 'breeze_clear_all_cache' );
			$done[] = 'breeze';
		}

		$out = array(
			'scope'   => $all ? 'site' : 'posts',
			'flushed' => $done,
		);
		if ( ! $all ) {
			$out['posts'] = $post_ids;
		}
		if ( $css ) {
			// Neu erzeugte Elementor-CSS je Beitrag: status file (mit url), inline oder empty.
			$out['elementor_css'] = $css;
		}
		return $out;
	}
}
