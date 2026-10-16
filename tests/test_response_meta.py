"""POST /fetch tells the caller when a body was skipped, so an empty body is never mistaken for an empty page."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.api.v1.endpoints.fetch as fetch_mod
from app.core.fetcher import FetcherResult
from app.schemas.scraping import FetchRequest, RequestMeta


def _call(result: FetcherResult, **req):
    import asyncio

    async def fake_fetch(**kwargs):
        return result

    orig = fetch_mod.httpcloak_fetch
    fetch_mod.httpcloak_fetch = fake_fetch
    try:
        return asyncio.run(fetch_mod.fetch(FetchRequest(url="https://fast.example/f/abc", **req)))
    finally:
        fetch_mod.httpcloak_fetch = orig


def test_meta_defaults_say_the_body_is_complete():
    meta = RequestMeta(via="http", request_type="http_request")
    assert meta.body_truncated is False and meta.body_note is None


def test_skipped_video_body_is_reported_with_the_final_url():
    res = _call(FetcherResult(
        status_code=200, headers={"content-type": "video/x-matroska"}, body="", cookies={},
        final_url="https://cdn.example/movie.mkv", protocol="h2", truncated=True,
        note="binary response (video/x-matroska) not read",
    ))
    assert res.status_code == 200 and res.body == "" and res.url == "https://cdn.example/movie.mkv"
    assert res.meta.body_truncated is True and "video/x-matroska" in res.meta.body_note
    assert any(l.startswith("[HTTP-LARGE]") for l in res.logs), res.logs


def test_force_http_path_reports_it_too():
    res = _call(
        FetcherResult(status_code=200, headers={}, body="", cookies={}, final_url="https://cdn.example/a.zip",
                      truncated=True, note="binary response (application/zip) not read"),
        force_http=True,
    )
    assert res.meta.body_truncated is True and res.url == "https://cdn.example/a.zip"


def test_normal_page_is_not_flagged():
    res = _call(FetcherResult(status_code=200, headers={"content-type": "text/html"}, body="<p>hi</p>", cookies={},
                              final_url="https://fast.example/f/abc", protocol="h2"))
    assert res.body == "<p>hi</p>" and res.meta.body_truncated is False and res.meta.body_note is None
    assert not any("HTTP-LARGE" in l for l in res.logs)


def test_openapi_documents_the_new_meta_fields():
    import main

    schema = main.app.openapi()["components"]["schemas"]["RequestMeta"]["properties"]
    assert "body_truncated" in schema and "body_note" in schema
    assert "MAX_RESPONSE_MB" in schema["body_truncated"]["description"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
