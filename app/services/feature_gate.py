"""
Feature gating.

Single source of truth for "can this agency do X" — reads the agency's
assigned plan (plans.features jsonb) rather than hardcoding checks
scattered across routers. Add a new gate anywhere in the app by calling
require_feature() as a FastAPI dependency, or check_limit() inline where
you need to compare against a numeric cap (e.g. max_reports_per_month).

Numeric limits use -1 as the "unlimited" sentinel (see PlanFeatures).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from app.db.supabase_client import get_current_agency_id, get_supabase


def get_agency_plan(agency_id: str) -> dict:
    """Fetch the agency's plan row (joined), raising if misconfigured
    rather than silently falling back — a missing plan is a data bug,
    not a normal "no access" case."""
    sb = get_supabase()
    agency_res = sb.table("agencies").select("plan_id").eq("id", agency_id).single().execute()
    if not agency_res.data or not agency_res.data.get("plan_id"):
        raise HTTPException(status_code=500, detail="Agency has no plan assigned — contact support.")

    plan_res = sb.table("plans").select("*").eq("id", agency_res.data["plan_id"]).single().execute()
    if not plan_res.data:
        raise HTTPException(status_code=500, detail="Agency's assigned plan no longer exists — contact support.")
    return plan_res.data


def agency_has_feature(agency_id: str, feature_key: str) -> bool:
    plan = get_agency_plan(agency_id)
    return bool(plan.get("features", {}).get(feature_key, False))


def require_feature(feature_key: str, error_message: str | None = None):
    """
    FastAPI dependency factory — use like:
        @router.post(..., dependencies=[Depends(require_feature("connected_accounts"))])
    Raises 402 Payment Required (not 403) since this is specifically a
    plan/billing gate, distinct from a permissions/auth failure.
    """

    async def _check(agency_id: str = Depends(get_current_agency_id)) -> None:
        if not agency_has_feature(agency_id, feature_key):
            raise HTTPException(
                status_code=402,
                detail=error_message or f"This feature ('{feature_key}') requires a plan upgrade.",
            )

    return _check


def check_limit(agency_id: str, limit_key: str, current_count: int) -> None:
    """
    Inline numeric-limit check — call this before creating a new row
    (report, client, team member) rather than as a route-level dependency,
    since it needs the current count computed first.

        check_limit(agency_id, "max_clients", current_client_count)
    """
    plan = get_agency_plan(agency_id)
    limit = plan.get("features", {}).get(limit_key)
    if limit is None:
        return  # key not defined on this plan — fail open rather than block unexpectedly
    if limit == -1:
        return  # unlimited
    if current_count >= limit:
        plan_name = plan.get("name", "your current plan")
        raise HTTPException(
            status_code=402,
            detail=f"You've reached the {limit_key.replace('_', ' ')} limit ({limit}) for {plan_name}. Upgrade to continue.",
        )
