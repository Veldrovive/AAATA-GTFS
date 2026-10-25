"""Serving generated charts over HTTP, for viewing from a remote machine."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

DEFAULT_SERVE_PORT = 8000


def serve(path: Path, port: int = DEFAULT_SERVE_PORT) -> None:
    """Serve the directory containing `path` on localhost until interrupted."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(path.resolve().parent))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Serving at http://localhost:{port}/{quote(path.name)} (Ctrl+C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
