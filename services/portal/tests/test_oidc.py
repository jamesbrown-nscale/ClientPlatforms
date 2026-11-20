"""AUTH_MODE=oidc: the identity provider says who it is; only people on the portal's list get in.
Break-glass admins can still use a password. Admins find people in the directory to add them."""
import importlib
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

import tests.test_portal as base  # noqa: F401  (sets the test environment first)
from app import access, directory, main, oidc

ISSUER, CLIENT = "https://accounts.google.com", "client-123.apps.googleusercontent.com"
ADMIN = "james.brown@nscale.com"


class FakeResponse:
    def __init__(self, status: int, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


@pytest.fixture
def op(monkeypatch, tmp_path):
    """The portal in oidc mode, with a pretend identity provider."""
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "oidc.db"))
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN)
    monkeypatch.setenv("ADMIN_PASSWORD", "break-glass-password")
    monkeypatch.setenv("DIRECTORY", "google")
    monkeypatch.setenv("DIRECTORY_GOOGLE_ADMIN", "admin@nscale.com")
    for m in (access, oidc, directory, main):
        importlib.reload(m)
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
    monkeypatch.setattr(oidc.provider, "conf", lambda: {"issuer": ISSUER, "authorization_endpoint": "https://idp.test/auth",
                                                       "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/keys"})
    monkeypatch.setattr(oidc.provider, "keys", lambda refresh=False: JsonWebKey.import_key_set({"keys": [key.as_dict(is_private=False)]}))
    state = {}

    def id_token(**kw):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT, "sub": "1", "email": ADMIN, "email_verified": True, "name": "James Brown",
                  "iat": now, "exp": now + 600, "nonce": state["nonce"], **kw}
        return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key).decode()

    def token_post(url, **kw):
        state["posted"] = kw
        return FakeResponse(200, {"id_token": state.pop("next_token", None) or id_token(**state.get("claims", {}))})

    monkeypatch.setattr("httpx.post", token_post)

    with TestClient(main.app, base_url="https://cp.test") as c:
        def sign_in(email=ADMIN, nxt="/", **claims):
            """Through the whole round trip; returns the callback's response."""
            c.cookies.clear()
            r = c.get("/login/oidc", params={"next": nxt}, follow_redirects=False)
            assert r.status_code == 303
            q = parse_qs(urlsplit(r.headers["location"]).query)
            state["nonce"] = q["nonce"][0]
            state["claims"] = {"email": email, **claims}
            return c.get("/auth/callback", params={"state": q["state"][0], "code": "the-code"}, follow_redirects=False)
        c.sign_in, c.state, c.id_token = sign_in, state, id_token
        yield c
    monkeypatch.undo()
    for m in (access, oidc, directory, main):
        importlib.reload(m)


def test_sign_in_page_and_the_trip_to_the_provider(op):
    page = op.get("/login")
    assert page.status_code == 200 and "Sign in with Google" in page.text and "break-glass" in page.text
    assert op.get("/", follow_redirects=False).headers["location"] == "/login"
    r = op.get("/login/oidc", params={"next": "/tasks/"}, follow_redirects=False)
    where = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(where.query).items()}
    assert f"{where.scheme}://{where.netloc}{where.path}" == "https://idp.test/auth"
    assert q["client_id"] == CLIENT and q["response_type"] == "code" and q["scope"] == "openid email profile"
    assert q["redirect_uri"] == "https://cp.test/auth/callback" and q["code_challenge_method"] == "S256" and q["hd"] == "nscale.com"
    assert q["state"] and q["nonce"] and q["code_challenge"]
    cookie = r.headers["set-cookie"]
    assert "cp_oidc=" in cookie and "HttpOnly" in cookie and "Path=/auth/callback" in cookie and "secure" in cookie.lower()


def test_someone_on_the_list_gets_in(op):
    r = op.sign_in(nxt="/tasks/")
    assert r.status_code == 303 and r.headers["location"] == "/tasks/"
    sent = op.state["posted"]
    assert sent["auth"] == (CLIENT, "shh") and sent["data"]["code"] == "the-code" and sent["data"]["code_verifier"]
    me = op.get("/api/me").json()
    assert me["email"] == ADMIN and me["oidc"] and me["provider"] == "Google" and me["directory"] == "Google Workspace"
    assert me["passwords"] is True   # the first admin is a break-glass admin
    ok = op.get("/auth/check", headers={"x-forwarded-uri": "/restart/"})
    assert ok.status_code == 200 and access.verify(ok.headers["x-cp-user"], "restart")["email"] == ADMIN
    # Signing out goes to a signed-out page, not straight back to the provider.
    out = op.get("/logout", follow_redirects=False)
    assert out.headers["location"] == "/login?out=1" and "signed out" in op.get("/login?out=1").text
    assert op.get("/api/me").status_code == 401


