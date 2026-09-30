import time
import pytest
import threading
import uvicorn
import httpx
import boto3
from botocore.client import Config
from pathlib import Path
from minamo.app import create_app
from minamo.config.manager import ConfigManager

class ServerThread(threading.Thread):
    def __init__(self, app, host="127.0.0.1", port=8888):
        super().__init__()
        self.server = uvicorn.Server(config=uvicorn.Config(app, host=host, port=port, log_level="error"))
        self.host = host
        self.port = port

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True

def test_live_http_server(tmp_path: Path):
    raw_cfg = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "127.0.0.1:8888",
            "region": "us-east-1",
            "enforce_signature": False,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")}
        },
        "secrets": {"access_key": "minamoaccesskey", "secret_key": "minamosecretkey"},
        "hsm": {"enabled": False, "tiers": []}
    }
    cm = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    app = create_app(cm)

    server_thread = ServerThread(app, host="127.0.0.1", port=8888)
    server_thread.start()

    # Wait for server to start
    endpoint_url = "http://127.0.0.1:8888"
    for _ in range(50):
        try:
            r = httpx.get(endpoint_url, timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    try:
        # Use boto3 S3 client to communicate with the live server
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id="minamoaccesskey",
            aws_secret_access_key="minamosecretkey",
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

        # Create bucket
        s3.create_bucket(Bucket="livebucket")

        # Put object
        s3.put_object(Bucket="livebucket", Key="hello.txt", Body=b"Live Minamo Test Data")

        # Get object
        res = s3.get_object(Bucket="livebucket", Key="hello.txt")
        body = res["Body"].read()
        assert body == b"Live Minamo Test Data"

        # List objects
        list_res = s3.list_objects_v2(Bucket="livebucket")
        assert len(list_res.get("Contents", [])) == 1
        assert list_res["Contents"][0]["Key"] == "hello.txt"

        # Delete object and bucket
        s3.delete_object(Bucket="livebucket", Key="hello.txt")
        s3.delete_bucket(Bucket="livebucket")

    finally:
        server_thread.stop()
        server_thread.join(timeout=5)
