"""
Supabase client wiring + FastAPI auth dependency.

The frontend authenticates directly with Supabase Auth and sends the
resulting JWT as a Bearer token on every API request. The backend verifies
that token and uses the SERVICE ROLE client (server-side only) to perform
DB operations, manually scoping every query by the verified user id
so RLS is respected in spirit even though the service key bypasses it.

NOTE ON VERIFICATION STRATEGY: we deliberately do NOT decode the JWT
locally (e.g. via `jwt.decode(..., algorithms=["HS256"])`). Supabase
projects can be configured to sign tokens with either the legacy shared
HS256 secret or the newer asymmetric ES256/JWKS signing keys — assuming
one specific algorithm locally causes hard-to-diagnose 401s
("The specified alg value is not allowed") the moment a project uses the
other mode. Calling `auth.get_user(token)` instead delegates verification
to Supabase itself, so it works correctly regardless of signing method.
This costs one extra network round-trip per request; for high-throughput
production use, consider caching valid tokens briefly or switching to
JWKS-based local verification once you've confirmed which signing mode
your project uses (Project Settings -> API -> JWT Settings).
"""
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client

from app.core.config import get_settings

security = HTTPBearer()


@lru_cache
def get_supabase() -> Client:
    settings = get_settings()
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Verify the Supabase JWT via the Supabase Auth API, returning the user's UUID."""
    token = credentials.credentials
    sb = get_supabase()

    try:
        user_response = sb.auth.get_user(token)
    except Exception as exc:  # noqa: BLE001 — supabase-py raises a generic AuthApiError
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
        ) from exc

    user = getattr(user_response, "user", None)
    if not user or not user.id:
        raise HTTPException(status_code=401, detail="Token did not resolve to a valid user")

    return user.id


async def get_current_agency_id(user_id: str = Depends(get_current_user_id)) -> str:
    """
    Resolve the agency a user belongs to. MVP assumes one active membership
    per user (the common case for an agency's own staff); if someone is a
    member of multiple agencies in the future, this picks the first active
    one — revisit with an agency-switcher UI if that becomes a real need.
    """
    sb = get_supabase()
    res = (
        sb.table("agency_members")
        .select("agency_id")
        .eq("user_id", user_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=403,
            detail="No active agency membership found for this user.",
        )
    return res.data[0]["agency_id"]


async def get_current_user_id_optional_agency(user_id: str = Depends(get_current_user_id)) -> str:
    """Just the verified user id, with no agency-membership requirement —
    used by platform-admin-only routes, since a platform admin manages
    every agency and isn't necessarily a member of any single one."""
    return user_id


async def require_platform_admin(user_id: str = Depends(get_current_user_id)) -> str:
    """
    Gate for the super-admin dashboard. Platform admins are entirely
    separate from agency roles (owner/admin/member) — they manage plans,
    pricing, and feature flags across every agency on the platform, not
    just their own workspace. Bootstrap the first one directly via SQL
    (see migration 004's comment) since there's necessarily no admin UI
    to create the very first admin from.
    """
    sb = get_supabase()
    res = sb.table("platform_admins").select("user_id").eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=403, detail="Platform admin access required.")
    return user_id