def test_only_people_on_the_list(op):
    # A good sign-in from someone who isn't on the list: nothing.
    r = op.sign_in("amy.chen@nscale.com")
    assert r.status_code == 403 and "amy.chen@nscale.com" in r.text and "on the Client Platforms list" in r.text
    assert "cp_session" not in r.headers.get("set-cookie", "")
    assert op.get("/api/me").status_code == 401
    # An admin adds her (no password); now she gets in, with her proper name from the provider.
    op.sign_in()
    assert op.post("/api/users", headers=base.H, json={"email": "amy.chen@nscale.com", "roles": []}).status_code == 200
    r = op.sign_in("amy.chen@nscale.com", name="Amy Chen-Wong")
    assert r.status_code == 303
    me = op.get("/api/me").json()
    assert me["email"] == "amy.chen@nscale.com" and me["name"] == "Amy Chen-Wong" and me["passwords"] is False
    # Turned off: out at once, and can't sign in again.
    op.sign_in()
    assert op.patch("/api/users/amy.chen@nscale.com", headers=base.H, json={"disabled": True}).status_code == 200
    assert op.sign_in("amy.chen@nscale.com").status_code == 403


@pytest.mark.parametrize("claims,why", [
    ({"email": "james@gmail.com"}, "nscale.com account"),
    ({"email_verified": False}, "verified with the sign-in provider"),
    ({"aud": "someone-else"}, "be checked"),
    ({"iss": "https://evil.example.com"}, "be checked"),
    ({"exp": int(time.time()) - 600}, "be checked"),
    ({"nonce": "replayed"}, "be checked"),
    ({"email": ""}, "share your email"),
])
def test_bad_sign_ins_are_turned_away(op, claims, why):
    r = op.sign_in(**claims)
    assert r.status_code == 400 and why in r.text
    assert op.get("/api/me").status_code == 401


