"""The admin page on a phone: it fits, its tabs stay in one row, and the layout editor works
by touch. Nothing here is saved."""

from __future__ import annotations

from conftest import open_admin, unlock

SECTIONS = ["sources", "cameras", "views", "layouts", "display", "detection", "device", "security"]

# A finger dragged across a fresh 3 × 2 layout, corner to corner. The events go to the cell the
# touch started on, as a browser's implicit touch capture sends them: an editor listening per
# cell never hears the rest of the drag.
TOUCH_DRAG = """async () => {
  const card = document.querySelector('#layouts > article:last-of-type');
  const cells = card.querySelector('.layout-cells');
  const cell = (x, y) => cells.querySelector(`[data-x="${x}"][data-y="${y}"]`);
  cell(0, 0).scrollIntoView({ block: 'center' });
  const centre = (node) => {
    const r = node.getBoundingClientRect();
    return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  };
  const [from, to] = [centre(cell(0, 0)), centre(cell(2, 1))];
  const send = (type, at) => cell(0, 0).dispatchEvent(new PointerEvent(type, {
    bubbles: true, cancelable: true, pointerId: 9, pointerType: 'touch', isPrimary: true,
    button: 0, buttons: 1, ...at }));
  const before = card.querySelectorAll('.layout-tile').length;
  send('pointerdown', from);
  send('pointermove', to);
  send('pointerup', to);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const after = document.querySelector('#layouts > article:last-of-type');
  return { before, after: after.querySelectorAll('.layout-tile').length,
           touchAction: getComputedStyle(after.querySelector('.layout-cells')).touchAction };
}"""


def test_every_section_fits_a_phone_and_the_tabs_stay_in_one_row(phone):
    page = phone.page()
    unlock(phone)
    open_admin(page, "sources")
    for section in SECTIONS:
        page.evaluate(f"location.hash = '#{section}'")
        page.locator(f"#panel-{section}").wait_for()
        page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        too_wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert too_wide <= 0, f"{section} is {too_wide}px wider than the phone"
    rows = page.evaluate("new Set([...document.querySelectorAll('#tabs .tab')].map((t) => t.offsetTop)).size")
    assert rows == 1


def test_an_unsaved_change_brings_up_the_save_bar_until_it_is_discarded(phone):
    page = phone.page()
    unlock(phone)
    open_admin(page, "display")
    switch = page.locator("#display input.switch").first
    was = switch.is_checked()
    switch.click()
    page.locator("#savebar").wait_for(state="visible")
    assert page.locator('.tab[data-section="display"]').evaluate("t => t.classList.contains('dirty')")
    page.locator("#discard").click()
    page.locator("#savebar").wait_for(state="hidden")
    assert page.locator("#display input.switch").first.is_checked() == was


def test_a_layout_can_be_edited_by_touch(phone):
    page = phone.page()
    unlock(phone)
    open_admin(page, "layouts")
    page.locator("#add-layout").click()
    page.wait_for_timeout(500)                    # the new layout scrolls into view smoothly
    drag = page.evaluate(TOUCH_DRAG)
    assert drag["touchAction"] == "none", "a finger on the editor would scroll the page instead"
    assert (drag["before"], drag["after"]) == (6, 1)
    page.locator("#discard").click()
    page.locator("#savebar").wait_for(state="hidden")
