"""Local CLI validator.

Replays a saved optimize-energy response against the original request and
verifies every spec invariant. Useful for offline checks before deploying.

Usage:
    python validate.py <response.json> <request.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `backend.app` importable when this script is run from the repo root.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "backend"))

from app import directives as directives_mod  # noqa: E402
from app import replay  # noqa: E402
from app.schemas import (  # noqa: E402
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate.py <response.json> <request.json>")
        return 2

    resp_path = Path(sys.argv[1])
    req_path = Path(sys.argv[2])

    resp_raw = json.loads(resp_path.read_text(encoding="utf-8"))
    req_raw = json.loads(req_path.read_text(encoding="utf-8"))

    req = OptimizeRequest.model_validate(req_raw)
    resp = OptimizeResponse.model_validate(resp_raw)

    # Re-derive effective directives from the response's interpretation.
    nd_list = []
    for entry in resp.directive_interpretation:
        nd_list.append(
            directives_mod.NormalizedDirective(
                note_index=entry.note_index,
                directive_type=entry.directive_type,
                hours=(entry.structured_adjustment.hours if entry.structured_adjustment else []) or [],
                factor=(entry.structured_adjustment.factor if entry.structured_adjustment else None),
                minimum_energy_kwh=(entry.structured_adjustment.minimum_energy_kwh if entry.structured_adjustment else None),
                max_grid_kwh=(entry.structured_adjustment.max_grid_kwh if entry.structured_adjustment else None),
                explanation=entry.explanation,
            )
        )
    eff = directives_mod.build_effective(nd_list, req)

    # Build a ScheduleResult from the response's plan.
    by_hour = {e.hour: e for e in resp.hourly_plan}
    plan_by_hour = [by_hour[h] for h in range(24)]
    charge = [
        plan_by_hour[h].battery_kwh if plan_by_hour[h].battery_action == "charge" else 0.0
        for h in range(24)
    ]
    disch = [
        plan_by_hour[h].battery_kwh if plan_by_hour[h].battery_action == "discharge" else 0.0
        for h in range(24)
    ]
    from app.optimizer import ScheduleResult
    sched = ScheduleResult(
        grid=[e.grid_kwh for e in plan_by_hour],
        solar_used=[e.solar_used_kwh for e in plan_by_hour],
        charge=charge,
        discharge=disch,
        battery_after=[e.battery_energy_after_kwh for e in plan_by_hour],
        objective=0.0,
    )

    violations = replay.verify(req, eff, sched)
    if violations:
        print("VALIDATION FAILED:")
        for v in violations:
            print("  -", v)
        return 1

    # Recompute totals to spot-check.
    total_grid = sum(e.grid_kwh for e in plan_by_hour)
    total_cost = sum(e.grid_kwh * req.hours[e.hour].tariff_bdt_per_kwh for e in plan_by_hour)
    peak = max(e.grid_kwh for e in plan_by_hour)

    print("OK — replay passed all checks.")
    print(f"  total_grid_kwh   = {total_grid:.4f} (response: {resp.total_grid_kwh:.4f})")
    print(f"  total_cost_bdt   = {total_cost:.4f} (response: {resp.total_cost_bdt:.4f})")
    print(f"  peak_grid_kwh    = {peak:.4f} (response: {resp.peak_grid_kwh:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())