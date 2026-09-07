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

import asyncio
import json
import mimetypes
import sys
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config

#: Where the browser opens its socket. One port for the page and the socket, so
#: there is one URL to remember and one thing to unblock in a firewall.
WS_PATH = "/ws"

#: Answers "is it already running, and is anyone looking at it". Written for
#: the taskbar shortcut, which must not start a second server or open a second
#: window, and useful on its own when nothing seems to be happening.
STATUS_PATH = "/status"

#: Beside Ranger's own files. Holds the pid and the port, so a shortcut can
#: find a server that is already up and a stop can find one to stop.
LOCK_FILE = "server.json"

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
        (".woff2", "font/woff2"),
    ):
        mimetypes.add_type(kind, suffix)


async def _off_the_loop(work):
    """Run a blocking call on a daemon thread and await it.

    Not `asyncio.to_thread`. That uses the default executor, whose threads are
    not daemons and which the interpreter joins on the way out. A reader
    blocked on a socket that nobody is going to write to never returns, so
    ctrl-c would print its goodbye and then hang forever with a browser tab
    still open. Same shape as the timer that once made the test suite say it
    had finished and then sit there for sixty seconds.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def run() -> None:
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiter
            loop.call_soon_threadsafe(_settle, done, exc, True)
        else:
            loop.call_soon_threadsafe(_settle, done, result, False)

    threading.Thread(target=run, daemon=True, name="ranger-ws-read").start()
    return await done


def _settle(future, value, failed: bool) -> None:
    if future.done():  # the connection went away while we were reading
        return
    if failed:
        future.set_exception(value)
    else:
        future.set_result(value)


class FrontEnd(ThreadingHTTPServer):
    """Not SO_REUSEADDR.

    On Windows that option lets a second server bind a port the first one is
    already using, and the second silently wins. That would mean editing a page
    and reloading a different one. A taken port should say so.
    """

    allow_reuse_address = False
    daemon_threads = True


class FrontEndHandler(SimpleHTTPRequestHandler):
    """Static files out of one directory, and one websocket."""

    #: Set by `serve`. Printing a line per request turns the terminal into a
    #: log when the operator is trying to read one number off the screen.
    verbose = False
    #: Set by `build`. The socket needs it to assemble a core.
    config: Config | None = None
    #: Set by `build`. How a connection gets its core. Overridden by tests with
    #: a scripted provider, and by 7c with a gate that can ask in the browser.
    session_factory = None

    #: How many browser sessions are connected right now. Class level, because
    #: every connection gets its own handler instance.
    sessions = 0

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path == WS_PATH.rstrip("/"):
            return self._websocket()
        if path == STATUS_PATH.rstrip("/"):
            return self._status()
        return super().do_GET()

    def _status(self) -> None:
        import os

        body = json.dumps(
            {
                "ranger": True,
                "pid": os.getpid(),
                "port": self.server.server_address[1],
                "sessions": type(self).sessions,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- the websocket -------------------------------------------------

    def _allowed_origin(self) -> bool:
        """A websocket is not subject to the same-origin policy. This is.

        Nothing stops a page on any website the operator happens to be visiting
        from opening ws://localhost:8765/ws and asking Ranger about their
        accounts. CORS does not apply to websockets, so the Origin header has
        to be checked here or not at all. A client that sends no Origin at all
        is not a browser, which is how the tests and curl reach it.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        allowed = {
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
            f"http://[::1]:{port}",
        }
        return origin in allowed

    def _websocket(self) -> None:
        from .wsframe import ProtocolError, accept_key

        self.close_connection = True

        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(400, "this endpoint speaks websocket")
            return
        if self.headers.get("Sec-WebSocket-Version", "") != "13":
            self.send_error(400, "only websocket version 13")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "no Sec-WebSocket-Key")
            return
        if not self._allowed_origin():
            # Deliberately blunt. This is another page trying to talk to the
            # operator's assistant, and it should learn nothing from the reply.
            self.log_error("refused a websocket from origin %r", self.headers.get("Origin"))
            self.send_error(403, "not your socket")
            return

        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key(key).encode("ascii") + b"\r\n"
            b"\r\n"
        )
        self.wfile.flush()

        try:
            asyncio.run(self._session())
        except (ProtocolError, OSError, ConnectionError):
            pass
        finally:
            try:
                self.connection.close()
            except OSError:
                pass

    async def _session(self) -> None:
        """One connection, one conversation, one turn at a time.

        Reads block, so they run in a worker thread. Everything else, including
        the core, runs on this connection's own event loop, which is why the
        provider's HTTP client is created and used on the same loop.
        """
        from .bridge import build_session
        from .wsframe import ProtocolError, close_frame, read_message, text_frame

        assert self.config is not None

        def send_raw(frame: bytes) -> None:
            self.wfile.write(frame)
            self.wfile.flush()

        def send(payload: dict[str, Any]) -> None:
            send_raw(text_frame(json.dumps(payload)))

        factory = type(self).session_factory or build_session
        session = factory(self.config, send)
        session.hello()

        type(self).sessions += 1
        try:
            while True:
                try:
                    message = await _off_the_loop(lambda: read_message(self.rfile, send_raw))
                except ProtocolError as exc:
                    send_raw(close_frame(exc.code, str(exc)))
                    return
                except (OSError, ValueError):
                    return
                if message is None:
                    send_raw(close_frame())
                    return
                # Not awaited to completion for a turn: `handle` starts one as
                # a task and returns. The gate asks this socket and waits for
                # the answer, so a read loop that stopped during a turn would
                # make every confirmation time out.
                await session.handle(message)
        finally:
            type(self).sessions = max(0, type(self).sessions - 1)
            session.close()

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


