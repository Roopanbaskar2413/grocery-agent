import json
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from agent.oauth import OAuthClient, PkcePair, TokenSet, TokenStore

REDIRECT = "http://localhost/callback"


class FakeAuthServer:
    """A scriptable stand-in for mcp.swiggy.com's /auth endpoints."""

    def __init__(self):
        self.registered = None
        self.token_requests = []       # decoded JSON bodies, in order
        self.next_tokens = {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 5 * 24 * 3600}

    def handler(self, request: httpx.Request):
        payload = json.loads(request.content) if request.content else {}
        if request.url.path == "/auth/register":
            self.registered = payload
            return httpx.Response(200, json={"client_id": "client-abc"})
        if request.url.path == "/auth/token":
            self.token_requests.append(payload)
            return httpx.Response(200, json=dict(self.next_tokens))
        return httpx.Response(404)


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def store(tmp_path, key):
    return TokenStore(str(tmp_path / "tokens.bin"), key=key)


@pytest.fixture
def server():
    return FakeAuthServer()


@pytest.fixture
def client(store, server):
    http = httpx.Client(transport=httpx.MockTransport(server.handler))
    return OAuthClient(store, REDIRECT, http=http)


# ---------------------------------------------------------------- PKCE
def test_pkce_pair_is_random_and_well_formed():
    a, b = PkcePair.generate(), PkcePair.generate()
    assert a.verifier != b.verifier
    assert 43 <= len(a.verifier) <= 128           # RFC 7636 verifier length
    assert "=" not in a.challenge and "+" not in a.challenge and "/" not in a.challenge


# ---------------------------------------------------------------- registration + authorize URL
def test_register_client_calls_the_auth_server_with_the_redirect_uri(client, server):
    client_id = client.register_client()
    assert client_id == "client-abc"
    assert server.registered["redirect_uris"] == [REDIRECT]
    assert server.registered["token_endpoint_auth_method"] == "none"


def test_authorize_url_requires_registration_first(client):
    with pytest.raises(RuntimeError):
        client.build_authorize_url()


def test_authorize_url_carries_pkce_challenge_and_state(client):
    client.register_client()
    url = client.build_authorize_url(state="xyz")
    assert "client_id=client-abc" in url
    assert "state=xyz" in url
    assert "code_challenge=" in url and "code_challenge_method=S256" in url


# ---------------------------------------------------------------- code exchange
def test_exchange_code_stores_tokens(client, server, store):
    client.register_client()
    client.build_authorize_url(state="xyz")
    tokens = client.exchange_code("the-code", "xyz")
    assert tokens.access_token == "acc-1"
    assert store.load().access_token == "acc-1"
    sent = server.token_requests[0]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert "code_verifier" in sent


def test_exchange_code_rejects_unknown_state(client):
    client.register_client()
    with pytest.raises(RuntimeError):
        client.exchange_code("the-code", "never-issued")


# ---------------------------------------------------------------- refresh + rotation
def test_refresh_rotates_the_refresh_token(client, server, store):
    client.register_client()
    client.build_authorize_url(state="xyz")
    client.exchange_code("the-code", "xyz")
    assert store.load().refresh_token == "ref-1"

    server.next_tokens = {"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600}
    client.refresh()

    stored = store.load()
    assert stored.access_token == "acc-2"
    assert stored.refresh_token == "ref-2"          # the old refresh token (ref-1) is gone, not kept
    sent = server.token_requests[-1]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "ref-1"          # it sent the token it had, not the new one


def test_refresh_without_a_prior_login_fails_closed(client):
    with pytest.raises(RuntimeError):
        client.refresh()


def test_get_access_token_refreshes_when_expired(client, server, store):
    store.save(TokenSet("stale", "ref-1", datetime.now() - timedelta(seconds=1)))
    server.next_tokens = {"access_token": "fresh", "refresh_token": "ref-2", "expires_in": 3600}
    assert client.get_access_token() == "fresh"


def test_get_access_token_reuses_a_still_valid_token(client, server, store):
    store.save(TokenSet("still-good", "ref-1", datetime.now() + timedelta(hours=1)))
    assert client.get_access_token() == "still-good"
    assert server.token_requests == []               # no network call needed


def test_get_access_token_with_no_stored_token_fails_closed(client):
    with pytest.raises(RuntimeError):
        client.get_access_token()


# ---------------------------------------------------------------- encrypted storage
def test_token_store_requires_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SWIGGY_TOKEN_KEY", raising=False)
    with pytest.raises(RuntimeError):
        TokenStore(str(tmp_path / "t.bin"))


def test_tokens_on_disk_are_not_plaintext(store):
    tokens = TokenSet("super-secret-access-token", "super-secret-refresh-token",
                      datetime.now() + timedelta(days=1))
    store.save(tokens)
    raw = open(store.path, "rb").read()
    assert b"super-secret-access-token" not in raw
    assert b"super-secret-refresh-token" not in raw


def test_token_store_round_trips(store):
    tokens = TokenSet("a", "b", datetime.now().replace(microsecond=0) + timedelta(days=1))
    store.save(tokens)
    loaded = store.load()
    assert loaded.access_token == "a" and loaded.refresh_token == "b"
    assert loaded.expires_at == tokens.expires_at


def test_token_store_clear_removes_the_file(store):
    store.save(TokenSet("a", "b", datetime.now() + timedelta(days=1)))
    store.clear()
    assert store.load() is None


def test_token_store_load_with_no_file_returns_none(tmp_path, key):
    s = TokenStore(str(tmp_path / "missing.bin"), key=key)
    assert s.load() is None


def test_wrong_key_cannot_decrypt_another_store(store, tmp_path):
    store.save(TokenSet("a", "b", datetime.now() + timedelta(days=1)))
    other = TokenStore(store.path, key=Fernet.generate_key().decode())
    with pytest.raises(Exception):
        other.load()