def test_the_round_trip_must_match(op):
    r = op.get("/login/oidc", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    op.state["nonce"] = q["nonce"][0]
    assert "match the one started here" in op.get("/auth/callback", params={"state": "forged", "code": "x"}).text
    op.cookies.clear()   # started in another browser, or the cookie expired
    assert "took too long" in op.get("/auth/callback", params={"state": q["state"][0], "code": "x"}).text
    assert "cancelled" in op.get("/auth/callback", params={"error": "access_denied"}).text
    # A forged cookie is no good either.
    op.cookies.set("cp_oidc", oidc._sign("wrong-secret", {"s": "a", "n": "b", "v": "c", "r": "x", "next": "/", "exp": time.time() + 60}),
                   path="/auth/callback")
    assert "took too long" in op.get("/auth/callback", params={"state": "a", "code": "x"}).text


def test_break_glass_passwords(op):
    # The break-glass admin can use a password.
    r = op.post("/login", data={"email": ADMIN, "password": "break-glass-password"}, follow_redirects=False)
    assert r.status_code == 303 and op.get("/api/me").json()["email"] == ADMIN
    # Everyone else signs in with the provider, even with a password that would match.
    assert op.post("/api/users", headers=base.H, json={"email": "amy.chen@nscale.com", "roles": []}).status_code == 200
    main.db.set_password("amy.chen@nscale.com", "amys-secret-password")
    assert op.post("/api/users/amy.chen@nscale.com/password", headers=base.H, json={"password": "x" * 12}).status_code == 400
    op.cookies.clear()
    r = op.post("/login", data={"email": "amy.chen@nscale.com", "password": "amys-secret-password"}, follow_redirects=False)
    assert r.status_code == 401 and "break-glass" in r.text
    assert op.get("/api/me").status_code == 401


def test_directory_search(op, monkeypatch):
    op.sign_in()
    asked = []
    users = [{"primaryEmail": "Amy.Chen@nscale.com", "name": {"fullName": "Amy Chen"}, "organizations": [{"primary": True, "title": "Engineer"}]},
             {"primaryEmail": "amos.p@nscale.com", "name": {"givenName": "Amos", "familyName": "Patel"}},
             {"primaryEmail": "gone@nscale.com", "name": {"fullName": "Amy Gone"}, "suspended": True},
             {"primaryEmail": ADMIN, "name": {"fullName": "Amy James Brown"}}]

    def get(url, **kw):
        asked.append(kw["params"]["query"])
        assert kw["headers"]["Authorization"] == "Bearer tok" and kw["params"]["customer"] == "my_customer"
        return FakeResponse(200, {"users": users})

    monkeypatch.setattr(directory.google_token, "get", lambda: "tok")
    monkeypatch.setattr("httpx.get", get)
    r = op.get("/api/directory", params={"q": "am"}).json()
    assert r["directory"] == "Google Workspace"
    assert asked == ["givenName:am* isSuspended=false", "familyName:am* isSuspended=false", "email:am* isSuspended=false"]
    assert [(p["email"], p["name"], p["added"]) for p in r["people"]] == [
        ("amos.p@nscale.com", "Amos Patel", False), ("amy.chen@nscale.com", "Amy Chen", False), (ADMIN, "Amy James Brown", True)]
    assert r["people"][1]["title"] == "Engineer"
    # Every word has to match; too short asks nothing.
    assert [p["email"] for p in op.get("/api/directory", params={"q": "amy chen"}).json()["people"]] == ["amy.chen@nscale.com"]
    asked.clear()
    assert op.get("/api/directory", params={"q": "a"}).json()["people"] == [] and not asked
    # The directory saying no comes back as a plain message.
    monkeypatch.setattr("httpx.get", lambda url, **kw: FakeResponse(403, {}))
    r = op.get("/api/directory", params={"q": "amy"})
    assert r.status_code == 502 and "service account" in r.json()["detail"]
    # Only people who manage people can search.
    assert op.post("/api/users", headers=base.H, json={"email": "amy.chen@nscale.com", "roles": []}).status_code == 200
    op.sign_in("amy.chen@nscale.com")
    assert op.get("/api/directory", params={"q": "amy"}).status_code == 403


def test_okta_directory(monkeypatch):
    monkeypatch.setenv("DIRECTORY", "okta")
    monkeypatch.setenv("OIDC_ISSUER", "https://nscale.okta.com/oauth2/default")
    monkeypatch.setenv("OKTA_API_TOKEN", "ssws-token")
    importlib.reload(directory)
    assert directory.OKTA_ORG == "https://nscale.okta.com" and directory.name() == "Okta"
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return FakeResponse(200, [{"profile": {"email": "Amy.Chen@nscale.com", "firstName": "Amy", "lastName": "Chen", "title": "Engineer"}},
                                  {"profile": {"login": "amir@nscale.com", "displayName": "Amir K"}}])

    monkeypatch.setattr("httpx.get", get)
    found = directory.search('am"y')
    assert seen["url"] == "https://nscale.okta.com/api/v1/users" and seen["headers"]["Authorization"] == "SSWS ssws-token"
    assert seen["params"]["search"].startswith('status eq "ACTIVE" and (profile.firstName sw "am') and '"y' not in seen["params"]["search"]
    assert [(p["email"], p["name"]) for p in directory.search("am")] == [("amir@nscale.com", "Amir K"), ("amy.chen@nscale.com", "Amy Chen")]
    assert [p["email"] for p in found] == ["amy.chen@nscale.com"]   # the quote splits it into "am" and "y": only Amy has both
    monkeypatch.undo()
    importlib.reload(directory)


def test_needs_its_settings(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "")
    importlib.reload(oidc)
    with pytest.raises(RuntimeError, match="OIDC_ISSUER"):
        oidc.check_settings()
    monkeypatch.setenv("OIDC_ISSUER", "http://insecure.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "a")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "b")
    importlib.reload(oidc)
    with pytest.raises(RuntimeError, match="https"):
        oidc.check_settings()
    monkeypatch.undo()
    importlib.reload(oidc)


@pytest.mark.parametrize("mode", ["cloudflare", "iap"])
def test_old_sign_in_modes_are_refused(monkeypatch, mode):
    """A leftover AUTH_MODE from an older setup stops the portal starting, rather than letting anyone in."""
    monkeypatch.setenv("AUTH_MODE", mode)
    importlib.reload(access)
    with pytest.raises(RuntimeError, match="must be oidc or local"):
        importlib.reload(main)
    monkeypatch.undo()
    importlib.reload(access)
    importlib.reload(main)
