"""
jobs/ssl_provision.py
RQ job — SSL Certificate Provisioning
Handles:
  - letsencrypt mode: certbot certonly --nginx for custom domains
  - custom mode: write uploaded cert+key bytes to RPRX-01 via SSH
  - wildcard (praesidium-legal.com): covered by existing cert, no action
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

log = logging.getLogger(__name__)

RPRX_HOST = os.environ.get("RPRX_HOST", "10.10.40.50")
CERT_BASE_DIR = "/etc/praesidium/certs"


def _ssh_run(cmd: str, input_bytes: bytes | None = None, timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"praesidium@{RPRX_HOST}", cmd],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def provision_letsencrypt_custom(domain: str, slug: str) -> dict:
    """
    Run certbot for a custom domain (not praesidium-legal.com).
    Cert stored at /etc/letsencrypt/live/{domain}/.
    """
    cmd = (
        f"sudo certbot certonly --nginx "
        f"-d {domain} "
        f"--non-interactive --agree-tos --email admin@praesidium.legal "
        f"--redirect"
    )
    rc, stdout, stderr = _ssh_run(cmd, timeout=180)
    if rc != 0:
        log.error("certbot failed for %s: %s", domain, stderr)
        return {"status": "failed", "error": stderr}

    # Write cert paths back to DB is caller's responsibility via provision_tenant
    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"
    log.info("certbot success for %s: %s", domain, stdout)
    return {
        "status": "ok",
        "cert_path": cert_path,
        "key_path": key_path,
    }


def provision_custom_cert(slug: str, cert_pem: bytes, key_pem: bytes) -> dict:
    """
    Write uploaded cert+key to RPRX-01 at /etc/praesidium/certs/{slug}/.
    Returns paths for storage in tenants table.
    """
    cert_dir = f"{CERT_BASE_DIR}/{slug}"
    cert_path = f"{cert_dir}/fullchain.pem"
    key_path = f"{cert_dir}/privkey.pem"

    # Ensure dir exists
    rc, _, err = _ssh_run(f"sudo mkdir -p {cert_dir} && sudo chmod 700 {cert_dir}")
    if rc != 0:
        return {"status": "failed", "error": err}

    # Write cert
    rc, _, err = _ssh_run(f"sudo tee {cert_path} > /dev/null", input_bytes=cert_pem)
    if rc != 0:
        return {"status": "failed", "error": err}

    # Write key
    rc, _, err = _ssh_run(f"sudo tee {key_path} > /dev/null", input_bytes=key_pem)
    if rc != 0:
        return {"status": "failed", "error": err}

    # Restrict permissions
    _ssh_run(f"sudo chmod 600 {key_path} && sudo chmod 644 {cert_path}")

    log.info("Custom cert written for %s at %s", slug, cert_dir)
    return {"status": "ok", "cert_path": cert_path, "key_path": key_path}


def get_cert_expiry(cert_path: str) -> str | None:
    """
    SSH to RPRX-01 and read cert expiry via openssl.
    Returns ISO date string or None on failure.
    """
    cmd = f"openssl x509 -in {cert_path} -noout -enddate 2>/dev/null"
    rc, stdout, _ = _ssh_run(cmd, timeout=15)
    if rc != 0 or not stdout.strip():
        return None
    # notAfter=Mar 31 00:00:00 2027 GMT
    try:
        raw = stdout.strip().split("=", 1)[1]
        from datetime import datetime
        dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return stdout.strip()
