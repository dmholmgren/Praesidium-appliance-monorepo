# ── Timeslips configure page ──────────────────────────────────────────────────
# ADD THESE TWO ROUTES to modules/connectors/router.py
# Insert after the connector_save_config route (around line 120)

@router.get("/tenant-admin/connectors/timeslips/configure", response_class=HTMLResponse)
async def timeslips_configure_get(request: Request, user=Depends(get_current_user)):
    import secrets
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    connector = await ConnectorService.get_connector(tenant_id, "timeslips")

    # Load existing config
    config = connector.get("config", {}) if connector else {}

    # Load API key from credentials vault if exists
    api_key = None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT value FROM credentials_vault
                    WHERE tenant_id = :tid AND provider = 'timeslips' AND key_type = 'ingest_api_key'
                    LIMIT 1
                """),
                {"tid": tenant_id}
            )
            row = result.first()
            if row:
                api_key = row[0]
    except Exception:
        pass

    # Build ingest URL from request
    base_url = f"{request.url.scheme}://{request.url.hostname}"
    ingest_url = f"{base_url}/api/connectors/timeslips/ingest"

    nav = await get_nav_context(request, page="connectors")
    return _templates(request).TemplateResponse(request, "connectors/timeslips_configure.html", {
        "connector": connector,
        "config": config,
        "api_key": api_key,
        "ingest_url": ingest_url,
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("error"),
        "nav": nav,
        "branding": getattr(request.state, "branding", None),
        "user": user,
    })


@router.post("/tenant-admin/connectors/timeslips/configure")
async def timeslips_configure_post(
    request: Request,
    user=Depends(get_current_user),
    sync_frequency: str = Form("hourly"),
    config_fb_host: str = Form(""),
    config_fb_database: str = Form(""),
    config_fb_user: str = Form("SYSDBA"),
    config_fb_password: str = Form(""),
    config_import_from: str = Form(""),
):
    import secrets
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    config = {
        "fb_host": config_fb_host.strip(),
        "fb_database": config_fb_database.strip(),
        "fb_user": config_fb_user.strip(),
        "import_from": config_import_from.strip(),
    }
    # Store password in credentials_vault, not in config
    if config_fb_password.strip():
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO credentials_vault (id, tenant_id, provider, key_type, value)
                        VALUES (gen_random_uuid(), :tid, 'timeslips', 'fb_password', :val)
                        ON CONFLICT (tenant_id, provider, key_type)
                        DO UPDATE SET value = EXCLUDED.value
                    """),
                    {"tid": tenant_id, "val": config_fb_password.strip()}
                )
                await session.commit()
        except Exception as e:
            return RedirectResponse(
                f"/tenant-admin/connectors/timeslips/configure?error={str(e)[:80]}",
                status_code=303
            )

    # Generate API key if not exists
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT value FROM credentials_vault
                    WHERE tenant_id = :tid AND provider = 'timeslips' AND key_type = 'ingest_api_key'
                    LIMIT 1
                """),
                {"tid": tenant_id}
            )
            row = result.first()
            if not row:
                api_key = secrets.token_urlsafe(32)
                await session.execute(
                    text("""
                        INSERT INTO credentials_vault (id, tenant_id, provider, key_type, value)
                        VALUES (gen_random_uuid(), :tid, 'timeslips', 'ingest_api_key', :val)
                        ON CONFLICT (tenant_id, provider, key_type)
                        DO UPDATE SET value = EXCLUDED.value
                    """),
                    {"tid": tenant_id, "val": api_key}
                )
                await session.commit()
    except Exception:
        pass

    # Save connector config
    await ConnectorService.upsert_connector(
        tenant_id, "timeslips",
        enabled=True,
        sync_frequency=sync_frequency,
        config=config,
    )

    return RedirectResponse(
        "/tenant-admin/connectors/timeslips/configure?saved=1",
        status_code=303
    )
