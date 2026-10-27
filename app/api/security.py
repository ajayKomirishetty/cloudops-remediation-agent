"""API-key authentication and role-based authorisation."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.api.schemas import Principal

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def current_principal(request: Request, key: str | None = Depends(api_key_header)) -> Principal:
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-API-Key header")
    entry = request.app.state.settings.api_keys.get(key)
    if entry is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    user, role = entry
    return Principal(user=user, role=role)


def require_operator(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.role != "operator":
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{principal.role}' may not perform this action")
    return principal
