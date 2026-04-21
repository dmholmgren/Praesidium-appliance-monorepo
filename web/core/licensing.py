# core/licensing.py
# Praesidium Series 2.0
# ⚖  PATENT NOTICE: Patent Pending — 64/020,027
#    Filed March 29, 2026 by Dennis M. Holmgren, Reg. No. 54,168
#
# Feature licensing enforcement utility.
# RULE 12: Every licensed feature endpoint MUST call check_feature() before executing.
# RULE 12: Unlicensed features return HTTP 402 — NEVER 403.
#
# Logic (in order):
#  1. Check feature_overrides table — global kill switch.
#     If enabled_globally=False for this flag: return False regardless of tenant license.
#  2. Check tenant_licenses.feature_flags[feature_flag].
#     If tenant has no license record, or flag is absent/False: return False.
#  3. Both checks pass: return True.
#
# Usage in endpoint:
#   if not await check_feature(tenant_id, "feature_knowledge_graph"):
#       raise HTTPException(status_code=402, detail="Feature not licensed")
#
# Or use the decorator form:
#   @require_feature("feature_knowledge_graph")

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from fastapi import HTTPException, Request
from sqlalchemy import select, text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ── Canonical feature flag names ─────────────────────────────────────────────
# Reference these constants — never hardcode string literals in endpoints.
# Series 2.0 features

FEATURE_KNOWLEDGE_GRAPH         = "feature_knowledge_graph"
FEATURE_ISSUE_MAP_VERSIONING    = "feature_issue_map_versioning"
FEATURE_CASE_DRIFT_DETECTION    = "feature_case_drift_detection"
FEATURE_WIAM                    = "feature_wiam"
FEATURE_DEPO_PREP               = "feature_depo_prep"
FEATURE_AI_REVIEW_PASSES        = "feature_ai_review_passes"
FEATURE_TAG_INTELLIGENCE        = "feature_tag_intelligence"
FEATURE_DISCOVERY_POSTMORTEM    = "feature_discovery_postmortem"
FEATURE_PRODUCTION_BATES        = "feature_production_bates"
FEATURE_INTELLIGENCE_CHAT       = "feature_intelligence_chat"
FEATURE_CROSS_MATTER            = "feature_cross_matter"
FEATURE_GLOBAL_LEARNING_NETWORK = "feature_global_learning_network"
FEATURE_TENANT_ADMIN            = "feature_tenant_admin"
FEATURE_CASE_DRIFT_MAP_UI       = "feature_case_drift_map_ui"

# Series 1.0 features (included here for completeness)
FEATURE_EDISCOVERY              = "feature_ediscovery"
FEATURE_BILLING                 = "feature_billing"
FEATURE_COURT_CALENDAR          = "feature_court_calendar"
FEATURE_DMS                     = "feature_dms"
FEATURE_SMS_PORTAL              = "feature_sms_portal"


