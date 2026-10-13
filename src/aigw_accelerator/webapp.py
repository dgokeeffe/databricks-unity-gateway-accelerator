"""FastAPI operator console for the Unity AI Gateway accelerator.

The app is demo-only unless ``AIGW_APP_MODE=live`` is explicitly configured.
All live operations are restricted to ``ADMIN_EMAILS`` and mutations require
an explicit confirmation plus a durable Unity Catalog Volume audit destination.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cli import apply_config, build_plan, verify_config, write_audit_record
from .config import ConfigError, parse_config_text
from . import tiers
from .usage import spend_estimates_by_principal

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parent / "web"
EXAMPLE_CONFIG = ROOT / "config" / "example.yml"

app = FastAPI(title="Unity AI Gateway Governance Console")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class ConfigRequest(BaseModel):
    config_yaml: str = Field(min_length=1, max_length=200_000)


class ApplyRequest(ConfigRequest):
    confirmation: str
    apply_budgets: bool = False


class TierRequest(ConfigRequest):
    email: str = Field(min_length=3, max_length=320)
    kind: str = Field(default="runaway", pattern="^(monthly|runaway)$")
    target: str | None = Field(default=None, max_length=256)
    spend_usd: float | None = Field(default=None, ge=0)


class TierMoveRequest(TierRequest):
    confirmation: str


class TierResetRequest(ConfigRequest):
    dry_run: bool = True
    confirmation: str = ""


def _mode() -> str:
    mode = os.environ.get("AIGW_APP_MODE", "demo").strip().lower()
    return mode if mode in {"demo", "live"} else "demo"


def _require_admin(request: Request) -> str:
    email = (
        (request.headers.get("X-Forwarded-Email") or os.environ.get("DEV_USER", "")).strip().lower()
    )
    admins = {
        value.strip().lower()
        for value in os.environ.get("ADMIN_EMAILS", "").split(",")
        if value.strip()
    }
    if not email or email not in admins:
        raise HTTPException(
            status_code=403, detail="Live console access requires an administrator."
        )
    return email


def _require_live_admin(request: Request) -> str:
    if _mode() != "live":
        raise HTTPException(
            status_code=409,
            detail="This deployment is in demo mode; no Databricks APIs were called.",
        )
    return _require_admin(request)


def _parse(body: ConfigRequest) -> dict[str, Any]:
    try:
        return parse_config_text(body.config_yaml)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _reject_unresolved_placeholders(config: Any, path: str = "config") -> None:
    if isinstance(config, dict):
        for key, value in config.items():
            _reject_unresolved_placeholders(value, f"{path}.{key}")
    elif isinstance(config, list):
        for index, value in enumerate(config):
            _reject_unresolved_placeholders(value, f"{path}[{index}]")
    elif isinstance(config, str) and re.search(r"<[^<>]+>", config):
        raise HTTPException(
            status_code=422,
            detail=f"Replace the example placeholder at {path} before live operations.",
        )


def _configured_audit_volume() -> str | None:
    path = os.environ.get("AIGW_AUDIT_VOLUME", "").rstrip("/")
    if re.fullmatch(r"/Volumes/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", path):
        return path
    return None


def _audit_volume() -> str:
    path = _configured_audit_volume()
    if not path:
        raise HTTPException(
            status_code=503,
            detail=(
                "Set AIGW_AUDIT_VOLUME to a pre-created Unity Catalog Volume path "
                "(/Volumes/<catalog>/<schema>/<volume>) before enabling app-side apply."
            ),
        )
    return path


def _volume_audit_writer(config: dict[str, Any], **kwargs: Any) -> str:
    """Persist CLI audit records to a customer-owned UC Volume before/after apply."""
    return _upload_audit(write_audit_record(config, **kwargs))


def _upload_audit(path: Path) -> str:
    destination = f"{_audit_volume()}/{path.name}"
    from databricks.sdk import WorkspaceClient

    WorkspaceClient().files.upload(
        file_path=destination,
        contents=io.BytesIO(path.read_bytes()),
        overwrite=False,
    )
    return destination


def _demo_summary(config: dict[str, Any]) -> dict[str, Any]:
    daily = config.get("daily_raise", {})
    return {
        "mode": "demo",
        "message": "Synthetic preview only. No Databricks APIs or customer data were accessed.",
        "counts": {
            "groups": len(config.get("groups", [])),
            "budgets": len(config.get("budgets", [])),
            "model_services": len(config.get("model_services", [])),
            "model_provider_services": len(config.get("model_provider_services", [])),
            "grants": len(config.get("grants", [])),
            "daily_tiers": len(daily.get("tiers", [])),
        },
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    mode = _mode()
    return {
        "mode": mode,
        "app_version": "0.1.0",
        "daily_spend_source": "public system-table estimates",
        "daily_spend_is_enforcement_signal": False,
        "apply_enabled": (
            mode == "live" and os.environ.get("AIGW_ENABLE_APPLY", "").strip().lower() == "true"
        ),
        "apply_audit_ready": _configured_audit_volume() is not None,
    }


@app.get("/api/example-config")
def example_config() -> dict[str, str]:
    if not EXAMPLE_CONFIG.exists():
        raise HTTPException(status_code=500, detail="Example configuration is unavailable.")
    return {"config_yaml": EXAMPLE_CONFIG.read_text(encoding="utf-8")}


@app.post("/api/config/validate")
def validate_configuration(body: ConfigRequest) -> dict[str, Any]:
    config = _parse(body)
    summary = _demo_summary(config)
    summary["message"] = "Configuration is valid. Validation performs no Databricks calls."
    summary["mode"] = _mode()
    return summary


@app.post("/api/plan")
async def plan_configuration(body: ConfigRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    if _mode() != "live":
        return {**_demo_summary(config), "plan": "Demo only — no remote state was read."}
    _require_admin(request)
    _reject_unresolved_placeholders(config)
    try:
        plan_text = await asyncio.to_thread(build_plan, config)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Plan failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"mode": "live", "plan": plan_text}


@app.post("/api/verify")
async def verify_configuration(body: ConfigRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    if _mode() != "live":
        return {
            **_demo_summary(config),
            "verification": "Demo only — no remote state was checked.",
            "passed": None,
        }
    _require_admin(request)
    _reject_unresolved_placeholders(config)
    try:
        failures = await asyncio.to_thread(verify_config, config)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Verification failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"mode": "live", "passed": not failures, "failures": failures}


@app.post("/api/apply")
async def apply_configuration(body: ApplyRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    _require_live_admin(request)
    _reject_unresolved_placeholders(config)
    if os.environ.get("AIGW_ENABLE_APPLY", "").strip().lower() != "true":
        raise HTTPException(status_code=403, detail="App-side apply is disabled by configuration.")
    if body.confirmation.strip() != "APPLY":
        raise HTTPException(status_code=400, detail='Type "APPLY" to confirm these changes.')
    _audit_volume()  # Fail closed before any write if durable audit storage is absent.
    try:
        started, completed = await asyncio.to_thread(
            apply_config,
            config,
            apply_budgets=body.apply_budgets,
            audit_writer=_volume_audit_writer,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Apply failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        "mode": "live",
        "message": "Apply completed.",
        "audit_records": [str(started), str(completed)],
    }


@app.get("/api/spend-estimates")
async def spend_estimates(request: Request) -> dict[str, Any]:
    _require_live_admin(request)
    try:
        return await asyncio.to_thread(spend_estimates_by_principal)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Spend telemetry is unavailable: {type(exc).__name__}: {exc}",
        ) from exc


def _require_live_writes(request: Request, confirmation: str, expected: str) -> str:
    actor = _require_live_admin(request)
    if os.environ.get("AIGW_ENABLE_APPLY", "").strip().lower() != "true":
        raise HTTPException(status_code=403, detail="App-side apply is disabled by configuration.")
    if confirmation.strip() != expected:
        raise HTTPException(status_code=400, detail=f'Type "{expected}" to confirm.')
    _audit_volume()  # Fail closed before any write if durable audit storage is absent.
    return actor


def _tier_preview(config: dict[str, Any], body: TierRequest) -> dict[str, Any]:
    from .account import AccountAutomation, make_account_client

    spend, source = body.spend_usd, "operator-supplied" if body.spend_usd is not None else None
    if spend is None and body.kind == "runaway" and not body.target:
        if os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip():
            rows = spend_estimates_by_principal().get("rows", [])
            match = next(
                (r for r in rows if str(r.get("principal", "")).lower() == body.email.lower()),
                None,
            )
            spend = match["month_to_date_spend_usd"] if match else 0.0
            source = "system-table estimate (may lag enforcement)"
    account = AccountAutomation(make_account_client(config["account"]))
    result = tiers.preview(
        account, config, email=body.email, kind=body.kind, target=body.target, spend_usd=spend
    )
    result["spend_source"] = source
    return {"account": account, "plan": result}


@app.post("/api/tiers/preview")
async def tier_preview(body: TierRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    _require_live_admin(request)
    _reject_unresolved_placeholders(config)
    try:
        result = await asyncio.to_thread(_tier_preview, config, body)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Tier preview failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"mode": "live", **result["plan"]}


@app.post("/api/tiers/move")
async def tier_move(body: TierMoveRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    actor = _require_live_writes(request, body.confirmation, "MOVE")
    _reject_unresolved_placeholders(config)

    def run() -> dict[str, Any]:
        prepared = _tier_preview(config, body)
        moved = tiers.apply_move(prepared["account"], prepared["plan"])
        moved["audit"] = _upload_audit(tiers.write_tier_audit(moved, actor=actor, action="move"))
        return moved

    try:
        result = await asyncio.to_thread(run)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Tier move failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"mode": "live", **result}


@app.post("/api/tiers/reset-runaway")
async def tier_reset(body: TierResetRequest, request: Request) -> dict[str, Any]:
    config = _parse(body)
    if body.dry_run:
        actor = _require_live_admin(request)
    else:
        actor = _require_live_writes(request, body.confirmation, "RESET")
    _reject_unresolved_placeholders(config)
    from .account import AccountAutomation, make_account_client

    def run() -> dict[str, Any]:
        account = AccountAutomation(make_account_client(config["account"]))
        result = tiers.reset_runaway(account, config, dry_run=body.dry_run)
        if not body.dry_run:
            result["audit"] = _upload_audit(
                tiers.write_tier_audit(result, actor=actor, action="reset")
            )
        return result

    try:
        result = await asyncio.to_thread(run)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Runaway reset failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"mode": "live", **result}


@app.get("/api/demo-data")
def demo_data() -> dict[str, Any]:
    """Synthetic values for public preview; deliberately contains no employee data."""
    return {
        "monthly": {"cap_usd": 1500, "spend_usd": 520, "period": "month-to-date"},
        "daily": {
            "cap_usd": 125,
            "spend_usd": 32.4,
            "period": "today (UTC)",
            "source": "synthetic demo data",
            "is_enforcement_signal": False,
        },
        "daily_raise": {
            "monthly_to_daily_divisor": 15,
            "minimum_next_tier_usd": 132.4,
            "next_tier_usd": 150,
            "tiers": [100, 125, 150, 200, 250],
        },
        "tier_move_example": {
            "current_group": "daily-tier-125",
            "target_group": "daily-tier-150",
        },
        "note": "Demo values are synthetic; daily values from system tables are estimates.",
    }
