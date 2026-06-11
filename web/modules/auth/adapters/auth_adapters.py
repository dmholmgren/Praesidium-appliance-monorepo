"""
COMP 16 — LDAPSAuthAdapter
COMP 17 — AzureADAuthAdapter

REFACTORED for multi-tenant: adapters now accept optional config and
credentials dicts resolved from tenant_connectors + credentials_vault.
When provided, DB config is used. When omitted, falls back to os.environ
for backward compatibility with production systems not yet migrated.

Config resolution (per adapter):
  1. config dict from tenant_connectors.config (passed by routes.py)
  2. credentials dict from credentials_vault (passed by routes.py)
  3. os.environ fallback (legacy single-tenant path)
"""

import os
import ssl
import logging
from datetime import datetime, timezone
from typing import Optional

from core.services.auth import AuthService, AuthResult, UserInfo

logger = logging.getLogger(__name__)


def _cfg(config: dict, credentials: dict, config_key: str,
         credential_key: str = None, env_key: str = None,
         default: str = "") -> str:
    """
    Resolve a config value with priority:
      1. credentials dict (for secrets like passwords, bind DNs)
      2. config dict (for non-secret config like URLs, DNs)
      3. os.environ (legacy fallback)
      4. default

    This single helper replaces all the os.environ.get() calls
    throughout the adapters.
    """
    # Secrets first — credentials_vault has bind_dn, bind_password, etc.
    if credential_key and credentials and credential_key in credentials:
        return str(credentials[credential_key])
    # Config from tenant_connectors.config JSONB
    if config and config_key in config:
        v = config[config_key]
        if isinstance(v, bool):
            return str(v).lower()
        return str(v) if v is not None else default
    # Env var fallback
    if env_key:
        return os.environ.get(env_key, default)
    return default


# ═══════════════════════════════════════════════════════════════
# COMP 16 — LDAPS AUTH ADAPTER
# ═══════════════════════════════════════════════════════════════

