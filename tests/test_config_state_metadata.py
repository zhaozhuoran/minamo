import os
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from minamo.config.manager import ConfigManager, _coerce, _get_path
from minamo.config.scaffold import scaffold_config
from minamo.state.manager import StateManager
from minamo.metadata.store import MetadataStore
from minamo.metadata.models import ObjectInfo, BucketInfo

def test_config_coercion():
    assert _coerce("true") is True
    assert _coerce("FALSE") is False
    assert _coerce("1") is True
    assert _coerce("0") is False
    assert _coerce("100") == 100
    assert _coerce("hello") == "hello"

def test_get_path():
    d = {"a": {"b": {"c": 42}}}
    assert _get_path(d, ["a", "b", "c"]) == 42

def test_config_manager_from_dict_and_reload(tmp_path: Path):
    raw = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "0.0.0.0:8000",
            "region": "us-east-1",
            "enforce_signature": False,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")}
        },
        "secrets": {
            "access_key": "test_key",
            "secret_key": "test_secret"
        },
        "storage_local_disk": {
            "type": "local_disk",
            "root": "localdisk"
        }
    }
    cm = ConfigManager.from_dict(raw, config_dir=tmp_path)
    assert cm.settings.backend == "local_disk"
    assert cm.backend_config("local_disk")["type"] == "local_disk"

    cm.ensure_dirs()
    assert (tmp_path / "data" / "localdisk").exists()

    # reload when no config files
    cm.reload_config()

def test_config_scaffold(tmp_path: Path):
    target = tmp_path / "my_config"
    scaffold_config(target)
    assert target.exists()
    assert (target / "app.toml").exists()
    assert (target / "secrets.toml").exists()

def test_state_manager(tmp_path: Path):
    sm = StateManager(tmp_path / "state")
    assert sm.read("oauth") == {}

    sm.write("oauth", {"token": "abc"})
    assert sm.read("oauth") == {"token": "abc"}

    sm.update("oauth", token="xyz", refresh="123")
    assert sm.read("oauth") == {"token": "xyz", "refresh": "123"}

def test_metadata_store_uncovered(tmp_path: Path):
    db_path = tmp_path / "metadata.db"
    store = MetadataStore(db_path)
    store.init()

    # Create bucket
    now = datetime.now(timezone.utc)
    store.create_bucket("b1", now, backend="local_disk")
    assert store.bucket_exists("b1") is True
    b_info = store.get_bucket("b1")
    assert b_info.name == "b1"
    assert store.get_bucket("nonexistent") is None

    # Put Object
    obj_info = ObjectInfo(
        key="dir/obj.txt",
        size=100,
        etag="etag1",
        content_type="text/plain",
        last_modified=now,
        storage_class="STANDARD",
        content_encoding=None,
        expires=None,
        metadata={"a": "b"},
        current_tier="tier0",
        migration_state="idle",
        heat_score=100.0,
    )
    store.put_object("b1", obj_info)
    retrieved = store.get_object("b1", "dir/obj.txt")
    assert retrieved.size == 100

    # Prune access logs
    store.log_access("b1", "dir/obj.txt", "READ")
    logs = store.get_access_logs("b1", "dir/obj.txt")
    assert len(logs) == 1
    all_logs = store.get_all_access_logs()
    assert ("b1", "dir/obj.txt") in all_logs

    store.prune_access_logs(now + timedelta(days=1))
    assert len(store.get_access_logs("b1", "dir/obj.txt")) == 0

    # HSM updates
    store.update_object_tier("b1", "dir/obj.txt", "tier1")
    store.update_hsm_state("b1", "dir/obj.txt", "migrating_down")
    store.update_heat_score("b1", "dir/obj.txt", 80.0)
    store.update_heat_scores_bulk([(50.0, "b1", "dir/obj.txt")])
    assert store.get_tier_size("tier1") == 100
    hsm_info = store.get_all_objects_hsm_info()
    assert len(hsm_info) == 1

    # Migration Tasks DB
    task_id = store.create_migration_task("b1", "dir/obj.txt", "tier0", "tier1")
    store.update_migration_task(task_id, "failed", error_message="Network error", retry_count=1)
    tasks = store.get_migration_tasks()
    assert len(tasks) == 1
    assert tasks[0][5] == "failed"

    store.update_migration_task(task_id, "completed")
    store.update_migration_task(task_id, "retrying", retry_count=2)

    # Delete object & bucket
    store.delete_object("b1", "dir/obj.txt")
    assert store.get_object("b1", "dir/obj.txt") is None

    store.delete_bucket("b1")
    assert store.bucket_exists("b1") is False

    store.close()
