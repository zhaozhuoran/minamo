# Minamo

A lightweight, modern, **S3-compatible storage gateway**.

Minamo presents a unified S3-compatible interface while abstracting storage
providers behind a clean, pluggable backend interface. Clients never know
where data is actually stored — they just talk S3.

## Architecture

Minamo is strictly layered. HTTP handlers never touch the filesystem.

```
HTTP / S3 API   (FastAPI routers, SigV4 verification, XML responses)
      │
S3 Service       (bucket/object/multipart orchestration, error mapping)
      │
Metadata         (bucket/object/upload bookkeeping — SQLite, provider-independent)
      │
Storage Backend  (pluggable interface)
      │
Local Disk       (the bundled backend)
```

The backend is selected through a single configuration value, and new storage
providers can be added by implementing one interface — without touching the API
or service layers. The bundled backend stores objects on the local filesystem.

## Features

* **Objects**
  * `PutObject` / `GetObject` / `DeleteObject` / `HeadObject`
  * HTTP `Range` downloads (partial content, `206`) for large-file clients such
    as rclone and the AWS CLI
* **Listing**
  * `ListObjectsV2` with `prefix`, `delimiter` and pagination
    (`continuation-token`, `start-after`, `max-keys`)
* **Buckets**
  * `CreateBucket` / `DeleteBucket` / `HeadBucket` / `ListBuckets`
  * `DeleteBucket` refuses to delete a non-empty bucket
* **Multipart upload**
  * `CreateMultipartUpload`, `UploadPart`, `ListParts`,
    `CompleteMultipartUpload`, `AbortMultipartUpload`
* **Presigned URLs**
  * Query-string / SigV4 presigned URLs for time-limited, auth-free access
* **Authentication**
  * AWS Signature Version 4 (header + query), including AWS-chunked streaming
* **Compatibility**
  * Works with standard S3 SDKs and tools: boto3, AWS CLI, rclone, Cyberduck, ...

## Running

```bash
pip install -e ".[test]"
python -m minamo                 # scaffolds config/ on first run, then serves on :8000
python -m minamo init            # only create config/, then exit
# or: uvicorn minamo.app:app --port 8000   (uses defaults if config/ is absent)
```

### Configuration

Minamo is configured through operator-authored TOML files under `config/`
(gitignored). On first run, if `config/` is missing, Minamo scaffolds it from
`config.example/` and **exits** so you set real values first. See
[`docs/configuration.md`](docs/configuration.md) for the full per-key reference.

Load precedence is **CLI > `MINAMO_*` env vars > `config/*.toml`**.

```
config/  app.toml · secrets.toml · storage-localdisk.toml · storage-onedrive.toml
data/    localdisk/ · metadata/ · cache/ · state/   (state = program-maintained)
```

| Variable (env override) | File key | Default | Description |
|-------------------------|----------|---------|-------------|
| `MINAMO_BACKEND` | `app.backend` | `local_disk` | Active storage backend |
| `MINAMO_ENDPOINT_HOST` | `app.endpoint_host` | `localhost` | Used to detect virtual-hosted bucket names |
| `MINAMO_REGION` | `app.region` | `us-east-1` | SigV4 region |
| `MINAMO_ENFORCE_SIGNATURE` | `app.enforce_signature` | `true` | Verify request signatures |
| `MINAMO_PRESIGN_TTL` | `app.presign_ttl` | `3600` | Default presigned URL expiry (seconds) |
| `MINAMO_ACCESS_KEY` / `MINAMO_SECRET_KEY` | `secrets.access_key` / `secrets.secret_key` | `minamo` / `minamo-secret` | SigV4 credentials |
| `MINAMO_DATA_ROOT` | `app.data.root` | `data` | Runtime data root (holds localdisk/metadata/cache/state) |

# Example

```python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:8000",
    aws_access_key_id="minamo",
    aws_secret_access_key="minamo-secret",
    region_name="us-east-1",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    ),
)

s3.create_bucket(Bucket="my-bucket")
s3.put_object(Bucket="my-bucket", Key="hello.txt", Body=b"hi")

print(
    s3.get_object(
        Bucket="my-bucket",
        Key="hello.txt",
    )["Body"].read()
)
```

# Testing

Compatibility is validated using real AWS SDK clients against a live Minamo server.

```bash
pytest
```

The current implementation passes **22 boto3 compatibility tests**.

# Design Goals

- Full S3 compatibility
- Simple, maintainable architecture
- Pluggable storage providers
- Low operational cost
- Zero application-side changes
- Storage provider independence

# License

Licensed under the **GNU Affero General Public License v3.0**.

See [LICENSE](LICENSE) for details.
