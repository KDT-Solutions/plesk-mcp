/*
 * Runner für die WordPress-Tools des plesk-mcp (wird von wpsite/__init__.py
 * zusammengesetzt und per `wp eval-file -` als Subscription-User ausgeführt).
 *
 * Vorher stehen im selben Skript die Klassen des Plugins tabside-mcp (aus
 * wpsite/php/tabside-mcp/, nur geladen, wenn das Plugin auf der Seite nicht
 * ohnehin aktiv ist) und die Variable $__pmcp mit {"ability": "...", "input": {...}}.
 *
 * Es wird nichts auf dem Server installiert. Ausgeführt wird als erster
 * Administrator der Seite (bei Multisite: erster Super-Admin), die Rechte
 * prüft das Plugin wie über die REST-API.
 *
 * Ausgabe: eine Zeile "@@PLESKMCP-WPSITE@@" gefolgt vom JSON-Ergebnis
 * {"ok": true, "result": ...} oder {"ok": false, "code": ..., "message": ..., "data": ...}.
 */

$__pmcp_out = static function ( $data ) {
	echo "\n@@PLESKMCP-WPSITE@@" . wp_json_encode( $data, JSON_UNESCAPED_UNICODE | JSON_PARTIAL_OUTPUT_ON_ERROR ) . "\n";
};

$__pmcp_error = static function ( $code, $message, $data = null ) use ( $__pmcp_out ) {
	$__pmcp_out(
		array(
			'ok'      => false,
			'code'    => (string) $code,
			'message' => (string) $message,
			'data'    => $data,
		)
	);
};

try {
	if ( ! is_user_logged_in() ) {
		$__pmcp_user = null;
		if ( is_multisite() ) {
			foreach ( (array) get_super_admins() as $__pmcp_login ) {
				$__pmcp_user = get_user_by( 'login', $__pmcp_login );
				if ( $__pmcp_user ) {
					break;
				}
			}
		} else {
			$__pmcp_users = get_users(
				array(
					'role'    => 'administrator',
					'orderby' => 'ID',
					'order'   => 'ASC',
					'number'  => 1,
				)
			);
			$__pmcp_user  = $__pmcp_users ? $__pmcp_users[0] : null;
		}
		if ( ! $__pmcp_user ) {
			$__pmcp_error( 'pmcp_no_admin', 'Kein Administrator-Benutzer in dieser WordPress-Installation gefunden.' );
			return;
		}
		wp_set_current_user( $__pmcp_user->ID );
	}

	$__pmcp_slug = (string) ( $__pmcp['ability'] ?? '' );
	$__pmcp_defs = Tabside_MCP_Abilities::all_definitions();
	if ( ! isset( $__pmcp_defs[ $__pmcp_slug ] ) ) {
		$__pmcp_error( 'pmcp_ability_not_found', 'Diese Funktion gibt es auf dieser Seite nicht.', array( 'ability' => $__pmcp_slug ) );
		return;
	}
	$__pmcp_def    = $__pmcp_defs[ $__pmcp_slug ];
	$__pmcp_schema = array(
		'type'                 => 'object',
		'default'              => array(),
		'properties'           => $__pmcp_def['properties'],
		'required'             => $__pmcp_def['required'] ?? array(),
		'additionalProperties' => false,
	);
	$__pmcp_input  = $__pmcp['input'] ?? array();
	if ( ! is_array( $__pmcp_input ) ) {
		$__pmcp_input = array();
	}

	// Wie der REST-Endpunkt der Abilities: erst prüfen, dann in echte Typen umwandeln.
	$__pmcp_valid = rest_validate_value_from_schema( $__pmcp_input, $__pmcp_schema, 'input' );
	if ( is_wp_error( $__pmcp_valid ) ) {
		$__pmcp_error( 'pmcp_invalid_input', 'Ungültige Eingabe: ' . $__pmcp_valid->get_error_message() );
		return;
	}
	$__pmcp_input = rest_sanitize_value_from_schema( $__pmcp_input, $__pmcp_schema, 'input' );
	if ( is_wp_error( $__pmcp_input ) ) {
		$__pmcp_error( 'pmcp_invalid_input', 'Ungültige Eingabe: ' . $__pmcp_input->get_error_message() );
		return;
	}

	$__pmcp_perm = call_user_func( $__pmcp_def['permission'], $__pmcp_input );
	if ( is_wp_error( $__pmcp_perm ) ) {
		$__pmcp_error( $__pmcp_perm->get_error_code(), $__pmcp_perm->get_error_message(), $__pmcp_perm->get_error_data() );
		return;
	}
	if ( true !== $__pmcp_perm ) {
		$__pmcp_error( 'pmcp_forbidden', 'Keine Berechtigung für diese Funktion.' );
		return;
	}

	$__pmcp_result = call_user_func( $__pmcp_def['execute'], $__pmcp_input );
	if ( is_wp_error( $__pmcp_result ) ) {
		$__pmcp_error( $__pmcp_result->get_error_code(), $__pmcp_result->get_error_message(), $__pmcp_result->get_error_data() );
		return;
	}

	// Nach einer ausgeführten Änderung wie im Plugin: Caches leeren und im Protokoll der Seite vermerken.
	if ( empty( $__pmcp_def['readonly'] ) && is_array( $__pmcp_result ) && ! empty( $__pmcp_result['confirmed'] ) ) {
		$__pmcp_after = new ReflectionMethod( 'Tabside_MCP_Abilities', 'after_write' );
		$__pmcp_after->setAccessible( true );
		$__pmcp_args = array( $__pmcp_slug, $__pmcp_def, $__pmcp_input, &$__pmcp_result );
		$__pmcp_after->invokeArgs( null, $__pmcp_args );
	}

	$__pmcp_out(
		array(
			'ok'     => true,
			'result' => $__pmcp_result,
			'source' => $__pmcp_source,
		)
	);
} catch ( Throwable $__pmcp_e ) {
	$__pmcp_error( 'pmcp_exception', get_class( $__pmcp_e ) . ': ' . $__pmcp_e->getMessage() );
}
