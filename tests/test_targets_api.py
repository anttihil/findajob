from typing import Any

import pytest
from fastapi.testclient import TestClient

from findajob.web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_targets_list_and_capacity(client: TestClient) -> None:
    res = client.get("/api/targets")
    assert res.status_code == 200
    data: dict[str, Any] = res.json()
    assert "queries" in data
    assert isinstance(data["queries"], list)
    assert isinstance(data["locations"], list)

    cap_res = client.get("/api/targets/capacity")
    assert cap_res.status_code == 200
    cap_data: dict[str, Any] = cap_res.json()
    assert "search_pairs" in cap_data
    assert "cycle_days" in cap_data
    assert "zone" in cap_data


def test_target_query_crud(client: TestClient) -> None:
    add_res = client.post(
        "/api/targets/queries",
        json={"query": "Test AI Agent Specialist", "enabled": True},
    )
    assert add_res.status_code == 200
    qid: int = add_res.json()["id"]

    res = client.get("/api/targets")
    queries = res.json()["queries"]
    assert any(q["id"] == qid and q["query"] == "Test AI Agent Specialist" for q in queries)

    up_res = client.put(
        f"/api/targets/queries/{qid}",
        json={"query": "Test AI Agent Specialist Updated"},
    )
    assert up_res.status_code == 200

    tog_res = client.put(
        f"/api/targets/queries/{qid}/toggle",
        json={"enabled": False},
    )
    assert tog_res.status_code == 200
    assert tog_res.json()["enabled"] is False

    del_res = client.delete(f"/api/targets/queries/{qid}")
    assert del_res.status_code == 200

    res_after = client.get("/api/targets")
    assert not any(q["id"] == qid for q in res_after.json()["queries"])


def test_target_location_crud(client: TestClient) -> None:
    loc_id = "test_city"
    add_res = client.post(
        "/api/targets/locations",
        json={
            "id": loc_id,
            "label": "Test City, TC",
            "search_label": "Test City",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": False,
            "distance": 25,
            "enabled": True,
        },
    )
    assert add_res.status_code == 200

    res = client.get("/api/targets")
    locs = res.json()["locations"]
    assert any(loc["id"] == loc_id and loc["label"] == "Test City, TC" for loc in locs)

    up_res = client.put(
        f"/api/targets/locations/{loc_id}",
        json={"distance": 40},
    )
    assert up_res.status_code == 200

    tog_res = client.put(
        f"/api/targets/locations/{loc_id}/toggle",
        json={"enabled": False},
    )
    assert tog_res.status_code == 200
    assert tog_res.json()["enabled"] is False

    del_res = client.delete(f"/api/targets/locations/{loc_id}")
    assert del_res.status_code == 200

    res_after = client.get("/api/targets")
    assert not any(loc["id"] == loc_id for loc in res_after.json()["locations"])
