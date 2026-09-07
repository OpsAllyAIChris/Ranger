"""Tier 7a: the static server and the front end it ships.

Nothing here renders anything. What it can prove is the set of things that
break the browser without any Python error to show for it: a missing vendored
file, a script served under a MIME type the browser refuses, or a page that
quietly depends on the network.
"""

from __future__ import annotations

import http.client
import mimetypes
import re
import threading
from pathlib import Path

import pytest

from ranger.server import WEB_ROOT, build, describe

PAGE = WEB_ROOT / "index.html"
ORB = WEB_ROOT / "orb.js"
SHELL = WEB_ROOT / "shell.js"
STYLE = WEB_ROOT / "shell.css"


@pytest.fixture
def running(config):
    """A real server on a real socket, on a port the OS picks."""
    from dataclasses import replace

    server = build(replace(config, server=replace(config.server, port=0)))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


# ------------------------------------------------------------------ serving


def test_the_front_end_ships_with_the_package():
    """Inside ranger/, not beside it. A non-editable install still has a face."""
    assert PAGE.is_file()
    assert ORB.is_file()
    assert WEB_ROOT.is_relative_to(Path(__file__).resolve().parent.parent / "ranger")


def test_the_page_and_the_module_are_served(running):
    status, _, body = get(running, "/")
    assert status == 200
    assert b"<canvas id=\"scene\">" in body

    status, _, body = get(running, "/orb.js")
    assert status == 200
    assert b"export function createOrb" in body


def test_javascript_is_served_as_javascript_even_on_a_poisoned_registry(running, monkeypatch):
    """The Windows failure with no Linux symptom.

    mimetypes seeds itself from HKEY_CLASSES_ROOT on Windows, and plenty of
    machines have .js mapped to text/plain because an installer wrote it there
    years ago. A browser refuses to execute a module script served under that,
    so the page would load, fetch orb.js, and do nothing at all, with no error
    anywhere in Python. Simulated here by poisoning the table the same way.
    """
    mimetypes.add_type("text/plain", ".js")
    from ranger.server import _force_types

    _force_types()

    _, headers, _ = get(running, "/orb.js")
    assert headers["Content-type"] == "text/javascript"


def test_nothing_outside_the_web_root_is_reachable(running):
    for path in ("/../ranger.toml", "/%2e%2e/ranger.toml", "/../../etc/passwd"):
        status, _, _ = get(running, path)
        assert status in (400, 403, 404), path


def test_there_are_no_directory_listings(running):
    status, _, _ = get(running, "/vendor/")
    assert status == 404


def test_the_page_is_never_cached(running):
    """The operator will be reloading this while tuning it."""
    _, headers, _ = get(running, "/")
    assert headers["Cache-Control"] == "no-store"


def test_the_url_is_printed_as_localhost(config):
    from dataclasses import replace

    assert describe(config).endswith(f":{config.server.port}/")
    assert describe(config).startswith("http://localhost:")
    other = replace(config, server=replace(config.server, host="192.168.1.9"))
    assert describe(other).startswith("http://192.168.1.9:")


def test_a_missing_front_end_says_so_rather_than_serving_nothing(config, tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        build(config, root=tmp_path)
    assert "index.html" in str(exc.value)


# ------------------------------------------------- the page needs no network


def test_the_page_asks_nothing_of_the_internet():
    """A CDN link is a black rectangle on a laptop behind a corporate proxy.

    It is also a third party learning every time the operator opens a page that
    renders their account names and their drafts.
    """
    for path in (PAGE, ORB, SHELL, STYLE):
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s\"'<>)]+", text):
            assert url.startswith("http://www.w3.org/"), f"{path.name} reaches out to {url}"


