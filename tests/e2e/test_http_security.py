"""The defences that hold on a real network, checked against a running agent."""

from __future__ import annotations

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import TOKEN, WALL


@pytest.mark.parametrize("path", ["/", "/admin"])
def test_pages_allow_only_their_own_scripts_and_no_framing(path):
    headers = httpx.get(f"{WALL}{path}").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"


def test_a_web_page_on_another_site_cannot_watch_through_the_relay():
    url = f"{WALL.replace('http', 'ws')}/api/media/ws?src=nvr_0_sub&token={TOKEN}"
    with pytest.raises(InvalidStatus):
        with connect(url, additional_headers={"Origin": "http://attacker.example"}, open_timeout=5):
            pass


def test_a_rebound_domain_is_refused():
    headers = {"Host": "attacker.example.com", "Authorization": f"Bearer {TOKEN}"}
    assert httpx.get(f"{WALL}/api/health", headers=headers).status_code == 400


def test_the_admin_api_refuses_changes_without_a_session_even_with_the_token():
    resp = httpx.put(f"{WALL}/api/config/display", json={"fit": "cover"},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 401


def test_login_answers_are_never_cached():
    resp = httpx.post(f"{WALL}/api/admin/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401 and resp.headers["cache-control"] == "no-store"
