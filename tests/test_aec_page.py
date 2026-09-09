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


def test_noise_suppression_is_off_by_default_and_measured_separately(code: str):
    """Noise suppression removing the echo would answer the cancellation
    question with the wrong mechanism. It gets its own run instead of being
    reasoned about: it is aimed squarely at a room that swings nineteen times
    over, and that is worth a measurement."""
    assert "noiseSuppression = false" in code, "off unless asked for"
    assert "run(true)" in code and "run(false)" in code, "both runs exist"
    assert "autoGainControl: false" in code, (
        "AGC moves every level at once, which makes passes incomparable"
    )


def test_it_measures_the_operator_through_the_cancelled_stream(code: str):
    """**The pass without which none of this decides anything.**

    A reduction from 1210 to 534 says the canceller works. It says nothing
    about whether the operator is audible over what is left, and a ratio
    between two numbers that do not include the operator is not a margin.
    """
    assert "talking: true" in code
    assert code.count("playing: true") == 3, (
        "uncancelled, cancelled, and cancelled with the operator talking"
    )


def test_a_talking_pass_counts_the_operator_in(code: str):
    """Four seconds that start while somebody is drawing breath measure the
    breath."""
    assert "get ready to talk" in code
    assert "TALK NOW" in code


def test_it_will_not_say_build_it_when_the_voice_is_under_the_residual(code: str):
    """The reading that would be worst to get wrong. A voice below the residual
    echo is a pass that caught the operator mid-pause, not a margin of less
    than one, and reporting it as a thin margin would invite a build on it."""
    assert "voice.held <= on.held" in code
    assert "not louder than the residual echo" in code


def test_it_refuses_a_margin_too_thin_to_be_worth_the_change(code: str):
    """A ceiling under 1.6x is a build that buys almost nothing, and saying so
    is the point of running this before building rather than after."""
    assert "ceiling < 1.6" in code
    assert "Do not build this" in code


def test_the_margin_uses_the_detector_own_three_terms(code: str):
    """floor, room x room_margin, echo x margin -- the same rule bargein.py
    applies, so the number reported here is the number the detector would see
    rather than a friendlier one."""
    assert "ROOM_MARGIN = 1.4" in code
    assert "mic-bargein has to measure through the" in code, (
        "and it says that this page becomes the authority only if capture moves"
    )


# -- the reading, run rather than read -------------------------------------
#
# Everything above searches the source, which is as far as a text check goes.
# It is not far enough: wiring the room term to a constant `true` left every
# one of those passing, because they asserted that the words `roomBar` and
# `clearsRoom` appear in the file.
#
# So the verdict function is run, against the levels this machine actually
# reported plus the states that have to be refused.


@pytest.fixture(scope="module")
def verdicts():
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("the verdict checks need node")
    result = subprocess.run(
        [node, str(Path(__file__).resolve().parent / "js" / "aec.mjs")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_ordinary_voice_over_the_measured_residual_is_a_build(verdicts):
    """Room 282, echo 1210 uncancelled and 534 cancelled: measured. The voice
    is the pass that was missing, and 3000 is ordinary speech."""
    assert verdicts["ordinary"]["kind"] == "good"
    assert "Build it" in verdicts["ordinary"]["text"]


def test_a_quiet_voice_still_clears_it(verdicts):
    assert verdicts["quiet"]["kind"] == "good"


def test_a_very_quiet_voice_is_refused_as_too_thin(verdicts):
    """1.6x is not a margin worth moving the microphone for."""
    assert verdicts["veryQuiet"]["kind"] == "bad"
    assert "Do not build this" in verdicts["veryQuiet"]["text"]


def test_a_voice_under_the_residual_is_a_bad_pass_not_a_thin_margin(verdicts):
    assert verdicts["underResidual"]["kind"] == "warn"
    assert "not louder than the residual" in verdicts["underResidual"]["text"]


def test_a_room_the_size_of_the_voice_is_refused_even_with_the_echo_gone(verdicts):
    """**The room term, run rather than read.**

    The echo is cancelled to 534 and the operator holds 3000 over it, which on
    the echo term alone is a comfortable build. The room holds 2400. Wiring
    this branch to a constant `true` passed every source check in this file.
    """
    assert verdicts["roomTooLoud"]["kind"] == "bad"
    assert "does not leave room for you" in verdicts["roomTooLoud"]["text"]


def test_a_room_that_changed_mid_test_invalidates_the_run(verdicts):
    """This guard could not fire. It compared the difference between the two
    room readings against 1.5x the larger of them, and a difference between two
    numbers is never more than the larger one -- so a room that quadrupled
    read as a valid measurement. It is a ratio now."""
    assert verdicts["roomChanged"]["kind"] == "warn"
    assert "The room changed" in verdicts["roomChanged"]["text"]


def test_a_silent_speaker_is_named_rather_than_read_as_perfect(verdicts):
    assert verdicts["speakerMuted"]["kind"] == "warn"
    assert "proves nothing" in verdicts["speakerMuted"]["text"]


def test_a_refused_constraint_is_not_an_answer_about_web_audio(verdicts):
    assert verdicts["refused"]["kind"] == "bad"
    assert "refused the constraint" in verdicts["refused"]["text"]


def test_the_noise_suppression_run_says_which_run_it_was(verdicts):
    """Two runs produce two verdicts and they have to be tellable apart on the
    screen, or the better number gets remembered without its condition."""
    assert "noise suppression on" in verdicts["withNs"]["text"]


def test_it_writes_nothing_and_talks_to_nothing(code: str):
    """A diagnostic that posted anywhere would be a new intake path, and this
    one is meant to be openable without thinking about that."""
    for reach in ("fetch(", "XMLHttpRequest", "WebSocket", "localStorage",
                  "sessionStorage", "indexedDB", "navigator.sendBeacon"):
        assert reach not in code, f"the page reaches out through {reach}"
