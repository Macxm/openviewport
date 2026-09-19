"""Talking to the host: the socket protocol, the argument checks, and the stand-in device."""

from __future__ import annotations

import asyncio
import json

import pytest

from viewport.hostctl import HostControl, HostError, check_psk, check_ssid


@pytest.fixture
def socket_path(tmp_path):
    return str(tmp_path / "host.sock")


async def serve(path: str, handler) -> asyncio.AbstractServer:
    """A stand-in for deploy/pi/hostd.py: one JSON request per line, one answer per line."""
    async def connected(reader, writer):
        line = await reader.readline()
        answer = handler(json.loads(line))
        writer.write(json.dumps(answer).encode() + b"\n")
        await writer.drain()
        writer.close()
    return await asyncio.start_unix_server(connected, path=path)


# ----- what the agent may send ---------------------------------------------------

@pytest.mark.parametrize("ssid", ["Kitchen", "a", "x" * 32, " Kitchen "])
def test_usable_network_names_are_accepted(ssid):
    assert check_ssid(ssid) == ssid.strip()


@pytest.mark.parametrize("ssid", ["", "   ", "x" * 33, "what\never", "a\x00b"])
def test_unusable_network_names_are_refused(ssid):
    with pytest.raises(HostError):
        check_ssid(ssid)


@pytest.mark.parametrize("psk", ["", "12345678", "x" * 63, "0" * 64])
def test_usable_passwords_are_accepted(psk):
    assert check_psk(psk) == psk


@pytest.mark.parametrize("psk", ["short", "x" * 64, "x" * 200, "pass\nword"])
def test_unusable_passwords_are_refused(psk):
    """A 64-character password is only allowed as a hex PSK, which "x" * 64 is not."""
    with pytest.raises(HostError):
        check_psk(psk)


# ----- the socket ----------------------------------------------------------------

async def test_a_request_reaches_the_host_and_its_answer_comes_back(socket_path):
    seen = []

    def handler(request):
        seen.append(request)
        return {"ok": True, "result": {"link": "wifi", "ssid": request["ssid"]}}

    server = await serve(socket_path, handler)
    try:
        host = HostControl(socket_path=socket_path, fake=False)
        assert host.kind == "helper" and host.available
        assert await host.join("Kitchen", "a good password") == {"link": "wifi", "ssid": "Kitchen"}
        assert seen == [{"op": "join", "ssid": "Kitchen", "psk": "a good password"}]
    finally:
        server.close()


async def test_the_hosts_refusal_is_passed_on_as_it_is(socket_path):
    server = await serve(socket_path, lambda _: {"ok": False, "error": "no wifi device"})
    try:
        with pytest.raises(HostError, match="no wifi device"):
            await HostControl(socket_path=socket_path, fake=False).reboot()
    finally:
        server.close()


async def test_nonsense_from_the_host_is_not_passed_on_as_success(socket_path):
    async def rubbish(reader, writer):
        await reader.readline()
        writer.write(b"<html>not json</html>\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(rubbish, path=socket_path)
    try:
        host = HostControl(socket_path=socket_path, fake=False)
        with pytest.raises(HostError, match="nonsense"):
            await host.networks()
        # status() is what the read-only panel calls, so it reports rather than raises.
        assert (await host.status())["error"] == "the host helper answered with nonsense"
    finally:
        server.close()


async def test_without_a_helper_nothing_is_attempted_and_status_still_answers(tmp_path):
    host = HostControl(socket_path=str(tmp_path / "absent.sock"), fake=False)
    assert host.kind == "none" and not host.available
    assert await host.status() == {"kind": "none"}       # the panel still has something to show
    with pytest.raises(HostError, match="no host helper"):
        await host.reboot()


# ----- the stand-in device --------------------------------------------------------

async def test_joining_a_network_takes_the_device_off_its_access_point():
    host = HostControl(fake=True)
    await host.access_point(True)
    assert (await host.status())["access_point"] is True

    await host.join("Kitchen", "a good password")
    status = await host.status()
    assert status["access_point"] is False
    assert (status["link"], status["ssid"]) == ("wifi", "Kitchen")


async def test_a_wrong_password_is_refused_rather_than_silently_accepted():
    host = HostControl(fake=True)
    with pytest.raises(HostError, match="not accepted"):
        await host.join("Kitchen", "the wrong one")
    assert (await host.status())["link"] == "ethernet", "and it stays as it was"


async def test_the_stand_in_says_it_is_simulated():
    """The admin page shows this, so nobody takes a reboot button here for a real one."""
    host = HostControl(fake=True)
    assert host.kind == "fake"
    assert (await host.status())["simulated"] is True
    assert (await host.reboot())["simulated"] is True


async def test_networks_come_back_with_which_one_is_in_use():
    host = HostControl(fake=True)
    await host.join("Kitchen", "a good password")
    networks = await host.networks()
    kitchen = next(n for n in networks if n["ssid"] == "Kitchen")
    assert kitchen["active"] and kitchen["saved"]
    assert all(set(n) == {"ssid", "signal", "security", "saved", "active"} for n in networks)


async def test_a_link_is_ethernet_or_wifi_and_nothing_else():
    host = HostControl(fake=True)
    assert (await host.prefer("ethernet"))["link"] == "ethernet"
    with pytest.raises(HostError):
        await host.prefer("carrier pigeon")
