<?php
/**
 * Sicherungen für Einstellungen (Optionen, Menüs, Customizer-CSS, Widgets, Begriffe) und das Protokoll
 * aller ausgeführten Änderungen. Beides als Option ohne Autoload.
 *
 * @package tabside-mcp
 */

defined( 'ABSPATH' ) || exit;

final class Tabside_MCP_Store {

	const BACKUPS     = '_tabside_mcp_option_backups';
	const AUDIT       = '_tabside_mcp_audit_log';
	const MAX_BACKUPS = 20;
	const MAX_AUDIT   = 500;

	/**
	 * Sichert einen Zustand und gibt die ID der Sicherung zurück. Pro Art (kind) bleiben die letzten 20.
	 *
	 * @param string $kind   option | menu | menu_locations | custom_css | widget | term.
	 * @param string $target Lesbares Ziel, z.B. Optionsname oder Menü-ID.
	 * @param mixed  $data   Gesicherter Zustand.
	 * @return string
	 */
	public static function backup( $kind, $target, $data ) {
		$id    = gmdate( 'Ymd-His' ) . '-' . strtolower( wp_generate_password( 6, false ) );
		$list  = self::all_backups();
		$entry = array(
			'id'          => $id,
			'created_gmt' => gmdate( 'Y-m-d H:i:s' ),
			'kind'        => $kind,
			'target'      => (string) $target,
			'user'        => wp_get_current_user()->user_login,
			'data'        => $data,
		);
		array_unshift( $list, $entry );
		$max   = (int) apply_filters( 'tabside_mcp_max_backups', self::MAX_BACKUPS );
		$count = array();
		$keep  = array();
		foreach ( $list as $item ) {
			$count[ $item['kind'] ] = ( $count[ $item['kind'] ] ?? 0 ) + 1;
			if ( $count[ $item['kind'] ] <= max( 1, $max ) ) {
				$keep[] = $item;
			}
		}
		self::save( self::BACKUPS, $keep );
		return $id;
	}

	/**
	 * @param string[]    $kinds     Erlaubte Arten.
	 * @param string|null $backup_id ID oder leer für die neueste.
	 * @param string|null $target    Optional nur Sicherungen dieses Ziels.
	 * @return array<string, mixed>|null
	 */
	public static function find_backup( array $kinds, $backup_id = null, $target = null ) {
		foreach ( self::all_backups() as $entry ) {
			if ( ! in_array( $entry['kind'], $kinds, true ) ) {
				continue;
			}
			if ( null !== $target && '' !== $target && (string) $target !== $entry['target'] ) {
				continue;
			}
			if ( null === $backup_id || '' === $backup_id || $entry['id'] === $backup_id ) {
				return $entry;
			}
		}
		return null;
	}

	/**
	 * Kurzliste ohne Inhalt.
	 *
	 * @param string[]    $kinds  Arten.
	 * @param string|null $target Optional nur dieses Ziel.
	 * @return array<int, array<string, string>>
	 */
	public static function backup_summary( array $kinds, $target = null ) {
		$out = array();
		foreach ( self::all_backups() as $entry ) {
			if ( in_array( $entry['kind'], $kinds, true ) && ( null === $target || (string) $target === $entry['target'] ) ) {
				$out[] = array(
					'id'          => $entry['id'],
					'created_gmt' => $entry['created_gmt'],
					'kind'        => $entry['kind'],
					'target'      => $entry['target'],
					'user'        => $entry['user'],
				);
			}
		}
		return $out;
	}

	/**
	 * Protokolliert eine ausgeführte Änderung.
	 *
	 * @param string $ability   Name der Ability ohne Kategorie.
	 * @param string $target    Ziel, z.B. "post 12".
	 * @param string $backup_id Sicherungs-ID oder leer.
	 */
	public static function log( $ability, $target, $backup_id ) {
		$list = get_option( self::AUDIT, array() );
		$list = is_array( $list ) ? $list : array();
		array_unshift(
			$list,
			array(
				'time_gmt'  => gmdate( 'Y-m-d H:i:s' ),
				'user'      => wp_get_current_user()->user_login,
				'ability'   => $ability,
				'target'    => (string) $target,
				'backup_id' => (string) $backup_id,
			)
		);
		self::save( self::AUDIT, array_slice( $list, 0, self::MAX_AUDIT ) );
	}

	/**
	 * @param int         $limit Anzahl.
	 * @param string|null $user  Nur Einträge dieses Benutzers.
	 * @return array<int, array<string, string>>
	 */
	public static function audit( $limit, $user = null ) {
		$list = get_option( self::AUDIT, array() );
		$out  = array();
		foreach ( is_array( $list ) ? $list : array() as $entry ) {
			if ( null === $user || $entry['user'] === $user ) {
				$out[] = $entry;
			}
			if ( count( $out ) >= $limit ) {
				break;
			}
		}
		return $out;
	}

	private static function all_backups() {
		$list = get_option( self::BACKUPS, array() );
		return is_array( $list ) ? array_values( $list ) : array();
	}

	private static function save( $name, $value ) {
		// Ohne Autoload, damit die Daten nicht bei jedem Seitenaufruf geladen werden.
		if ( false === get_option( $name, false ) ) {
			add_option( $name, $value, '', false );
		} else {
			update_option( $name, $value, false );
		}
	}
}
