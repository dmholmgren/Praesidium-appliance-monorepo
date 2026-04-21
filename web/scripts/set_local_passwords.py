"""
One-time script to set bcrypt passwords for local auth users.
Run on WEB-01:
  docker exec praesidium-web python3 /app/scripts/set_local_passwords.py
"""
import asyncio
import bcrypt
from sqlalchemy import text

# tenant_id → [(username_or_email, password)]
USERS = {
    "f99d1cef-ee79-4c29-9470-9cf5462e99ae": [
        ("praesidium_admin", "!Praesidium!123"),
        ("praesidium_user",  "!Praesidium!123"),
    ],
}

async def main():
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        for tenant_id, users in USERS.items():
            for username, password in users:
                hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                result = await session.execute(
                    text("""
                        UPDATE users SET password_hash = :hash
                        WHERE TRIM(tenant_id) = :tid
                          AND (username = :uname OR email = :uname)
                    """),
                    {"hash": hashed, "tid": tenant_id, "uname": username}
                )
                print(f"  {username}: {result.rowcount} row(s) updated")
        await session.commit()
    print("Done")

asyncio.run(main())
