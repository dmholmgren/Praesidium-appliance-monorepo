from sqlalchemy import text as sa_text
"""
COMP 16 — LDAPSAuthAdapter
LDAP_URL from .env (dc01.hjmmlegal.com for HJMM test bed).
Authenticates against Active Directory over LDAPS (port 636).
Syncs users and groups from AD to local users table.

COMP 17 — AzureADAuthAdapter
OIDC/OAuth2 flow for Azure AD / Entra ID tenants.
Tenant config driven — CLIENT_ID, TENANT_ID, CLIENT_SECRET from .env.
"""

import os
import ssl
import logging
from datetime import datetime, timezone
from typing import Optional

from core.services.auth import AuthService, AuthResult, UserInfo

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# COMP 16 — LDAPS AUTH ADAPTER
# ═══════════════════════════════════════════════════════════════

class LDAPSAuthAdapter(AuthService):
    """
    Authenticates users against Active Directory via LDAPS.
    LDAP_URL from environment — e.g. ldaps://dc01.hjmmlegal.com:636
    LDAP_BASE_DN from environment — e.g. DC=hjmmlegal,DC=com
    LDAP_BIND_DN — service account for searching
    LDAP_BIND_PASSWORD — service account password
    """

    def __init__(self):
        self.ldap_url = os.environ["LDAP_URL"]  # ldaps://dc01.hjmmlegal.com:636
        self.base_dn = os.environ["LDAP_BASE_DN"]  # DC=hjmmlegal,DC=com
        self.bind_dn = os.environ.get("LDAP_BIND_DN", "")
        self.bind_password = os.environ.get("LDAP_BIND_PASSWORD", "")
        self.user_search_base = os.environ.get(
            "LDAP_USER_SEARCH_BASE",
            f"OU=Users,{self.base_dn}",
        )
        self.group_search_base = os.environ.get(
            "LDAP_GROUP_SEARCH_BASE",
            f"OU=Groups,{self.base_dn}",
        )
        self.user_filter = os.environ.get(
            "LDAP_USER_FILTER",
            "(&(objectClass=user)(sAMAccountName={username}))",
        )
        self.verify_cert = os.environ.get("LDAP_VERIFY_CERT", "true").lower() == "true"

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
                    # Extract CN from DN
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
                user_id=None,  # Will be resolved by auth middleware
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
        """List all users from Active Directory."""
        import ldap3
        from ldap3 import SUBTREE

        try:
            conn = self._get_connection()
            search_filter = "(&(objectClass=user)(objectCategory=person)"
            if search:
                search_filter += f"(|(sAMAccountName=*{search}*)(displayName=*{search}*)(mail=*{search}*))"
            search_filter += ")"

            conn.search(
                search_base=self.user_search_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=[
                    "sAMAccountName", "mail", "displayName",
                    "department", "title", "userAccountControl",
                ],
                paged_size=500,
            )

            users = []
            for entry in conn.entries:
                uac = int(str(entry.userAccountControl)) if hasattr(entry, "userAccountControl") else 0
                users.append(UserInfo(
                    username=str(entry.sAMAccountName),
                    email=str(entry.mail) if hasattr(entry, "mail") and entry.mail else "",
                    display_name=str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else "",
                    department=str(entry.department) if hasattr(entry, "department") and entry.department else "",
                    title=str(entry.title) if hasattr(entry, "title") and entry.title else "",
                    enabled=not bool(uac & 0x0002),
                ))

            conn.unbind()
            return users

        except Exception as e:
            logger.error(f"LDAP list users error: {e}")
            return []

    async def sync_users(self, tenant_id: str) -> int:
        """
        Sync all AD users to the local users table.
        Delegates to jobs/sync_directory_ldap.py for full implementation.
        Retained for backward compatibility — prefer the RQ job.
        """
        from jobs.sync_directory_ldap import _run_async
        await _run_async(tenant_id)
        ad_users = await self.list_users(tenant_id)
        return len(ad_users)


# ═══════════════════════════════════════════════════════════════
# COMP 17 — AZURE AD AUTH ADAPTER
# ═══════════════════════════════════════════════════════════════

class AzureADAuthAdapter(AuthService):
    """
    OIDC/OAuth2 authentication via Azure AD (Entra ID).
    Configuration from environment:
      AZURE_AD_TENANT_ID, AZURE_AD_CLIENT_ID, AZURE_AD_CLIENT_SECRET
    """

    def __init__(self):
        self.azure_tenant_id = os.environ.get("AZURE_AD_TENANT_ID", "")
        self.client_id = os.environ.get("AZURE_AD_CLIENT_ID", "")
        self.client_secret = os.environ.get("AZURE_AD_CLIENT_SECRET", "")
        self.authority = f"https://login.microsoftonline.com/{self.azure_tenant_id}"
        self.token_url = f"{self.authority}/oauth2/v2.0/token"
        self.authorize_url = f"{self.authority}/oauth2/v2.0/authorize"
        self.graph_url = "https://graph.microsoft.com/v1.0"
        self.scopes = ["User.Read", "User.ReadBasic.All"]

    async def authenticate(self, username: str, password: str,
                           tenant_id: str) -> AuthResult:
        """
        Authenticate via ROPC flow (Resource Owner Password Credentials).
        Note: ROPC is used for non-interactive auth. For interactive,
        use the OIDC redirect flow via /auth/azure/login.
        """
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

                # Get user profile from Graph API
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

                # Get group memberships
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
        import uuid as uuid_mod

        ad_users = await self.list_users(tenant_id)
        session = TenantSession(get_session_factory()(), tenant_id)
        synced = 0

        for ad_user in ad_users:
            if not ad_user.email:
                continue
            existing = session.execute(
                sa_text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
                {"tid": tenant_id, "email": ad_user.email},
            ).fetchone()

            if existing:
                session.execute(
                    sa_text("""UPDATE users SET display_name = :dn, department = :dept,
                       title = :title = :active, synced_at = :now
                       WHERE id = :id AND tenant_id = :tid"""),
                    {
                        "dn": ad_user.display_name, "dept": ad_user.department,
                        "title": ad_user.title, "active": ad_user.enabled,
                        "now": datetime.now(timezone.utc).isoformat(),
                        "id": existing["id"], "tid": tenant_id,
                    },
                )
            else:
                user_id = str(uuid_mod.uuid4())
                session.execute(
                    sa_text("""INSERT INTO users
                    (id, tenant_id, username, email, display_name,
                     department, title,  auth_provider, created_at, synced_at)
                    VALUES (:id, :tid, :un, :email, :dn,
                            :dept, :title, :active, 'azure_ad', :now, :now)"""),
                    {
                        "id": user_id, "tid": tenant_id,
                        "un": ad_user.username, "email": ad_user.email,
                        "dn": ad_user.display_name,
                        "dept": ad_user.department, "title": ad_user.title,
                        "active": ad_user.enabled,
                        "now": datetime.now(timezone.utc).isoformat(),
                    },
                )
            synced += 1

        session.commit()
        return synced

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
