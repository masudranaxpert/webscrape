"""Tiny local website for browser-engine tests: click targets, redirect chains and a huge download. No real sites."""

import http.server
import socketserver
import threading

VIDEO_MB = 500


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "T/1"

    def _html(self, body: str, status: int = 200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, status: int, to: str):
        self.send_response(status)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        p = self.path
        if p == "/":
            self._html("<html><body><button id='go' onclick=\"location.href='/hop1'\">Go</button>"
                       "<button id='dl' onclick=\"location.href='/get-video'\">Download</button>"
                       "<button id='noop' onclick=\"document.title='clicked'\">Nothing</button></body></html>")
        elif p == "/hop1":
            self._redirect(302, "/hop2")
        elif p == "/hop2":
            self._redirect(301, "/final")
        elif p == "/final":
            self._html("<html><title>Final page</title><p id='ok'>arrived</p></html>")
        elif p == "/get-video":
            self._redirect(302, "/movie.mkv")
        elif p == "/movie.mkv":
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(VIDEO_MB << 20))
            self.end_headers()
            chunk = b"x" * (1 << 20)
            sent = 0
            try:
                for _ in range(VIDEO_MB):
                    self.wfile.write(chunk)
                    sent += 1
            except Exception:
                pass
            self.server.video_mb_sent = max(self.server.video_mb_sent, sent)
        else:
            self._html("not found", 404)

    def log_message(self, *a):  # noqa: D401
        pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    video_mb_sent = 0


def start() -> tuple[_Server, str]:
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