class LDAPSAuthAdapter(AuthService):
    """
    Authenticates users against Active Directory via LDAPS.

    Multi-tenant: accepts config and credentials dicts from the
    tenant_connectors + credentials_vault resolver in routes.py.
    Falls back to env vars when called without config (backward compat).
    """

    def __init__(self, config: dict = None, credentials: dict = None):
        c = config or {}
        cr = credentials or {}

        self.ldap_url = _cfg(c, cr, "ldap_url", env_key="LDAP_URL")
        self.base_dn = _cfg(c, cr, "base_dn", env_key="LDAP_BASE_DN")
        self.bind_dn = _cfg(c, cr, "bind_dn", credential_key="bind_dn",
                            env_key="LDAP_BIND_DN")
        self.bind_password = _cfg(c, cr, "bind_password",
                                  credential_key="bind_password",
                                  env_key="LDAP_BIND_PASSWORD")
        self.user_search_base = _cfg(
            c, cr, "user_search_base",
            env_key="LDAP_USER_SEARCH_BASE",
            default=f"OU=Users,{self.base_dn}",
        )
        self.group_search_base = _cfg(
            c, cr, "group_search_base",
            env_key="LDAP_GROUP_SEARCH_BASE",
            default=f"OU=Groups,{self.base_dn}",
        )
        self.user_filter = _cfg(
            c, cr, "user_filter",
            env_key="LDAP_USER_FILTER",
            default="(&(objectClass=user)(sAMAccountName={username}))",
        )

        verify_raw = _cfg(c, cr, "verify_cert", env_key="LDAP_VERIFY_CERT",
                          default="false")
        self.verify_cert = verify_raw.lower() in ("true", "1", "yes")

    def _get_connection(self, bind_dn=None, bind_password=None):
        """Create LDAP connection with TLS."""
        import ldap3
        from ldap3 import Server, Connection, SUBTREE, Tls

        tls_config = Tls(
            validate=ssl.CERT_REQUIRED if self.verify_cert else ssl.CERT_NONE,
        )

        server = Server(
            self.ldap_url,
            use_ssl=True,
            tls=tls_config,
            get_info=ldap3.ALL,
        )

        conn = Connection(
            server,
            user=bind_dn or self.bind_dn,
            password=bind_password or self.bind_password,
            auto_bind=True,
            raise_exceptions=True,
        )
        return conn

    async def authenticate(self, username: str, password: str,
                           tenant_id: str) -> AuthResult:
        """
        Authenticate against AD:
        1. Bind with service account to find user DN
        2. Re-bind with user's DN + password to validate credentials
        3. Return user info + groups on success
        """
        import ldap3
        from ldap3 import SUBTREE

        try:
            # Step 1: Find user DN with service account
            conn = self._get_connection()
            user_filter = self.user_filter.replace("{username}", username)

            conn.search(
                search_base=self.user_search_base,
                search_filter=user_filter,
                search_scope=SUBTREE,
                attributes=[
                    "distinguishedName", "sAMAccountName", "mail",
                    "displayName", "memberOf", "department", "title",
                    "telephoneNumber", "userAccountControl",
                ],
            )

            if not conn.entries:
                conn.unbind()
                return AuthResult(success=False, error="User not found in directory")

            entry = conn.entries[0]
            user_dn = str(entry.distinguishedName)
            conn.unbind()

            # Step 2: Validate credentials by binding as the user
            try:
                user_conn = self._get_connection(
                    bind_dn=user_dn,
                    bind_password=password,
                )
                user_conn.unbind()
            except Exception:
                return AuthResult(success=False, error="Invalid credentials")

            # Step 3: Extract user info
            groups = []
            if hasattr(entry, "memberOf") and entry.memberOf:
                for group_dn in entry.memberOf:
                    cn = str(group_dn).split(",")[0].replace("CN=", "")
                    groups.append(cn)

            email = str(entry.mail) if hasattr(entry, "mail") and entry.mail else ""
            display_name = str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else username

            # Check if account is disabled
            uac = int(str(entry.userAccountControl)) if hasattr(entry, "userAccountControl") else 0
            if uac & 0x0002:  # ACCOUNTDISABLE flag
                return AuthResult(success=False, error="Account is disabled")

            return AuthResult(
                success=True,
                user_id=None,
                username=username,
                email=email,
                display_name=display_name,
                groups=groups,
                metadata={
                    "department": str(entry.department) if hasattr(entry, "department") and entry.department else "",
                    "title": str(entry.title) if hasattr(entry, "title") and entry.title else "",
                    "phone": str(entry.telephoneNumber) if hasattr(entry, "telephoneNumber") and entry.telephoneNumber else "",
                },
            )

        except Exception as e:
            logger.error(f"LDAP authentication error: {e}")
            return AuthResult(success=False, error=f"Authentication service error: {str(e)}")

    async def get_user_info(self, username: str,
                            tenant_id: str) -> Optional[UserInfo]:
        """Look up a user in Active Directory."""
        import ldap3
        from ldap3 import SUBTREE

        try:
            conn = self._get_connection()
            user_filter = self.user_filter.replace("{username}", username)

            conn.search(
                search_base=self.user_search_base,
                search_filter=user_filter,
                search_scope=SUBTREE,
                attributes=[
                    "sAMAccountName", "mail", "displayName", "memberOf",
                    "department", "title", "telephoneNumber", "userAccountControl",
                ],
            )

            if not conn.entries:
                conn.unbind()
                return None

            entry = conn.entries[0]
            groups = []
            if hasattr(entry, "memberOf") and entry.memberOf:
                for group_dn in entry.memberOf:
                    cn = str(group_dn).split(",")[0].replace("CN=", "")
                    groups.append(cn)

            uac = int(str(entry.userAccountControl)) if hasattr(entry, "userAccountControl") else 0

            info = UserInfo(
                username=username,
                email=str(entry.mail) if hasattr(entry, "mail") and entry.mail else "",
                display_name=str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else username,
                groups=groups,
                department=str(entry.department) if hasattr(entry, "department") and entry.department else "",
                title=str(entry.title) if hasattr(entry, "title") and entry.title else "",
                phone=str(entry.telephoneNumber) if hasattr(entry, "telephoneNumber") and entry.telephoneNumber else "",
                enabled=not bool(uac & 0x0002),
            )
            conn.unbind()
            return info

        except Exception as e:
            logger.error(f"LDAP user lookup error: {e}")
            return None

    async def list_users(self, tenant_id: str, search: str = "") -> list[UserInfo]:
        """List all users from Active Directory using plain conn.search().

        Earlier we tried extend.standard.paged_search(generator=True) —
        it hung in this environment. Sub-1000-user directories don't need
        paging (AD default size limit is 1000). Keeping plain search().

        Errors are propagated (not swallowed) so the sync log captures them.
        Diagnostic INFO logs at start and on completion.
        """
        import ldap3
        from ldap3 import SUBTREE

        conn = self._get_connection()
        try:
            search_filter = "(&(objectClass=user)(objectCategory=person)"
            if search:
                search_filter += f"(|(sAMAccountName=*{search}*)(displayName=*{search}*)(mail=*{search}*))"
            search_filter += ")"

            logger.info(
                f"[ldap_list_users] base={self.user_search_base!r} "
                f"filter={search_filter!r} bind_dn={self.bind_dn!r}"
            )

            ok = conn.search(
                search_base=self.user_search_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=[
                    "sAMAccountName", "mail", "displayName",
                    "department", "title", "userAccountControl",
                ],
            )
            result_desc = conn.result.get("description") if conn.result else None
            logger.info(
                f"[ldap_list_users] search ok={ok} "
                f"entries={len(conn.entries)} result={result_desc!r}"
            )

            users = []
            for entry in conn.entries:
                # ldap3 entries expose attributes via .value; safer than getattr
                # which raises LDAPCursorError on missing attributes.
                def _v(name):
                    if not hasattr(entry, name):
                        return ""
                    val = getattr(entry, name).value
                    return val if val is not None else ""

                uac_raw = _v("userAccountControl")
                uac = int(uac_raw) if str(uac_raw).strip() else 0
                users.append(UserInfo(
                    username=str(_v("sAMAccountName")),
                    email=str(_v("mail")),
                    display_name=str(_v("displayName")),
                    department=str(_v("department")),
                    title=str(_v("title")),
                    enabled=not bool(uac & 0x0002),
                ))

            logger.info(f"[ldap_list_users] returning {len(users)} users")
            return users

        except Exception as e:
            logger.exception(
                f"[ldap_list_users] FAILED: {type(e).__name__}: {e}"
            )
            raise
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    async def sync_users(self, tenant_id: str) -> int:
        """Sync all AD users to the local users table."""
        ad_users = await self.list_users(tenant_id)
        return len(ad_users)


