"""
infra/
Praesidium Series 2.0 — Infrastructure Adapters

This package contains pluggable backends for infrastructure operations
that vary by deployment type:

  infra/proxy/    — reverse-proxy provisioning (nginx vhost write + reload)
  infra/storage/  — tenant storage root provisioning (mkdir on filesystem)

Each subpackage exposes a get_*_backend() factory. Backends are selected
at runtime via env vars:

  PROXY_BACKEND       local | ssh-rprx
  STORAGE_BACKEND     local | cifs

This separation keeps the data layer (jobs/provision_tenant.py) portable
across appliance, on-prem, HJMM production, and future cloud installs.
The data layer cares about WHAT to provision; infra cares about HOW.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""
