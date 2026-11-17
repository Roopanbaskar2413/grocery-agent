"""OAuth 2.1 + PKCE login for the real Instamart MCP server (mcp.swiggy.com).

Confirmed from Swiggy's Builders Club docs (mcp.swiggy.com/builders/docs/start/authenticate/):
clients register themselves (RFC 7591 dynamic registration, no client secret to request by hand),
then run an authorization-code + PKCE (S256) flow. Access tokens last about 5 days, refresh tokens
30 days and rotate on every use -- the old refresh token must be discarded, never reused. Read the
real expiry from each token response's `expires_in` rather than assuming the documented number.

Tokens are encrypted at rest (Fernet, key from SWIGGY_TOKEN_KEY) and never logged. This module only
talks to a scripted stand-in in tests; nothing here has been exercised against the real server. See
DRYRUN.md for what to confirm once Builders Club access is approved.
"""
import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx
from cryptography.fernet import Fernet

AUTH_BASE = "https://mcp.swiggy.com"
SCOPES = "mcp:tools mcp:resources mcp:prompts"
DEFAULT_TOKEN_LIFETIME_SECONDS = 5 * 24 * 3600   # fallback only if a response omits expires_in


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class PkcePair:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> "PkcePair":
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        return cls(verifier, challenge)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime

    def to_dict(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "TokenSet":
        return cls(d["access_token"], d["refresh_token"], datetime.fromisoformat(d["expires_at"]))


class TokenStore:
    """Tokens at rest, encrypted with a key that is never committed to the repo (SWIGGY_TOKEN_KEY)."""

    def __init__(self, path: str, key: Optional[str] = None):
        self.path = path
        key = key or os.environ.get("SWIGGY_TOKEN_KEY")
        if not key:
            raise RuntimeError("SWIGGY_TOKEN_KEY is not set; refusing to store or read Instamart tokens")
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def load(self) -> Optional[TokenSet]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
            raw = f.read()
        if not raw:
            return None
        return TokenSet.from_dict(json.loads(self._fernet.decrypt(raw)))

    def save(self, tokens: TokenSet) -> None:
        raw = self._fernet.encrypt(json.dumps(tokens.to_dict()).encode())
        with open(self.path, "wb") as f:
            f.write(raw)
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


class OAuthClient:
    """Dynamic client registration + PKCE login against Swiggy's MCP auth server.

    The browser sign-in step (phone + OTP on Swiggy's side) happens outside this process; this class
    only builds the authorize URL, exchanges the resulting code, and keeps the token set refreshed.
    """

    def __init__(self, token_store: TokenStore, redirect_uri: str, http: Optional[httpx.Client] = None,
                 base_url: str = AUTH_BASE, client_name: str = "grocery-agent"):
        self.store = token_store
        self.redirect_uri = redirect_uri
        self.base_url = base_url
        self.client_name = client_name
        self.http = http or httpx.Client(timeout=10.0)
        self.client_id: Optional[str] = None
        self._pending: dict = {}   # state -> PkcePair, while the browser round trip is in flight

    def register_client(self) -> str:
        r = self.http.post(f"{self.base_url}/auth/register", json={
            "client_name": self.client_name,
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",     # public client, PKCE instead of a secret
        })
        r.raise_for_status()
        self.client_id = r.json()["client_id"]
        return self.client_id

    def build_authorize_url(self, state: Optional[str] = None) -> str:
        if not self.client_id:
            raise RuntimeError("register_client() must run before building an authorize URL")
        state = state or _b64url(secrets.token_bytes(16))
        pkce = PkcePair.generate()
        self._pending[state] = pkce
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
                  "scope": SCOPES, "state": state, "code_challenge": pkce.challenge,
                  "code_challenge_method": "S256"}
        return f"{self.base_url}/auth/authorize?{httpx.QueryParams(params)}"

    def exchange_code(self, code: str, state: str) -> TokenSet:
        pkce = self._pending.pop(state, None)
        if pkce is None:
            raise RuntimeError("unknown or expired OAuth state")
        r = self.http.post(f"{self.base_url}/auth/token", json={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": pkce.verifier})
        r.raise_for_status()
        tokens = self._tokens_from_response(r.json())
        self.store.save(tokens)
        return tokens

    def refresh(self) -> TokenSet:
        current = self.store.load()
        if current is None:
            raise RuntimeError("no stored Instamart token; run the sign-in flow first")
        r = self.http.post(f"{self.base_url}/auth/token",
                           json={"grant_type": "refresh_token", "refresh_token": current.refresh_token})
        r.raise_for_status()
        tokens = self._tokens_from_response(r.json())
        self.store.save(tokens)      # overwrites on disk: the old (now-rotated) refresh token is gone
        return tokens

    def get_access_token(self, skew_seconds: int = 60) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise RuntimeError("no stored Instamart token; run the sign-in flow first")
        if datetime.now() >= tokens.expires_at - timedelta(seconds=skew_seconds):
            tokens = self.refresh()
        return tokens.access_token

    @staticmethod
    def _tokens_from_response(body: dict) -> TokenSet:
        expires_in = body.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)
        return TokenSet(body["access_token"], body["refresh_token"],
                        datetime.now() + timedelta(seconds=expires_in))