async def check_feature(tenant_id: str, feature_flag: str) -> bool:
    """Check whether a feature is enabled for a tenant.

    Steps:
      1. Query feature_overrides — if the flag is globally disabled, return False.
      2. Query tenant_licenses.feature_flags[feature_flag] — if missing or False, return False.
      3. Return True only if both checks pass.

    This function is safe to call in hot paths — it queries only two rows maximum
    and uses the existing async session pool.

    Args:
        tenant_id: The tenant's CHAR(36) UUID string.
        feature_flag: One of the FEATURE_* constants defined in this module.

    Returns:
        True if the feature is licensed and globally enabled, False otherwise.
        Never raises an exception — returns False on any DB error (fail-closed).
    """
    try:
        async with AsyncSessionLocal() as session:
            # Step 1: Global override check (kill switch)
            override_result = await session.execute(
                text(
                    "SELECT enabled_globally FROM feature_overrides "
                    "WHERE feature_flag = :flag"
                ),
                {"flag": feature_flag},
            )
            override_row = override_result.fetchone()
            if override_row is not None and not override_row[0]:
                logger.debug(
                    "feature_check: %s disabled globally via feature_overrides",
                    feature_flag,
                )
                return False

            # Step 2: Tenant license check
            license_result = await session.execute(
                text(
                    "SELECT feature_flags->:flag AS licensed "
                    "FROM tenant_licenses "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"flag": feature_flag, "tenant_id": tenant_id},
            )
            license_row = license_result.fetchone()

            if license_row is None:
                # No license record for this tenant
                logger.warning(
                    "feature_check: no license record for tenant_id=%s, feature=%s",
                    tenant_id,
                    feature_flag,
                )
                return False

            licensed_value = license_row[0]
            if licensed_value is None or licensed_value is False or licensed_value == "false":
                logger.debug(
                    "feature_check: %s not in license for tenant_id=%s",
                    feature_flag,
                    tenant_id,
                )
                return False

            return True

    except Exception:
        # Fail-closed: any DB error means the feature is not available.
        # Log at error level so this surfaces in monitoring.
        logger.exception(
            "feature_check: DB error checking %s for tenant_id=%s — failing closed",
            feature_flag,
            tenant_id,
        )
        return False


async def require_feature(tenant_id: str, feature_flag: str) -> None:
    """Raise HTTP 402 if the feature is not licensed.

    Use this for inline enforcement inside endpoint functions:
        await require_feature(tenant_id, FEATURE_WIAM)

    RULE 12: Unlicensed features return HTTP 402 — NEVER 403.
    """
    if not await check_feature(tenant_id, feature_flag):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "feature_not_licensed",
                "feature": feature_flag,
                "message": (
                    "This feature is not included in your current subscription. "
                    "Contact your administrator to upgrade."
                ),
            },
        )


def licensed(feature_flag: str) -> Callable:
    """FastAPI route dependency that enforces a feature license.

    Usage on an endpoint:
        @router.get("/wiam/{matter_id}", dependencies=[Depends(licensed(FEATURE_WIAM))])
        async def run_wiam(...):
            ...

    The dependency extracts tenant_id from request.state.tenant_id
    (set by the auth middleware).

    Returns HTTP 402 if the feature is not licensed.
    """
    async def _dependency(request: Request) -> None:
        tenant_id: str | None = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            raise HTTPException(
                status_code=401,
                detail="Authentication required — tenant context not established.",
            )
        await require_feature(tenant_id, feature_flag)

    return _dependency


