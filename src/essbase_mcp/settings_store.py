"""Persist Admin-tab settings to <data_dir>/settings.json.

Secrets (client secret, access token, LLM API key) are encrypted with Fernet (AES-128-CBC +
HMAC). The key comes from EMCP_SECRET_KEY, or is generated once into <data_dir>/secret.key
with owner-only permissions. In production, put the key in a vault (OCI Vault, Azure Key
Vault) and inject it as EMCP_SECRET_KEY.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from essbase_mcp.config import CHOICES, SECRET_FIELDS, Settings

PREFIX = "enc:"


def _fernet(data_dir: str) -> Fernet:
    key = os.environ.get("EMCP_SECRET_KEY")
    if not key:
        path = Path(data_dir) / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
            path.chmod(0o600)
        key = path.read_bytes().decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def _path(s: Settings) -> Path:
    return Path(s.data_dir) / "settings.json"


def apply_saved(s: Settings) -> Settings:
    path = _path(s)
    if not path.exists():
        return s
    saved = json.loads(path.read_text())
    f = _fernet(s.data_dir)
    values = {}
    for name in Settings.editable_fields():
        if name not in saved:
            continue
        v = saved[name]
        if name in SECRET_FIELDS and isinstance(v, str) and v.startswith(PREFIX):
            try:
                v = f.decrypt(v[len(PREFIX):].encode()).decode()
            except InvalidToken:
                v = ""  # key changed; secret must be re-entered
        values[name] = v
    return dataclasses.replace(s, **values)


def save(s: Settings) -> None:
    f = _fernet(s.data_dir)
    out = {}
    for name in Settings.editable_fields():
        v = getattr(s, name)
        if name in SECRET_FIELDS and v:
            v = PREFIX + f.encrypt(v.encode()).decode()
        out[name] = v
    path = _path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    path.chmod(0o600)


def validate(values: dict) -> list[str]:
    errors = []
    for name, allowed in CHOICES.items():
        if name in values and values[name] not in allowed:
            errors.append(f"{name} must be one of {', '.join(allowed)}")
    for name in ("mcp_url", "token_url", "llm_base_url"):
        v = values.get(name)
        if v and not v.startswith(("https://", "http://")):
            errors.append(f"{name} must start with https:// (or http:// for local testing)")
    if values.get("active_cube") and "." not in values["active_cube"]:
        errors.append("active_cube must look like Application.Database")
    return errors


def merge(current: Settings, updates: dict) -> Settings:
    """Apply UI updates. Empty secret fields mean 'keep current'; use clear_<field> to blank."""
    values = {}
    for name in Settings.editable_fields():
        if name in SECRET_FIELDS:
            if updates.get(f"clear_{name}"):
                values[name] = ""
            elif updates.get(name):
                values[name] = updates[name]
        elif name in updates and updates[name] is not None:
            values[name] = bool(updates[name]) if name == "verify_tls" else str(updates[name]).strip()
    return dataclasses.replace(current, **values)


def public_view(s: Settings) -> dict:
    """Settings safe to send to the browser: secrets replaced by a 'set' flag."""
    out = {}
    for name in Settings.editable_fields():
        if name in SECRET_FIELDS:
            out[f"{name}_set"] = bool(getattr(s, name))
        else:
            out[name] = getattr(s, name)
    return out
