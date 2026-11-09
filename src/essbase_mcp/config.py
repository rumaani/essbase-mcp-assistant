"""Settings loaded from environment variables (see .env.example)."""
from __future__ import annotations

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


@dataclass
class Settings:
    mcp_url: str = "http://localhost:9100/essbase/rest/v1/ess-mcp"
    profile: str = "viewer"
    auth_mode: str = "client_credentials"
    token_url: str = "http://localhost:9100/oauth2/v1/token"
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    scope: str = "urn:opc:idm:__myscopes__"
    access_token: str = field(default="", repr=False)
    llm_provider: str = "none"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    context_file: str = "contexts/Sample.Basic.yaml"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        e = os.environ.get
        return cls(
            mcp_url=e("ESSBASE_MCP_URL", cls.mcp_url),
            profile=e("ESSBASE_PROFILE", cls.profile),
            auth_mode=e("AUTH_MODE", cls.auth_mode),
            token_url=e("OAUTH_TOKEN_URL", cls.token_url),
            client_id=e("OAUTH_CLIENT_ID", ""),
            client_secret=e("OAUTH_CLIENT_SECRET", ""),
            scope=e("OAUTH_SCOPE", cls.scope),
            access_token=e("ACCESS_TOKEN", ""),
            llm_provider=e("LLM_PROVIDER", cls.llm_provider),
            llm_base_url=e("LLM_BASE_URL", ""),
            llm_model=e("LLM_MODEL", ""),
            llm_api_key=e("LLM_API_KEY", ""),
            context_file=e("CONTEXT_FILE", cls.context_file),
        )