async def get_tenant_features(tenant_id: str) -> dict[str, bool]:
    """Return all feature flags for a tenant (licensed + global overrides applied).

    Returns a dict of {feature_flag: bool} for all known features.
    Useful for building feature-aware UI menus.
    """
    all_flags = [
        FEATURE_KNOWLEDGE_GRAPH,
        FEATURE_ISSUE_MAP_VERSIONING,
        FEATURE_CASE_DRIFT_DETECTION,
        FEATURE_WIAM,
        FEATURE_DEPO_PREP,
        FEATURE_AI_REVIEW_PASSES,
        FEATURE_TAG_INTELLIGENCE,
        FEATURE_DISCOVERY_POSTMORTEM,
        FEATURE_PRODUCTION_BATES,
        FEATURE_INTELLIGENCE_CHAT,
        FEATURE_CROSS_MATTER,
        FEATURE_GLOBAL_LEARNING_NETWORK,
        FEATURE_TENANT_ADMIN,
        FEATURE_CASE_DRIFT_MAP_UI,
        FEATURE_EDISCOVERY,
        FEATURE_BILLING,
        FEATURE_COURT_CALENDAR,
        FEATURE_DMS,
        FEATURE_SMS_PORTAL,
    ]

    try:
        async with AsyncSessionLocal() as session:
            # Load global overrides in one query
            overrides_result = await session.execute(
                text("SELECT feature_flag, enabled_globally FROM feature_overrides")
            )
            global_overrides: dict[str, bool] = {
                row[0]: bool(row[1]) for row in overrides_result.fetchall()
            }

            # Load tenant license flags
            license_result = await session.execute(
                text(
                    "SELECT feature_flags FROM tenant_licenses WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
            license_row = license_result.fetchone()
            tenant_flags: dict[str, Any] = license_row[0] if license_row else {}

        result: dict[str, bool] = {}
        for flag in all_flags:
            # Global kill switch takes priority
            if flag in global_overrides and not global_overrides[flag]:
                result[flag] = False
                continue
            # Tenant license
            raw = tenant_flags.get(flag)
            result[flag] = bool(raw) if raw is not None else False

        return result

    except Exception:
        logger.exception("get_tenant_features failed for tenant_id=%s", tenant_id)
        return {flag: False for flag in all_flags}


async def provision_tenant_license(
    tenant_id: str,
    tier: str,
    feature_flags: dict[str, bool],
    billing_plan: str | None = None,
    notes: str | None = None,
    expires_at: str | None = None,
) -> bool:
    """Create or replace a tenant's license record.

    Called by the admin provisioning panel (Chat 8 — Tenant Admin).
    Creates a tenant_provisioning_log entry for every change.

    Args:
        tenant_id: Tenant CHAR(36) UUID.
        tier: foundation|litigation|intelligence|enterprise
        feature_flags: dict of {flag_name: True|False}
        billing_plan: Optional billing plan identifier.
        notes: Optional admin notes.
        expires_at: Optional ISO timestamp string for license expiry.

    Returns:
        True on success, False on failure.
    """
    import json
    import uuid

    try:
        async with AsyncSessionLocal() as session:
            # Check for existing record
            existing_result = await session.execute(
                text(
                    "SELECT id, tier, feature_flags FROM tenant_licenses "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
            existing = existing_result.fetchone()
            prior_state = (
                {"tier": existing[1], "feature_flags": existing[2]} if existing else None
            )

            if existing:
                await session.execute(
                    text(
                        "UPDATE tenant_licenses "
                        "SET tier = :tier, feature_flags = :flags, "
                        "    billing_plan = :plan, notes = :notes, "
                        "    expires_at = :expires_at "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {
                        "tier": tier,
                        "flags": json.dumps(feature_flags),
                        "plan": billing_plan,
                        "notes": notes,
                        "expires_at": expires_at,
                        "tenant_id": tenant_id,
                    },
                )
                action = "upgraded" if tier > (existing[1] or "") else "updated"
            else:
                await session.execute(
                    text(
                        "INSERT INTO tenant_licenses "
                        "(id, tenant_id, tier, feature_flags, billing_plan, notes, expires_at) "
                        "VALUES (:id, :tenant_id, :tier, :flags, :plan, :notes, :expires_at)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tenant_id": tenant_id,
                        "tier": tier,
                        "flags": json.dumps(feature_flags),
                        "plan": billing_plan,
                        "notes": notes,
                        "expires_at": expires_at,
                    },
                )
                action = "created"

            # Audit log entry
            await session.execute(
                text(
                    "INSERT INTO tenant_provisioning_log "
                    "(id, tenant_id, action, performed_by, prior_state, new_state) "
                    "VALUES (:id, :tenant_id, :action, :performed_by, :prior, :new)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "action": action,
                    "performed_by": "system:provision_tenant_license",
                    "prior": json.dumps(prior_state),
                    "new": json.dumps({"tier": tier, "feature_flags": feature_flags}),
                },
            )
            await session.commit()
            logger.info(
                "provision_tenant_license: %s tenant_id=%s tier=%s",
                action,
                tenant_id,
                tier,
            )
            return True

    except Exception:
        logger.exception(
            "provision_tenant_license failed for tenant_id=%s", tenant_id
        )
        return False
