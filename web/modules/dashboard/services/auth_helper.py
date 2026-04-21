"""Auth helper — matches billing pattern. Reads from AuthMiddleware."""
from fastapi import Request, HTTPException


def get_current_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_role(*roles):
    def checker(request: Request):
        user = get_current_user(request)
        if hasattr(user, "role") and user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker
