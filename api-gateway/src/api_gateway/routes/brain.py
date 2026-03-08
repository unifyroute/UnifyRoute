import datetime
import logging
from uuid import UUID
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from shared.database import get_db_session
from shared.models import Provider, Credential, GatewayKey
from api_gateway.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/brain", tags=["Brain"])

class BrainProviderAssign(BaseModel):
    provider_id: UUID
    credential_id: UUID
    model_id: str
    priority: int = 100

class BrainImportRequest(BaseModel):
    format: str = "yaml"   # "yaml" or "json"
    content: str

class BrainProviderAssignResponse(BaseModel):
    id: UUID
    provider_id: UUID
    credential_id: UUID
    model_id: str
    priority: int
    enabled: bool
    class Config:
        from_attributes = True

@router.get("/status")
async def brain_status(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from shared.models import BrainConfig
    from sqlalchemy.orm import selectinload
    from brain.tester import get_cached_health

    stmt = (
        select(BrainConfig)
        .where(BrainConfig.enabled == True)
        .options(
            selectinload(BrainConfig.provider),
            selectinload(BrainConfig.credential),
        )
        .order_by(BrainConfig.priority)
    )
    res = await session.execute(stmt)
    entries = res.scalars().all()

    items = []
    for e in entries:
        cached = await get_cached_health(e.credential_id, e.model_id)
        items.append({
            "id": str(e.id),
            "provider": e.provider.name,
            "provider_display": e.provider.display_name,
            "credential_label": e.credential.label,
            "credential_id": str(e.credential_id),
            "model_id": e.model_id,
            "priority": e.priority,
            "enabled": e.enabled,
            "health": {
                "ok": cached.get("ok", None) if cached else None,
                "latency_ms": cached.get("latency_ms") if cached else None,
                "message": cached.get("message", "Not yet tested") if cached else "Not yet tested",
                "tested_at": cached.get("tested_at") if cached else None,
            },
        })

    logger.info("Brain status: %d provider(s) returned", len(items))
    return {"brain_providers": items, "total": len(items)}


@router.post("/providers", response_model=BrainProviderAssignResponse)
async def brain_assign_provider(
    data: BrainProviderAssign,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from shared.models import BrainConfig

    prov_res = await session.execute(select(Provider).where(Provider.id == data.provider_id))
    if not prov_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Provider not found")
    cred_res = await session.execute(select(Credential).where(Credential.id == data.credential_id))
    if not cred_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Credential not found")

    entry = BrainConfig(
        provider_id=data.provider_id,
        credential_id=data.credential_id,
        model_id=data.model_id,
        priority=data.priority,
        enabled=True,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    logger.info("Brain provider assigned: provider=%s model=%s priority=%d", data.provider_id, data.model_id, data.priority)
    return entry


class BrainProviderUpdate(BaseModel):
    priority: int | None = None
    enabled: bool | None = None


@router.patch("/providers/{entry_id}", response_model=BrainProviderAssignResponse)
async def brain_update_provider(
    entry_id: UUID,
    data: BrainProviderUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from shared.models import BrainConfig

    res = await session.execute(select(BrainConfig).where(BrainConfig.id == entry_id))
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Brain config entry not found")

    if data.priority is not None:
        entry.priority = data.priority
    if data.enabled is not None:
        entry.enabled = data.enabled

    await session.commit()
    await session.refresh(entry)
    return entry


@router.delete("/providers/{entry_id}")
async def brain_remove_provider(
    entry_id: UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from shared.models import BrainConfig
    res = await session.execute(select(BrainConfig).where(BrainConfig.id == entry_id))
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Brain config entry not found")
    await session.delete(entry)
    await session.commit()
    return {"status": "deleted", "id": str(entry_id)}


@router.post("/import")
async def brain_import(
    req: BrainImportRequest,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from brain.importer import import_from_yaml_str, import_from_json_str
    from brain.errors import brain_safe_message

    try:
        fmt = req.format.lower().strip()
        if fmt == "yaml":
            result = await import_from_yaml_str(req.content, session)
        elif fmt == "json":
            result = await import_from_json_str(req.content, session)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{req.format}'. Use 'yaml' or 'json'.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Brain import failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=brain_safe_message(exc))

    logger.info("Brain import: %d providers, %d credentials, %d models, %d assignments",
                result.providers_created, result.credentials_created, result.models_created, result.brain_assignments_created)
    return {
        "status": "success" if not result.errors else "partial",
        "providers_created": result.providers_created,
        "providers_skipped": result.providers_skipped,
        "credentials_created": result.credentials_created,
        "credentials_skipped": result.credentials_skipped,
        "models_created": result.models_created,
        "brain_assignments_created": result.brain_assignments_created,
        "brain_assignments_skipped": result.brain_assignments_skipped,
        "errors": result.errors,
    }


@router.post("/test")
async def brain_test(
    background_tasks: BackgroundTasks,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from brain.tester import test_all_brain_credentials
    from brain.errors import brain_safe_message

    try:
        results = await test_all_brain_credentials(session)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=brain_safe_message(exc))

    return {
        "tested": len(results),
        "healthy": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [
            {
                "brain_config_id": str(r.brain_config_id),
                "provider": r.provider,
                "credential_label": r.credential_label,
                "model_id": r.model_id,
                "ok": r.ok,
                "message": r.message,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
    }


@router.get("/ranking")
async def brain_ranking(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from brain.ranker import rank_brain_providers
    from brain.errors import brain_safe_message

    try:
        ranked = await rank_brain_providers(session)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=brain_safe_message(exc))

    return {
        "ranking": [
            {
                "rank": idx + 1,
                "brain_config_id": str(e.brain_config_id),
                "provider": e.provider,
                "credential_label": e.credential_label,
                "model_id": e.model_id,
                "priority": e.priority,
                "score": e.score,
                "health_ok": e.health_ok,
                "health_message": e.health_message,
                "latency_ms": e.latency_ms,
                "quota_remaining": e.quota_remaining,
            }
            for idx, e in enumerate(ranked)
        ]
    }


@router.post("/select")
async def brain_select(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    from brain.selector import select_for_brain
    from brain.errors import brain_safe_message

    try:
        selection = await select_for_brain(session)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=brain_safe_message(exc))

    if not selection.ok:
        return {
            "ok": False,
            "provider": None,
            "credential_id": None,
            "model_id": None,
            "score": 0.0,
            "reason": selection.reason,
        }

    return {
        "ok": True,
        "provider": selection.provider,
        "credential_id": str(selection.credential_id),
        "credential_label": selection.credential_label,
        "model_id": selection.model_id,
        "score": selection.score,
        "reason": selection.reason,
    }