def describe(config: Config, port: int | None = None) -> str:
    """The URL to open. One place, so the CLI and the tests agree.

    `port` overrides the configured one, because a configured port of 0 means
    "let the OS choose" and the chosen one is only known after binding. Without
    this the banner says localhost:0, which is not a thing anyone can open.
    """
    host = config.server.host
    shown = "localhost" if host in {"127.0.0.1", "0.0.0.0", "::1", ""} else host
    return f"http://{shown}:{port or config.server.port}/"


def build(
    config: Config,
    *,
    root: Path | None = None,
    verbose: bool = False,
    session_factory=None,
) -> FrontEnd:
    """Bind the server. Raises OSError if the port is taken."""
    _force_types()
    web_root = root or WEB_ROOT
    if not (web_root / "index.html").is_file():
        raise FileNotFoundError(
            f"the front end is missing: {web_root / 'index.html'} does not exist"
        )

    handler = partial(FrontEndHandler, directory=str(web_root))
    FrontEndHandler.verbose = verbose
    FrontEndHandler.config = config
    FrontEndHandler.session_factory = staticmethod(session_factory) if session_factory else None
    return FrontEnd((config.server.host, config.server.port), handler)


def lock_path(config: Config) -> Path:
    return config.vault.ranger / LOCK_FILE


def read_lock(config: Config) -> dict | None:
    try:
        return json.loads(lock_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def probe(config: Config, timeout: float = 1.0) -> dict | None:
    """Ask a server that may or may not be there. None means it is not.

    Over loopback to the configured address rather than trusting the lock
    file: a stale lock from a machine that lost power says a server is running
    when nothing is listening, and starting a second one would then fail on the
    port instead of doing the obvious thing.
    """
    import http.client

    host = config.server.host or "127.0.0.1"
    port = config.server.port
    if not port:
        # Configured as "let the OS choose", so only the running server knows.
        port = (read_lock(config) or {}).get("port") or 0
    if not port:
        return None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", STATUS_PATH)
        response = connection.getresponse()
        if response.status != 200:
            return None
        return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass


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

    url = describe(config, httpd.server_address[1])
    print(f"Ranger  {url}")
    print(f"  the orb        {url}")
    print(f"  the transport  {url}transport.html   (plain, for checking the socket)")
    print(f"  the socket     {url.replace('http://', 'ws://')}ws")
    print("  the terminal still works, unchanged: run 'ranger' with no flags.")
    print("  ctrl-c to stop.")

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    import os

    lock = lock_path(config)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": config.server.host,
                    "port": httpd.server_address[1],
                    "url": url,
                    "started": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # not fatal: the server works, finding it is harder
        print(f"  could not write {lock}: {exc}", file=sys.stderr)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            lock.unlink()
        except OSError:
            pass
    return 0
