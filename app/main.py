"""RAKH API — entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import admin, clients, integrations, public, reports, team, upload
from app.services.scheduler import start_scheduler, stop_scheduler

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ENABLE_SCHEDULER:
        start_scheduler()
    yield
    if settings.ENABLE_SCHEDULER:
        stop_scheduler()


app = FastAPI(
    title="RAKH API",
    description="White-Label AI Marketing Reporter — backend service",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clients.router)
app.include_router(upload.router)
app.include_router(reports.router)
app.include_router(public.router)
app.include_router(team.router)
app.include_router(integrations.router)
app.include_router(admin.router)


@app.get("/api/health", tags=["health"])
async def health_check():
    return {"status": "ok", "service": "rakh-api"}
