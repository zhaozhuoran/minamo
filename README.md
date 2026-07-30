# Minamo

> A lightweight **S3-compatible storage gateway** with intelligent multi-tier storage.

Minamo provides a standard **Amazon S3-compatible API** while abstracting storage providers behind a clean, pluggable architecture. Applications interact with a single S3 endpoint without needing to know where objects are physically stored.

## Why Minamo?

Many applications store large numbers of user-uploaded files.

In practice, most objects are accessed frequently only during the first few days after upload before becoming "cold" data. These objects still need to be retained, but keeping everything on expensive hot storage unnecessarily increases operating costs.

Minamo aims to solve this problem by providing a transparent storage gateway that can eventually:

- Keep hot data on high-performance storage
- Automatically cache frequently accessed objects
- Move cold data to low-cost storage
- Remain fully compatible with existing S3 clients

Applications continue using the standard S3 API without any storage-specific logic.

# Architecture

Minamo follows a strictly layered architecture.

```
HTTP / S3 API
(FastAPI, SigV4 verification, XML responses)
                │
                ▼
S3 Service
(bucket/object orchestration, error mapping)
                │
                ▼
Metadata
(provider-independent SQLite metadata)
                │
                ▼
Storage Backend Interface
(pluggable abstraction)
```

Each layer has a single responsibility. HTTP handlers never access storage directly, and storage providers are completely isolated from the S3 API implementation.

# Current Features

- PutObject
- GetObject
- DeleteObject
- HeadObject
- ListObjectsV2
  - prefix
  - delimiter
  - pagination
- Bucket operations
  - CreateBucket
  - DeleteBucket
  - HeadBucket
  - ListBuckets
- Multipart Upload
  - CreateMultipartUpload
  - UploadPart
  - ListParts
  - CompleteMultipartUpload
  - AbortMultipartUpload
- Presigned URLs
- AWS Signature Version 4
  - Header authentication
  - Query authentication
  - AWS-chunked streaming
- Compatible with:
  - boto3
  - AWS CLI
  - rclone
  - Cyberduck

# Roadmap

## Phase 1

- [x] Layered architecture
- [x] Local Disk backend
- [x] SigV4 authentication
- [x] Multipart Upload
- [x] Presigned URLs
- [ ] Complete compatibility testing
- [ ] Production hardening

## Phase 2

- [ ] Multi-storage routing
- [ ] Automatic object migration
- [ ] Background workers
- [ ] Storage policies

## Phase 3

- [ ] Local cache
- [ ] OneDrive backend
- [ ] Cloudflare R2 backend
- [ ] Generic S3 backend
- [ ] Storage analytics
- [ ] Lifecycle management

# Running

```bash
pip install -e ".[test]"
python -m minamo

# or

uvicorn minamo.app:app --port 8000
```

Configuration is provided through `MINAMO_*` environment variables.

| Variable                   | Default         | Description                   |
| -------------------------- | --------------- | ----------------------------- |
| `MINAMO_DATA_ROOT`         | `./data`        | Object storage root           |
| `MINAMO_METADATA_ROOT`     | `./metadata`    | SQLite metadata               |
| `MINAMO_BACKEND`           | `local_disk`    | Active backend                |
| `MINAMO_ENDPOINT_HOST`     | `localhost`     | Virtual-host bucket detection |
| `MINAMO_ACCESS_KEY`        | `minamo`        | Access key                    |
| `MINAMO_SECRET_KEY`        | `minamo-secret` | Secret key                    |
| `MINAMO_REGION`            | `us-east-1`     | AWS region                    |
| `MINAMO_ENFORCE_SIGNATURE` | `true`          | Enable SigV4 verification     |

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
