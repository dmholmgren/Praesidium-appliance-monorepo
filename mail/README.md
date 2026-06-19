# Praesidium Mail (Stalwart) — infrastructure-as-code

Three Stalwart instances run on the appliance (10.10.0.10). Only docker-compose +
sanitized config live here; **mail data and secrets are excluded** (see .gitignore).

| Dir | Container | Hostname | Backend / role |
|-----|-----------|----------|----------------|
| praesidium-mail | praesidium-mail | mail.hjmmlegal.com / imap.hjmmlegal.com | Firm mailbox, PostgreSQL-backed, DKIM signing |
| praesidium-mail-corp | praesidium-mail-corp | stalwart.praesidium.legal | Corp instance (RocksDB) |
| praesidium-mail-personal | praesidium-mail-personal | mail.theholmgrens.net | Personal mailbox (RocksDB) |

## Secrets (NOT in git)
- Recovery-admin + DB passwords are injected via `${RECOVERY_ADMIN_PASSWORD}` / `${STALWART_DB_PASSWORD}` (see `.env.example`).
- `praesidium-mail/config-json/config.json` DB password is set to `PLACEHOLDER_DB_PASSWORD` — replace at deploy.
- DKIM private key (`config/dkim/*.key`), TLS keys (`*.pem`), and the live `data/` stores are gitignored.

The deployed copies on the appliance keep the real values; this tree is the sanitized source of record.
