"""
KubeDeploy API test suite.
Uses KUBEDEPLOY_DEMO=true (default) so no real cluster is needed.
"""

import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from api.main import app
from api.state import build_registry


@pytest.fixture(autouse=True)
def clear_registry():
    """Clear build registry between tests."""
    build_registry.clear()
    yield
    build_registry.clear()


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_manual_deploy_triggers_build():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/deploy", json={
            "repo_url": "https://github.com/kavya-parekh/sample-app.git",
            "image_tag": "sample-app:abc1234",
            "app_name": "sample-app",
        })
    assert resp.status_code == 202
    data = resp.json()
    assert "build_id" in data
    assert data["message"] == "Build triggered"


@pytest.mark.asyncio
async def test_build_status_after_trigger():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        trigger = await client.post("/deploy", json={
            "repo_url": "https://github.com/kavya-parekh/sample-app.git",
            "image_tag": "sample-app:test001",
            "app_name": "sample-app",
        })
        build_id = trigger.json()["build_id"]

        # Status should exist immediately
        status = await client.get(f"/builds/{build_id}")
    assert status.status_code == 200
    data = status.json()
    assert data["build_id"] == build_id
    assert data["phase"] in ["PENDING", "BUILDING", "PUSHING", "DEPLOYING", "RUNNING"]


@pytest.mark.asyncio
async def test_list_builds_empty():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/builds")
    assert resp.status_code == 200
    assert resp.json()["builds"] == []


@pytest.mark.asyncio
async def test_list_builds_after_trigger():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/deploy", json={
            "repo_url": "https://github.com/kavya-parekh/sample-app.git",
            "image_tag": "sample-app:v1",
            "app_name": "sample-app",
        })
        resp = await client.get("/builds")
    assert len(resp.json()["builds"]) == 1


@pytest.mark.asyncio
async def test_build_not_found():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/builds/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_webhook_non_push_event_ignored():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/webhook/github",
            json={"ref": "refs/heads/main"},
            headers={"X-GitHub-Event": "ping"},
        )
    assert resp.status_code == 200
    assert "Ignored" in resp.json()["message"]


@pytest.mark.asyncio
async def test_pipeline_reaches_running(monkeypatch):
    """Wait for demo pipeline to reach RUNNING state."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        trigger = await client.post("/deploy", json={
            "repo_url": "https://github.com/kavya-parekh/sample-app.git",
            "image_tag": "sample-app:pipeline-test",
            "app_name": "testapp",
        })
        build_id = trigger.json()["build_id"]

        # The demo pipeline takes ~18s total; wait up to 30s
        for _ in range(30):
            await asyncio.sleep(1)
            status = await client.get(f"/builds/{build_id}")
            phase = status.json()["phase"]
            if phase == "RUNNING":
                break
            if phase == "FAILED":
                pytest.fail("Pipeline entered FAILED state")

    assert status.json()["phase"] == "RUNNING"
    assert status.json()["service_ip"] is not None
