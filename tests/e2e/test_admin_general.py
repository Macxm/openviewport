"""The General section: readable while locked, and nothing acts on the device until it is not.

The agents in this suite run against the stand-in device (VIEWPORT_HOST_FAKE), so the network
and power controls are exercised without anything real restarting.
"""

from __future__ import annotations

from conftest import TOKEN, open_admin, unlock, wait_until


def facts(page) -> dict[str, str]:
    """The read-only rows, as {label: value}."""
    return page.locator("#general").evaluate(
        """box => Object.fromEntries([...box.querySelectorAll('.setting')]
             .filter(row => row.querySelector('.fact'))
             .map(row => [row.querySelector('.setting-label, label, .label')?.innerText.split('\\n')[0].trim()
                            ?? '', row.querySelector('.fact').innerText.trim()]))""")


def test_the_device_can_be_read_while_the_page_is_locked(desktop):
    page = desktop.page()
    open_admin(page, "general", token=TOKEN)
    page.locator("#general .fact").first.wait_for()

    shown = facts(page)
    assert shown.get("Version"), "the version is shown"
    assert shown.get("Connection"), "and how it is on the network"
    assert page.locator("#lockable").evaluate("f => f.disabled"), "while everything stays locked"


def test_nothing_can_be_done_to_the_device_until_it_is_unlocked(desktop):
    page = desktop.page()
    open_admin(page, "general", token=TOKEN)
    page.locator("#general .button-row button").first.wait_for()

    buttons = page.locator("#general .button-row button")
    assert buttons.count() >= 4
    for i in range(buttons.count()):
        assert buttons.nth(i).is_disabled(), "every action is out of reach while locked"


def test_the_setup_network_can_be_turned_on_and_off_again(desktop, admin):
    page = desktop.page()
    open_admin(page, "general", token=TOKEN)
    unlock(desktop)
    page.reload()
    page.locator("#general .fact").first.wait_for()
    assert not page.locator("#lockable").evaluate("f => f.disabled")

    page.get_by_role("button", name="Turn it on").click()
    wait_until(lambda: "setup network" in facts(page).get("Connection", "").lower(),
               15, "the device to say it is showing its own network")

    page.get_by_role("button", name="Turn it off").click()
    wait_until(lambda: "setup network" not in facts(page).get("Connection", "").lower(),
               15, "the device to come back off its own network")


def test_restarting_asks_first_and_cancelling_does_nothing(desktop, admin):
    page = desktop.page()
    open_admin(page, "general", token=TOKEN)
    unlock(desktop)
    page.reload()
    page.get_by_role("button", name="Restart", exact=True).click()

    dialog = page.locator("dialog.confirm")
    dialog.wait_for(state="visible")
    assert "Restart this device?" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    assert page.locator("dialog.confirm").count() == 0


def test_resetting_the_whole_device_needs_a_word_typed_first(desktop, admin):
    """The dialog is opened and abandoned: this suite never actually wipes its own agent."""
    page = desktop.page()
    open_admin(page, "general", token=TOKEN)
    unlock(desktop)
    page.reload()
    page.get_by_role("button", name="Reset the device").click()

    dialog = page.locator("dialog.confirm")
    dialog.wait_for(state="visible")
    confirm = dialog.get_by_role("button", name="Reset everything")
    assert confirm.is_disabled(), "it cannot be pressed by accident"

    dialog.locator("input[type=text]").fill("reset")
    assert not confirm.is_disabled()
    dialog.get_by_role("button", name="Cancel").click()
