"""Client management (CRUD) — scoped to the signed-in user's agency (team)."""
from fastapi import APIRouter, Depends, HTTPException

from app.db.supabase_client import get_current_agency_id, get_supabase
from app.models.schemas import ClientCreate, ClientOut, ClientUpdate
from app.services.feature_gate import check_limit

router = APIRouter(prefix="/api/clients", tags=["clients"])


@router.get("", response_model=list[ClientOut])
async def list_clients(agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = sb.table("clients").select("*").eq("agency_id", agency_id).order("created_at", desc=True).execute()
    return res.data


@router.post("", response_model=ClientOut, status_code=201)
async def create_client(payload: ClientCreate, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()

    current_count = len(sb.table("clients").select("id").eq("agency_id", agency_id).execute().data or [])
    check_limit(agency_id, "max_clients", current_count)

    row = {**payload.model_dump(), "agency_id": agency_id}
    res = sb.table("clients").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Failed to create client")
    return res.data[0]


@router.get("/{client_id}", response_model=ClientOut)
async def get_client(client_id: str, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = sb.table("clients").select("*").eq("id", client_id).eq("agency_id", agency_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Client not found")
    return res.data


@router.patch("/{client_id}", response_model=ClientOut)
async def update_client(client_id: str, payload: ClientUpdate, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    res = (
        sb.table("clients")
        .update(updates)
        .eq("id", client_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Client not found or nothing to update")
    return res.data[0]


@router.delete("/{client_id}", status_code=204)
async def delete_client(client_id: str, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    sb.table("clients").delete().eq("id", client_id).eq("agency_id", agency_id).execute()
    return None
