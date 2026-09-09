"""The echo canceller diagnostic, and the ways it could stop answering.

`ranger/web/aec.html` exists to settle one assumption: whether Chrome's echo
canceller includes **Web Audio output** in its render reference. The whole
value of the page is that it plays audio exactly the way `voice.js` does. A
page that played through an `<audio>` element would answer a different question
and would answer it yes, which is the worst possible outcome -- a green light
for a build that then does not work.

So what is asserted here is not that the page renders. It is that it still asks
the question it was written to ask.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "ranger" / "web"
PAGE = WEB / "aec.html"


@pytest.fixture
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture
def code(page: str) -> str:
    """The page with its prose removed.

    Asked of the code rather than the comments, for the same reason the
    hotword check in test_bargein is: this file explains at length what it is
    *not* doing, and "<audio>" appears in that explanation. A test that reads
    the explanation as the implementation would fail on a page that is right
    and pass on one that had been quietly changed.
    """
    import re

    body = page[page.index("<script type=\"module\">"):page.rindex("</script>")]
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)   # block comments, jsdoc
    body = re.sub(r"^\s*//.*$", " ", body, flags=re.M)    # whole-line comments
    return body


def test_the_page_is_served_from_the_web_root():
    """It has to be reached over http, not opened as a file. getUserMedia
    needs a secure context and file:// is not one, so a page sitting anywhere
    else on disk cannot be run at all."""
    assert PAGE.is_file()
    assert (WEB / "index.html").is_file(), "same root the interface is served from"


def test_it_plays_the_way_voice_js_plays(code: str):
    """**The point of the whole exercise.**

    Jarvis plays TTS as raw int16 through createBuffer at 24000 into a
    BufferSourceNode. If this page used an <audio> element or decodeAudioData
    it would be testing a path the real one never takes.
    """
    assert "createBuffer(1, pcm.length, 24000)" in code
    assert "createBufferSource()" in code
    assert "context.destination" in code

    assert "<audio" not in code, "an audio element is a different render path"
    assert "decodeAudioData" not in code, "the real path never decodes a container"
    assert "new Audio(" not in code


def test_it_measures_both_settings_and_reports_what_chrome_applied(page: str):
    """Asking for echoCancellation and being given it are different things, and
    a driver that refuses the constraint would otherwise read as 'AEC does not
    work on Web Audio'."""
    assert "echoCancellation: true" in page or "echoCancellation," in page
    assert "onSettings.echoCancellation === false" in page, (
        "a refused constraint is reported as a refusal, not as an answer"
    )
    assert "getSettings()" in page


def test_it_has_a_control_pass_with_nothing_playing(page: str):
    """**The lie this test can tell.** A muted speaker makes every pass equal to
    the room, which looks exactly like perfect cancellation. The room is
    measured first, and the uncancelled echo has to be well above it before its
    absence means anything."""
    assert "playing: false" in page
    assert "No echo to cancel, so this proves nothing" in page
    assert "off.held < floor * 2.5" in page


def test_it_measures_the_room_twice(page: str):
    """A room that changed between the two playing passes invalidates the
    comparison, and a fan starting mid-test is exactly the hazard this machine
    has already shown."""
    assert page.count("playing: false") == 2
    assert "The room changed while this ran" in page


def test_it_names_the_portaudio_conflict_by_its_error(page: str):
    """The second question, answered in the same sitting. An exclusive hold on
    the device surfaces as NotReadableError, and a bare 'could not open the
    microphone' would leave that unrecognised."""
    assert "NotReadableError" in page
    assert "ranger mic" in page


def test_it_reports_in_the_units_everything_else_uses(page: str):
    """int16 RMS and a held level, so these numbers sit beside the ones
    mic-bargein already printed instead of needing conversion."""
    assert "const SCALE = 32768" in page
    assert "HELD_FRAMES = 3" in page
    assert "function held(" in page


def test_it_does_not_suppress_noise_while_it_is_measuring(page: str):
    """Noise suppression removing the echo would answer the question with the
    wrong mechanism, and the real path would behave differently the moment it
    was turned off."""
    assert "noiseSuppression: false" in page
    assert "autoGainControl: false" in page


def test_it_writes_nothing_and_talks_to_nothing(code: str):
    """A diagnostic that posted anywhere would be a new intake path, and this
    one is meant to be openable without thinking about that."""
    for reach in ("fetch(", "XMLHttpRequest", "WebSocket", "localStorage",
                  "sessionStorage", "indexedDB", "navigator.sendBeacon"):
        assert reach not in code, f"the page reaches out through {reach}"
