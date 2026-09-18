"""FastAPI entrypoint for the BUP CSE Fest 2026 preliminary challenge."""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import directives as directives_mod
from . import interpreter, optimizer, replay, summary
from .schemas import (
    DirectiveInterpretation,
    OptimizeRequest,
    OptimizeResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger("gridwise")

app = FastAPI(title="GridWise Optimizer", version="1.0.0")


# Warm up the LLM client on import so the first judge request doesn't pay the
# cold-start tax on Render free tier.
_llm_client = interpreter.get_client()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.exception_handler(ValueError)
async def _value_error_handler(_, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.post("/optimize-energy", response_model=OptimizeResponse)
def optimize_energy(req: OptimizeRequest) -> OptimizeResponse:
    started = time.time()
    try:
        # 1) Interpret operator notes via Gemini (with deterministic safe-failure fallback).
        raw_interps = interpreter.interpret_notes(
            notes=req.operator_notes,
            client=_llm_client,
            scenario_id=req.scenario_id,
            capacity_kwh=req.battery.capacity_kwh,
        )

        # 2) Validate + normalize. Must produce exactly len(notes) entries in order.
        normalized = directives_mod.normalize(raw_interps, req)
        if len(normalized) != len(req.operator_notes):
            raise HTTPException(
                status_code=500,
                detail=f"interpreter returned {len(normalized)} entries for "
                       f"{len(req.operator_notes)} notes",
            )

        # 3) Build effective directive model (solar multipliers, reserve floors,
        #    charge/discharge windows, grid caps).
        effective = directives_mod.build_effective(normalized, req)

        # 4) Solve LP.
        schedule = optimizer.solve(req, effective)
        if schedule is None:
            raise HTTPException(status_code=500, detail="optimizer found no feasible solution")

        # 5) Deterministic replay — internal guardrail.
        violations = replay.verify(req, effective, schedule)
        if violations:
            logger.error("Replay produced violations: %s", violations)
            raise HTTPException(status_code=500, detail="optimizer produced an invalid schedule")

        # 6) Compose response.
        plan_entries = replay.to_plan_entries(schedule)
        total_grid = sum(e.grid_kwh for e in plan_entries)
        total_cost = sum(e.grid_kwh * req.hours[e.hour].tariff_bdt_per_kwh for e in plan_entries)
        peak_grid = max(e.grid_kwh for e in plan_entries) if plan_entries else 0.0

        interp_entries = [
            DirectiveInterpretation(
                note_index=i.note_index,
                applies=(i.directive_type != "no_op"),
                directive_type=i.directive_type,
                structured_adjustment=i.adjustment_dict(),
                explanation=i.explanation,
            )
            for i in normalized
        ]

        plan_summary = summary.generate(req, normalized, plan_entries, effective)

        elapsed_ms = int((time.time() - started) * 1000)
        logger.info(
            "optimize-energy scenario=%s notes=%d cost_bdt=%.2f grid=%.2f peak=%.2f ms=%d",
            req.scenario_id, len(req.operator_notes), total_cost, total_grid, peak_grid, elapsed_ms,
        )

        return OptimizeResponse(
            scenario_id=req.scenario_id,
            directive_interpretation=interp_entries,
            hourly_plan=plan_entries,
            total_grid_kwh=round(total_grid, 4),
            total_cost_bdt=round(total_cost, 4),
            peak_grid_kwh=round(peak_grid, 4),
            plan_summary=plan_summary,
        )
    except HTTPException:
        raise
    except Exception as exc:  # never expose secrets or stacks
        logger.error("optimize-energy failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=500, detail=f"internal error: {exc.__class__.__name__}")