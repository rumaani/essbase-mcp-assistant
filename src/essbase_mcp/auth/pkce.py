"""PKCE helpers (RFC 7636) for the interactive authorization-code flow (roadmap)."""
import base64
import hashlib
import secrets


def new_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()


def challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
