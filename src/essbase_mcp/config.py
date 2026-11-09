"""Settings.

Precedence (lowest -> highest): built-in defaults < environment / .env < data/settings.json
(saved from the Admin tab). Secrets in data/settings.json are encrypted (see settings_store).
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


SECRET_FIELDS = ("client_secret", "access_token", "llm_api_key")
INT_LIMITS = {  # field: (min, max)
    "max_concurrent_calls": (1, 500),
    "queue_timeout_s": (1, 600),
    "request_timeout_s": (5, 600),
    "max_members_per_dimension": (10, 1_000_000),
    "session_hours": (1, 168),
}
CHOICES = {
    "mode": ("mock", "remote"),
    "profile": ("viewer", "analyst", "admin"),
    "auth_mode": ("client_credentials", "token", "none"),
    "identity_mode": ("shared", "per_user"),
    "llm_provider": ("none", "openai_compatible"),
}


@dataclass
class Settings:
    # connection
    mode: str = "mock"  # mock = built-in mock server; remote = real Essbase 26ai
    mcp_url: str = "https://<host>/essbase/rest/v1/ess-mcp"
    profile: str = "viewer"
    auth_mode: str = "client_credentials"
    token_url: str = "https://<identity-domain>.identity.oraclecloud.com/oauth2/v1/token"
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    scope: str = "urn:opc:idm:__myscopes__"
    access_token: str = field(default="", repr=False)
    verify_tls: bool = True
    # who Essbase sees: one shared service account, or each person signing in
    identity_mode: str = "shared"       # shared | per_user
    authorize_url: str = ""             # per_user; blank = token URL with /token -> /authorize
    redirect_url: str = ""              # per_user; blank = <this app>/auth/callback
    user_scope: str = "urn:opc:idm:__myscopes__ openid offline_access"
    session_hours: int = 8              # idle sign-out
    # query understanding
    llm_provider: str = "none"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    # capacity (many users share one connection to Essbase)
    max_concurrent_calls: int = 20      # Essbase MCP calls in flight at once; others queue
    queue_timeout_s: int = 30           # how long a queued call waits before "busy"
    request_timeout_s: int = 60         # per HTTP call to Essbase / identity domain
    max_members_per_dimension: int = 5000  # members cached per dimension; the rest are looked up
    # context
    context_dir: str = "contexts"
    active_cube: str = "Sample.Basic"
    # storage for saved settings + key (not a user-facing setting)
    data_dir: str = field(default="data", metadata={"internal": True})

    @property
    def context_file(self) -> str:
        return str(Path(self.context_dir) / f"{self.active_cube}.yaml")

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        e = os.environ.get
        d = cls()
        return cls(
            mode=e("EMCP_MODE", d.mode),
            mcp_url=e("ESSBASE_MCP_URL", d.mcp_url),
            profile=e("ESSBASE_PROFILE", d.profile),
            auth_mode=e("AUTH_MODE", d.auth_mode),
            token_url=e("OAUTH_TOKEN_URL", d.token_url),
            client_id=e("OAUTH_CLIENT_ID", ""),
            client_secret=e("OAUTH_CLIENT_SECRET", ""),
            scope=e("OAUTH_SCOPE", d.scope),
            access_token=e("ACCESS_TOKEN", ""),
            verify_tls=e("VERIFY_TLS", "true").lower() != "false",
            identity_mode=e("IDENTITY_MODE", d.identity_mode),
            authorize_url=e("OAUTH_AUTHORIZE_URL", ""),
            redirect_url=e("OAUTH_REDIRECT_URL", ""),
            user_scope=e("OAUTH_USER_SCOPE", d.user_scope),
            session_hours=int(e("SESSION_HOURS", d.session_hours)),
            llm_provider=e("LLM_PROVIDER", d.llm_provider),
            llm_base_url=e("LLM_BASE_URL", ""),
            llm_model=e("LLM_MODEL", ""),
            llm_api_key=e("LLM_API_KEY", ""),
            max_concurrent_calls=int(e("MAX_CONCURRENT_CALLS", d.max_concurrent_calls)),
            queue_timeout_s=int(e("QUEUE_TIMEOUT_S", d.queue_timeout_s)),
            request_timeout_s=int(e("REQUEST_TIMEOUT_S", d.request_timeout_s)),
            max_members_per_dimension=int(e("MAX_MEMBERS_PER_DIMENSION",
                                            d.max_members_per_dimension)),
            context_dir=e("CONTEXT_DIR", d.context_dir),
            active_cube=e("ACTIVE_CUBE", d.active_cube),
            data_dir=e("EMCP_DATA_DIR", d.data_dir),
        )

    @classmethod
    def load(cls) -> Settings:
        """Env/.env, then anything saved from the Admin tab on top."""
        from essbase_mcp import settings_store

        return settings_store.apply_saved(cls.from_env())

    @staticmethod
    def editable_fields() -> list[str]:
        return [f.name for f in dataclasses.fields(Settings) if not f.metadata.get("internal")]
