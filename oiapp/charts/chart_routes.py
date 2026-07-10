"""Flask routes for the modular charts workspace."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from .chart_service import build_chart_payload

charts_bp = Blueprint('charts_bp', __name__, url_prefix='/charts')

@charts_bp.route('/')
def page():
    return render_template('charts.html')

@charts_bp.route('/api/data')
def api_data():
    symbol = (request.args.get('symbol') or 'SPY').strip().upper() or 'SPY'
    tf = (request.args.get('timeframe') or '1d').strip()
    expiry = (request.args.get('expiry') or '').strip() or None
    try:
        payload = build_chart_payload(symbol, tf, expiry=expiry)
    except Exception as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(payload)
