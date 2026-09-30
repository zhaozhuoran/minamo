"""Tests for Minamo Admin Console REST API and Web Server.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from minamo.admin.app import create_admin_app
from minamo.app import create_app
from minamo.config import ConfigManager


@pytest.fixture
def admin_client(tmp_path):
    data_dir = tmp_path / "data"
    config = ConfigManager.from_dict({
        "app": {
            "backend": "local_disk",
            "data": {"root": str(data_dir)},
            "admin": {"enabled": True, "port": 8080, "password": "admin-test-password"},
        },
        "secrets": {
            "access_key": "test-access",
            "secret_key": "test-secret",
        },
        "hsm": {
            "enabled": True,
            "tiers": [
                {"id": "tier0", "backend": "local_disk", "priority": 0, "target_capacity": 1000},
            ]
        },
        "storage_local_disk": {
            "type": "local_disk",
            "id": "local_disk",
            "root": str(data_dir / "localdisk"),
        }
    })

    s3_app = create_app(config)
    admin_app = create_admin_app(s3_app=s3_app, config=config)

    # Trigger lifespan manually or use TestClient with app
    with TestClient(s3_app) as _, TestClient(admin_app) as client:
        yield client, config


def test_admin_auth_unauthorized(admin_client):
    client, _ = admin_client
    res = client.get("/api/admin/overview")
    assert res.status_code == 401


def test_admin_login_and_auth_session(admin_client):
    client, config = admin_client

    # 1. Invalid login
    res = client.post("/api/admin/auth/login", json={"access_key": "wrong", "secret_key": "wrong"})
    assert res.status_code == 401

    # 2. Valid login with key pair
    res = client.post("/api/admin/auth/login", json={"access_key": "test-access", "secret_key": "test-secret"})
    assert res.status_code == 200
    token = res.json()["token"]
    assert token

    # 3. Request with X-Admin-Token
    res = client.get("/api/admin/overview", headers={"X-Admin-Token": token})
    assert res.status_code == 200

    # 4. Valid login with admin password
    res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    assert res.status_code == 200


def test_admin_overview_and_health(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    res = client.get("/api/admin/overview", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "storage_bytes" in data
    assert "objects_count" in data
    assert "buckets_count" in data
    assert "system_status" in data
    assert data["system_status"]["s3_api"] == "Operational"


def test_admin_buckets_crud(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    # 1. Create bucket
    res = client.post("/api/admin/buckets", json={"name": "test-admin-bkt", "backend": "local_disk", "region": "us-east-1"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["name"] == "test-admin-bkt"

    # 2. List buckets
    res = client.get("/api/admin/buckets", headers=headers)
    assert res.status_code == 200
    bkt_names = [b["name"] for b in res.json()]
    assert "test-admin-bkt" in bkt_names

    # 3. List objects
    res = client.get("/api/admin/buckets/test-admin-bkt/objects", headers=headers)
    assert res.status_code == 200
    assert "objects" in res.json()

    # 4. Delete bucket
    res = client.delete("/api/admin/buckets/test-admin-bkt", headers=headers)
    assert res.status_code == 200


def test_admin_tiers_and_backends(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    res = client.get("/api/admin/tiers", headers=headers)
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    res = client.get("/api/admin/backends", headers=headers)
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_admin_hsm_control(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    res = client.get("/api/admin/hsm", headers=headers)
    assert res.status_code == 200

    res = client.post("/api/admin/hsm/control", json={"action": "tick"}, headers=headers)
    assert res.status_code == 200


def test_admin_cache_control(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    res = client.get("/api/admin/cache", headers=headers)
    assert res.status_code == 200

    res = client.post("/api/admin/cache/clear", headers=headers)
    assert res.status_code == 200


def test_admin_logs_and_security(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]
    headers = {"X-Admin-Token": token}

    res = client.get("/api/admin/logs/access", headers=headers)
    assert res.status_code == 200

    res = client.get("/api/admin/logs/app", headers=headers)
    assert res.status_code == 200

    res = client.get("/api/admin/security", headers=headers)
    assert res.status_code == 200
    assert res.json()["mode"] == "single_key"


def test_admin_static_assets(admin_client):
    client, _ = admin_client
    res = client.get("/")
    assert res.status_code == 200
    assert "Minamo" in res.text


def test_admin_spa_path_traversal_prevented(admin_client):
    client, _ = admin_client
    # Attempt path traversal to read project files outside STATIC_DIR
    res = client.get("/../pyproject.toml")
    assert res.status_code == 200
    assert "[project]" not in res.text
    assert "Minamo Admin Console" in res.text

    res = client.get("/../../requirements.txt")
    assert res.status_code == 200
    assert "fastapi" not in res.text
    assert "Minamo Admin Console" in res.text


def test_admin_websocket_connection_and_events(admin_client):
    client, _ = admin_client
    login_res = client.post("/api/admin/auth/login", json={"password": "admin-test-password"})
    token = login_res.json()["token"]

    client.cookies.set("minamo_admin_token", token)
    with client.websocket_connect("/api/admin/ws") as websocket:
        websocket.send_json({"type": "ping"})
        data = websocket.receive_json()
        assert data["type"] == "pong"
