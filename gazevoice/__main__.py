"""Start the GazeVoice control center: `python3 -m gazevoice`."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser

from .components import build_components
from .components.base import Context
from .paths import AppPaths, default_paths
from .runner import CommandRunner
from .server.app import ControlCenter, make_server
from .settings import load_settings

DEFAULT_PORT = 8977


def build_center(paths: AppPaths | None = None) -> ControlCenter:
    paths = paths or default_paths()
    paths.ensure()
    ctx = Context(runner=CommandRunner(), paths=paths, settings=load_settings(paths.settings_file))
    return ControlCenter(ctx, build_components(ctx))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gazevoice", description="GazeVoice control center")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to serve on (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    args = parser.parse_args(argv)

    center = build_center()
    try:
        server = make_server(center, port=args.port)
    except OSError as exc:
        print(f"Couldn't start on port {args.port}: {exc}. Is GazeVoice already running? Try --port.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"GazeVoice control center → {url}   (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    finally:
        # The mic, speech worker and brain sessions run in their own process groups,
        # so Ctrl+C in this terminal doesn't reach them; stop them explicitly.
        center.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
