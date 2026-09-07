"""The taskbar shortcut, and the icon on it.

Most of this file cannot be verified on anything but Windows, so what is tested
is what would be silently wrong there: the shape of the icon bytes, the quoting
in the PowerShell that writes the .lnk, and the decision about whether to start
a second server. The parts that talk to user32 are best effort by construction
and return rather than raise, which is itself asserted.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ranger.desktop import APP_FLAG, Shortcut, focus_window
from ranger.icon import SIZES, draw, ico, png


# -- the icon ---------------------------------------------------------------


def test_the_icon_is_a_real_ico_holding_every_size():
    data = ico()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an icon directory"
    assert count == len(SIZES)

    for index in range(count):
        entry = data[6 + index * 16 : 22 + index * 16]
        width, height, colours, _, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", entry
        )
        expected = SIZES[index]
        assert width == (0 if expected >= 256 else expected)
        assert bits == 32 and planes == 1 and colours == 0
        # Every entry has to point at a real PNG inside the same file.
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + length <= len(data)


def test_the_png_decodes_to_the_pixels_that_went_in():
    """Written by hand, so the encoding is checked rather than assumed."""
    rows = draw(16)
    data = png(rows)

    width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, colour_type) == (16, 16, 8, 6)

    start = data.index(b"IDAT") + 4
    end = start + struct.unpack(">I", data[data.index(b"IDAT") - 4 : data.index(b"IDAT")])[0]
    raw = zlib.decompress(data[start:end])
    assert len(raw) == 16 * (1 + 16 * 4)
    assert raw[0] == 0, "filter byte on the first row"
    assert tuple(raw[1:5]) == rows[0][0]


def test_the_icon_is_transparent_at_the_corners_and_bright_in_the_middle():
    """A rounded square with a lit centre, or it is not the orb."""
    rows = draw(64)
    assert rows[0][0][3] == 0, "the corner should be transparent"
    centre = rows[32][32]
    assert centre[3] == 255
    assert centre[1] > 200, "the core should be near white, not muddy"
    edge = rows[32][2]
    assert edge[0] < 60 and edge[1] < 90, "the ground should stay dark"


def test_the_icon_is_the_same_bytes_every_time():
    """It is generated rather than committed, so it has to be deterministic or
    every run is a diff."""
    assert ico() == ico()


# -- the shortcut -----------------------------------------------------------


@pytest.fixture
def shortcut(tmp_path):
    return Shortcut(
        path=tmp_path / "Ranger.lnk",
        target=Path(r"C:\Users\Chris.Hardwick\Ranger\.venv\Scripts\pythonw.exe"),
        arguments=r'-m ranger -c "C:\Users\Chris.Hardwick\Ranger\ranger.toml" open',
        working_directory=Path(r"C:\Users\Chris.Hardwick\Ranger"),
        icon=Path(r"C:\Users\Chris.Hardwick\Ranger\ranger.ico"),
    )


def test_the_shortcut_script_sets_everything_that_matters(shortcut):
    script = shortcut.script()
    assert "WScript.Shell" in script
    for value in (
        str(shortcut.target),
        str(shortcut.working_directory),
        str(shortcut.icon),
        "open",
    ):
        assert value in script
    assert "$link.Save()" in script


def test_a_quote_in_a_path_cannot_end_the_powershell_string(tmp_path):
    """Everything is single quoted, so the one escape that matters is a single
    quote, and it doubles."""
    nasty = Shortcut(
        path=tmp_path / "Ranger.lnk",
        target=Path("C:/O'Brien/python.exe"),
        arguments="-m ranger open",
        working_directory=Path("C:/O'Brien"),
        icon=Path("C:/O'Brien/ranger.ico"),
        description="Chris' Ranger",
    )
    script = nasty.script()
    assert "O''Brien" in script
    assert "Chris'' Ranger" in script
    # No unescaped quote can close a string early.
    for line in script.splitlines():
        if "= '" in line:
            body = line.split("= '", 1)[1]
            assert body.endswith("'")
            assert body[:-1].replace("''", "") .count("'") == 0


def test_focus_never_raises_wherever_it_runs():
    """It is best effort by design: a second window is a much smaller problem
    than a shortcut that errors."""
    result = focus_window("Definitely Not A Window " * 4)
    assert result.focused is False
    assert result.detail


def test_the_window_is_asked_for_as_an_app_not_a_tab():
    assert APP_FLAG == "--app="


# -- deciding whether to start anything -------------------------------------


def test_probe_says_nothing_is_running_when_nothing_is(config):
    from ranger.server import probe

    assert probe(config, timeout=0.2) is None


def test_probe_answers_a_running_server(config):
    """The check the shortcut makes before starting a second one."""
    import threading
    from dataclasses import replace

    from ranger.server import build, probe, read_lock

    server = build(replace(config, server=replace(config.server, port=0)))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bound = replace(config, server=replace(config.server, port=port))
        answer = probe(bound, timeout=2)
        assert answer is not None
        assert answer["ranger"] is True
        assert answer["port"] == port
        assert answer["sessions"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_configured_port_of_zero_is_read_back_off_the_lock_file(config, tmp_path):
    """0 means "let the OS choose", so only the running server knows which."""
    import json

    from ranger.server import lock_path, read_lock

    lock_path(config).parent.mkdir(parents=True, exist_ok=True)
    lock_path(config).write_text(json.dumps({"pid": 1, "port": 65000}), encoding="utf-8")
    assert read_lock(config)["port"] == 65000


def test_the_url_uses_the_port_that_was_actually_bound(config):
    """Otherwise a configured port of 0 prints localhost:0, which is not a
    thing anyone can open."""
    from ranger.server import describe

    assert describe(config, 34769).endswith(":34769/")
    assert describe(config).endswith(f":{config.server.port}/")
