"""Auth helper — extracts current_user from request.state (set by AuthMiddleware)."""
from fastapi import Request, HTTPException

def get_current_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
