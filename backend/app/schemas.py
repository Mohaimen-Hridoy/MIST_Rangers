"""Pydantic schemas matching BUP problem statement Sections 07 and 10.

These models enforce the wire contract for the /optimize-energy endpoint.
Validation here intentionally mirrors the spec's invariants so that
structurally invalid requests are rejected with HTTP 400/422 before any
LLM or solver work happens.
"""
from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class HourIn(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def _finite_non_negative(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf guard
            raise ValueError("must be a finite non-negative number")
        return v


class BatteryIn(BaseModel):
    capacity_kwh: float = Field(..., gt=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., ge=0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0)

    @field_validator(
        "capacity_kwh",
        "initial_energy_kwh",
        "minimum_energy_kwh",
        "max_charge_kwh_per_hour",
        "max_discharge_kwh_per_hour",
    )
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be finite")
        return v

    @model_validator(mode="after")
    def _battery_consistency(self) -> "BatteryIn":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError("initial_energy_kwh must be >= minimum_energy_kwh")
        return self


class OptimizeRequest(BaseModel):
    scenario_id: str = Field(..., min_length=1)
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourIn] = Field(..., min_length=24, max_length=24)
    battery: BatteryIn

    @field_validator("operator_notes")
    @classmethod
    def _non_empty_notes(cls, v: List[str]) -> List[str]:
        if any((not n) or not n.strip() for n in v):
            raise ValueError("operator_notes entries must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _hours_cover_0_to_23(self) -> "OptimizeRequest":
        hours = sorted({h.hour for h in self.hours})
        if hours != list(range(24)):
            raise ValueError("hours must contain exactly one entry per integer 0..23")
        return self


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

# Allowed directive types per Section 04.
ALLOWED_DIRECTIVES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


class StructuredAdjustment(BaseModel):
    """Loose container; the optimizer treats the `hours` list as canonical."""

    hours: Optional[List[int]] = None
    factor: Optional[float] = None
    minimum_energy_kwh: Optional[float] = None
    max_grid_kwh: Optional[float] = None


class DirectiveInterpretation(BaseModel):
    note_index: int = Field(..., ge=0)
    applies: bool
    directive_type: str
    structured_adjustment: Optional[StructuredAdjustment] = None
    explanation: str = ""

    @field_validator("directive_type")
    @classmethod
    def _directive_allowed(cls, v: str) -> str:
        if v not in ALLOWED_DIRECTIVES:
            raise ValueError(f"unsupported directive_type: {v}")
        return v

    @model_validator(mode="after")
    def _applies_matches_directive(self) -> "DirectiveInterpretation":
        if self.directive_type == "no_op":
            if self.applies is not False:
                raise ValueError("no_op must set applies=false")
            if self.structured_adjustment not in (None, StructuredAdjustment()):
                # Allow empty default; only reject if it carries fields.
                if any(
                    getattr(self.structured_adjustment, f) is not None
                    for f in ("hours", "factor", "minimum_energy_kwh", "max_grid_kwh")
                ):
                    raise ValueError("no_op must have structured_adjustment=null")
        else:
            if self.applies is not True:
                raise ValueError(f"{self.directive_type} must set applies=true")
            if self.structured_adjustment is None:
                raise ValueError(
                    f"{self.directive_type} must include structured_adjustment"
                )
            sa = self.structured_adjustment
            if sa.hours is None or not sa.hours:
                raise ValueError("structured_adjustment.hours is required")
            if any((h < 0 or h > 23) for h in sa.hours):
                raise ValueError("hours must be integers 0..23")
            if sorted(sa.hours) != list(sa.hours) or len(set(sa.hours)) != len(sa.hours):
                raise ValueError("hours must be ascending unique integers")
            if self.directive_type == "solar_reduction":
                if sa.factor is None or not (0.0 <= sa.factor <= 1.0):
                    raise ValueError("solar_reduction.factor must be in [0,1]")
            elif self.directive_type == "minimum_battery_reserve":
                if sa.minimum_energy_kwh is None or sa.minimum_energy_kwh < 0:
                    raise ValueError("minimum_battery_reserve.minimum_energy_kwh must be >= 0")
            elif self.directive_type == "max_grid_window":
                if sa.max_grid_kwh is None or sa.max_grid_kwh < 0:
                    raise ValueError("max_grid_window.max_grid_kwh must be >= 0")
        return self


class HourlyPlanEntry(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0)
    solar_used_kwh: float = Field(..., ge=0)
    battery_action: str
    battery_kwh: float = Field(..., ge=0)
    battery_energy_after_kwh: float = Field(..., ge=0)

    @field_validator("battery_action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in {"charge", "discharge", "idle"}:
            raise ValueError("battery_action must be charge|discharge|idle")
        return v


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


# Discriminated union used by the optimizer when normalizing raw LLM JSON.
RawLLMItem = dict
