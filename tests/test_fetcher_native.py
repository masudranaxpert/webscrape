"""httpcloak native-session safety: a timed-out or cancelled request must never free the session under a running
native request (use-after-free segfault that killed the API on 2026-10-16) nor cancel its native future."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.fetcher as fetcher


class _FakeResp:
    status_code = 200
    text = "<html>ok</html>"
    headers = {}
    final_url = "https://example.com/"
    protocol = "h2"


class _FakeSession:
    """Mimics httpcloak: request_async returns a future the 'native side' completes later, via the loop."""

    instances: list["_FakeSession"] = []
    native_delay = 0.0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed_at: float | None = None
        self.native_done_at: float | None = None
        self.callback_errors: list[BaseException] = []
        _FakeSession.instances.append(self)

    def set_cookie(self, **kw):
        pass

    def get_cookies(self):
        return []

    def close(self):
        self.closed_at = time.monotonic()

    async def request_async(self, **kw):
        self.timeout_arg = kw.get("timeout")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def native_complete():
            self.native_done_at = time.monotonic()
            if self.closed_at is not None:
                self.callback_errors.append(RuntimeError("native request ran after session.close()"))
            try:
                fut.set_result(_FakeResp())
            except asyncio.InvalidStateError as err:  # what the real callback hits after a cancel
                self.callback_errors.append(err)

        loop.call_later(_FakeSession.native_delay, native_complete)
        return await fut


def _run(coro):
    return asyncio.run(coro)


def _setup(native_delay: float, grace: float):
    _FakeSession.instances.clear()
    _FakeSession.native_delay = native_delay
    fetcher.httpcloak.Session = _FakeSession
    fetcher._NATIVE_GRACE_SEC = grace


def test_native_timeout_is_passed_and_success_closes_session():
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC)
    try:
        _setup(native_delay=0.01, grace=1.0)
        res = _run(fetcher.httpcloak_fetch("https://example.com/", timeout=5))
        s = _FakeSession.instances[0]
        assert res.status_code == 200 and s.timeout_arg == 5, "native timeout not passed to httpcloak"
        assert s.closed_at is not None and not s.callback_errors
    finally:
        fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC = orig


def test_hung_native_request_defers_close_until_it_finishes():
    orig = (fetcher.httpcloak.Session, fetcher._NATIVE_GRACE_SEC)
    try:
        # Python gives up at timeout(0)+grace(0.1)s; the native request finishes later at 0.4s
        _setup(native_delay=0.4, grace=0.1)

        async def scenario():
            res = await fetcher.httpcloak_fetch("https://slow.example/", timeout=0)
            s = _FakeSession.instances[0]
            assert res.status_code == 0 and "timed out" in (res.error or "")
            assert s.closed_at is None, "session freed while the native request was still running"
            await asyncio.sleep(0.5)
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


if __name__ == "__main__":
    test_native_timeout_is_passed_and_success_closes_session()
    test_hung_native_request_defers_close_until_it_finishes()
    test_caller_cancellation_leaves_native_request_to_finish()
    print("[OK] httpcloak native-session safety checks passed.")
