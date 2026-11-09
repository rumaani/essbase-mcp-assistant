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
CHOICES = {
    "mode": ("mock", "remote"),
    "profile": ("viewer", "analyst", "admin"),
    "auth_mode": ("client_credentials", "token", "none"),
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
    # query understanding
    llm_provider: str = "none"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
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
            llm_provider=e("LLM_PROVIDER", d.llm_provider),
            llm_base_url=e("LLM_BASE_URL", ""),
            llm_model=e("LLM_MODEL", ""),
            llm_api_key=e("LLM_API_KEY", ""),
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
