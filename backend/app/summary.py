"""Short, deterministic plan_summary generator (does not depend on the LLM)."""
from __future__ import annotations

from typing import List

from .directives import EffectiveDirectives, NormalizedDirective
from .schemas import HourlyPlanEntry, OptimizeRequest


def generate(
    req: OptimizeRequest,
    directives: List[NormalizedDirective],
    plan: List[HourlyPlanEntry],
    eff: EffectiveDirectives,
) -> str:
    parts: List[str] = []
    applied = [d for d in directives if d.directive_type != "no_op"]
    if applied:
        bits = []
        for d in applied:
            if d.directive_type == "solar_reduction":
                bits.append(f"solar reduced to {int((d.factor or 0) * 100)}% during hours {d.hours}")
            elif d.directive_type == "minimum_battery_reserve":
                bits.append(f"battery held above {d.minimum_energy_kwh:.0f} kWh during hours {d.hours}")
            elif d.directive_type == "no_charge_window":
                bits.append(f"charging disabled in hours {d.hours}")
            elif d.directive_type == "no_discharge_window":
                bits.append(f"discharging disabled in hours {d.hours}")
            elif d.directive_type == "max_grid_window":
                bits.append(f"grid capped at {d.max_grid_kwh:.0f} kWh in hours {d.hours}")
        parts.append("Applied directives: " + "; ".join(bits) + ".")
    else:
        parts.append("No operator directives affected the schedule.")

    # Strategy: describe peak vs off-peak charging strategy.
    if plan:
        by_hour = {e.hour: e for e in plan}
        avg_tariff = sum(h.tariff_bdt_per_kwh for h in req.hours) / 24
        peak_hours = [h.hour for h in req.hours if h.tariff_bdt_per_kwh >= avg_tariff * 1.05]
        charge_hours = [e.hour for e in plan if e.battery_action == "charge"]
        discharge_hours = [e.hour for e in plan if e.battery_action == "discharge"]
        if charge_hours and peak_hours and not set(charge_hours) & set(peak_hours):
            parts.append(
                f"Charges the battery off-peak (hours {charge_hours}) and avoids grid import "
                f"during expensive hours {peak_hours}."
            )
        elif discharge_hours:
            parts.append(
                f"Discharges the battery during hours {discharge_hours} to reduce grid import."
            )
        else:
            parts.append("Schedule relies primarily on direct grid supply with minimal battery cycling.")
    return " ".join(parts)
