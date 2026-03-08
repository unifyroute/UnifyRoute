import os
import hashlib
import time
import logging
import jwt
from fastapi import Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from shared.database import get_db_session
from shared.models import GatewayKey
from router.quota import get_redis

logger = logging.getLogger(__name__)

# Extract from main.py
JWT_SECRET = os.environ.get("JWT_SECRET", "super-secret-default")
JWT_ALGORITHM = "HS256"

async def get_current_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session)
) -> GatewayKey:
    """Auth middleware that handles Bearer token OR JWT cookie, with rate limiting."""
    
    auth_header = request.headers.get("Authorization")
    cookie_token = request.cookies.get("gateway_jwt")
    
    gateway_key = None
    
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header.split(" ")[1]
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        stmt = select(GatewayKey).where(GatewayKey.key_hash == key_hash, GatewayKey.enabled == True)
        result = await session.execute(stmt)
        gateway_key = result.scalar_one_or_none()
        if not gateway_key:
            logger.warning("Auth failed: invalid or disabled API key (hash=%s…)", key_hash[:8])
            raise HTTPException(status_code=401, detail="Invalid or disabled API key")
        logger.debug("Auth via Bearer token: key=%s label=%s", gateway_key.id, gateway_key.label)
            
    elif cookie_token:
        # GUI Session Auth via HTTPOnly Cookie
        try:
            payload = jwt.decode(cookie_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            if payload.get("sub") == "admin":
                gateway_key = GatewayKey(id=None, label="Admin GUI Session", scopes=["admin"], enabled=True)
                logger.debug("Auth via JWT cookie: admin session")
            else:
                logger.warning("Auth failed: invalid JWT subject '%s'", payload.get("sub"))
                raise HTTPException(status_code=401, detail="Invalid session token subject")
        except jwt.ExpiredSignatureError:
            logger.warning("Auth failed: expired JWT session")
            raise HTTPException(status_code=401, detail="Session expired")
        except jwt.InvalidTokenError:
            logger.warning("Auth failed: invalid JWT token")
            raise HTTPException(status_code=401, detail="Invalid session token")
    else:
        logger.warning("Auth failed: no Authorization header or JWT cookie on %s %s", request.method, request.url.path)
        raise HTTPException(status_code=401, detail="Missing authorization")

    # Optional Rate Limiting (per-key)
    if gateway_key and getattr(gateway_key, 'rate_limit_rpm', None) is not None and gateway_key.id is not None:
        redis_client = await get_redis()
        current_minute = int(time.time() // 60)
        # Unique bucket per key per minute
        rl_key = f"rate_limit:{gateway_key.id}:{current_minute}"
        
        count = await redis_client.incr(rl_key)
        if count == 1:
            await redis_client.expire(rl_key, 60)
            
        if count > gateway_key.rate_limit_rpm:
            logger.warning("Rate limit exceeded for key=%s (%d/%d rpm)", gateway_key.id, count, gateway_key.rate_limit_rpm)
            raise HTTPException(status_code=429, detail="API rate limit exceeded for this key")

    return gateway_key

async def require_admin_key(key: GatewayKey = Depends(get_current_key)) -> GatewayKey:
    """Auth middleware that additionally checks for admin scope."""
    if "admin" not in key.scopes:
        logger.warning("Admin access denied for key=%s (scopes=%s)", key.id, key.scopes)
        raise HTTPException(status_code=403, detail="Admin scope required")
    return key
