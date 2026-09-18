"""Directive normalization, validation, and the deterministic fallback.

This module owns the spec's section-04 contract:
    - allow-list of directive types
    - ascending-unique-hours invariant
    - factor in [0,1] for solar_reduction
    - non-negative reserve / grid cap, reserve <= capacity
    - applies=true iff directive != no_op

It also computes the *effective* parameters used by the optimizer:
    effective_solar[h], reserve_floor[h], charge_allowed[h],
    discharge_allowed[h], grid_cap[h]
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schemas import OptimizeRequest


ALLOWED = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


@dataclass
class NormalizedDirective:
    note_index: int
    directive_type: str
    hours: List[int]
    factor: Optional[float] = None
    minimum_energy_kwh: Optional[float] = None
    max_grid_kwh: Optional[float] = None
    explanation: str = ""

    def adjustment_dict(self) -> Optional[Dict[str, Any]]:
        if self.directive_type == "no_op":
            return None
        out: Dict[str, Any] = {"hours": list(self.hours)}
        if self.directive_type == "solar_reduction":
            out["factor"] = self.factor
        elif self.directive_type == "minimum_battery_reserve":
            out["minimum_energy_kwh"] = self.minimum_energy_kwh
        elif self.directive_type == "max_grid_window":
            out["max_grid_kwh"] = self.max_grid_kwh
        return out


@dataclass
class EffectiveDirectives:
    effective_solar: Dict[int, float]
    reserve_floor: Dict[int, float]
    charge_allowed: Dict[int, bool]
    discharge_allowed: Dict[int, bool]
    grid_cap: Dict[int, Optional[float]]
    directive_count: int = 0


# ---------------------------------------------------------------------------
# Normalization (called after the LLM has returned a list of dicts)
# ---------------------------------------------------------------------------

def normalize(raw: List[Dict[str, Any]], req: OptimizeRequest) -> List[NormalizedDirective]:
    """Validate and normalize the LLM/deterministic output.

    Rules enforced:
        - exactly len(operator_notes) entries, in note_index order 0..N-1
        - each entry's directive_type is in the allow-list
        - non-no_op entries must carry structured_adjustment with valid hours
        - solar_reduction.factor in [0,1]
        - minimum_battery_reserve.minimum_energy_kwh in [0, capacity]
        - max_grid_window.max_grid_kwh >= 0
        - hours: ascending unique integers 0..23
    """
    if not isinstance(raw, list):
        raise ValueError("interpretations must be a list")

    cap = req.battery.capacity_kwh
    out: List[NormalizedDirective] = []
    seen_indices: set = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("interpretation entry must be an object")
        idx = int(item.get("note_index", -1))
        if idx in seen_indices:
            raise ValueError(f"duplicate note_index {idx}")
        seen_indices.add(idx)
        if idx < 0 or idx >= len(req.operator_notes):
            raise ValueError(f"note_index {idx} out of range")

        dtype = str(item.get("directive_type", "")).strip()
        if dtype not in ALLOWED:
            raise ValueError(f"unsupported directive_type '{dtype}' at note_index {idx}")
        explanation = str(item.get("explanation", "")).strip()

        if dtype == "no_op":
            out.append(NormalizedDirective(
                note_index=idx,
                directive_type="no_op",
                hours=[],
                explanation=explanation or "no_op",
            ))
            continue

        adj = item.get("structured_adjustment")
        if not isinstance(adj, dict):
            raise ValueError(f"note_index {idx}: structured_adjustment required for {dtype}")
        hours = adj.get("hours")
        if not isinstance(hours, list) or not hours:
            raise ValueError(f"note_index {idx}: hours must be a non-empty list")
        if any((not isinstance(h, int)) or h < 0 or h > 23 for h in hours):
            raise ValueError(f"note_index {idx}: hours must be ints 0..23")
        hours_clean = sorted(set(hours))
        if hours_clean != list(hours):
            raise ValueError(f"note_index {idx}: hours must be ascending unique")

        nd = NormalizedDirective(
            note_index=idx,
            directive_type=dtype,
            hours=hours_clean,
            explanation=explanation,
        )

        if dtype == "solar_reduction":
            factor = adj.get("factor")
            if factor is None or not isinstance(factor, (int, float)) or math.isnan(float(factor)):
                raise ValueError(f"note_index {idx}: solar_reduction.factor required")
            factor = float(factor)
            if not (0.0 <= factor <= 1.0):
                raise ValueError(f"note_index {idx}: factor out of [0,1]")
            nd.factor = factor
        elif dtype == "minimum_battery_reserve":
            mr = adj.get("minimum_energy_kwh")
            if mr is None or not isinstance(mr, (int, float)) or math.isnan(float(mr)):
                raise ValueError(f"note_index {idx}: minimum_energy_kwh required")
            mr = float(mr)
            if mr < 0 or mr > cap:
                raise ValueError(f"note_index {idx}: minimum_energy_kwh out of [0, capacity]")
            nd.minimum_energy_kwh = mr
        elif dtype == "max_grid_window":
            mg = adj.get("max_grid_kwh")
            if mg is None or not isinstance(mg, (int, float)) or math.isnan(float(mg)):
                raise ValueError(f"note_index {idx}: max_grid_kwh required")
            mg = float(mg)
            if mg < 0:
                raise ValueError(f"note_index {idx}: max_grid_kwh must be >= 0")
            nd.max_grid_kwh = mg
        # no_charge_window / no_discharge_window carry no extra fields.

        out.append(nd)

    out.sort(key=lambda d: d.note_index)
    expected = list(range(len(req.operator_notes)))
    if [d.note_index for d in out] != expected:
        raise ValueError(
            f"interpretations must cover note_index in order; got {[d.note_index for d in out]}"
        )
    return out


def build_effective(
    directives: List[NormalizedDirective],
    req: OptimizeRequest,
) -> EffectiveDirectives:
    """Compose per-hour effective parameters from the directive set."""
    eff_solar = {h.hour: h.solar_kwh for h in req.hours}
    reserve = {h.hour: req.battery.minimum_energy_kwh for h in req.hours}
    charge_ok = {h.hour: True for h in req.hours}
    disch_ok = {h.hour: True for h in req.hours}
    grid_cap = {h.hour: None for h in req.hours}

    for d in directives:
        if d.directive_type == "no_op":
            continue
        for h in d.hours:
            if d.directive_type == "solar_reduction":
                eff_solar[h] = eff_solar[h] * (d.factor or 0.0)
            elif d.directive_type == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], d.minimum_energy_kwh or 0.0)
            elif d.directive_type == "no_charge_window":
                charge_ok[h] = False
            elif d.directive_type == "no_discharge_window":
                disch_ok[h] = False
            elif d.directive_type == "max_grid_window":
                grid_cap[h] = d.max_grid_kwh

    return EffectiveDirectives(
        effective_solar=eff_solar,
        reserve_floor=reserve,
        charge_allowed=charge_ok,
        discharge_allowed=disch_ok,
        grid_cap=grid_cap,
        directive_count=sum(1 for d in directives if d.directive_type != "no_op"),
    )


# ---------------------------------------------------------------------------
# Deterministic fallback interpreter (regex-based)
#
# Used when:
#   (a) GEMINI_API_KEY is missing at boot, OR
#   (b) the LLM returns malformed / unparseable JSON.
# Covers common paraphrasings of each supported directive.
# ---------------------------------------------------------------------------

def _hour_token(name: str) -> str:
    return rf"(?P<{name}>\d{{1,2}})(?::00)?(?:\s*(?P<{name}_ampm>am|pm))?(?:\s*(?:h|hr|hrs|hours?))?"

_TIME_RANGE_PATTERNS = [
    # "from 1 PM to 3 PM" / "between 13:00 and 15:00" / "1-3 PM"
    re.compile(
        rf"(?:from|between)?\s*{_hour_token('h1')}\s*(?:-|to|until|through|till|and)\s*{_hour_token('h2')}",
        re.IGNORECASE,
    ),
]


def _to_24h(value: str, ampm: Optional[str]) -> int:
    """Convert a '13' / '1pm' style token to 0..23."""
    n = int(value)
    ampm = (ampm or "").lower()
    if ampm in ("pm",) and n < 12:
        return n + 12
    if ampm in ("am",) and n == 12:
        return 0
    return n


def _extract_hours(text: str) -> List[int]:
    """Extract an inclusive-start, exclusive-end hours list from a phrase.

    Spec rule: "start hour included, end hour excluded" (e.g. 1 PM to 3 PM -> [13,14]).
    """
    text_clean = text.strip()
    for pat in _TIME_RANGE_PATTERNS:
        m = pat.search(text_clean)
        if not m:
            continue
        end_ampm = m.group("h2_ampm")
        # In shorthand such as "1-3 PM", the suffix applies to both ends.
        start_ampm = m.group("h1_ampm") or end_ampm
        a = _to_24h(m.group("h1"), start_ampm)
        b = _to_24h(m.group("h2"), end_ampm)
        # Handle wrap-around like "10 PM to 2 AM" -> [22,23,0,1]
        if b <= a:
            b += 24
        hours = list(range(a, b))
        # Drop any hour >= 24 (out of horizon)
        return [h for h in hours if 0 <= h < 24]
    return []


def deterministic_interpret(
    note: str,
    note_index: int,
    capacity_kwh: float,
) -> NormalizedDirective:
    """Regex-based fallback for one operator note."""
    t = note.lower()

    # No-op triggers (food/menu/etc.) — extend as needed.
    no_op_signals = [
        "cafeteria", "menu", "lunch", "dinner", "breakfast",
        "birthday", "holiday", "meeting", "schedule change",
        "reminder", "notice", "tomorrow", "announcement",
    ]
    energy_signals = (
        "solar", "pv", "panel", "battery", "charge", "discharge",
        "grid", "electricity", "energy", "reserve", "kwh",
    )
    if any(sig in t for sig in no_op_signals) and not any(sig in t for sig in energy_signals):
        return NormalizedDirective(
            note_index=note_index,
            directive_type="no_op",
            hours=[],
            explanation="Deterministic fallback: note does not affect the energy schedule.",
        )

    hours = _extract_hours(note)

    # max_grid_window: "grid import may not exceed", "cap at", "no more than N kWh"
    m = re.search(r"(?:grid[^.]*?(?:may\s+not\s+exceed|cap(?:ped)?\s+at|max(?:imum)?\s+of|no\s+more\s+than|limit(?:ed)?\s+to))\s*(\d+(?:\.\d+)?)\s*(?:kwh|kw|units?)", t)
    if m and hours:
        return NormalizedDirective(
            note_index=note_index,
            directive_type="max_grid_window",
            hours=hours,
            max_grid_kwh=float(m.group(1)),
            explanation=f"Deterministic fallback: grid cap detected ({m.group(1)} kWh).",
        )

    # minimum_battery_reserve: "keep at least N kWh", "reserve of N", "no less than N"
    m = re.search(r"(?:keep|maintain|reserve|hold|preserve)[^.]{0,40}?(?:at\s+least|no\s+less\s+than|of\s+at\s+least|minimum\s+of)\s*(\d+(?:\.\d+)?)\s*(?:kwh|kw)", t)
    if m and hours:
        return NormalizedDirective(
            note_index=note_index,
            directive_type="minimum_battery_reserve",
            hours=hours,
            minimum_energy_kwh=min(float(m.group(1)), capacity_kwh),
            explanation=f"Deterministic fallback: battery reserve {m.group(1)} kWh detected.",
        )

    # no_discharge_window
    if re.search(r"(do\s+not|don'?t|stop|prevent|block|disable|forbid)\s+(?:the\s+)?(?:battery\s+)?discharg", t):
        if hours:
            return NormalizedDirective(
                note_index=note_index,
                directive_type="no_discharge_window",
                hours=hours,
                explanation="Deterministic fallback: no-discharge window detected.",
            )

    # no_charge_window
    if re.search(r"(do\s+not|don'?t|stop|prevent|block|disable|forbid)\s+(?:the\s+)?(?:battery\s+)?charg", t):
        if hours:
            return NormalizedDirective(
                note_index=note_index,
                directive_type="no_charge_window",
                hours=hours,
                explanation="Deterministic fallback: no-charge window detected.",
            )

    # solar_reduction: "X% reduction", "drop to Y%", "leave about a fifth", "factor of 0.X"
    # "80% reduction" -> factor = 0.2 (1 - 0.8)
    m = re.search(r"(\d{1,3})\s*%\s*(?:reduction|drop|less|decrease|cut)", t)
    if m and hours:
        factor = max(0.0, min(1.0, 1.0 - float(m.group(1)) / 100.0))
        return NormalizedDirective(
            note_index=note_index,
            directive_type="solar_reduction",
            hours=hours,
            factor=factor,
            explanation=f"Deterministic fallback: {m.group(1)}% reduction -> factor {factor}.",
        )

    # "drop to X%", "leave about X% of normal"
    m = re.search(r"(?:drop\s+to|leave\s+(?:about|roughly)\s+)?(\d{1,3})\s*%\s*(?:of\s+normal\s+)?(?:solar|output|production|generation|rooftop|pv)?", t)
    if m and hours:
        factor = max(0.0, min(1.0, float(m.group(1)) / 100.0))
        # Skip if this looks like a reduction percentage from the prior pattern.
        return NormalizedDirective(
            note_index=note_index,
            directive_type="solar_reduction",
            hours=hours,
            factor=factor,
            explanation=f"Deterministic fallback: solar -> {factor} of normal.",
        )

    # Default to no_op if we couldn't classify it.
    return NormalizedDirective(
        note_index=note_index,
        directive_type="no_op",
        hours=[],
        explanation="Deterministic fallback: could not classify note.",
    )


def deterministic_interpret_all(
    notes: List[str],
    capacity_kwh: float,
) -> List[Dict[str, Any]]:
    return [deterministic_interpret(n, i, capacity_kwh).__dict__ for i, n in enumerate(notes)]
