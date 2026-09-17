"""The admin page's lock: settings are shown locked, and changing them needs the password."""

from __future__ import annotations

from conftest import ADMIN_PASSWORD, TOKEN, WALL, open_admin, unlock, wait_until


def test_without_the_token_nothing_is_shown_until_someone_unlocks(desktop):
    page = desktop.page()
    open_admin(page, "display")
    assert page.locator("#unlock-dialog").evaluate("d => d.open")
    assert page.locator("#shell").is_hidden()


def test_with_the_token_the_settings_are_shown_locked_and_usernames_are_not(desktop):
    page = desktop.page()
    open_admin(page, "sources", token=TOKEN)
    assert page.locator("#lockable").evaluate("f => f.disabled")
    assert page.locator("#lock-button").inner_text().strip() == "Unlock"
    assert page.locator("#lockbar").is_visible()
    username = page.locator('[data-source="nvr"] input[type=text]').first
    assert username.input_value() == "" and "Hidden" in username.get_attribute("placeholder")
    assert page.locator("#add-source").is_hidden()


def test_a_wrong_password_is_refused_in_the_dialog(desktop):
    page = desktop.page()
    open_admin(page, "display", token=TOKEN)
    page.locator("#lockbar-unlock").click()
    page.locator("#unlock-pass").fill("not the password")
    page.locator("#unlock-submit").click()
    error = page.locator("#unlock-error")
    error.wait_for(state="visible")
    assert "Wrong" in error.inner_text()
    assert page.locator("#lockable").evaluate("f => f.disabled")


def test_unlocking_saving_and_locking_again(desktop, admin, restore):
    page = desktop.page()
    open_admin(page, "display", token=TOKEN)
    page.locator("#lock-button").click()
    page.locator("#unlock-pass").fill(ADMIN_PASSWORD)
    page.locator("#unlock-submit").click()
    wait_until(lambda: not page.locator("#lockable").evaluate("f => f.disabled"), 10, "the page to unlock")

    clock = page.locator("#display input.switch").nth(3)          # On the screen: Clock
    before = clock.is_checked()
    clock.click()
    page.locator("#savebar").wait_for(state="visible")
    page.locator("#save").click()
    page.locator("#savebar").wait_for(state="hidden")
    assert admin.get("/api/config").json()["display"]["show_clock"] is (not before)

    page.reload()
    page.locator("#shell").wait_for(state="visible")
    assert page.locator("#display input.switch").nth(3).is_checked() is (not before)   # kept, and still unlocked
    assert page.locator("#lock-button").inner_text().strip() == "Lock"

    page.locator("#lock-button").click()
    wait_until(lambda: page.locator("#lockable").evaluate("f => f.disabled"), 10, "the page to lock again")
    assert page.locator("#lock-button").inner_text().strip() == "Unlock"


def test_a_session_that_ends_while_editing_asks_again_and_then_saves(desktop, admin, restore):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "display", token=TOKEN)
    page.locator("#display input.switch").nth(3).click()
    page.locator("#savebar").wait_for(state="visible")
    desktop.context.clear_cookies()                                  # the session runs out
    page.locator("#save").click()
    page.locator("#unlock-dialog").wait_for(state="visible")
    assert "session has ended" in page.locator("#unlock-reason").inner_text()
    page.locator("#unlock-pass").fill(ADMIN_PASSWORD)
    page.locator("#unlock-submit").click()
    page.locator("#savebar").wait_for(state="hidden", timeout=15_000)   # saved after unlocking


def test_security_says_where_the_password_comes_from(desktop):
    page = desktop.page()
    unlock(desktop)
    open_admin(page, "security")
    text = page.locator("#security").inner_text()
    assert "Settings are protected" in text and "VIEWPORT_ADMIN_PASSWORD" in text
    assert "Screens need a token" in text
