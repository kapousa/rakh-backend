"""
Team / agency member management.

MVP invite flow: inviting an email that doesn't have an account yet creates
a "pending" membership row (status='pending', invited_email set, user_id
null). When that person eventually signs up with the same email, a
lightweight matching step (see `claim_pending_invites`) links their new
auth user id to the pending row. This avoids needing transactional email
+ signed invite links for the MVP while still giving a real multi-user
team experience once someone signs up.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.db.supabase_client import get_current_agency_id, get_current_user_id, get_supabase
from app.models.schemas import AgencyOut, AgencyUpdate, DomainVerificationOut, MemberInvite, MemberOut, MemberRoleUpdate
from app.services import domain_service
from app.services.feature_gate import agency_has_feature, check_limit

router = APIRouter(prefix="/api/team", tags=["team"])


def _require_admin(sb, agency_id: str, user_id: str) -> None:
    res = (
        sb.table("agency_members")
        .select("role")
        .eq("agency_id", agency_id)
        .eq("user_id", user_id)
        .eq("status", "active")
        .single()
        .execute()
    )
    role = (res.data or {}).get("role")
    if role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only agency owners/admins can manage team members.")


@router.get("/agency", response_model=AgencyOut)
async def get_agency(agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = sb.table("agencies").select("*").eq("id", agency_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Agency not found")
    agency = res.data

    # Enrich with the assigned plan's feature map and this month's usage —
    # the sidebar plan-usage display and any client-side feature checks
    # read these two fields directly off the agency response rather than
    # needing a second round-trip.
    plan_features = {}
    if agency.get("plan_id"):
        plan_res = sb.table("plans").select("features").eq("id", agency["plan_id"]).single().execute()
        if plan_res.data:
            plan_features = plan_res.data.get("features", {})

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    reports_used = len(
        sb.table("reports").select("id").eq("agency_id", agency_id).gte("created_at", month_start).execute().data or []
    )

    return {**agency, "plan_features": plan_features, "reports_used_this_month": reports_used}


WHITE_LABEL_FIELDS = {
    "white_label_enabled", "platform_display_name", "platform_logo_url",
    "platform_favicon_url", "custom_domain",
}


@router.patch("/agency", response_model=AgencyOut)
async def update_agency(
    payload: AgencyUpdate,
    agency_id: str = Depends(get_current_agency_id),
    user_id: str = Depends(get_current_user_id),
):
    sb = get_supabase()
    _require_admin(sb, agency_id, user_id)
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}

    if WHITE_LABEL_FIELDS & updates.keys() and not agency_has_feature(agency_id, "platform_rebrand"):
        raise HTTPException(
            status_code=402,
            detail="Rebranding the app interface is an Enterprise feature. Upgrade your plan to enable it.",
        )

    res = sb.table("agencies").update(updates).eq("id", agency_id).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Failed to update agency")
    return res.data[0]


@router.post("/agency/verify-domain", response_model=DomainVerificationOut)
async def verify_agency_domain(
    agency_id: str = Depends(get_current_agency_id),
    user_id: str = Depends(get_current_user_id),
):
    """
    Runs a live DNS check against the agency's configured custom_domain and
    persists the result. Called from the "Verify DNS" button in Settings.
    Requires platform_rebrand (same gate as setting the domain at all).
    """
    sb = get_supabase()
    _require_admin(sb, agency_id, user_id)

    if not agency_has_feature(agency_id, "platform_rebrand"):
        raise HTTPException(status_code=402, detail="Custom domains are an Enterprise feature.")

    agency_res = sb.table("agencies").select("custom_domain").eq("id", agency_id).single().execute()
    domain = (agency_res.data or {}).get("custom_domain")
    if not domain:
        raise HTTPException(status_code=400, detail="Set a custom domain first, then verify it.")

    result = domain_service.verify_domain(domain)

    sb.table("agencies").update({
        "custom_domain_verified": result["verified"],
        "custom_domain_verified_at": datetime.now(timezone.utc).isoformat() if result["verified"] else None,
    }).eq("id", agency_id).execute()

    return result


@router.get("/members", response_model=list[MemberOut])
async def list_members(agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = sb.table("agency_members").select("*").eq("agency_id", agency_id).order("created_at").execute()
    return res.data


@router.post("/members/invite", response_model=MemberOut, status_code=201)
async def invite_member(
    payload: MemberInvite,
    agency_id: str = Depends(get_current_agency_id),
    user_id: str = Depends(get_current_user_id),
):
    sb = get_supabase()
    _require_admin(sb, agency_id, user_id)

    current_count = len(sb.table("agency_members").select("id").eq("agency_id", agency_id).execute().data or [])
    check_limit(agency_id, "max_team_members", current_count)

    # If this email already belongs to an existing auth user, link immediately
    # (status='active'); otherwise store as a pending invite by email.
    existing_user_id = None
    try:
        users_page = sb.auth.admin.list_users()
        for u in users_page:
            if getattr(u, "email", None) == payload.email:
                existing_user_id = u.id
                break
    except Exception as exc:  # noqa: BLE001 — admin listing is best-effort
        print(f"[team] Could not check existing users for invite matching: {exc}")

    row = {
        "agency_id": agency_id,
        "role": payload.role,
        "invited_email": payload.email,
        "user_id": existing_user_id,
        "status": "active" if existing_user_id else "pending",
    }
    res = sb.table("agency_members").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Failed to invite member (they may already be invited)")
    return res.data[0]


@router.patch("/members/{member_id}", response_model=MemberOut)
async def update_member_role(
    member_id: str,
    payload: MemberRoleUpdate,
    agency_id: str = Depends(get_current_agency_id),
    user_id: str = Depends(get_current_user_id),
):
    sb = get_supabase()
    _require_admin(sb, agency_id, user_id)
    res = (
        sb.table("agency_members")
        .update({"role": payload.role})
        .eq("id", member_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Member not found")
    return res.data[0]


@router.delete("/members/{member_id}", status_code=204)
async def remove_member(
    member_id: str,
    agency_id: str = Depends(get_current_agency_id),
    user_id: str = Depends(get_current_user_id),
):
    sb = get_supabase()
    _require_admin(sb, agency_id, user_id)
    sb.table("agency_members").delete().eq("id", member_id).eq("agency_id", agency_id).execute()
    return None
