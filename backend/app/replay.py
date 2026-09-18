"""Deterministic replay and validation of the LP solution.

Used both as an internal guardrail after solving and as the basis for the
validate.py CLI. Re-derives the effective solar / reserve floors / charge /
discharge / grid caps from the normalized directive list, then walks the
returned hourly_plan hour by hour asserting every spec invariant.
"""
from __future__ import annotations

from typing import List, Tuple

from .directives import EffectiveDirectives
from .optimizer import ScheduleResult
from .schemas import HourlyPlanEntry, OptimizeRequest

TOL = 1e-2  # matches the spec's 0.01 kWh / 0.01 BDT numeric tolerance


def verify(
    req: OptimizeRequest,
    eff: EffectiveDirectives,
    sched: ScheduleResult,
) -> List[str]:
    """Return a list of human-readable violations (empty list == pass)."""
    issues: List[str] = []
    hours_sorted = sorted(req.hours, key=lambda h: h.hour)
    bat = req.battery

    prev_e = bat.initial_energy_kwh
    for i, h in enumerate(hours_sorted):
        hh = h.hour
        grid = sched.grid[i]
        solar = sched.solar_used[i]
        ch = sched.charge[i]
        dis = sched.discharge[i]
        e_after = sched.battery_after[i]

        # Energy balance (within tolerance)
        lhs = grid + solar + dis
        rhs = h.demand_kwh + ch
        if abs(lhs - rhs) > TOL:
            issues.append(
                f"hour {hh}: balance violated (lhs={lhs:.4f} rhs={rhs:.4f})"
            )

        # Solar usage bound against effective solar
        if solar > eff.effective_solar[hh] + TOL:
            issues.append(
                f"hour {hh}: solar_used {solar:.4f} > effective_solar {eff.effective_solar[hh]:.4f}"
            )

        # Battery transition
        if abs(e_after - (prev_e + ch - dis)) > TOL:
            issues.append(
                f"hour {hh}: battery transition violated "
                f"(expected {prev_e + ch - dis:.4f}, got {e_after:.4f})"
            )

        # Battery bounds
        if e_after < bat.minimum_energy_kwh - TOL:
            issues.append(f"hour {hh}: battery below base minimum ({e_after:.4f})")
        if e_after > bat.capacity_kwh + TOL:
            issues.append(f"hour {hh}: battery above capacity ({e_after:.4f})")
        floor = eff.reserve_floor[hh]
        if e_after + 1e-9 < floor - TOL:
            issues.append(f"hour {hh}: battery below directive reserve floor {floor:.2f}")

        # Hourly charge/discharge limits
        if ch > bat.max_charge_kwh_per_hour + TOL:
            issues.append(f"hour {hh}: charge exceeds hourly limit")
        if dis > bat.max_discharge_kwh_per_hour + TOL:
            issues.append(f"hour {hh}: discharge exceeds hourly limit")

        # Directive windows
        if not eff.charge_allowed[hh] and ch > TOL:
            issues.append(f"hour {hh}: charge occurred during no_charge_window")
        if not eff.discharge_allowed[hh] and dis > TOL:
            issues.append(f"hour {hh}: discharge occurred during no_discharge_window")
        cap = eff.grid_cap[hh]
        if cap is not None and grid > cap + TOL:
            issues.append(f"hour {hh}: grid_kwh {grid:.4f} exceeds cap {cap:.4f}")

        prev_e = e_after

    # End-of-day neutrality
    if abs(sched.battery_after[23] - bat.initial_energy_kwh) > TOL:
        issues.append(
            f"end-of-day: battery {sched.battery_after[23]:.4f} != initial {bat.initial_energy_kwh:.4f}"
        )

    return issues


def to_plan_entries(sched: ScheduleResult) -> List[HourlyPlanEntry]:
    """Map raw LP variables into the spec's HourlyPlanEntry shape."""
    entries: List[HourlyPlanEntry] = []
    for h in range(24):
        ch = sched.charge[h]
        dis = sched.discharge[h]
        solar = sched.solar_used[h]
        grid = sched.grid[h]

        # Determine single battery_action per spec: charge takes precedence if non-zero,
        # else discharge if non-zero, else idle. Round-tiny numerical noise to 0.
        eps = 1e-6
        if ch > eps and dis > eps:
            # Both nonzero shouldn't happen; the LP doesn't allow simultaneous charge+discharge
            # because the solver picks whichever is cheaper. Pick the larger magnitude.
            action = "charge" if ch >= dis else "discharge"
            magnitude = ch if action == "charge" else dis
        elif ch > eps:
            action = "charge"
            magnitude = ch
        elif dis > eps:
            action = "discharge"
            magnitude = dis
        else:
            action = "idle"
            magnitude = 0.0

        entries.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=round(grid, 4),
                solar_used_kwh=round(solar, 4),
                battery_action=action,
                battery_kwh=round(magnitude, 4),
                battery_energy_after_kwh=round(sched.battery_after[h], 4),
            )
        )
    return entries
