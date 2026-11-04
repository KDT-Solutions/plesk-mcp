<?php
/**
 * Voraussetzungen, Admin-Hinweise, Berechtigung und MCP-Server "tabside".
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Plugin {

	const SERVER_ID = 'tabside';

	/**
	 * Ist das Plugin aktiv (WordPress-Version ausreichend)?
	 *
	 * @var bool
	 */
	private static $active = false;

	public static function boot() {
		add_action( 'admin_notices', array( __CLASS__, 'admin_notices' ) );

		if ( ! self::wp_version_ok() ) {
			return;
		}
		self::$active = true;

		add_action( 'wp_abilities_api_categories_init', array( 'Tabside_MCP_Abilities', 'register_category' ) );
		add_action( 'wp_abilities_api_init', array( 'Tabside_MCP_Abilities', 'register' ) );
		add_action( 'mcp_adapter_init', array( __CLASS__, 'register_server' ) );
	}

	public static function wp_version_ok() {
		$version = preg_replace( '/-.*$/', '', (string) get_bloginfo( 'version' ) );
		return version_compare( $version, TABSIDE_MCP_MIN_WP, '>=' ) && function_exists( 'wp_register_ability' );
	}

	/**
	 * Optional: nur dieser Benutzer darf die Abilities ausführen (TABSIDE_MCP_USER in der
	 * wp-config.php oder Filter tabside_mcp_allowed_user). Leer = jeder Benutzer mit den
	 * nötigen Rechten, der sich mit einem Anwendungspasswort anmeldet.
	 */
	public static function allowed_login() {
		$login = defined( 'TABSIDE_MCP_USER' ) ? (string) TABSIDE_MCP_USER : '';
		return (string) apply_filters( 'tabside_mcp_allowed_user', $login );
	}

	public static function admin_notices() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		$messages = array();
		if ( ! self::$active ) {
			$messages[] = sprintf(
				/* translators: 1: required version, 2: installed version */
				__( 'tabside MCP benötigt WordPress %1$s oder neuer (installiert: %2$s). Das Plugin ist inaktiv, bis WordPress aktualisiert ist.', 'tabside-mcp' ),
				TABSIDE_MCP_MIN_WP,
				get_bloginfo( 'version' )
			);
		} elseif ( '' !== self::allowed_login() ) {
			if ( ! get_user_by( 'login', self::allowed_login() ) ) {
				$messages[] = sprintf(
					/* translators: %s: user login */
					__( 'tabside MCP: Der in TABSIDE_MCP_USER eingetragene Benutzer "%s" existiert nicht.', 'tabside-mcp' ),
					self::allowed_login()
				);
			}
		}
		foreach ( $messages as $message ) {
			printf( '<div class="notice notice-error"><p>%s</p></div>', esc_html( $message ) );
		}
	}

	/**
	 * Prüft Benutzer, Anmeldeart und Capability. Wird von Transport und Abilities genutzt.
	 *
	 * @param string|null $capability Capability für current_user_can() oder null.
	 * @param mixed       ...$args    Weitere Argumente für current_user_can().
	 * @return true|WP_Error
	 */
	public static function check_user( $capability = null, ...$args ) {
		$user = wp_get_current_user();
		if ( ! $user || ! $user->exists() ) {
			return new WP_Error( 'tabside_mcp_not_logged_in', __( 'Nicht angemeldet.', 'tabside-mcp' ), array( 'status' => 401 ) );
		}
		$allowed = self::allowed_login();
		if ( '' !== $allowed && strtolower( $user->user_login ) !== strtolower( $allowed ) ) {
			return new WP_Error(
				'tabside_mcp_wrong_user',
				sprintf(
					/* translators: %s: user login */
					__( 'Nur der Benutzer "%s" darf diese Funktionen ausführen.', 'tabside-mcp' ),
					$allowed
				),
				array( 'status' => 403 )
			);
		}
		// Über die REST-API nur mit Anwendungspasswort, nicht mit einer Browser-Sitzung.
		$require_app_password = (bool) apply_filters( 'tabside_mcp_require_application_password', true );
		if ( $require_app_password && defined( 'REST_REQUEST' ) && REST_REQUEST && null === rest_get_authenticated_app_password() ) {
			return new WP_Error( 'tabside_mcp_app_password_required', __( 'Anmeldung nur mit Anwendungspasswort.', 'tabside-mcp' ), array( 'status' => 401 ) );
		}
		if ( null !== $capability && ! current_user_can( $capability, ...$args ) ) {
			return new WP_Error(
				'tabside_mcp_forbidden',
				sprintf(
					/* translators: %s: capability */
					__( 'Fehlende Berechtigung: %s', 'tabside-mcp' ),
					$capability
				),
				array( 'status' => 403 )
			);
		}
		return true;
	}

	/**
	 * Optional: Ist das Plugin "MCP Adapter" aktiv, gibt es zusätzlich einen MCP-Server
	 * /wp-json/mcp/tabside. Das Gateway braucht ihn nicht, es nutzt den REST-Endpunkt des Kerns
	 * /wp-json/wp-abilities/v1/abilities/tabside/<name>/run.
	 *
	 * @param \WP\MCP\Core\McpAdapter $adapter Adapter.
	 */
	public static function register_server( $adapter ) {
		$adapter->create_server(
			self::SERVER_ID,
			'mcp',
			self::SERVER_ID,
			'tabside MCP',
			'Diese WordPress-Seite verwalten: Inhalte, Elementor, Menüs, Einstellungen, Plugins, Themes, Benutzer.',
			TABSIDE_MCP_VERSION,
			array( \WP\MCP\Transport\HttpTransport::class ),
			\WP\MCP\Infrastructure\ErrorHandling\ErrorLogMcpErrorHandler::class,
			null,
			Tabside_MCP_Abilities::names(),
			array(),
			array(),
			array( __CLASS__, 'transport_permission' )
		);
	}

	/**
	 * @param WP_REST_Request $request Request.
	 * @return bool
	 */
	public static function transport_permission( $request ) {
		return true === self::check_user( 'edit_posts' );
	}
}
