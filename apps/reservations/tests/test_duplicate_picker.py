"""The duplicate-reservation picker asks twice before cloning a trip.

A chip used to submit the hidden form the instant it was clicked, so ×3 on the way to
reading the sentence cloned the trip three times — and undoing that is four deletes.
The chips now only *select*; the modal's own Duplicate button is what commits.

The picker is plain DOM rather than Alpine because the modal renders `html` through
`x-html`, which Alpine does not scan for directives — so these assertions read the
source of `static/js/app.js` the way the editor-component tests do.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


def _picker() -> str:
    """Just the duplicateReservation() body — so a match elsewhere can't pass a test."""
    start = APP_JS.index("duplicateReservation(pk)")
    return APP_JS[start : APP_JS.index("\n    },", start)]


# --- a chip selects; Duplicate commits ----------------------------------------------


def test_a_chip_click_selects_instead_of_duplicating():
    """The old handler cloned on the chip's own click. Nothing may submit from there."""
    body = _picker()
    assert "window.__apcDuplicateGo" not in APP_JS, "the submit-on-click global is back"
    assert 'onclick="window.__apcDup.pick(${n})"' in body, "chips no longer select"
    assert "[1, 2, 3, 5].map(chip)" in body, "the offered counts changed"


def test_only_the_confirm_button_submits_the_count():
    body = _picker()
    assert "onConfirm: () => submit(" in body
    # one submit path, reached only from the modal's own button
    assert body.count("form.submit()") == 1


def test_one_copy_is_selected_to_begin_with():
    """×1 renders already chosen, so Duplicate is meaningful without touching a chip.

    It is painted in the markup rather than by a paint() on open: x-html lands on
    Alpine's own schedule, and a timer racing it is how a picker opens unpainted.
    """
    body = _picker()
    chip = body[body.index("const chip = (n)") : body.index("Alpine.store")]
    assert 'aria-pressed="${n === 1}"' in chip
    assert "${n === 1 ? CHIP_ON : CHIP_OFF}" in chip, "the starting chip is not painted"
    assert "CHIP_ON = " in body and "bg-goldl" in body
    assert "paint()" not in chip, "painting on open races x-html"


# --- one selection at a time ---------------------------------------------------------


def test_picking_a_chip_empties_and_locks_the_custom_box():
    """Two visible numbers and no way to tell which one Duplicate will use is the bug."""
    body = _picker()
    pick = body[body.index("pick(n)") : body.index("useCustom()")]
    assert "readOnly = true" in pick
    assert 'value = ""' in pick, "the custom box keeps a stale number"
    assert "this.custom = false" in pick


def test_clicking_the_custom_box_drops_the_chip_selection():
    body = _picker()
    use_custom = body[body.index("useCustom() {") : body.index("paint() {")]
    assert "this.custom = true" in use_custom
    assert "readOnly = false" in use_custom, "the box never becomes editable"
    # readonly, not disabled — a disabled input never receives the click asking for it
    assert "disabled=" not in body and ".disabled" not in body
    assert "onfocus=" in body, "nothing hands the count to the box"


def test_the_custom_box_is_never_left_blank():
    """accept() always closes the modal, so a blank box at confirm time has no second
    chance to ask — it carries the count the chip had."""
    use_custom = _picker()
    use_custom = use_custom[use_custom.index("useCustom()") :]
    assert "value = String(this.count)" in use_custom


def test_the_committed_count_follows_whichever_input_is_selected():
    body = _picker()
    assert "value() { return this.custom ? this.input().value : this.count; }" in body
