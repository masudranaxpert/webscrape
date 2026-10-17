import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from main import app
from app.core.config import API_KEY
from app.schemas.scraping import FetchRequest, FetchResponse


def test_turnstile_endpoints():
    client = TestClient(app)

    # 1. Health check includes solver pool
    resp = client.get("/health")
    assert resp.status_code == 200, f"Health check failed: {resp.text}"
    data = resp.json()
    assert "browser_pool" in data

    # 2. Web router serves modular index.html
    web_resp = client.get("/")
    assert web_resp.status_code == 200
    html = web_resp.text
    assert "Stealth Request Studio" in html
    assert "Turnstile Token Solver" in html
    assert "Examples" in html
    assert "guideExamples" in html

    # 3. Solve-turnstile endpoint rejects invalid sitekey or URL
    bad_req = client.post(
        "/solve-turnstile",
        headers={"X-API-Key": API_KEY or "any"},
        json={"sitekey": "invalid sitekey with spaces!!", "siteurl": "not-a-url"},
    )
    assert bad_req.status_code == 422, f"Should reject malformed sitekey/siteurl with 422, got {bad_req.status_code}: {bad_req.text}"

    print("[OK] Turnstile solver endpoint & web docs tests passed.")


if __name__ == "__main__":
    test_turnstile_endpoints()
