"""Tier 7. The local server the browser front end talks to.

In 7a this serves static files and nothing else, because 7a is the orb and the
orb has no input. The websocket that carries a turn to `Ranger.turn()` arrives
in 7b, in this file, next to what is already here.

Amendment A decides the shape: the browser is the fourth caller of the core,
not a second copy of it. Nothing in `ranger/web` decides anything. This module
hands it pixels and, from 7b, a stream of events.

It binds to 127.0.0.1 by default. The vault holds customer emails and pricing,
and the page that renders them has no authentication, so it must not be
reachable from the network. Changing `server.host` is the operator's decision
to make deliberately, in config, not something that happens by default.
"""

from __future__ import annotations

import mimetypes
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config

#: Where the front end lives. Inside the package, so a non-editable install
#: still has one and nothing has to guess at the repository root.
WEB_ROOT = Path(__file__).resolve().parent / "web"


def _force_types() -> None:
    """Windows reads MIME types out of the registry, and gets .js wrong.

    `mimetypes` seeds itself from HKEY_CLASSES_ROOT on Windows, where plenty of
    machines have `.js` mapped to `text/plain` or `application/x-javascript`
    because some installer wrote it there years ago. A browser refuses to run a
    module script served under either, so the page would load, fetch orb.js,
    and silently do nothing. That is a Windows only failure with no Linux
    symptom, so the types are pinned here rather than trusted.
    """
    for suffix, kind in (
        (".js", "text/javascript"),
        (".mjs", "text/javascript"),
        (".html", "text/html"),
        (".css", "text/css"),
        (".json", "application/json"),
        (".svg", "image/svg+xml"),
        (".wasm", "application/wasm"),
    ):
        mimetypes.add_type(kind, suffix)


class FrontEnd(ThreadingHTTPServer):
    """Not SO_REUSEADDR.

    On Windows that option lets a second server bind a port the first one is
    already using, and the second silently wins. That would mean editing a page
    and reloading a different one. A taken port should say so.
    """

    allow_reuse_address = False
    daemon_threads = True


class FrontEndHandler(SimpleHTTPRequestHandler):
    """Static files out of one directory, quietly."""

    #: Set by `serve`. Printing a line per request turns the terminal into a
    #: log when the operator is trying to read one number off the screen.
    verbose = False

    def list_directory(self, path):  # noqa: D102 - no listings, ever
        self.send_error(404, "no listing here")
        return None

    def log_message(self, fmt: str, *args) -> None:
        if self.verbose:
            super().log_message(fmt, *args)

    def end_headers(self) -> None:
        # A front end being edited must not be served out of the disk cache.
        # The operator will be reloading this page while tuning it.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def describe(config: Config) -> str:
    """The URL to open. One place, so the CLI and the tests agree."""
    host = config.server.host
    shown = "localhost" if host in {"127.0.0.1", "0.0.0.0", "::1", ""} else host
    return f"http://{shown}:{config.server.port}/"


def build(config: Config, *, root: Path | None = None, verbose: bool = False) -> FrontEnd:
    """Bind the server. Raises OSError if the port is taken."""
    _force_types()
    web_root = root or WEB_ROOT
    if not (web_root / "index.html").is_file():
        raise FileNotFoundError(
            f"the front end is missing: {web_root / 'index.html'} does not exist"
        )

    handler = partial(FrontEndHandler, directory=str(web_root))
    FrontEndHandler.verbose = verbose
    return FrontEnd((config.server.host, config.server.port), handler)


def serve(config: Config, *, open_browser: bool = False, verbose: bool = False) -> int:
    try:
        httpd = build(config, verbose=verbose)
    except FileNotFoundError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"  cannot bind {config.server.host}:{config.server.port}: {exc}\n"
            f"  Something is already using that port. Change [server] port in your\n"
            f"  local config, or stop the other one.",
            file=sys.stderr,
        )
        return 1

    url = describe(config)
    print(f"Ranger  {url}")
    print("  the orb only. There is nothing to type into yet; that is 7b.")
    print("  the terminal is still the way to talk to it: run 'ranger' with no flags.")
    print("  ctrl-c to stop.")

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
