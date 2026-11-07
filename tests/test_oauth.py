"""OAuth / DCR / PKCE 加固回归：回调精确匹配、S256 强制、令牌过期与兼容开关。"""
from __future__ import annotations

import urllib.parse

import pytest

REDIRECT = "https://client.example.com/callback"


def _register(client, uris=None):
    return client.post("/register", json={"redirect_uris": [REDIRECT] if uris is None else uris})


def _query(server, client_id, verifier, redirect=REDIRECT, **extra):
    data = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": server._pkce_s256(verifier),
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    data.update(extra)
    return data


def _issue_code(server, client, client_id, verifier, redirect=REDIRECT, **extra):
    data = _query(server, client_id, verifier, redirect, **extra)
    response = client.post("/authorize", data={**data, "password": server.TOKEN},
                           follow_redirects=False)
    assert response.status_code == 302, response.text
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)
    return query["code"][0], data


def test_dcr_rejects_fragments_wildcards_and_invalid_uris(gateway_client):
    _, client, _ = gateway_client
    for bad in (
        "https://client.example.com/cb#fragment",
        "https://*.example.com/cb",
        "https://user:pass@client.example.com/cb",
        "javascript:alert(1)",
        "not-an-absolute-uri",
        "",
    ):
        assert client.post("/register", json={"redirect_uris": [bad]}).status_code == 400, bad
    assert client.post("/register", json={"redirect_uris": []}).status_code == 400
    good = _register(client, ["https://Example.com/cb", "myapp://callback"])
    assert good.status_code == 201
    assert good.json()["redirect_uris"] == ["https://example.com/cb", "myapp://callback"]


def test_authorize_validates_redirect_and_pkce_on_both_methods(gateway_client):
    server, client, _ = gateway_client
    client_id = _register(client).json()["client_id"]
    verifier = "v" * 43

    # GET 阶段就拒绝未注册回调与缺少 PKCE，避免用户白输一次口令
    assert client.get("/authorize", params=_query(
        server, client_id, verifier, redirect="https://evil.example.com/cb"),
        follow_redirects=False).status_code == 400
    assert client.get("/authorize", params={
        k: v for k, v in _query(server, client_id, verifier).items()
        if not k.startswith("code_challenge")}, follow_redirects=False).status_code == 400
    assert client.get("/authorize", params=_query(
        server, client_id, verifier, code_challenge_method="plain"),
        follow_redirects=False).status_code == 400

    form = client.get("/authorize", params=_query(server, client_id, verifier),
                      follow_redirects=False)
    assert form.status_code == 200
    assert "code_challenge" in form.text and REDIRECT in form.text

    # POST 同样精确匹配，且不依赖 GET 表单
    assert client.post("/authorize", data={
        **_query(server, client_id, verifier, redirect=REDIRECT + "/evil"),
        "password": server.TOKEN}, follow_redirects=False).status_code == 400
    assert client.post("/authorize", data={
        **_query(server, client_id, verifier, code_challenge_method="plain"),
        "password": server.TOKEN}, follow_redirects=False).status_code == 400
    assert client.post("/authorize", data={
        **_query(server, client_id, verifier), "password": "wrong"},
        follow_redirects=False).status_code == 401


def test_full_pkce_flow_exact_match_replay_and_token_expiry(gateway_client):
    server, client, _ = gateway_client
    client_id = _register(client).json()["client_id"]
    verifier = "v" * 43
    code, data = _issue_code(server, client, client_id, verifier)
    base = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": REDIRECT}

    # 错误回调 / 错误 verifier 都不签发令牌，且不会误消费授权码
    assert client.post("/token", data={**base, "redirect_uri": REDIRECT + "/evil",
                                       "code_verifier": verifier}).status_code == 400
    assert client.post("/token", data={**base, "redirect_uri": "",
                                       "code_verifier": verifier}).status_code == 400
    assert client.post("/token", data={**base, "code_verifier": "wrong"}).status_code == 400

    issued = client.post("/token", data={**base, "code_verifier": verifier})
    assert issued.status_code == 200, issued.text
    token = issued.json()["access_token"]
    assert issued.json()["expires_in"] == server.TOKEN_TTL
    assert isinstance(server.ISSUED_TOKENS.get(token), float)

    assert client.get("/health/details",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/token", data={**base, "code_verifier": verifier}).status_code == 400

    server.ISSUED_TOKENS[token] = server.time.time() - 1
    assert client.get("/health/details",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert token not in server.ISSUED_TOKENS


def test_expired_tokens_are_cleaned_even_without_being_presented(gateway_client):
    server, client, _ = gateway_client
    stale = "stale-token"
    server.ISSUED_TOKENS[stale] = server.time.time() - 10
    assert server._token_is_authorized("some-other-token") is False
    assert stale not in server.ISSUED_TOKENS
    assert client.get("/health/details",
                      headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_compat_switches_default_off_and_only_work_when_explicitly_enabled(gateway_client,
                                                                          monkeypatch):
    server, client, _ = gateway_client
    legacy = {"response_type": "code", "client_id": "legacy-client",
              "redirect_uri": "myapp://callback"}
    assert server.OAUTH_ALLOW_ANY_REDIRECT is False
    assert server.OAUTH_ALLOW_NO_PKCE is False
    assert client.get("/authorize", params=legacy,
                      follow_redirects=False).status_code == 400

    monkeypatch.setattr(server, "OAUTH_ALLOW_ANY_REDIRECT", True)
    monkeypatch.setattr(server, "OAUTH_ALLOW_NO_PKCE", True)
    assert client.get("/authorize", params=legacy,
                      follow_redirects=False).status_code == 200
    response = client.post("/authorize", data={**legacy, "password": server.TOKEN},
                           follow_redirects=False)
    assert response.status_code == 302
    code = urllib.parse.parse_qs(
        urllib.parse.urlsplit(response.headers["location"]).query)["code"][0]
    token = client.post("/token", data={"grant_type": "authorization_code", "code": code})
    assert token.status_code == 200 and "access_token" in token.json()
    # 兼容开关不放松对片段 / 通配回调的拒绝
    assert client.post("/register", json={
        "redirect_uris": ["https://a.example.com/cb#x"]}).status_code == 400
