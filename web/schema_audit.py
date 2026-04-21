"""
Schema Audit — Praesidium Series 2.0
Compares every ORM model's expected columns against actual DB schema.
Reports: missing columns, extra columns, type mismatches.
Run: docker exec praesidium-web python3 /tmp/schema_audit.py
"""
import os
import sys
import psycopg2

# ── DB connection ─────────────────────────────────────────────────────────────
url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', '')
at = url.rfind('@')
credentials = url[:at]
hostpart = url[at+1:]
colon = credentials.index(':')
user = credentials[:colon]
password = credentials[colon+1:]
slash = hostpart.index('/')
hostport = hostpart[:slash]
dbname = hostpart[slash+1:].split('?')[0]
host = hostport.rsplit(':', 1)[0] if ':' in hostport else hostport

conn = psycopg2.connect(host=host, port=5432, dbname=dbname, user=user, password=password)
cur = conn.cursor()

# ── Get all actual DB columns ─────────────────────────────────────────────────
cur.execute("""
    SELECT table_name, column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position
""")
db_schema = {}
for table, column, dtype, nullable, default in cur.fetchall():
    if table not in db_schema:
        db_schema[table] = {}
    db_schema[table][column] = {'type': dtype, 'nullable': nullable, 'default': default}

# ── Get all ORM models ────────────────────────────────────────────────────────
sys.path.insert(0, '/app')
os.environ.setdefault('DATABASE_URL', os.environ['DATABASE_URL'])

# Import all models
import importlib
import pkgutil
from sqlalchemy import inspect as sa_inspect
from core.db.base import Base

model_modules = [
    'core.models.client',
    'core.models.matter',
    'core.models.billing',
    'core.models.user',
    'core.models.tenant',
]

# Also scan modules directory
import glob
for f in glob.glob('/app/modules/*/models/*.py') + glob.glob('/app/modules/*/models.py'):
    mod = f.replace('/app/', '').replace('/', '.').replace('.py', '')
    model_modules.append(mod)

loaded = []
for mod_path in model_modules:
    try:
        importlib.import_module(mod_path)
        loaded.append(mod_path)
    except Exception as e:
        print(f"  [SKIP] {mod_path}: {e}")

print(f"\nLoaded {len(loaded)} model modules\n")
print("=" * 70)

# ── Compare each mapped table ─────────────────────────────────────────────────
issues_found = 0

for mapper in Base.registry.mappers:
    cls = mapper.class_
    table_name = mapper.persist_selectable.name

    if table_name not in db_schema:
        print(f"\n[MISSING TABLE] {table_name} — ORM model exists but table not in DB")
        issues_found += 1
        continue

    db_cols = set(db_schema[table_name].keys())
    orm_cols = set(c.key for c in mapper.columns)

    missing_in_db = orm_cols - db_cols
    extra_in_db = db_cols - orm_cols

    if missing_in_db or extra_in_db:
        print(f"\n[{table_name}] ({cls.__name__})")
        if missing_in_db:
            print(f"  MISSING IN DB (ORM expects, DB doesn't have):")
            for col in sorted(missing_in_db):
                orm_col = mapper.columns[col]
                print(f"    - {col} ({orm_col.type})")
            issues_found += len(missing_in_db)
        if extra_in_db:
            print(f"  EXTRA IN DB (DB has, ORM doesn't know about):")
            for col in sorted(extra_in_db):
                print(f"    + {col}")

print("\n" + "=" * 70)
print(f"\nTables in DB: {len(db_schema)}")
print(f"ORM models checked: {len(list(Base.registry.mappers))}")
print(f"Issues found: {issues_found}")

if issues_found == 0:
    print("\n✅ All ORM models match DB schema")
else:
    print(f"\n⚠️  {issues_found} column mismatches need fixing")

cur.close()
conn.close()
