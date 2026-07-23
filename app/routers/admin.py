"""
Super-admin (platform admin) dashboard API.

Entirely separate from agency-scoped routes — these endpoints manage
plans, pricing, and feature flags across every agency on the platform.
Gated by require_platform_admin, not the agency-membership dependency
used everywhere else in the app.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.db.supabase_client import get_current_user_id, get_supabase, require_platform_admin
from app.models.schemas import (
    AdminAgencyOut,
    AgencyPlanAssignment,
    PlanCreate,
    PlanOut,
    PlanUpdate,
    PlatformAdminCheck,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/me", response_model=PlatformAdminCheck)
async def check_admin_status(user_id: str = Depends(get_current_user_id)):
    """
    Lightweight check the frontend calls on load to decide whether to show
    the admin nav link at all — returns false rather than 403ing, since
    "not an admin" is an expected, normal response here, not an error.
    """
    sb = get_supabase()
    res = sb.table("platform_admins").select("user_id").eq("user_id", user_id).execute()
    return {"is_platform_admin": bool(res.data)}


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(_admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    res = sb.table("plans").select("*").order("sort_order").execute()
    return res.data


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(payload: PlanCreate, _admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    row = payload.model_dump()
    row["features"] = row["features"]  # already a plain dict via PlanFeatures -> model_dump
    res = sb.table("plans").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Failed to create plan (key may already exist)")
    return res.data[0]


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(plan_id: str, payload: PlanUpdate, _admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    res = sb.table("plans").update(updates).eq("id", plan_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Plan not found")
    return res.data[0]


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, _admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    in_use = sb.table("agencies").select("id").eq("plan_id", plan_id).limit(1).execute()
    if in_use.data:
        raise HTTPException(status_code=409, detail="Cannot delete a plan that agencies are currently assigned to. Reassign them first.")
    sb.table("plans").delete().eq("id", plan_id).execute()
    return None


@router.get("/agencies", response_model=list[AdminAgencyOut])
async def list_agencies(_admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    agencies_res = sb.table("agencies").select("id, name, plan_id, created_at").order("created_at", desc=True).execute()
    plans_res = sb.table("plans").select("id, key, name").execute()
    plans_by_id = {p["id"]: p for p in (plans_res.data or [])}

    out = []
    for a in agencies_res.data or []:
        plan = plans_by_id.get(a["plan_id"], {})
        out.append({
            **a,
            "plan_key": plan.get("key"),
            "plan_name": plan.get("name"),
        })
    return out


@router.patch("/agencies/{agency_id}/plan", response_model=AdminAgencyOut)
async def assign_agency_plan(agency_id: str, payload: AgencyPlanAssignment, _admin: str = Depends(require_platform_admin)):
    sb = get_supabase()
    plan_res = sb.table("plans").select("id, key, name").eq("id", payload.plan_id).single().execute()
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")

    res = sb.table("agencies").update({"plan_id": payload.plan_id}).eq("id", agency_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Agency not found")

    agency = res.data[0]
    return {
        **agency,
        "plan_key": plan_res.data["key"],
        "plan_name": plan_res.data["name"],
    }