# ═══════════════════════════════════════════════════════════════
# COMP 17 — AZURE AD AUTH ADAPTER
# ═══════════════════════════════════════════════════════════════

class AzureADAuthAdapter(AuthService):
    """
    OIDC/OAuth2 authentication via Azure AD (Entra ID).

    Multi-tenant: accepts config and credentials dicts.
    Falls back to env vars for backward compat.
    """

    def __init__(self, config: dict = None, credentials: dict = None):
        c = config or {}
        cr = credentials or {}

        self.azure_tenant_id = _cfg(c, cr, "azure_tenant_id",
                                    credential_key="tenant_id",
                                    env_key="AZURE_AD_TENANT_ID")
        self.client_id = _cfg(c, cr, "client_id",
                              credential_key="client_id",
                              env_key="AZURE_AD_CLIENT_ID")
        self.client_secret = _cfg(c, cr, "client_secret",
                                  credential_key="client_secret",
                                  env_key="AZURE_AD_CLIENT_SECRET")
        self.authority = f"https://login.microsoftonline.com/{self.azure_tenant_id}"
        self.token_url = f"{self.authority}/oauth2/v2.0/token"
        self.authorize_url = f"{self.authority}/oauth2/v2.0/authorize"
        self.graph_url = "https://graph.microsoft.com/v1.0"
        self.scopes = ["User.Read", "User.ReadBasic.All"]

    async def authenticate(self, username: str, password: str,
                           tenant_id: str) -> AuthResult:
        """Authenticate via ROPC flow."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "password",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "username": username,
                        "password": password,
                        "scope": "openid profile email User.Read",
                    },
                )

                if resp.status_code != 200:
                    error = resp.json().get("error_description", "Authentication failed")
                    return AuthResult(success=False, error=error)

                tokens = resp.json()
                access_token = tokens.get("access_token", "")

                profile_resp = await client.get(
                    f"{self.graph_url}/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                if profile_resp.status_code != 200:
                    return AuthResult(
                        success=True, username=username,
                        email=username, display_name=username,
                    )

                profile = profile_resp.json()

                groups_resp = await client.get(
                    f"{self.graph_url}/me/memberOf",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                groups = []
                if groups_resp.status_code == 200:
                    for g in groups_resp.json().get("value", []):
                        if g.get("@odata.type") == "#microsoft.graph.group":
                            groups.append(g.get("displayName", ""))

                return AuthResult(
                    success=True,
                    username=profile.get("userPrincipalName", username),
                    email=profile.get("mail", profile.get("userPrincipalName", "")),
                    display_name=profile.get("displayName", username),
                    groups=groups,
                    metadata={
                        "azure_id": profile.get("id", ""),
                        "job_title": profile.get("jobTitle", ""),
                        "department": profile.get("department", ""),
                        "access_token": access_token,
                        "refresh_token": tokens.get("refresh_token", ""),
                    },
                )

        except Exception as e:
            logger.error(f"Azure AD authentication error: {e}")
            return AuthResult(success=False, error=str(e))

    async def get_user_info(self, username: str,
                            tenant_id: str) -> Optional[UserInfo]:
        """Look up user in Azure AD via Graph API."""
        import httpx

        token = await self._get_app_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.graph_url}/users/{username}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    return None

                profile = resp.json()
                return UserInfo(
                    username=profile.get("userPrincipalName", ""),
                    email=profile.get("mail", ""),
                    display_name=profile.get("displayName", ""),
                    department=profile.get("department", ""),
                    title=profile.get("jobTitle", ""),
                    phone=profile.get("businessPhones", [""])[0] if profile.get("businessPhones") else "",
                    enabled=profile.get("accountEnabled", True),
                )
        except Exception as e:
            logger.error(f"Azure AD user lookup error: {e}")
            return None

    async def list_users(self, tenant_id: str, search: str = "") -> list[UserInfo]:
        """List users from Azure AD."""
        import httpx

        token = await self._get_app_token()
        if not token:
            return []

        try:
            params = {"$top": 500, "$select": "userPrincipalName,displayName,mail,department,jobTitle,accountEnabled"}
            if search:
                params["$filter"] = f"startswith(displayName,'{search}') or startswith(mail,'{search}')"

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.graph_url}/users",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    return []

                return [
                    UserInfo(
                        username=u.get("userPrincipalName", ""),
                        email=u.get("mail", ""),
                        display_name=u.get("displayName", ""),
                        department=u.get("department", ""),
                        title=u.get("jobTitle", ""),
                        enabled=u.get("accountEnabled", True),
                    )
                    for u in resp.json().get("value", [])
                ]
        except Exception as e:
            logger.error(f"Azure AD list users error: {e}")
            return []

    async def sync_users(self, tenant_id: str) -> int:
        """Sync Azure AD users to local users table."""
        ad_users = await self.list_users(tenant_id)
        return len(ad_users)

    async def _get_app_token(self) -> Optional[str]:
        """Get application-level token for Graph API calls."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                )
                if resp.status_code == 200:
                    return resp.json().get("access_token")
        except Exception as e:
            logger.error(f"Failed to get Azure AD app token: {e}")
        return None
