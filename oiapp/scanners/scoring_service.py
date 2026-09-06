from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, List
import json
from functools import lru_cache
from datetime import date

from .edge_factors import edge_profile
from ..services.fundamentals import beta_info
from .earnings_calendar import get_earnings_info

CONFIG_PATH = Path(__file__).resolve().parent / 'scoring_config.json'


@lru_cache(maxsize=8192)
def _earnings_info(symbol: str) -> Dict[str, Any]:
    sym = str(symbol or '').strip().upper()
    if not sym:
        return {
            'earn_days': 999, 'earn_date': None, 'next_earn_date': None,
            'last_earn_date': None, 'last_eps_actual': None, 'last_eps_estimate': None,
            'last_surprise_pct': None, 'earn_reaction_pct': None, 'surprise_streak': None,
            'earn_score': 0,
        }
    try:
        row = get_earnings_info(sym) or {}
        return {
            'earn_days': row.get('earn_days', 999) if row.get('earn_days') is not None else 999,
            'earn_date': row.get('earn_date'),
            'next_earn_date': row.get('next_earn_date'),
            'last_earn_date': row.get('last_earn_date'),
            'last_eps_actual': row.get('last_eps_actual'),
            'last_eps_estimate': row.get('last_eps_estimate'),
            'last_surprise_pct': row.get('last_surprise_pct'),
            'earn_reaction_pct': row.get('earn_reaction_pct'),
            'surprise_streak': row.get('surprise_streak'),
            'earn_score': row.get('earn_score', 0) or 0,
        }
    except Exception:
        return {
            'earn_days': 999, 'earn_date': None, 'next_earn_date': None,
            'last_earn_date': None, 'last_eps_actual': None, 'last_eps_estimate': None,
            'last_surprise_pct': None, 'earn_reaction_pct': None, 'surprise_streak': None,
            'earn_score': 0,
        }


def earnings_info(symbol: str) -> Dict[str, Any]:
    return dict(_earnings_info(symbol))


def filter_by_min_earnings(rows: List[Dict[str, Any]], min_earn_days: Optional[int]) -> List[Dict[str, Any]]:
    if min_earn_days is None:
        return rows
    try:
        min_days = int(min_earn_days)
    except Exception:
        return rows
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        ed = r.get('earn_days')
        if ed is None:
            sym = r.get('symbol')
            if sym:
                info = earnings_info(sym)
                r.setdefault('earn_days', info.get('earn_days'))
                r.setdefault('earn_date', info.get('earn_date'))
                ed = r.get('earn_days')
        if ed is None or int(ed) >= min_days:
            out.append(r)
    return out

DEFAULT_CONFIG: Dict[str, Any] = {
    'weights': {
        'rs': 0.20,
        'vol': 0.15,
        'sector': 0.20,
        'institutional': 0.25,
        'expected_move': 0.20,
    },
    'blend': {
        'composite_weight': 0.70,
        'native_weight': 0.30,
    },
    'trend_exhaustion': {
        'early_max': 20,
        'mature_max': 60,
        'exhausted_max': 120,
        'reversal_rsi_drop': 3.0,
        'continuation_relvol': 1.5,
    },
}


def load_scoring_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open('r', encoding='utf-8') as f:
                loaded = json.load(f) or {}
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if x != x or x in (float('inf'), float('-inf')):
            return default
        return x
    except Exception:
        return default


