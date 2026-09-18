"""OR-Tools LP for the 24-hour GridWise energy schedule.

Decision variables per hour h=0..23:
    grid[h]         grid energy purchased this hour (>= 0)
    solar_used[h]   solar energy used this hour     [0, effective_solar[h]]
    charge[h]       battery charge this hour        [0, max_charge]
    discharge[h]    battery discharge this hour     [0, max_discharge]
    E[h]            battery energy after hour h     [base_min, capacity]

Hard constraints:
    E[h] = E[h-1] + charge[h] - discharge[h]   (E[-1] = initial_energy_kwh)
    grid[h] + solar_used[h] + discharge[h] = demand[h] + charge[h]   (energy balance)
    E[23] = initial_energy_kwh                                          (end-of-day neutrality)
    charge[h] == 0 for no_charge_window hours
    discharge[h] == 0 for no_discharge_window hours
    grid[h] <= max_grid_kwh for max_grid_window hours
    E[h] >= max(base_min, directive_reserve) for minimum_battery_reserve hours

Objective: minimize Σ grid[h] * tariff[h]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ortools.linear_solver import pywraplp

from .directives import EffectiveDirectives
from .schemas import OptimizeRequest


@dataclass
class ScheduleResult:
    grid: List[float]
    solar_used: List[float]
    charge: List[float]
    discharge: List[float]
    battery_after: List[float]
    objective: float


def solve(req: OptimizeRequest, eff: EffectiveDirectives) -> Optional[ScheduleResult]:
    solver = pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        # Fall back to GLOP if CBC isn't available (e.g. slim images).
        solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        raise RuntimeError("No LP solver available in OR-Tools build")

    H = 24
    bat = req.battery
    hours_sorted = sorted(req.hours, key=lambda h: h.hour)

    # Variables
    grid = [solver.NumVar(0.0, solver.infinity(), f"grid_{h.hour}") for h in hours_sorted]
    solar_used = [solver.NumVar(0.0, eff.effective_solar[h.hour], f"solar_{h.hour}") for h in hours_sorted]
    charge = [solver.NumVar(0.0, bat.max_charge_kwh_per_hour, f"charge_{h.hour}") for h in hours_sorted]
    discharge = [solver.NumVar(0.0, bat.max_discharge_kwh_per_hour, f"disch_{h.hour}") for h in hours_sorted]
    charge_mode = [solver.BoolVar(f"charge_mode_{h.hour}") for h in hours_sorted]
    energy = [solver.NumVar(bat.minimum_energy_kwh, bat.capacity_kwh, f"E_{h.hour}") for h in hours_sorted]

    # Objective
    solver.Minimize(
        sum(grid[i] * hours_sorted[i].tariff_bdt_per_kwh for i in range(H))
    )

    # Battery recursion + end-of-day neutrality
    prev_E = bat.initial_energy_kwh
    for i, h in enumerate(hours_sorted):
        solver.Add(energy[i] == prev_E + charge[i] - discharge[i])
        prev_E = energy[i]
    # End-of-day: E[23] == initial
    solver.Add(energy[23] == bat.initial_energy_kwh)

    # Energy balance per hour
    for i, h in enumerate(hours_sorted):
        solver.Add(
            grid[i] + solar_used[i] + discharge[i] == h.demand_kwh + charge[i]
        )
        solver.Add(charge[i] <= bat.max_charge_kwh_per_hour * charge_mode[i])
        solver.Add(discharge[i] <= bat.max_discharge_kwh_per_hour * (1 - charge_mode[i]))

    # Directive-imposed hard constraints
    for i, h in enumerate(hours_sorted):
        hh = h.hour
        if not eff.charge_allowed[hh]:
            solver.Add(charge[i] == 0)
        if not eff.discharge_allowed[hh]:
            solver.Add(discharge[i] == 0)
        cap = eff.grid_cap[hh]
        if cap is not None:
            solver.Add(grid[i] <= cap)

    # Reserve floors
    for i, h in enumerate(hours_sorted):
        hh = h.hour
        floor = eff.reserve_floor[hh]
        if floor > bat.minimum_energy_kwh + 1e-9:
            # E[h] >= floor; solver-side variable bound handles up to capacity.
            solver.Add(energy[i] >= floor)

    status = solver.Solve()
    if status != pywraplp.Solver.OPTIMAL:
        return None

    schedule = ScheduleResult(
        grid=[grid[i].solution_value() for i in range(H)],
        solar_used=[solar_used[i].solution_value() for i in range(H)],
        charge=[charge[i].solution_value() for i in range(H)],
        discharge=[discharge[i].solution_value() for i in range(H)],
        battery_after=[energy[i].solution_value() for i in range(H)],
        objective=solver.Objective().Value(),
    )
    return schedule
