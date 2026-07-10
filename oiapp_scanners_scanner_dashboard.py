from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request

from .scanner_builder import _ensure_tables, _watchlists

scanner_dashboard_bp = Blueprint('scanner_dashboard_bp', __name__, url_prefix='/scanner-builder')
DB_PATH = str(Path(__file__).resolve().parents[2] / 'options_data.db')


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    return con


def _ensure_dashboard_tables():
    con = _conn()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS scanner_dashboards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                layout_json TEXT NOT NULL DEFAULT '{"settings":{},"tiles":[]}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                last_opened_at TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _dashboard_watchlists_payload() -> Dict[str, Any]:
    """Return all watchlists for the dashboard dropdown."""
    try:
        from .watchlist_manager import _ensure_tables as _ensure_watchlist_tables
    except Exception:
        _ensure_watchlist_tables = None
    if _ensure_watchlist_tables:
        try:
            _ensure_watchlist_tables()
        except Exception:
            pass
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT w.id, w.name, w.description, w.fetch_options_oi,
                   COALESCE(w.is_default, 0) AS is_default,
                   w.color, w.created_at,
                   COALESCE(w.last_fetch_at, '') AS last_fetch_at,
                   COALESCE(w.last_fetch_mode, '') AS last_fetch_mode,
                   COALESCE(w.last_fetch_count, 0) AS last_fetch_count,
                   COUNT(ws.id) AS symbol_count
            FROM watchlists w
            LEFT JOIN watchlist_symbols ws ON ws.watchlist_id = w.id
            GROUP BY w.id
            ORDER BY COALESCE(w.is_default, 0) DESC, lower(w.name)
            """
        ).fetchall()
        result = [dict(r) for r in rows]
        return {'watchlists': result}
    finally:
        con.close()


def _default_dashboard_layout(name: str = 'My Scanner Dashboard') -> Dict[str, Any]:
    return {
        'settings': {
            'name': name,
            'default_watchlist_id': '',
            'timeframe': '1h',
            'auto_refresh': False,
            'edit_layout': False,
            'freeze_tile_size': False,
            'tile_width': 420,
            'tile_height': 380,
        },
        'tiles': [
            {
                'id': 'tile-rsi-1',
                'title': 'RSI_OveBought_Sold',
                'query_text': 'RSIDiff90() >= 12 OR RSIDiff90() <= -12',
                'watchlist_id': '',
                'prior_days': 0,
                'limit': 200,
                'width': 420,
                'height': 380,
                'x': 0,
                'y': 0,
                'result_template_id': '',
                'result_columns_json': '',
            },
            {
                'id': 'tile-mr-1',
                'title': 'Momentum Retrace',
                'query_text': 'scan(Momentum Retrace)',
                'watchlist_id': '',
                'prior_days': 0,
                'limit': 200,
                'width': 420,
                'height': 380,
                'x': 440,
                'y': 0,
                'result_template_id': '',
                'result_columns_json': '',
            },
            {
                'id': 'tile-rs-1',
                'title': 'RSI MTF',
                'query_text': 'scan(RSI MTF)',
                'watchlist_id': '',
                'prior_days': 0,
                'limit': 200,
                'width': 420,
                'height': 380,
                'x': 880,
                'y': 0,
                'result_template_id': '',
                'result_columns_json': '',
            },
        ],
    }


def _normalize_tile(tile: Dict[str, Any], idx: int = 0, default_watchlist_id: str = '', default_width: int = 420, default_height: int = 380) -> Dict[str, Any]:
    tile = dict(tile or {})
    tile.setdefault('id', f'tile-{idx + 1}-{int(datetime.now().timestamp())}')
    tile.setdefault('title', f'Tile {idx + 1}')
    tile.setdefault('query_text', '')
    tile.setdefault('watchlist_id', default_watchlist_id or '')
    tile.setdefault('prior_days', 0)
    tile.setdefault('limit', 200)
    tile.setdefault('width', default_width or 420)
    tile.setdefault('height', default_height or 380)
    tile.setdefault('x', 0)
    tile.setdefault('y', 0)
    tile.setdefault('result_template_id', '')
    tile.setdefault('result_columns_json', '')
    tile['title'] = str(tile.get('title') or f'Tile {idx + 1}')[:80]
    tile['query_text'] = str(tile.get('query_text') or '').strip()
    try:
        tile['prior_days'] = max(0, int(tile.get('prior_days') or 0))
    except Exception:
        tile['prior_days'] = 0
    try:
        tile['limit'] = max(1, int(tile.get('limit') or 200))
    except Exception:
        tile['limit'] = 200
    try:
        tile['width'] = max(320, int(tile.get('width') or default_width or 420))
    except Exception:
        tile['width'] = 420
    try:
        tile['height'] = max(240, int(tile.get('height') or default_height or 380))
    except Exception:
        tile['height'] = 380
    try:
        tile['x'] = max(0, int(tile.get('x') or 0))
    except Exception:
        tile['x'] = 0
    try:
        tile['y'] = max(0, int(tile.get('y') or 0))
    except Exception:
        tile['y'] = 0
    tile['watchlist_id'] = '' if tile.get('watchlist_id') in (None, 'None') else str(tile.get('watchlist_id') or '')
    tile['result_template_id'] = '' if tile.get('result_template_id') in (None, 'None') else str(tile.get('result_template_id') or '')
    if isinstance(tile.get('result_columns_json'), (list, dict)):
        tile['result_columns_json'] = json.dumps(tile.get('result_columns_json'))
    else:
        tile['result_columns_json'] = '' if tile.get('result_columns_json') in (None, 'None') else str(tile.get('result_columns_json') or '')
    return tile


def _normalize_layout(layout: Dict[str, Any], dashboard_name: str = '') -> Dict[str, Any]:
    layout = dict(layout or {})
    settings = dict(layout.get('settings') or {})
    tiles = layout.get('tiles') or []
    default_watchlist_id = str(settings.get('default_watchlist_id') or '')
    settings.setdefault('name', dashboard_name or 'My Scanner Dashboard')
    settings.setdefault('default_watchlist_id', default_watchlist_id)
    settings.setdefault('timeframe', '1h')
    settings.setdefault('auto_refresh', False)
    settings.setdefault('edit_layout', False)
    settings['freeze_tile_size'] = bool(settings.get('freeze_tile_size', False))
    try:
        settings['tile_width'] = max(320, int(settings.get('tile_width') or 420))
    except Exception:
        settings['tile_width'] = 420
    try:
        settings['tile_height'] = max(240, int(settings.get('tile_height') or 380))
    except Exception:
        settings['tile_height'] = 380
    if not isinstance(tiles, list):
        tiles = []
    tiles = [_normalize_tile(t, i, default_watchlist_id=default_watchlist_id, default_width=settings['tile_width'], default_height=settings['tile_height']) for i, t in enumerate(tiles)]
    return {'settings': settings, 'tiles': tiles}


def _dashboard_row_to_obj(row) -> Dict[str, Any]:
    raw = dict(row)
    try:
        layout = json.loads(raw.get('layout_json') or '{}')
    except Exception:
        layout = {}
    normalized = _normalize_layout(layout, dashboard_name=raw.get('name') or '')
    return {
        'id': raw.get('id'),
        'name': raw.get('name'),
        'created_at': raw.get('created_at'),
        'updated_at': raw.get('updated_at'),
        'last_opened_at': raw.get('last_opened_at'),
        **normalized,
    }


def _seed_default_dashboard_if_needed() -> None:
    _ensure_dashboard_tables()
    con = _conn()
    try:
        row = con.execute('SELECT COUNT(*) FROM scanner_dashboards').fetchone()
        if row and int(row[0] or 0) > 0:
            return
        layout = _default_dashboard_layout()
        con.execute(
            "INSERT INTO scanner_dashboards (name, layout_json, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
            (layout['settings']['name'], json.dumps(layout)),
        )
        con.commit()
    finally:
        con.close()


def _list_dashboards() -> List[Dict[str, Any]]:
    _seed_default_dashboard_if_needed()
    con = _conn()
    try:
        rows = con.execute(
            'SELECT id, name, created_at, updated_at, last_opened_at, layout_json FROM scanner_dashboards ORDER BY updated_at DESC, created_at DESC'
        ).fetchall()
        return [_dashboard_row_to_obj(r) for r in rows]
    finally:
        con.close()


def _get_dashboard(dash_id: int):
    _seed_default_dashboard_if_needed()
    con = _conn()
    try:
        row = con.execute(
            'SELECT id, name, created_at, updated_at, last_opened_at, layout_json FROM scanner_dashboards WHERE id=?',
            (dash_id,),
        ).fetchone()
        return _dashboard_row_to_obj(row) if row else None
    finally:
        con.close()


def _save_dashboard_payload(payload: Dict[str, Any], dashboard_id: int | None = None) -> Dict[str, Any]:
    _seed_default_dashboard_if_needed()
    name = str(payload.get('name') or '').strip() or 'My Scanner Dashboard'
    layout = payload.get('layout')
    if layout is None:
        layout = {
            'settings': payload.get('settings') or {},
            'tiles': payload.get('tiles') or [],
        }
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except Exception:
            layout = {}
    layout = _normalize_layout(layout, dashboard_name=name)
    layout['settings']['name'] = name

    con = _conn()
    try:
        existing = None
        if dashboard_id is not None:
            existing = con.execute('SELECT id FROM scanner_dashboards WHERE id=?', (dashboard_id,)).fetchone()
        if existing is None:
            existing = con.execute('SELECT id FROM scanner_dashboards WHERE lower(name)=lower(?)', (name,)).fetchone()

        if existing:
            did = int(existing[0])
            con.execute(
                'UPDATE scanner_dashboards SET name=?, layout_json=?, updated_at=datetime(\'now\') WHERE id=?',
                (name, json.dumps(layout), did),
            )
        else:
            con.execute(
                'INSERT INTO scanner_dashboards (name, layout_json, created_at, updated_at) VALUES (?, ?, datetime(\'now\'), datetime(\'now\'))',
                (name, json.dumps(layout)),
            )
            did = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.commit()
        row = con.execute('SELECT id, name, created_at, updated_at, last_opened_at, layout_json FROM scanner_dashboards WHERE id=?', (did,)).fetchone()
        return _dashboard_row_to_obj(row)
    finally:
        con.close()


@scanner_dashboard_bp.route('/dashboard')
def dashboard_page():
    _ensure_tables()
    _seed_default_dashboard_if_needed()
    return render_template('scanner_dashboard.html')


@scanner_dashboard_bp.route('/dashboard/api/watchlists', methods=['GET'])
def dashboard_watchlists_api():
    _ensure_dashboard_tables()
    try:
        return jsonify(_dashboard_watchlists_payload())
    except Exception as e:
        return jsonify({'watchlists': [], 'error': str(e)}), 500


@scanner_dashboard_bp.route('/dashboard/api/dashboards', methods=['GET'])
def dashboard_list_api():
    return jsonify({'dashboards': _list_dashboards()})


@scanner_dashboard_bp.route('/dashboard/api/dashboards', methods=['POST'])
def dashboard_create_api():
    payload = request.get_json(force=True) or {}
    dash = _save_dashboard_payload(payload, dashboard_id=payload.get('id'))
    return jsonify({'ok': True, 'dashboard': dash})


@scanner_dashboard_bp.route('/dashboard/api/dashboards/<int:dash_id>', methods=['GET'])
def dashboard_get_api(dash_id: int):
    dash = _get_dashboard(dash_id)
    if not dash:
        return jsonify({'error': 'dashboard not found'}), 404
    con = _conn()
    try:
        con.execute('UPDATE scanner_dashboards SET last_opened_at=datetime(\'now\') WHERE id=?', (dash_id,))
        con.commit()
    finally:
        con.close()
    return jsonify({'dashboard': dash})


@scanner_dashboard_bp.route('/dashboard/api/dashboards/<int:dash_id>', methods=['PUT'])
def dashboard_update_api(dash_id: int):
    payload = request.get_json(force=True) or {}
    payload['id'] = dash_id
    dash = _save_dashboard_payload(payload, dashboard_id=dash_id)
    return jsonify({'ok': True, 'dashboard': dash})


@scanner_dashboard_bp.route('/dashboard/api/dashboards/<int:dash_id>', methods=['DELETE'])
def dashboard_delete_api(dash_id: int):
    con = _conn()
    try:
        con.execute('DELETE FROM scanner_dashboards WHERE id=?', (dash_id,))
        con.commit()
    finally:
        con.close()
    _seed_default_dashboard_if_needed()
    return jsonify({'ok': True})


@scanner_dashboard_bp.route('/dashboard/api/dashboards/<int:dash_id>/touch', methods=['POST'])
def dashboard_touch_api(dash_id: int):
    con = _conn()
    try:
        con.execute('UPDATE scanner_dashboards SET last_opened_at=datetime(\'now\') WHERE id=?', (dash_id,))
        con.commit()
    finally:
        con.close()
    return jsonify({'ok': True})
