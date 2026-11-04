<?php
/**
 * Updates aus den GitHub-Releases (Tag plugin-v<Version>, Anhang tabside-mcp-<Version>.zip).
 *
 * Nutzt den Mechanismus von WordPress für Plugins mit eigenem "Update URI" (Filter
 * update_plugins_github.com, ab WordPress 5.8). Das Update erscheint wie jedes andere unter
 * Plugins bzw. Dashboard > Aktualisierungen und wird automatisch installiert.
 *
 * Ausschalten: define( 'TABSIDE_MCP_DISABLE_AUTO_UPDATE', true ); in der wp-config.php
 * (dann nur noch Hinweis auf das Update, Installation von Hand).
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Updater {

	const REPO       = 'tabside/wordpress-mcp';
	const TAG_PREFIX = 'plugin-v';
	const CACHE_KEY  = 'tabside_mcp_release';
	const CACHE_TTL  = 6 * HOUR_IN_SECONDS;

	public static function boot() {
		add_filter( 'update_plugins_github.com', array( __CLASS__, 'check' ), 10, 3 );
		add_filter( 'plugins_api', array( __CLASS__, 'info' ), 10, 3 );
		add_filter( 'auto_update_plugin', array( __CLASS__, 'auto_update' ), 10, 2 );
		add_action( 'upgrader_process_complete', array( __CLASS__, 'flush' ), 10, 0 );
	}

	public static function basename() {
		return plugin_basename( TABSIDE_MCP_DIR . 'tabside-mcp.php' );
	}

	/**
	 * Neuestes Plugin-Release von GitHub, 6 Stunden zwischengespeichert.
	 *
	 * @return array{version: string, package: string, url: string, notes: string, published: string}|null
	 */
	public static function latest() {
		$cached = get_site_transient( self::CACHE_KEY );
		if ( is_array( $cached ) ) {
			return $cached['release'];
		}
		$api      = (string) apply_filters( 'tabside_mcp_update_api', 'https://api.github.com/repos/' . self::REPO . '/releases?per_page=20' );
		$response = wp_remote_get(
			$api,
			array(
				'timeout' => 10,
				'headers' => array(
					'Accept'     => 'application/vnd.github+json',
					'User-Agent' => 'tabside-mcp/' . TABSIDE_MCP_VERSION,
				),
			)
		);
		$release = null;
		if ( ! is_wp_error( $response ) && 200 === wp_remote_retrieve_response_code( $response ) ) {
			$items = json_decode( wp_remote_retrieve_body( $response ), true );
			foreach ( is_array( $items ) ? $items : array() as $item ) {
				$tag = (string) ( $item['tag_name'] ?? '' );
				if ( ! empty( $item['draft'] ) || ! empty( $item['prerelease'] ) || 0 !== strpos( $tag, self::TAG_PREFIX ) ) {
					continue;
				}
				$version = substr( $tag, strlen( self::TAG_PREFIX ) );
				foreach ( (array) ( $item['assets'] ?? array() ) as $asset ) {
					$url = (string) ( $asset['browser_download_url'] ?? '' );
					if ( 'tabside-mcp-' . $version . '.zip' === ( $asset['name'] ?? '' ) && self::allowed_package( $url ) ) {
						$release = array(
							'version'   => $version,
							'package'   => $url,
							'url'       => (string) ( $item['html_url'] ?? '' ),
							'notes'     => (string) ( $item['body'] ?? '' ),
							'published' => (string) ( $item['published_at'] ?? '' ),
						);
						break 2;
					}
				}
			}
			$ttl = self::CACHE_TTL;
		} else {
			// GitHub nicht erreichbar oder Rate-Limit: in einer Stunde erneut versuchen.
			$ttl = HOUR_IN_SECONDS;
		}
		set_site_transient( self::CACHE_KEY, array( 'release' => $release ), $ttl );
		return $release;
	}

	/**
	 * Nur https (http nur für lokale Tests).
	 */
	private static function allowed_package( $url ) {
		$parts = wp_parse_url( $url );
		if ( empty( $parts['scheme'] ) || empty( $parts['host'] ) ) {
			return false;
		}
		return 'https' === $parts['scheme'] || ( 'http' === $parts['scheme'] && in_array( $parts['host'], array( '127.0.0.1', 'localhost' ), true ) );
	}

	/**
	 * @param array|false $update      Bisheriger Wert.
	 * @param array       $plugin_data Plugin-Header.
	 * @param string      $plugin_file Plugin-Basename.
	 * @return array|false
	 */
	public static function check( $update, $plugin_data, $plugin_file ) {
		if ( self::basename() !== $plugin_file ) {
			return $update;
		}
		$release = self::latest();
		if ( null === $release ) {
			return $update;
		}
		return array(
			'id'           => 'github.com/' . self::REPO,
			'slug'         => 'tabside-mcp',
			'plugin'       => $plugin_file,
			'version'      => $release['version'],
			'url'          => $release['url'],
			'package'      => $release['package'],
			'requires'     => TABSIDE_MCP_MIN_WP,
			'requires_php' => '7.4',
		);
	}

	/**
	 * Details-Fenster ("Details ansehen") für das Update.
	 */
	public static function info( $result, $action, $args ) {
		if ( 'plugin_information' !== $action || empty( $args->slug ) || 'tabside-mcp' !== $args->slug ) {
			return $result;
		}
		$release = self::latest();
		if ( null === $release ) {
			return $result;
		}
		return (object) array(
			'name'          => 'tabside MCP',
			'slug'          => 'tabside-mcp',
			'version'       => $release['version'],
			'author'        => 'tabside',
			'homepage'      => 'https://github.com/' . self::REPO,
			'requires'      => TABSIDE_MCP_MIN_WP,
			'requires_php'  => '7.4',
			'last_updated'  => $release['published'],
			'download_link' => $release['package'],
			'sections'      => array(
				'changelog' => wpautop( esc_html( '' !== $release['notes'] ? $release['notes'] : 'Version ' . $release['version'] ) ),
			),
		);
	}

	/**
	 * Automatische Updates für dieses Plugin standardmässig an.
	 */
	public static function auto_update( $update, $item ) {
		if ( isset( $item->plugin ) && self::basename() === $item->plugin ) {
			return ! ( defined( 'TABSIDE_MCP_DISABLE_AUTO_UPDATE' ) && TABSIDE_MCP_DISABLE_AUTO_UPDATE );
		}
		return $update;
	}

	public static function flush() {
		delete_site_transient( self::CACHE_KEY );
	}
}
