import logging
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Dict, Any, List

from shared.database import get_db_session
from shared.models import GatewayKey, ProviderModel, RoutingConfig
from shared.schemas import ModelResponse

from api_gateway.auth import get_current_key
import yaml

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Models"])

@router.get("/models")
async def list_models(
    key: GatewayKey = Depends(get_current_key),
    session: AsyncSession = Depends(get_db_session)
):
    """
    OpenAI-compatible models list.
    - If user is admin (has 'admin' scope), they see the full catalog (including inactive models).
    - If user is a standard API key, they only see 'virtual' models defined in the routing config.
    """
    
    # ── Admin sees full catalog ──
    if "admin" in key.scopes:
        stmt = select(ProviderModel).order_by(ProviderModel.model_id)
        result = await session.execute(stmt)
        all_models = result.scalars().all()
        
        data = []
        for pm in all_models:
            data.append({
                "id": pm.model_id,
                "object": "model",
                "created": 1686935002,
                "owned_by": pm.provider_id.hex if pm.provider_id else "unknown",
                "tier": pm.tier,
                "enabled": pm.enabled
            })
        logger.debug("Models list: admin view, %d model(s)", len(data))
        return {"object": "list", "data": data}

    # ── Standard user sees virtual aliases from routing config ──
    cfg_stmt = select(RoutingConfig).limit(1)
    cfg_result = await session.execute(cfg_stmt)
    cfg = cfg_result.scalar_one_or_none()
    
    data = []
    if cfg and cfg.yaml_content:
        import yaml
        try:
            parsed = yaml.safe_load(cfg.yaml_content)
            for tier_name in parsed.get("tiers", {}).keys():
                data.append({
                    "id": tier_name,
                    "object": "model",
                    "created": 1686935002,
                    "owned_by": "unifyroute",
                    "virtual": True
                })
        except Exception:
            pass

    return {"object": "list", "data": data}
