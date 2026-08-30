import base64

import pytest
from fastapi.testclient import TestClient

from careerradar.web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_no_auth_configured_allows_unheadered_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAREERRADAR_OWNER", raising=False)
    monkeypatch.delenv("CAREERRADAR_PASSWORD", raising=False)
    monkeypatch.delenv("CAREERRADAR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CAREERRADAR_USER", raising=False)

    response = client.get("/api/sync/status")
    # Endpoint should not return 401 or 403
    assert response.status_code != 401
    assert response.status_code != 403


def test_tailscale_owner_matching(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREERRADAR_OWNER", "alice@github")
    monkeypatch.delenv("CAREERRADAR_PASSWORD", raising=False)
    monkeypatch.delenv("CAREERRADAR_AUTH_TOKEN", raising=False)

    # Correct Tailscale login
    resp = client.get("/api/sync/status", headers={"tailscale-user-login": "alice@github"})
    assert resp.status_code != 403

    # Incorrect Tailscale login
    resp_bad = client.get("/api/sync/status", headers={"tailscale-user-login": "bob@github"})
    assert resp_bad.status_code == 403
    assert resp_bad.json() == {"detail": "Not authorised for this dashboard."}


def test_basic_auth_password(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREERRADAR_PASSWORD", "supersecret123")
    monkeypatch.delenv("CAREERRADAR_OWNER", raising=False)
    monkeypatch.delenv("CAREERRADAR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CAREERRADAR_USER", raising=False)

    # Missing auth header -> 401
    resp_unauth = client.get("/api/sync/status")
    assert resp_unauth.status_code == 401
    assert resp_unauth.headers.get("www-authenticate") == 'Basic realm="CareerRadar"'

    # Invalid Basic Auth credentials
    bad_creds = base64.b64encode(b"admin:wrongpassword").decode("utf-8")
    resp_bad = client.get("/api/sync/status", headers={"Authorization": f"Basic {bad_creds}"})
    assert resp_bad.status_code == 401

    # Valid Basic Auth credentials
    valid_creds = base64.b64encode(b"admin:supersecret123").decode("utf-8")
    resp_good = client.get("/api/sync/status", headers={"Authorization": f"Basic {valid_creds}"})
    assert resp_good.status_code != 401
    assert resp_good.status_code != 403


def test_bearer_and_api_key_auth(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREERRADAR_AUTH_TOKEN", "token_abc_xyz")
    monkeypatch.delenv("CAREERRADAR_OWNER", raising=False)
    monkeypatch.delenv("CAREERRADAR_PASSWORD", raising=False)

    # Bearer token
    resp_bearer = client.get("/api/sync/status", headers={"Authorization": "Bearer token_abc_xyz"})
    assert resp_bearer.status_code != 401

    # Invalid Bearer token
    resp_bad_bearer = client.get(
        "/api/sync/status", headers={"Authorization": "Bearer wrong_token"}
    )
    assert resp_bad_bearer.status_code == 401

    # X-API-Key header
    resp_apikey = client.get("/api/sync/status", headers={"X-API-Key": "token_abc_xyz"})
    assert resp_apikey.status_code != 401

    # Invalid X-API-Key
    resp_bad_apikey = client.get("/api/sync/status", headers={"X-API-Key": "wrong_key"})
    assert resp_bad_apikey.status_code == 401
