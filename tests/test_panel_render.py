"""What the interface actually renders, asserted against the rendered result.

Two bugs shipped in one commit because nothing here could see a screen. The
preview sheet was `hidden` and on screen anyway, permanently, over the left
half of the window. The clear button on a draft row rendered and was invisible.
Both were reported as built, and every test we had looked at Python.

So there are two kinds of check in this file, because the two bugs had two
different causes:

- **The rendered tree.** `tests/js/render.mjs` builds the real `shell.js`
  against a small fake DOM and prints what it built. That catches a control
  that is not rendered, or is rendered on the wrong row, or is not wired to
  anything. It needs node, and skips without it.
- **The stylesheet.** A DOM cannot tell you that `display: flex` beats the
  browser's own `[hidden]` rule, or that `opacity: 0` means nobody can see the
  button that is definitely there. Those are read out of the CSS.

Neither is a browser. The operator looking at the window is still the check
that matters, and both of these bugs are the reason to say so plainly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "ranger" / "web"
RENDER = Path(__file__).resolve().parent / "js" / "render.mjs"
CSS = (WEB / "shell.css").read_text(encoding="utf-8")
SHELL = (WEB / "shell.js").read_text(encoding="utf-8")

node = shutil.which("node")
needs_node = pytest.mark.skipif(
    node is None,
    reason="the rendered-tree checks need node. The CSS checks below run anyway.",
)


@pytest.fixture(scope="module")
def rendered():
    result = subprocess.run(
        [node, str(RENDER)], capture_output=True, text=True, timeout=60,
        cwd=str(RENDER.parent),
    )
    assert result.returncode == 0, f"the shell did not render:\n{result.stderr}"
    return json.loads(result.stdout)


def state(rendered, label):
    for entry in rendered["states"]:
        if entry["label"] == label:
            return entry
    raise AssertionError(f"no state called {label}")


def rows(node, out=None):
    out = [] if out is None else out
    if "entry" in node["class"].split():
        out.append(node)
    for child in node["children"]:
        rows(child, out)
    return out


# -- the empty state, which is the one that shipped wrong ------------------


@needs_node
def test_a_fresh_session_has_no_preview_sheet_and_no_orb_offset(rendered):
    """**The snapshot of the empty state.**

    The default on load is: no sheet, orb centred, full starfield. The sheet
    does not exist until a document is previewed. Asserting only the open state
    is what let a permanently open, permanently empty sheet ship.
    """
    fresh = state(rendered, "load")

    assert fresh["preview"]["hidden"] is True
    assert fresh["preview"]["children"] == 0
    assert fresh["bodyClass"] == ""
    assert fresh["offsets"] == [], "the orb is not moved until there is something to move for"


@needs_node
def test_drawing_the_panel_does_not_open_the_preview(rendered):
    """A panel push happens on every turn. None of them is a document."""
    after = state(rendered, "panel")

    assert after["preview"]["hidden"] is True
    assert after["preview"]["children"] == 0
    assert after["offsets"] == []


@needs_node
def test_a_document_opens_the_sheet_and_the_orb_steps_aside(rendered):
    opened = state(rendered, "document")

    assert opened["preview"]["hidden"] is False
    assert opened["preview"]["children"] == 3, "chrome, caveat, body"
    assert "previewing" in opened["bodyClass"]
    assert opened["offsets"] and opened["offsets"][-1] > 0


@needs_node
def test_closing_puts_everything_back(rendered):
    """Orb centred, sheet gone, not merely emptied."""
    closed = state(rendered, "closed")

    assert closed["preview"]["hidden"] is True
    assert closed["preview"]["children"] == 0
    assert closed["bodyClass"] == ""
    assert closed["offsets"][-1] == 0


@needs_node
def test_the_sheet_has_a_visible_close_control(rendered):
    """Escape closes it and escape is not discoverable."""
    opened = state(rendered, "document")
    nodes = walk(opened["preview"]["tree"])
    clickable = [node["text"] for node in nodes if node["clickable"]]
    texts = [node["text"] for node in nodes]

    assert "close" in clickable, "escape is not discoverable; a control is"
    assert "show in folder" in clickable
    # The download is an anchor with a real href rather than a click handler,
    # which is what makes the browser save the file rather than the page
    # deciding to.
    assert "download" in texts


def walk(node, out=None):
    out = [] if out is None else out
    out.append(node)
    for child in node["children"]:
        walk(child, out)
    return out


# -- the control that was there and could not be seen ----------------------


@needs_node
def test_every_draft_row_has_a_clear_control(rendered):
    """It was rendering, and rendering invisibly. A Python test of clear_draft
    passes whether or not the button is on screen, which is why this asks the
    rendered tree instead."""
    drafts = [
        row for row in rows(rendered["panel"])
        if "import" not in row["class"].split()
    ]

    assert len(drafts) == 2, "one row per draft"
    for row in drafts:
        clears = [
            child for child in row["children"]
            if "dismiss" in child["class"].split() and child["clickable"]
        ]
        assert clears, f"no clear control on {row['children'][0]['text']!r}"
        assert clears[0]["text"] == "×"


@needs_node
def test_the_clear_control_sends_the_clear(rendered):
    """Wired to the server, not to a local idea of clearing."""
    assert rendered["sent"][0] == {
        "type": "clear_draft", "name": "Ranger/drafts/2026-09-08-telly.md"
    }


@needs_node
def test_a_generated_document_row_keeps_the_clear_and_gains_a_preview(rendered):
    """The document rows are new, and adding them must not have cost the
    drafts their clear control."""
    document_row = [
        row for row in rows(rendered["panel"]) if "document" in row["class"].split()
    ]

    assert len(document_row) == 1
    classes = [child["class"] for child in document_row[0]["children"]]
    assert any("preview-open" in name for name in classes)
    assert any("dismiss" in name for name in classes)


# -- what a DOM cannot see -------------------------------------------------


TOGGLED = re.findall(r"el\.(\w+)\.hidden\s*=", SHELL) + ["preview", "confirm"]


@pytest.mark.parametrize("name", sorted(set(TOGGLED)))
def test_anything_hidden_from_javascript_is_actually_hidden_by_the_stylesheet(name):
    """**The bug, generalised.**

    `hidden` is a browser default with the lowest possible specificity, so any
    `display:` rule on the same element beats it. `#confirm` carries a comment
    saying exactly this, written after the same trap left an invisible overlay
    eating every click. `#preview` was written one commit later and did not
    have the rule, so the sheet was hidden and on screen at the same time.

    Every element the shell hides from JavaScript is checked here, so the third
    one fails a test instead of shipping.
    """
    element = f"#{name}"
    if not re.search(rf"^{re.escape(element)}\s*\{{[^}}]*\bdisplay:", CSS, re.M):
        pytest.skip(f"{element} sets no display, so [hidden] works on its own")
    assert re.search(rf"{re.escape(element)}\[hidden\]\s*\{{[^}}]*display:\s*none", CSS), (
        f"{element} sets display and has no {element}[hidden] rule, so setting "
        "hidden in JavaScript will not hide it"
    )


def test_the_clear_control_is_visible_without_hovering():
    """It was `opacity: 0` until the row was under the pointer. Four drafts on
    screen and no sign there was a way to clear them: a control nobody can see
    is a control nobody has."""
    block = CSS.split(".entry .dismiss {")[1].split("}")[0]
    found = re.search(r"opacity:\s*([\d.]+)", block)

    assert found, "the clear control has no opacity rule at all"
    assert float(found.group(1)) > 0.2, (
        "the clear control is invisible until hover, which is how it was "
        "reported missing"
    )


def test_the_preview_sheet_is_not_in_the_markup_until_it_is_needed():
    """The element exists in index.html and starts hidden. Both halves matter:
    hidden in the markup, and a stylesheet that honours it."""
    page = (WEB / "index.html").read_text(encoding="utf-8")

    assert re.search(r'<section id="preview"[^>]*\bhidden\b', page)
    assert "#preview[hidden]" in CSS


# -- the drop, as far as a fake DOM can see it ------------------------------


@needs_node
def test_a_dropped_file_gets_a_row_with_its_date(rendered):
    """The panel shows imports the way it shows drafts, and the date is the
    point: an import is a dated snapshot of what an export said that day."""
    dropped = [row for row in rows(rendered["panel"]) if "import" in row["class"].split()]

    assert len(dropped) == 2
    titles = [row["children"][0]["text"] for row in dropped]
    assert titles == ["netsuite gp.xlsx", "pricing.pdf"]
    assert any("2026-09-09" == child["text"] for child in dropped[0]["children"])


@needs_node
def test_only_a_spreadsheet_offers_an_import(rendered):
    """A dropped PDF is context. Failing to be a GP export is not a failure and
    does not put a button on the row that would do nothing."""
    dropped = [row for row in rows(rendered["panel"]) if "import" in row["class"].split()]
    buttons = [
        [child["text"] for child in row["children"] if child["clickable"]]
        for row in dropped
    ]

    assert buttons[0] == ["import"]
    assert buttons[1] == [], "a PDF has nothing to map"


@needs_node
def test_nothing_is_parsed_until_the_import_button_is_clicked(rendered):
    """The drop lands and stops. Asking for a mapping is a separate act, taken
    by a person, which is what makes an accidental drop free."""
    assert {"type": "import_propose", "name": "netsuite gp.xlsx"} in rendered["sentAfterConfirm"]


@needs_node
def test_the_mapping_card_shows_the_rows_it_would_write(rendered):
    """A confirmation that does not show what it will write is a click, not a
    decision."""
    texts = [node["text"] for node in walk(rendered["proposal"])]

    assert "netsuite gp.xlsx" in texts
    assert any("not seen this shape before" in text for text in texts)
    assert "period column" in texts and "gross profit column" in texts
    assert "2026-06" in texts and "2026-08" in texts
    assert any("was 48250" in text for text in texts)
    assert any("2 months updated" in text for text in texts)
    assert any("Total 573500" in text for text in texts), "and what it will not write"


@needs_node
def test_confirming_sends_the_columns_and_closes_the_sheet(rendered):
    assert rendered["sentAfterConfirm"][-1] == {
        "type": "import_apply",
        "name": "netsuite gp.xlsx",
        "period": "Period",
        "amount": "Gross Profit",
    }
    closed = state(rendered, "after confirm")
    assert closed["preview"]["hidden"] is True
    assert closed["offsets"][-1] == 0, "and the orb goes back to the middle"


def test_the_drag_target_is_not_the_microphone_colour():
    """Orange means the microphone is live and keeps one meaning."""
    block = CSS.split("body.dropping::after {")[1].split("}")[0]

    assert "var(--accent)" in block
    assert "orange" not in block.lower()
    for hex_colour in re.findall(r"#([0-9a-fA-F]{6})", block):
        red, green, blue = (int(hex_colour[i:i + 2], 16) for i in (0, 2, 4))
        assert green + blue > red, f"#{hex_colour} is a warm colour"
