"""Admin authentication: who may change the configuration."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_camera
from test_api import FakeAdapter
from test_go2rtc import FakeGo2rtc
from viewport.api import create_app
from viewport.auth import (COOKIE, MAX_ATTEMPTS, AdminAuth, admin_from_config, hash_password,
                           verify_password)
from viewport.config import AppConfig, AuthConfig, SourceConfig
from viewport.go2rtc import Go2rtcClient
from viewport.runtime import Runtime
from viewport.secretstore import SecretStore
from viewport.store import ConfigStore

PASSWORD = "correct horse battery staple"
TOKEN = "wall-token-1234"
WRITES = [("put", "/api/config/views", {"views": [{"name": "x", "layout": "auto", "cameras": "all"}]}),
          ("put", "/api/config/display", {"fit": "cover"}),
          ("put", "/api/config/device", {"max_main_streams": 2}),
          ("put", "/api/config/layouts", {"layouts": []}),
          ("put", "/api/config/sources/nvr", {"protocol": "rtsp"})]


def make_client(tmp_path, password=PASSWORD, token=TOKEN, secrets_file="secrets.json", **auth):
    config = AppConfig(auth=AuthConfig(token=token, admin_password=password, **auth),
                       sources=[SourceConfig(id="nvr", host="nvr", username="viewer", password="p")])
    rt = Runtime(config, adapters=[FakeAdapter([make_camera(0)])], store=ConfigStore(tmp_path / "s.json"),
                 secrets_store=SecretStore(tmp_path / secrets_file if secrets_file else ""),
                 go2rtc=Go2rtcClient("http://g", transport=httpx.MockTransport(FakeGo2rtc().handler)))
    rt.wall.update_cameras("nvr", [make_camera(0)])
    return TestClient(create_app(config, rt, start_background=False))


BEARER = {"Authorization": f"Bearer {TOKEN}"}


def sign_in(client, username="admin", password=PASSWORD):
    return client.post("/api/admin/login", json={"username": username, "password": password})


# ----- hashing ----------------------------------------------------------------

def test_a_password_is_stored_only_as_a_hash():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "!", encoded)


def test_the_same_password_hashes_differently_every_time():
    assert hash_password(PASSWORD) != hash_password(PASSWORD)     # salted


@pytest.mark.parametrize("broken", ["", "nonsense", "scrypt$bad", "md5$1$2$3$aa$bb"])
def test_a_broken_hash_never_verifies(broken):
    assert not verify_password(PASSWORD, broken)


# ----- sessions ---------------------------------------------------------------

def test_a_session_survives_a_restart_but_not_a_password_change():
    auth = AdminAuth("admin", hash_password(PASSWORD))
    cookie = auth.issue_session()
    assert AdminAuth("admin", auth.password_hash).session_valid(cookie)     # agent restarted
    assert not AdminAuth("admin", hash_password("something else")).session_valid(cookie)


def test_an_expired_session_is_refused():
    auth = AdminAuth("admin", hash_password(PASSWORD), session_hours=-1)
    assert not auth.session_valid(auth.issue_session())


@pytest.mark.parametrize("mangle", [lambda c: c[:-4] + "AAAA", lambda c: "AAAA." + c.split(".")[1],
                                    lambda c: c.replace(".", ""), lambda c: ""])
def test_a_tampered_session_is_refused(mangle):
    auth = AdminAuth("admin", hash_password(PASSWORD))
    assert not auth.session_valid(mangle(auth.issue_session()))


# ----- the routes -------------------------------------------------------------

@pytest.mark.parametrize("verb,path,body", WRITES)
def test_the_api_token_alone_cannot_change_the_configuration(tmp_path, verb, path, body):
    """The token is in the kiosk URL, so it must not grant this."""
    client = make_client(tmp_path)
    resp = getattr(client, verb)(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 401
    assert "sign in" in resp.json()["detail"]


def test_the_token_reads_the_configuration_locked_and_without_usernames(tmp_path):
    """The admin page shows settings before anyone unlocks it. What it shows is already on the
    TV or in /api/health, except usernames and where settings are kept, which stay out."""
    client = make_client(tmp_path)
    assert client.get("/api/config").status_code == 401                   # still needs the token
    locked = client.get("/api/config", headers=BEARER).json()
    assert locked["locked"] is True
    assert locked["sources"][0]["username"] is None and locked["sources"][0]["username_set"] is True
    assert locked["state_file"] is None
    assert "viewer" not in client.get("/api/config", headers=BEARER).text
    sign_in(client)
    unlocked = client.get("/api/config").json()
    assert unlocked["locked"] is False and unlocked["sources"][0]["username"] == "viewer"


def test_signing_in_allows_changes(tmp_path):
    client = make_client(tmp_path)
    assert sign_in(client).status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.put("/api/config/display", json={"fit": "cover"}).status_code == 200


def test_an_admin_session_also_grants_what_the_token_grants(tmp_path):
    """So the admin page needs no token in its URL."""
    client = make_client(tmp_path)
    assert client.get("/api/wall").status_code == 401
    sign_in(client)
    assert client.get("/api/wall").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_signing_out_ends_it(tmp_path):
    client = make_client(tmp_path)
    sign_in(client)
    assert client.post("/api/admin/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/config", headers=BEARER).json()["locked"] is True
    assert client.put("/api/config/display", json={"fit": "cover"}, headers=BEARER).status_code == 401


@pytest.mark.parametrize("username,password", [("admin", "wrong"), ("root", PASSWORD), ("", "")])
def test_wrong_credentials_are_refused(tmp_path, username, password):
    client = make_client(tmp_path)
    assert sign_in(client, username, password).status_code == 401
    assert client.get("/api/config", headers=BEARER).json()["locked"] is True
    assert client.put("/api/config/display", json={"fit": "cover"}).status_code == 401


def test_repeated_failures_lock_logins_for_a_while(tmp_path):
    client = make_client(tmp_path)
    for _ in range(MAX_ATTEMPTS):
        assert sign_in(client, password="wrong").status_code == 401
    # Even the right password is refused while locked, so guessing cannot be ground out.
    assert sign_in(client).status_code == 429
    assert client.get("/api/admin/session").json()["locked_for"] > 0


def test_the_session_cookie_is_not_readable_by_scripts(tmp_path):
    client = make_client(tmp_path)
    header = sign_in(client).headers["set-cookie"]
    assert COOKIE in header
    assert "httponly" in header.lower() and "samesite=strict" in header.lower()


def test_the_password_never_comes_back_from_the_api(tmp_path):
    client = make_client(tmp_path)
    sign_in(client)
    for path in ("/api/config", "/api/health", "/api/admin/session"):
        assert PASSWORD not in client.get(path).text


# ----- without an admin password (development) ---------------------------------

def test_without_an_admin_password_the_token_still_works(tmp_path):
    client = make_client(tmp_path, password="")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/config", headers=headers).status_code == 200
    assert client.put("/api/config/display", json={"fit": "cover"}, headers=headers).status_code == 200
    assert client.get("/api/admin/session", headers=headers).json() == {
        "authenticated": True, "required": False, "username": "admin", "locked_for": 0,
        "password_source": None, "can_set_password": True, "token_required": True}
    assert client.get("/api/config", headers=headers).json()["locked"] is False


def test_with_neither_credential_everything_is_open(tmp_path):
    client = make_client(tmp_path, password="", token="")
    assert client.get("/api/config").status_code == 200


def test_a_supplied_hash_is_used_as_is():
    encoded = hash_password(PASSWORD)
    auth = admin_from_config(AuthConfig(admin_password_hash=encoded))
    assert auth.password_hash == encoded and auth.required
    assert auth.check_login("admin", PASSWORD)


@pytest.mark.parametrize("verb,path,body", WRITES)
def test_without_an_admin_password_the_token_still_guards_changes(tmp_path, verb, path, body):
    """Regression: moving config onto the admin router left it open when only a token was set."""
    client = make_client(tmp_path, password="")
    assert getattr(client, verb)(path, json=body).status_code == 401
    assert getattr(client, verb)(path, json=body,
                                 headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


# ----- choosing the password in the admin page ------------------------------------

NEW = "a much better one"


def choose(client, new=NEW, current="", username="admin", headers=None):
    return client.post("/api/admin/password", headers=headers or {},
                       json={"username": username, "current_password": current, "new_password": new})


def test_a_first_password_can_be_chosen_in_the_page_and_then_it_is_needed(tmp_path):
    client = make_client(tmp_path, password="", token="")
    resp = choose(client)
    assert resp.status_code == 200
    assert resp.json()["password_source"] == "page" and COOKIE in resp.headers["set-cookie"]
    assert client.put("/api/config/display", json={"fit": "cover"}).status_code == 200   # still in

    someone_else = TestClient(client.app)
    assert someone_else.get("/api/admin/session").json()["required"] is True
    assert someone_else.get("/api/config").json()["locked"] is True
    assert someone_else.put("/api/config/display", json={"fit": "cover"}).status_code == 401
    assert sign_in(someone_else, password=NEW).status_code == 200


def test_the_first_password_needs_the_token_when_there_is_one(tmp_path):
    """Until a password exists, the token is what guards changes; choosing one is a change."""
    client = make_client(tmp_path, password="")
    assert choose(client).status_code == 401
    assert choose(client, headers=BEARER).status_code == 200


def test_changing_the_password_needs_the_current_one_and_ends_other_sessions(tmp_path):
    client = make_client(tmp_path, password="", token="")
    choose(client)
    other = TestClient(client.app)
    sign_in(other, password=NEW)
    assert choose(client, new="the next one", current="not it").status_code == 401
    assert choose(client, new="the next one", current=NEW).status_code == 200
    assert other.put("/api/config/display", json={"fit": "cover"}).status_code == 401   # old session gone
    assert sign_in(other, password="the next one").status_code == 200


def test_guessing_the_current_password_is_rate_limited_like_a_login(tmp_path):
    client = make_client(tmp_path, password="", token="")
    choose(client)
    for _ in range(MAX_ATTEMPTS):
        assert choose(client, new="whatever123", current="guess").status_code == 401
    assert choose(client, new="whatever123", current=NEW).status_code == 429


def test_a_short_password_is_refused(tmp_path):
    client = make_client(tmp_path, password="", token="")
    resp = choose(client, new="short")
    assert resp.status_code == 422 and "at least 8" in resp.json()["detail"]


def test_a_password_from_the_configuration_cannot_be_changed_here(tmp_path):
    client = make_client(tmp_path)
    sign_in(client)
    assert client.get("/api/admin/session").json()["can_set_password"] is False
    resp = choose(client, current=PASSWORD)
    assert resp.status_code == 409 and "VIEWPORT_ADMIN_PASSWORD" in resp.json()["detail"]


def test_without_a_secrets_file_there_is_nowhere_to_keep_one(tmp_path):
    client = make_client(tmp_path, password="", token="", secrets_file=None)
    assert client.get("/api/admin/session").json()["can_set_password"] is False
    assert choose(client).status_code == 409


def test_a_chosen_password_is_kept_only_as_a_hash_and_survives_a_restart(tmp_path):
    client = make_client(tmp_path, password="", token="")
    choose(client, username="max")
    text = (tmp_path / "secrets.json").read_text()
    assert NEW not in text and "scrypt$" in text

    restarted = make_client(tmp_path, password="", token="")
    assert restarted.get("/api/admin/session").json()["username"] == "max"
    assert sign_in(restarted, username="max", password=NEW).status_code == 200


def test_a_password_in_the_configuration_wins_which_is_how_a_forgotten_one_is_recovered(tmp_path):
    choose(make_client(tmp_path, password="", token=""))
    recovered = make_client(tmp_path, password=PASSWORD, token="")
    assert recovered.get("/api/admin/session").json()["password_source"] == "config"
    assert sign_in(recovered, password=NEW).status_code == 401
    assert sign_in(recovered, password=PASSWORD).status_code == 200


def test_the_admin_login_does_not_disturb_source_credentials(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set_source("nvr", "viewer", "cam-pass")
    store.set_admin_login("admin", hash_password(NEW))
    store.set_source("shed", "u", "p")
    store.forget_source("shed")
    assert set(store.load()) == {"nvr"}                   # the admin entry is not a source
    assert store.admin_login()[0] == "admin"


def test_a_hand_edited_admin_entry_that_is_not_a_hash_is_ignored(tmp_path):
    (tmp_path / "secrets.json").write_text('{"_admin": {"username": "admin", "password_hash": "plaintext"}}')
    assert SecretStore(tmp_path / "secrets.json").admin_login() is None


# ----- pausing guessers without locking the owner out ---------------------------------------

def test_one_client_guessing_does_not_lock_everyone_else_out():
    """With one lockout for everybody, anyone on the network could keep the owner out."""
    auth = AdminAuth("admin", hash_password(PASSWORD))
    for _ in range(MAX_ATTEMPTS):
        assert not auth.check_login("admin", "guess", client="10.0.0.66")
    assert auth.locked_for("10.0.0.66") > 0
    assert not auth.check_login("admin", PASSWORD, client="10.0.0.66")      # paused, even when right
    assert auth.locked_for("10.0.0.5") == 0
    assert auth.check_login("admin", PASSWORD, client="10.0.0.5")


def test_guessing_spread_over_many_clients_pauses_everyone():
    from viewport.auth import GLOBAL_MAX_ATTEMPTS
    auth = AdminAuth("admin", hash_password(PASSWORD))
    for i in range(GLOBAL_MAX_ATTEMPTS):
        auth.check_login("admin", "guess", client=f"10.0.1.{i}")            # under the per-client limit
    assert auth.locked_for("10.0.0.5") > 0
    assert not auth.check_login("admin", PASSWORD, client="10.0.0.5")


def test_a_successful_login_clears_only_that_clients_failures():
    auth = AdminAuth("admin", hash_password(PASSWORD))
    for _ in range(MAX_ATTEMPTS - 1):
        auth.check_login("admin", "guess", client="a")
        auth.check_login("admin", "guess", client="b")
    assert auth.check_login("admin", PASSWORD, client="a")
    assert not auth.check_login("admin", "guess", client="b")
    assert auth.locked_for("b") > 0 and auth.locked_for("a") == 0
