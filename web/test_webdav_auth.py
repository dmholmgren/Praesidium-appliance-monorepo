#!/usr/bin/env python3
"""
Debug WebDAV auth — test LDAP bind from inside the container.
Run: docker exec praesidium-web python3 /app/test_webdav_auth.py
"""
import asyncio
import os
import sys

# Minimal test — no app imports needed for the first checks
print("=== WebDAV Auth Debug ===\n")

# 1. Test vault decrypt
print("1. Testing Fernet key derivation...")
import base64
from cryptography.fernet import Fernet

secret = os.environ.get("SECRET_KEY", "")
print(f"   SECRET_KEY present: {bool(secret)} (len={len(secret)})")
key_bytes = (secret[:32]).encode().ljust(32, b"0")
f = Fernet(base64.urlsafe_b64encode(key_bytes))
print(f"   Fernet key OK")

# 2. Test credential retrieval
print("\n2. Testing credential retrieval from vault...")

async def test_creds():
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tid = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT key_type, encrypted_key
                FROM credentials_vault
                WHERE TRIM(tenant_id) = :tid
                  AND provider = 'auth_ldap'
            """),
            {"tid": tid}
        )
        rows = list(result.mappings())
    
    print(f"   Found {len(rows)} credential rows")
    
    creds = {}
    for row in rows:
        kt = row["key_type"]
        enc = row["encrypted_key"]
        print(f"   {kt}: encrypted_key len={len(enc)}")
        try:
            decrypted = f.decrypt(enc.encode()).decode()
            creds[kt] = decrypted
            # Mask for display
            if kt == "bind_dn":
                print(f"   {kt} decrypted: {decrypted}")
            else:
                print(f"   {kt} decrypted: {'*' * len(decrypted)} (len={len(decrypted)})")
        except Exception as e:
            print(f"   {kt} DECRYPT FAILED: {e}")
            # Try as plaintext
            creds[kt] = enc
            print(f"   {kt} using as plaintext: {enc[:20]}...")
    
    return creds

creds = asyncio.run(test_creds())

# 3. Test LDAP config from tenant_connectors
print("\n3. Testing LDAP config from tenant_connectors...")

async def test_ldap_config():
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    
    tid = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT config FROM tenant_connectors
                WHERE TRIM(tenant_id) = :tid AND connector = 'auth_ldap'
            """),
            {"tid": tid}
        )
        row = result.mappings().first()
    
    if not row:
        print("   NO auth_ldap connector found!")
        return None
    
    config = row["config"]
    print(f"   ldap_url: {config.get('ldap_url')}")
    print(f"   base_dn: {config.get('base_dn')}")
    print(f"   user_search_base: {config.get('user_search_base')}")
    print(f"   user_filter: {config.get('user_filter')}")
    return config

ldap_config = asyncio.run(test_ldap_config())

# 4. Test LDAP bind
if ldap_config and creds.get("bind_dn") and creds.get("bind_password"):
    print("\n4. Testing LDAP service account bind...")
    import ssl
    import ldap3
    
    ldap_url = ldap_config["ldap_url"]
    bind_dn = creds["bind_dn"]
    bind_pw = creds["bind_password"]
    
    print(f"   URL: {ldap_url}")
    print(f"   Bind DN: {bind_dn}")
    
    tls_config = None
    if ldap_url.startswith("ldaps://"):
        tls_config = ldap3.Tls(validate=ssl.CERT_NONE)
    
    try:
        server = ldap3.Server(ldap_url, use_ssl=ldap_url.startswith("ldaps://"), tls=tls_config)
        conn = ldap3.Connection(server, user=bind_dn, password=bind_pw)
        bound = conn.bind()
        print(f"   Service account bind: {'SUCCESS' if bound else 'FAILED'}")
        if not bound:
            print(f"   Bind result: {conn.result}")
        else:
            # 5. Search for dholmgren
            print("\n5. Searching for dholmgren...")
            search_base = ldap_config.get("user_search_base", ldap_config.get("base_dn"))
            search_filter = ldap_config.get("user_filter", "(&(objectClass=user)(sAMAccountName={username}))").replace("{username}", "dholmgren")
            print(f"   Search base: {search_base}")
            print(f"   Filter: {search_filter}")
            
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=["sAMAccountName", "mail", "displayName", "memberOf"],
            )
            print(f"   Results: {len(conn.entries)}")
            if conn.entries:
                entry = conn.entries[0]
                print(f"   DN: {entry.entry_dn}")
                print(f"   displayName: {entry.displayName if hasattr(entry, 'displayName') else 'N/A'}")
                
                # 6. Test user bind
                print("\n6. Testing user bind for dholmgren...")
                user_dn = str(entry.entry_dn)
                user_conn = ldap3.Connection(server, user=user_dn, password='!250879_Dh!')
                user_bound = user_conn.bind()
                print(f"   User bind: {'SUCCESS' if user_bound else 'FAILED'}")
                if not user_bound:
                    print(f"   Result: {user_conn.result}")
                user_conn.unbind()
            
            conn.unbind()
    except Exception as e:
        print(f"   LDAP ERROR: {e}")
        import traceback
        traceback.print_exc()

# 7. Test tenant resolution
print("\n7. Testing tenant resolution for docs.hjmmlegal.com...")

async def test_tenant():
    from modules.dms.services.webdav_auth import resolve_tenant_from_host
    result = await resolve_tenant_from_host("docs.hjmmlegal.com")
    print(f"   Result: {result}")

asyncio.run(test_tenant())

print("\n=== DONE ===")
