"""httpcloak native-session safety: a timed-out or cancelled request must never free the session under a running
native request (use-after-free segfault that killed the API on 2026-10-16) nor cancel its native future."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.fetcher as fetcher


class _FakeStream:
    """Mimics httpcloak.StreamResponse."""

    def __init__(self, session, status=200, headers=None, content_length=15, body=b"<html>ok</html>", chunk=8):
        self.session = session
        self.status_code = status
        self.headers = headers if headers is not None else {"content-type": "text/html; charset=utf-8"}
        self.final_url = "https://example.com/"
        self.protocol = "h2"
        self.content_length = content_length
        self.cookies = []
        self._body, self._chunk = body, chunk
        self.closed_at: float | None = None
        self.read_chunks = 0

    def iter_content(self, chunk_size=8192):
        if self.session.closed_at is not None:
            self.session.callback_errors.append(RuntimeError("stream read after session.close()"))
        for i in range(0, len(self._body), self._chunk):
            self.read_chunks += 1
            time.sleep(self.session.read_delay)
            yield self._body[i:i + self._chunk]

    def close(self):
        self.closed_at = time.monotonic()


class _FakeSession:
    """Mimics httpcloak: request_stream_async returns a future the 'native side' completes later, via the loop."""

    instances: list["_FakeSession"] = []
    native_delay = 0.0
    read_delay = 0.0
    stream_factory = staticmethod(lambda session: _FakeStream(session))

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed_at: float | None = None
        self.native_done_at: float | None = None
        self.callback_errors: list[BaseException] = []
        self.stream: _FakeStream | None = None
        self.read_delay = _FakeSession.read_delay
        self.calls: list[dict] = []
        _FakeSession.instances.append(self)

    def set_cookie(self, **kw):
        pass

    def get_cookies(self):
        return []

    def close(self):
        self.closed_at = time.monotonic()

    async def request_stream_async(self, **kw):
        self.timeout_arg = kw.get("timeout")
        self.calls.append(kw)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def native_complete():
            self.native_done_at = time.monotonic()
            if self.closed_at is not None:
                self.callback_errors.append(RuntimeError("native request ran after session.close()"))
            self.stream = _FakeSession.stream_factory(self)
            self.stream.final_url = kw["url"]
            try:
                fut.set_result(self.stream)
            except asyncio.InvalidStateError as err:  # what the real callback hits after a cancel
                self.callback_errors.append(err)

        loop.call_later(_FakeSession.native_delay, native_complete)
        return await fut


def _run(coro):
    return asyncio.run(coro)


def _setup(native_delay: float, grace: float, factory=None, read_delay: float = 0.0):
    _FakeSession.instances.clear()
    _FakeSession.native_delay = native_delay
    _FakeSession.read_delay = read_delay
    _FakeSession.stream_factory = staticmethod(factory or (lambda session: _FakeStream(session)))
    fetcher.httpcloak.Session = _FakeSession
    fetcher._NATIVE_GRACE_SEC = grace


def test_native_timeout_is_passed_and_success_closes_session():
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC)
    try:
        _setup(native_delay=0.01, grace=1.0)
        res = _run(fetcher.httpcloak_fetch("https://example.com/", timeout=5))
        s = _FakeSession.instances[0]
        # The remaining budget (timeout minus time already spent) is handed to the native side, so redirects cannot
        # stretch the total past `timeout`
        assert res.status_code == 200 and 4 <= s.timeout_arg <= 5, "native timeout not passed to httpcloak"
        assert s.closed_at is not None and not s.callback_errors
    finally:
        fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC = orig


def test_hung_native_request_defers_close_until_it_finishes():
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC)
    try:
        # Python gives up at the 1s native-timeout floor + grace(0.1)s; the native request finishes later, at 1.6s
        _setup(native_delay=1.6, grace=0.1)

        async def scenario():
            res = await fetcher.httpcloak_fetch("https://slow.example/", timeout=0)
            s = _FakeSession.instances[0]
            assert res.status_code == 0 and "timed out" in (res.error or "")
            assert s.closed_at is None, "session freed while the native request was still running"
            await asyncio.sleep(1.2)
            return s

        s = _run(scenario())
        assert s.native_done_at is not None and s.closed_at is not None and s.closed_at >= s.native_done_at
        assert not s.callback_errors, f"native callback hit a dead future / closed session: {s.callback_errors}"
    finally:
        fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC = orig


def test_caller_cancellation_leaves_native_request_to_finish():
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC)
    try:
        _setup(native_delay=0.3, grace=5.0)

        async def scenario():
            task = asyncio.create_task(fetcher.httpcloak_fetch("https://example.com/", timeout=5))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            s = _FakeSession.instances[0]
            assert s.closed_at is None, "session freed on caller cancellation"
            await asyncio.sleep(0.4)
            return s

        s = _run(scenario())
        assert s.closed_at is not None and s.closed_at >= s.native_done_at
        assert not s.callback_errors, s.callback_errors
    finally:
        fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC = orig


def _with_fakes(fn):
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC, fetcher.MAX_RESPONSE_MB)
    try:
        fn()
    finally:
        fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC, fetcher.MAX_RESPONSE_MB = orig


def test_video_response_is_never_read():
    """The 2026-10-16 memory incident: a redirect to a multi-GB .mkv was buffered ~12x in RAM."""
    def run():
        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "video/x-matroska"}, content_length=2_340_000_000, body=b"x" * 64))
        res = _run(fetcher.httpcloak_fetch("https://fast.example/f/abc", timeout=5))
        s = _FakeSession.instances[0]
        assert res.status_code == 200 and res.body == "" and res.truncated
        assert res.final_url == "https://fast.example/f/abc" and "video" in res.note
        assert s.stream.read_chunks == 0, "body of a video was read"
        assert s.stream.closed_at is not None and s.closed_at is not None

    _with_fakes(run)


def test_oversized_or_download_text_is_not_read():
    def run():
        fetcher.MAX_RESPONSE_MB = 1
        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html"}, content_length=50 * 1024 * 1024, body=b"x" * 64))
        res = _run(fetcher.httpcloak_fetch("https://big.example/", timeout=5))
        assert res.truncated and _FakeSession.instances[0].stream.read_chunks == 0 and "limit" in res.note

        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/plain", "content-disposition": "attachment; filename=a.txt"}))
        res = _run(fetcher.httpcloak_fetch("https://dl.example/", timeout=5))
        assert res.truncated and "attachment" in res.note

    _with_fakes(run)


def test_unknown_length_body_is_cut_at_the_cap():
    def run():
        fetcher.MAX_RESPONSE_MB = 1 / 1024  # 1 KB
        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html"}, content_length=-1, body=b"a" * 10_000, chunk=512))
        res = _run(fetcher.httpcloak_fetch("https://chunked.example/", timeout=5))
        assert res.truncated and len(res.body) == 1024
        assert _FakeSession.instances[0].stream.read_chunks <= 3, "kept reading after the cap"

    _with_fakes(run)


def test_normal_pages_decode_with_declared_charset_and_are_not_truncated():
    def run():
        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html; charset=utf-8"}, content_length=-1,
            body="<p>হ্যালো Hello</p>".encode("utf-8")))
        res = _run(fetcher.httpcloak_fetch("https://example.com/", timeout=5))
        assert res.body == "<p>হ্যালো Hello</p>" and not res.truncated and res.note is None
        assert res.redirect_chain == [], "no redirect, so no chain"

        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html; charset=windows-1252"}, content_length=4, body="caf\xe9".encode("cp1252")))
        assert _run(fetcher.httpcloak_fetch("https://example.com/", timeout=5)).body == "caf\xe9"

        _setup(0.01, 1.0, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html; charset=nonsense"}, content_length=2, body=b"ok"))
        assert _run(fetcher.httpcloak_fetch("https://example.com/", timeout=5)).body == "ok"

    _with_fakes(run)


def test_session_closes_only_after_a_slow_body_read_finishes():
    """Timeout while a worker thread is still reading the stream: closing now would free memory under it."""
    def run():
        _setup(0.01, 0.1, factory=lambda s: _FakeStream(
            s, headers={"content-type": "text/html"}, content_length=-1, body=b"x" * 400, chunk=100), read_delay=0.3)

        async def scenario():
            res = await fetcher.httpcloak_fetch("https://slow-body.example/", timeout=0)
            s = _FakeSession.instances[0]
            assert res.status_code == 0 and "timed out" in (res.error or "")
            assert s.closed_at is None, "session closed while the body read was still running"
            await asyncio.sleep(1.6)
            return s

        s = _run(scenario())
        assert s.closed_at is not None and not s.callback_errors, s.callback_errors

    _with_fakes(run)


def _redirect_chain(*hops):
    """Factory answering hop N of a request with hops[N]: ('302', '/next') or ('200', body_bytes)."""
    def factory(session):
        code, arg = hops[min(len(session.calls) - 1, len(hops) - 1)]
        if code in ("301", "302", "303", "307", "308"):
            return _FakeStream(session, status=int(code), headers={"location": arg}, content_length=0, body=b"")
        return _FakeStream(session, headers={"content-type": "text/html"}, content_length=len(arg), body=arg, chunk=64)
    return factory


def test_redirects_are_followed_without_reading_their_bodies():
    def run():
        _setup(0.01, 1.0, factory=_redirect_chain(("302", "/step2"), ("301", "https://other.example/final"), ("200", b"<p>done</p>")))
        res = _run(fetcher.httpcloak_fetch("https://start.example/a", timeout=10, cookies={"sid": "1"}))
        s = _FakeSession.instances[0]
        urls = [c["url"] for c in s.calls]
        assert urls == ["https://start.example/a", "https://start.example/step2", "https://other.example/final"], urls
        assert res.status_code == 200 and res.body == "<p>done</p>" and res.final_url == "https://other.example/final"
        assert [(h["url"], h["status"], h["location"]) for h in res.redirect_chain] == [
            ("https://start.example/a", 302, "https://start.example/step2"),
            ("https://start.example/step2", 301, "https://other.example/final"),
            ("https://other.example/final", 200, None),
        ]
        assert all(c["allow_redirects"] is False for c in s.calls)
        assert s.calls[0]["cookies"] == {"sid": "1"} and s.calls[1]["cookies"] == {"sid": "1"}
        assert s.calls[2]["cookies"] == {}, "per-request cookies leaked to another site"
        assert s.closed_at is not None

    _with_fakes(run)


def test_redirect_method_rules_match_browsers():
    def run():
        for first, expect in (("302", "GET"), ("303", "GET"), ("307", "POST"), ("308", "POST")):
            _setup(0.01, 1.0, factory=_redirect_chain((first, "/next"), ("200", b"ok")))
            _run(fetcher.httpcloak_fetch("https://x.example/a", method="POST", body="payload", timeout=10))
            calls = _FakeSession.instances[0].calls
            assert calls[1]["method"] == expect, (first, calls[1]["method"])
            assert (calls[1]["data"] is None) == (expect == "GET"), (first, "body kept/dropped wrongly")

    _with_fakes(run)


def test_redirect_loop_is_capped_and_returns_the_last_hop():
    def run():
        _setup(0.01, 1.0, factory=_redirect_chain(("302", "/loop")))
        res = _run(fetcher.httpcloak_fetch("https://loop.example/a", timeout=10))
        calls = _FakeSession.instances[0].calls
        assert len(calls) == fetcher._MAX_REDIRECTS + 1, len(calls)
        assert res.status_code == 302 and res.error is None

    _with_fakes(run)


def test_redirect_to_a_video_returns_its_url_without_reading_it():
    """Fast Cloud style link: 302 -> multi-GB .mkv. The caller needs the final URL, never the bytes."""
    def run():
        def factory(session):
            if len(session.calls) == 1:
                return _FakeStream(session, status=302, headers={"location": "https://cdn.example/movie.mkv"}, content_length=0, body=b"")
            return _FakeStream(session, headers={"content-type": "video/x-matroska"}, content_length=2_340_000_000, body=b"x" * 64)

        _setup(0.01, 1.0, factory=factory)
        res = _run(fetcher.httpcloak_fetch("https://fast.example/f/abc", timeout=10))
        s = _FakeSession.instances[0]
        assert res.final_url == "https://cdn.example/movie.mkv" and res.truncated and res.body == ""
        assert s.stream.read_chunks == 0

    _with_fakes(run)


if __name__ == "__main__":
    test_native_timeout_is_passed_and_success_closes_session()
    test_hung_native_request_defers_close_until_it_finishes()
    test_caller_cancellation_leaves_native_request_to_finish()
    test_video_response_is_never_read()
    test_oversized_or_download_text_is_not_read()
    test_unknown_length_body_is_cut_at_the_cap()
    test_normal_pages_decode_with_declared_charset_and_are_not_truncated()
    test_session_closes_only_after_a_slow_body_read_finishes()
    test_redirects_are_followed_without_reading_their_bodies()
    test_redirect_method_rules_match_browsers()
    test_redirect_loop_is_capped_and_returns_the_last_hop()
    test_redirect_to_a_video_returns_its_url_without_reading_it()
    print("[OK] httpcloak native-session safety checks passed.")
