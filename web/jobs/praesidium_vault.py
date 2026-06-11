"""
praesidium_vault.py — Reusable vault key decryption for batch jobs.
Reads encrypted API keys from credentials_vault using the same Fernet
pattern as anthropic_adapter.py.

Usage:
    from praesidium_vault import get_api_key
    key = get_api_key("voyage", tenant_id)
"""
import os
import base64
import psycopg2

def _derive_fernet_key():
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return base64.urlsafe_b64encode(key_bytes)

def _get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:].split("?")[0]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(host=host, port=int(port), dbname=dbname, user=user, password=password)

def get_api_key(provider, tenant_id):
    """Decrypt and return an API key from credentials_vault."""
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT encrypted_key FROM credentials_vault "
        "WHERE TRIM(tenant_id) = %s AND provider = %s AND key_type = 'api_key' "
        "ORDER BY updated_at DESC LIMIT 1",
        (tenant_id.strip(), provider)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return None
    from cryptography.fernet import Fernet
    f = Fernet(_derive_fernet_key())
    return f.decrypt(row[0].encode()).decode()
