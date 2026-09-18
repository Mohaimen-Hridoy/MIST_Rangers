"""Operator-note interpretation via Google Gemini (gemini-2.0-flash).

If GEMINI_API_KEY is missing or the LLM returns malformed/unsupported
structured output, we fall back to the deterministic regex interpreter so
the API stays alive and valid. The spec requires that the LLM be part of
the interpretation path; the fallback is for safe failure only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from .directives import deterministic_interpret_all

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

log = logging.getLogger("gridwise.interpreter")

# Lazy client (so the module imports even without the SDK present).
_client = None
_has_sdk = False
try:
    from google import genai  # type: ignore
    _has_sdk = True
except Exception:  # pragma: no cover - dev without the SDK installed
    _has_sdk = False


_SYSTEM_PROMPT = """\
You are GridWise's directive interpreter. Convert each campus-operator note into ONE
structured directive per the JSON schema below. Never invent directive types, hours, or values
outside what the note literally states. If the note does not affect the 24-hour energy
schedule (e.g. cafeteria menu, holiday greeting, generic reminder), use directive_type "no_op".

Allowed directive_type values (exactly one per note):
1. "solar_reduction"        - structured_adjustment: {"hours":[int...], "factor":number in [0,1]}
                              factor is the usable fraction that REMAINS (80% drop -> 0.2).
2. "minimum_battery_reserve"- {"hours":[int...], "minimum_energy_kwh":number >= 0}
3. "no_charge_window"       - {"hours":[int...]}      (charging disabled)
4. "no_discharge_window"    - {"hours":[int...]}      (discharging disabled)
5. "max_grid_window"        - {"hours":[int...], "max_grid_kwh":number >= 0}
6. "no_op"                  - structured_adjustment: null

Hour semantics:
- Whole-hour integers 0..23, ascending, unique.
- Start hour INCLUDED, end hour EXCLUDED. "1 PM to 3 PM" -> hours [13,14].
- "6 PM until 9 PM" -> hours [18,19,20].

Output rules (strict):
- Return ONLY a single JSON object, no prose, no code fences.
- Shape: {"interpretations": [{"note_index": int, "applies": bool,
                                "directive_type": "...", "structured_adjustment": {...}|null,
                                "explanation": "short string"}, ...]}
- Order entries by note_index ascending. One entry per note. No duplicates, no gaps.
- For no_op: applies=false, structured_adjustment=null.
- For every other directive: applies=true and structured_adjustment must match the required shape.
- Do not invent demand/solar/tariff/battery parameters. Do not output any unsupported type.
"""


_USER_TEMPLATE = """\
Interpret the following {n} operator note(s) for scenario '{scenario_id}'.

Notes (in order, 0-indexed):
{notes_block}

Respond with the strict JSON object described in the system prompt.
"""


def get_client():
    """Return a singleton Gemini client (or None if unavailable)."""
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not _has_sdk:
        if not _has_sdk:
            log.warning("google-genai SDK not installed; deterministic fallback only")
        else:
            log.warning("GEMINI_API_KEY not set; deterministic fallback only")
        return None
    try:
        _client = genai.Client(api_key=api_key)  # type: ignore[attr-defined]
        return _client
    except Exception as exc:
        log.warning("Failed to init Gemini client: %s", exc)
        return None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def _coerce_to_list(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Accept either {"interpretations": [...]} or a bare list, or wrap a single dict."""
    if payload is None:
        return None
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        if "interpretations" in payload and isinstance(payload["interpretations"], list):
            return [p for p in payload["interpretations"] if isinstance(p, dict)]
        # Allow {"directives": [...]} or {"results": [...]}
        for k in ("directives", "results", "notes"):
            if k in payload and isinstance(payload[k], list):
                return [p for p in payload[k] if isinstance(p, dict)]
    return None


def _build_prompt(notes: List[str], scenario_id: str) -> str:
    bullets = "\n".join(f"[{i}] {n}" for i, n in enumerate(notes))
    return _USER_TEMPLATE.format(n=len(notes), scenario_id=scenario_id, notes_block=bullets)


def _call_gemini(notes: List[str], scenario_id: str, client) -> Optional[str]:
    """Single Gemini call, returning the raw text (may be JSON or fenced)."""
    try:
        prompt = _build_prompt(notes, scenario_id)
        # Combine system + user into a single user turn; the SDK exposes a
        # simple generate_content interface that works without a chat session.
        combined = f"{_SYSTEM_PROMPT}\n\n{prompt}"
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            contents=combined,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )
        text = getattr(response, "text", None)
        if not text:
            # Some SDK paths return candidates; extract defensively.
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                parts = getattr(candidates[0].content, "parts", None) or []
                text = "".join(getattr(p, "text", "") for p in parts)
        return text
    except Exception as exc:
        log.warning("Gemini call failed: %s", exc)
        return None


def interpret_notes(
    notes: List[str],
    client=None,
    scenario_id: str = "scenario",
    capacity_kwh: float = 1e9,
) -> List[Dict[str, Any]]:
    """Return one interpretation dict per note (LLM or deterministic fallback).

    Always returns len(notes) entries with monotonic note_index 0..N-1.
    """
    capacity_hint = None  # Used by the deterministic fallback for reserve caps.
    client = client if client is not None else get_client()

    parsed: Optional[List[Dict[str, Any]]] = None
    if client is not None:
        raw = _call_gemini(notes, scenario_id=scenario_id, client=client)
        if raw:
            cleaned = _strip_code_fences(raw)
            try:
                payload = json.loads(cleaned)
                parsed = _coerce_to_list(payload)
            except Exception as exc:
                log.warning("LLM JSON parse failed: %s -- trying fuzzy extract", exc)
                # Fuzzy extract: find first {...} block containing "interpretations"
                m = re.search(r"\{[\s\S]*?\"interpretations\"[\s\S]*?\}", cleaned)
                if m:
                    try:
                        parsed = _coerce_to_list(json.loads(m.group(0)))
                    except Exception:
                        parsed = None

    if parsed is None or len(parsed) != len(notes):
        # Safe failure -> deterministic fallback for everything.
        log.info("Using deterministic fallback (parsed=%s, expected=%d)",
                 None if parsed is None else len(parsed), len(notes))
        return deterministic_interpret_all(notes, capacity_kwh)

    # Make sure note_index labels match the original ordering; if the LLM
    # misnumbered, fix it up so the wire format stays valid.
    fixed: List[Dict[str, Any]] = []
    for i, item in enumerate(parsed):
        item = dict(item)
        item.setdefault("note_index", i)
        item["note_index"] = i
        # Backfill applies from directive_type if the LLM omitted it.
        if "directive_type" in item and "applies" not in item:
            item["applies"] = item["directive_type"] != "no_op"
        fixed.append(item)

    # Patch explanation to note the LLM was used.
    for item in fixed:
        item.setdefault("explanation", "")
        if not item["explanation"]:
            item["explanation"] = "LLM interpretation."
    return fixed
