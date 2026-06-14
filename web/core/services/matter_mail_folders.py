"""
core/services/matter_mail_folders.py
Publish a user's selected matters as IMAP folders under "Matters/" in their
Stalwart mailbox, via the JMAP management API (same recovery-admin auth as
stalwart_service). Idempotent: creates missing folders, prunes de-selected.

Selection is stored per-user in users.user_preferences->'mail_matter_ids'.
"""
from __future__ import annotations
import os, logging
from typing import Optional
import httpx

log = logging.getLogger("praesidium.services.matter_folders")

JMAP_URL = os.getenv("STALWART_JMAP_URL", "http://10.10.0.10:8080/jmap")
ADMIN_USER = os.getenv("STALWART_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("STALWART_ADMIN_PASS", "Praesidium2026!")
PARENT_NAME = "Matters"

_P = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:principals", "urn:stalwart:jmap"]
_M = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]


async def _jmap(using: list, calls: list, timeout: float = 20.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(JMAP_URL, json={"using": using, "methodCalls": calls},
                         auth=(ADMIN_USER, ADMIN_PASS))
        r.raise_for_status()
        return r.json()


def folder_name(matter_number: str, matter_name: str) -> str:
    return f"{matter_number} {matter_name}".replace("/", "-").strip()[:120]


async def _account_id(email: str) -> Optional[str]:
    name = email.split("@")[0].lower().strip()
    resp = await _jmap(_P, [["x:Account/get", {}, "t"]])
    for a in resp["methodResponses"][0][1].get("list", []):
        if a.get("name", "").lower() == name:
            return a["id"]
    return None


async def sync_matter_folders(email: str, matters: list[tuple], prune: bool = True) -> dict:
    """matters = list of (matter_number, matter_name). Returns summary dict."""
    aid = await _account_id(email)
    if not aid:
        return {"success": False, "error": f"no mailbox for {email}"}
    want = {folder_name(n, nm) for n, nm in matters}

    resp = await _jmap(_M, [["Mailbox/get", {"accountId": aid,
                            "properties": ["id", "name", "parentId"]}, "t"]])
    boxes = resp["methodResponses"][0][1].get("list", [])
    parent = next((b["id"] for b in boxes if b["name"] == PARENT_NAME and not b.get("parentId")), None)
    if not parent and want:
        mr = (await _jmap(_M, [["Mailbox/set", {"accountId": aid,
              "create": {"p": {"name": PARENT_NAME, "parentId": None}}}, "t"]]))["methodResponses"][0][1]
        parent = mr.get("created", {}).get("p", {}).get("id")
    children = {b["name"]: b["id"] for b in boxes if b.get("parentId") == parent}

    created, removed = [], []
    for nm in want:
        if nm not in children and parent:
            mr = (await _jmap(_M, [["Mailbox/set", {"accountId": aid,
                  "create": {"c": {"name": nm, "parentId": parent}}}, "t"]]))["methodResponses"][0][1]
            if "c" in mr.get("created", {}):
                created.append(nm)
    if prune:
        for nm, mid in children.items():
            if nm not in want:
                mr = (await _jmap(_M, [["Mailbox/set", {"accountId": aid,
                      "onDestroyRemoveEmails": False, "destroy": [mid]}, "t"]]))["methodResponses"][0][1]
                if mid in mr.get("destroyed", []):
                    removed.append(nm)
    log.info("matter folders %s: +%d -%d", email, len(created), len(removed))
    return {"success": True, "created": created, "removed": removed, "total": len(want)}
