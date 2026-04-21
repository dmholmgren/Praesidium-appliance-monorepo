"""Fix AICapExceededError -> AICapBreach in billing chat endpoint."""
from pathlib import Path

p = Path("/app/modules/billing/api/billing_routes.py")
src = p.read_text()

old_import = """    from modules.intelligence import (
        call as ai_call,
        AICallContext,
        AIKeyMissingError,
        AICapExceededError,
    )"""

new_import = """    from modules.intelligence import (
        call as ai_call,
        AICallContext,
        AIKeyMissingError,
        AICapBreach,
        AIRoutingMissingError,
    )"""

if old_import not in src:
    print("ERROR: import block not found — file may have drifted")
    raise SystemExit(1)
src = src.replace(old_import, new_import, 1)
print("  OK  import block patched")

old_except = """    except AICapExceededError as exc:
        text_response = (
            f\"\u26a0 Daily AI budget reached: {exc}. Contact a partner to \"
            \"increase limits or try again tomorrow.\"
        )"""

new_except = """    except AICapBreach as exc:
        text_response = (
            f\"\u26a0 Daily AI budget reached: {exc}. Contact a partner to \"
            \"increase limits or try again tomorrow.\"
        )
    except AIRoutingMissingError as exc:
        text_response = (
            f\"\u26a0 AI routing not configured for this module: {exc}. \"
            \"Contact admin.\"
        )"""

if old_except not in src:
    print("ERROR: except block not found — file may have drifted")
    raise SystemExit(1)
src = src.replace(old_except, new_except, 1)
print("  OK  except block patched")

p.write_text(src)

import ast
ast.parse(src)
print("  OK  billing_routes.py parses clean")

# Verify our changes are actually on disk
check = p.read_text()
breach_count = check.count("AICapBreach")
old_count = check.count("AICapExceededError")
print(f"\nVerification: AICapBreach={breach_count}  AICapExceededError={old_count}")
if old_count > 0:
    print("  FAIL: old name still present")
    raise SystemExit(1)
print("  OK   no stale references")