def test_every_vendored_import_resolves_to_a_vendored_file():
    """The import closure, checked.

    Vendoring three.js by hand means copying a file list. Miss one transitive
    import and everything works here and fails in the browser with a bare
    "failed to resolve module specifier". This walks what is actually imported.
    """
    vendor = WEB_ROOT / "vendor" / "three"
    addons = vendor / "addons"

    seen: set[Path] = set()
    queue = [ORB]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        source = current.read_text(encoding="utf-8")
        for spec in re.findall(r"from\s+['\"]([^'\"]+)['\"]", source):
            if spec == "three":
                target = vendor / "three.module.min.js"
            elif spec.startswith("three/addons/"):
                target = addons / spec[len("three/addons/"):]
            elif spec.startswith("."):
                target = (current.parent / spec).resolve()
            else:
                pytest.fail(f"{current.name} imports {spec!r}, which nothing maps")
            assert target.is_file(), f"{current.name} imports {spec!r}, missing at {target}"
            queue.append(target)

    assert (vendor / "three.module.min.js") in seen, "the module build is never reached"
    assert len(seen) >= 5, f"only walked {len(seen)} files, the closure looks wrong"


def test_the_import_map_matches_where_the_files_actually_are():
    page = PAGE.read_text(encoding="utf-8")
    assert '"three": "./vendor/three/three.module.min.js"' in page
    assert '"three/addons/": "./vendor/three/addons/"' in page
    assert (WEB_ROOT / "vendor/three/three.module.min.js").is_file()
    assert (WEB_ROOT / "vendor/three/addons").is_dir()


def test_three_is_licensed_and_its_version_recorded():
    readme = (WEB_ROOT / "vendor" / "three" / "README.md").read_text(encoding="utf-8")
    assert "0.160.0" in readme
    assert (WEB_ROOT / "vendor" / "three" / "LICENSE").is_file()


# ----------------------------------------------------------- design tokens


@pytest.mark.parametrize(
    "token",
    ["#0E0F13", "#16171D", "#2DD4A8", "cubic-bezier(0.16, 1, 0.3, 1)", "JetBrains Mono", "Inter"],
)
def test_the_design_tokens_are_in_the_page(token):
    assert token in STYLE.read_text(encoding="utf-8")


def test_every_transition_uses_the_one_easing_curve():
    """No exceptions was the instruction, so this is the check.

    Catches the transition written in a hurry with `ease` or `ease-out`, which
    is the one thing that makes a set of animations feel assembled rather than
    designed.
    """
    import re

    style = STYLE.read_text(encoding="utf-8")
    for line in style.splitlines():
        stripped = line.strip()
        if not re.match(r"^(transition|animation)(-timing-function)?\s*:", stripped):
            continue
        assert "var(--ease)" in stripped, f"not on the shared easing: {stripped}"
    assert "--ease: cubic-bezier(0.16, 1, 0.3, 1)" in style


def test_the_fonts_are_vendored_and_licensed():
    fonts = WEB_ROOT / "vendor" / "fonts"
    faces = sorted(p.name for p in fonts.glob("*.woff2"))
    assert faces == [
        "inter-latin-400-normal.woff2",
        "inter-latin-500-normal.woff2",
        "inter-latin-600-normal.woff2",
        "jetbrains-mono-latin-400-normal.woff2",
    ]
    assert (fonts / "LICENSE-inter").is_file()
    assert (fonts / "LICENSE-jetbrains-mono").is_file()
    style = STYLE.read_text(encoding="utf-8")
    for face in faces:
        assert face in style, f"{face} is vendored but never used"


def test_the_shell_decides_nothing():
    """Amendment A in the browser.

    The shell may render what arrived and send what was typed or clicked. It
    may not decide whether a tool runs, and it may not keep its own idea of
    what state Ranger is in.
    """
    source = SHELL.read_text(encoding="utf-8")
    assert "type: 'decision'" in source, "the card must send its answer, not act on it"
    for forbidden in ("localStorage", "sessionStorage", "eval(", "innerHTML ="):
        assert forbidden not in source, f"the shell should not use {forbidden}"


def test_the_orb_has_exactly_one_input():
    """Amendment A, at the far end. The scene knows a number and nothing else."""
    source = ORB.read_text(encoding="utf-8")
    assert "setVoiceBright" in source
    for forbidden in ("WebSocket", "fetch(", "XMLHttpRequest", "localStorage"):
        assert forbidden not in source, f"orb.js should not know about {forbidden}"
