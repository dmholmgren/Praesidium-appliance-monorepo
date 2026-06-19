"""
vault_put.py — write a credential into credentials_vault using the SAME Fernet
key derivation as praesidium_vault.get_api_key (so it round-trips correctly).

Secret entered at a hidden prompt; never in argv/history/files. Run with -it:
    docker exec -it praesidium-web python /app/jobs/vault_put.py runpod api_key
"""
import os
import sys
import uuid
import getpass

sys.path.insert(0, os.path.dirname(__file__))
from praesidium_vault import _derive_fernet_key, _get_db_conn  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
provider = sys.argv[1] if len(sys.argv) > 1 else "runpod"
key_type = sys.argv[2] if len(sys.argv) > 2 else "api_key"

raw = getpass.getpass(f"Enter {provider}/{key_type} value (hidden): ")
# Windows/clipboard pastes can arrive as UTF-16 (NUL between chars); strip NULs
# and surrounding whitespace. For ASCII keys this reconstructs the original.
value = raw.replace("\x00", "").strip()
if not value:
    print("empty value — aborting")
    sys.exit(1)
print(f"(received {len(value)} chars, starts with {value[:4]!r})")

enc = Fernet(_derive_fernet_key()).encrypt(value.encode()).decode()
hint = (value[:8] + "..." + value[-4:]) if len(value) >= 12 else "***"

conn = _get_db_conn()
cur = conn.cursor()
cur.execute(
    "DELETE FROM credentials_vault WHERE TRIM(tenant_id)=%s AND provider=%s AND key_type=%s",
    (TENANT, provider, key_type),
)
cur.execute(
    "INSERT INTO credentials_vault "
    "(id, tenant_id, provider, key_type, encrypted_key, key_hint, created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
    (str(uuid.uuid4()), TENANT, provider, key_type, enc, hint),
)
conn.commit()

back = Fernet(_derive_fernet_key()).decrypt(enc.encode()).decode()
print(f"stored {provider}/{key_type}  hint={hint}  roundtrip_ok={back == value}")
cur.close()
conn.close()