def composite_edge_score(edge: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> float:
    cfg = cfg or load_scoring_config()
    weights = cfg.get('weights', {})
    rs = _safe_float(edge.get('rs_score'))
    vol = _safe_float(edge.get('vol_score'))
    sector = _safe_float(edge.get('sector_score'))
    inst = _safe_float(edge.get('institutional_score'))
    em = _safe_float(edge.get('expected_move_score'))
    score = (
        rs * _safe_float(weights.get('rs'), 0.20)
        + vol * _safe_float(weights.get('vol'), 0.15)
        + sector * _safe_float(weights.get('sector'), 0.20)
        + inst * _safe_float(weights.get('institutional'), 0.25)
        + em * _safe_float(weights.get('expected_move'), 0.20)
    )
    return round(min(100.0, max(0.0, score)), 1)


def final_scanner_score(native_score: Any, composite_score: Any, cfg: Optional[Dict[str, Any]] = None) -> float:
    cfg = cfg or load_scoring_config()
    blend = cfg.get('blend', {})
    native_w = _safe_float(blend.get('native_weight'), 0.30)
    comp_w = _safe_float(blend.get('composite_weight'), 0.70)
    score = _safe_float(native_score) * native_w + _safe_float(composite_score) * comp_w
    return round(min(100.0, max(0.0, score)), 1)


def trend_exhaustion_state_from_profile(result: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or load_scoring_config()
    te = cfg.get('trend_exhaustion', {})
    age = _safe_float(result.get('trend_age'))
    rsi = _safe_float(result.get('rsi'))
    rsi_diff = _safe_float(result.get('rsi_ema_diff'))
    rel_vol = _safe_float(result.get('rel_vol'))
    direction = str(result.get('direction') or '').upper()

    if age <= _safe_float(te.get('early_max'), 20):
        base = 'Early'
    elif age <= _safe_float(te.get('mature_max'), 60):
        base = 'Mature'
    elif age <= _safe_float(te.get('exhausted_max'), 120):
        base = 'Exhausted'
    else:
        base = 'Exhausted'

    if direction in ('BULL_EXHAUSTION', 'BULL', 'CALLS'):
        if rsi >= 70 and rsi_diff <= -_safe_float(te.get('reversal_rsi_drop'), 3.0):
            return 'Reversal Risk'
        if rel_vol >= _safe_float(te.get('continuation_relvol'), 1.5) and rsi >= 60:
            return 'Continuation Risk'
    if direction in ('BEAR_EXHAUSTION', 'BEAR', 'PUTS'):
        if rsi <= 30 and rsi_diff >= _safe_float(te.get('reversal_rsi_drop'), 3.0):
            return 'Reversal Risk'
        if rel_vol >= _safe_float(te.get('continuation_relvol'), 1.5) and rsi <= 40:
            return 'Continuation Risk'
    return base


def attach_scanner_scores(
    result: Dict[str, Any],
    symbol: str,
    frame: Any = None,
    *,
    setup_type: str = '',
    direction: str = '',
    native_score: Any = None,
    trend_age: Any = None,
    benchmark: str = 'SPY',
) -> Dict[str, Any]:
    edge = edge_profile(symbol, frame=frame, benchmark=benchmark)
    composite = composite_edge_score(edge)
    native = _safe_float(native_score if native_score is not None else result.get('score', 0.0))
    final = final_scanner_score(native, composite)

    if trend_age is None:
        trend_age = result.get('trend_age', edge.get('trend_age'))

    earn = earnings_info(symbol)
    beta = beta_info(symbol).get('beta')
    result.update({
        'setup_type': setup_type or result.get('setup_type') or 'Scanner',
        'direction': direction or result.get('direction') or 'NEUTRAL',
        'native_score': round(native, 1),
        'composite_score': composite,
        'final_score': final,
        'score': final,
        'trend_age': trend_age,
        'edge_score': edge.get('edge_score'),
        'edge_label': edge.get('edge_label'),
        'rs_score': edge.get('rs_score'),
        'rs_bias': edge.get('rs_bias'),
        'vol_score': edge.get('vol_score'),
        'vol_regime': edge.get('vol_regime'),
        'sector_score': edge.get('sector_score'),
        'institutional_score': edge.get('institutional_score'),
        'expected_move_score': edge.get('expected_move_score'),
        'expected_move_pct': edge.get('expected_move_pct'),
        'room_up_pct': edge.get('room_up_pct'),
        'room_down_pct': edge.get('room_down_pct'),
        'sector': edge.get('sector'),
        'sector_etf': edge.get('sector_etf'),
        'rsi': edge.get('rsi', result.get('rsi')),
        'atr_pct': edge.get('atr_pct', result.get('atr_pct')),
        'rel_vol': edge.get('rel_vol', result.get('rel_vol')),
        'oi_change_pct': edge.get('oi_change_pct', result.get('oi_change_pct')),
        'call_share': edge.get('call_share', result.get('call_share')),
        'put_share': edge.get('put_share', result.get('put_share')),
        'earn_days': result.get('earn_days', earn.get('earn_days')),
        'earn_date': result.get('earn_date', earn.get('earn_date')),
        'next_earn_date': earn.get('next_earn_date'),
        'last_earn_date': earn.get('last_earn_date'),
        'last_eps_actual': earn.get('last_eps_actual'),
        'last_eps_estimate': earn.get('last_eps_estimate'),
        'last_surprise_pct': earn.get('last_surprise_pct'),
        'earn_reaction_pct': earn.get('earn_reaction_pct'),
        'surprise_streak': earn.get('surprise_streak'),
        'edge_notes': edge.get('edge_notes', []),
        'edge': edge,
        'beta': beta,
    })
    if 'setup_state' not in result and setup_type.upper().startswith('TREND EXHAUSTION'):
        result['setup_state'] = trend_exhaustion_state_from_profile(result)
    return result
