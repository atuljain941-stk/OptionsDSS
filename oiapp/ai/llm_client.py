# oiapp/ai/llm_client.py
"""
Shared LLM client for the AI Copilot agents (post-mortem, pre-trade risk
check, daily briefing).

Design principle: the LLM is used ONLY for reasoning/synthesis/writing on
top of data that has already been fetched deterministically from the app's
own scanners, journal, and DB. It is never given free rein to invent a
price, OI figure, or P/L number — those always come from SQL/scanner
output that we assemble ourselves and hand it as context.

Configuration (same pattern as telegram_alerts.py — setting first, env
var fallback):
    llm_api_key   setting   /  ANTHROPIC_API_KEY   env var
    llm_model     setting   /  ANTHROPIC_MODEL      env var  (default claude-sonnet-5)
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_MODEL = "claude-sonnet-5"
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _get_setting(key: str, default: str = "") -> str:
    from ..scanners.watchlist_manager import _get_setting as _gs
    return _gs(key, default)


def _set_setting(key: str, value: str) -> None:
    from ..scanners.watchlist_manager import _set_setting as _ss
    _ss(key, value)


def _runtime_config() -> Dict[str, str]:
    api_key = (_get_setting("llm_api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")).strip()
    model = (_get_setting("llm_model", "") or os.getenv("ANTHROPIC_MODEL", "") or DEFAULT_MODEL).strip()
    return {"api_key": api_key, "model": model}


def llm_configured() -> bool:
    return bool(_runtime_config()["api_key"])


def get_llm_settings() -> Dict[str, Any]:
    cfg = _runtime_config()
    return {"configured": bool(cfg["api_key"]), "model": cfg["model"]}


def set_llm_settings(api_key: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    if api_key is not None and api_key != "":
        _set_setting("llm_api_key", api_key.strip())
    if model:
        _set_setting("llm_model", model.strip())
    return get_llm_settings()


def call_llm(system: str, user: str, *, max_tokens: int = 1800, temperature: Optional[float] = None,
             timeout: int = 60) -> Dict[str, Any]:
    """Single-turn call to the Anthropic Messages API. Returns
    {"ok": True, "text": "..."} or {"ok": False, "error": "..."}.
    Never raises — callers can always safely check result["ok"].

    Note: current-generation models (e.g. Claude Sonnet 5) return a 400 error
    if temperature/top_p/top_k are set to any non-default value at all, so
    this deliberately does NOT send temperature unless the caller explicitly
    asks for it. Use system-prompt instructions to steer determinism/style
    instead."""
    cfg = _runtime_config()
    if not cfg["api_key"]:
        return {"ok": False, "error": "No LLM API key configured. Add one in AI Copilot → Settings."}

    body_dict = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if temperature is not None:
        body_dict["temperature"] = temperature
    body = json.dumps(body_dict).encode("utf-8")

    req = Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": cfg["api_key"],
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            msg = (err_body.get("error") or {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        if e.code == 400 and "deprecated" in msg.lower() and temperature is not None:
            # Some current-generation models 400 on any sampling param at all.
            # Retry once without temperature rather than surfacing a
            # confusing error for something the caller didn't need anyway.
            retry_body = dict(body_dict)
            retry_body.pop("temperature", None)
            req2 = Request(
                API_URL,
                data=json.dumps(retry_body).encode("utf-8"),
                method="POST",
                headers={
                    "content-type": "application/json",
                    "x-api-key": cfg["api_key"],
                    "anthropic-version": ANTHROPIC_VERSION,
                },
            )
            try:
                with urlopen(req2, timeout=timeout) as resp2:
                    data = json.loads(resp2.read().decode("utf-8"))
                blocks = data.get("content") or []
                text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                if text:
                    return {"ok": True, "text": text, "model": cfg["model"], "usage": data.get("usage")}
            except Exception:
                pass
        return {"ok": False, "error": f"Anthropic API error ({e.code}): {msg}"}
    except URLError as e:
        return {"ok": False, "error": f"Network error calling Anthropic API: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error calling Anthropic API: {e}"}

    try:
        blocks = data.get("content") or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            return {"ok": False, "error": "LLM returned an empty response."}
        return {"ok": True, "text": text, "model": cfg["model"], "usage": data.get("usage")}
    except Exception as e:
        return {"ok": False, "error": f"Could not parse LLM response: {e}"}


def call_llm_json(system: str, user: str, *, max_tokens: int = 1800, temperature: Optional[float] = None,
                   timeout: int = 60) -> Dict[str, Any]:
    """Same as call_llm, but expects (and asks for) a JSON object back.
    Returns {"ok": True, "data": {...}, "text": "..."} or {"ok": False, "error": ...}."""
    strict_system = (
        system
        + "\n\nCRITICAL: Respond with ONLY a single valid JSON object. No markdown "
          "fences, no preamble, no commentary before or after the JSON."
    )
    result = call_llm(strict_system, user, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
    if not result.get("ok"):
        return result
    raw = result["text"].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"LLM did not return valid JSON: {e}", "raw": raw}
    result["data"] = parsed
    return result
