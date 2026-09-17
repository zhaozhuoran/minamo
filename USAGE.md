# Usage

---

## 1. Prerequisites & Installation

Minamo requires Python 3.10 or later.

```bash
# Clone the repository
git clone https://github.com/zhaozhuoran/minamo.git
cd minamo

# Install dependencies
pip install -e .
```

## 2. Configuration

Minamo generates default configuration files in the `config/` directory automatically on its first run.

### Primary Options (`config/app.toml` & `config/secrets.toml`)

- **Service Port & Host**: Default server runs on `http://localhost:8000`.
- **Credentials**: Managed in `config/secrets.toml`:
  ```toml
  access_key = "minamo"
  secret_key = "minamo-secret"
  ```

### Environment Variables (Optional)

You can override config settings via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `MINAMO_ACCESS_KEY` | `minamo` | S3 Gateway Access Key |
| `MINAMO_SECRET_KEY` | `minamo-secret` | S3 Gateway Secret Key |
| `MINAMO_ENDPOINT_HOST` | `localhost` | Server host |
| `MINAMO_REGION` | `us-east-1` | AWS Region |

For detailed multi-backend and HSM tier configuration, see [docs/configuration.md](docs/configuration.md).

## 3. Running the Server

Start Minamo using Python or Uvicorn:

```bash
# Option A: Run directly with Python
python -m minamo

# Option B: Run via Uvicorn
uvicorn minamo.app:app --host 0.0.0.0 --port 8000
```

## 4. Connecting S3 Clients

### Python (`boto3`)

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

# Example usage
s3.create_bucket(Bucket="my-bucket")
s3.put_object(Bucket="my-bucket", Key="example.txt", Body=b"Hello Minamo!")

response = s3.get_object(Bucket="my-bucket", Key="example.txt")
print(response["Body"].read().decode("utf-8"))
```

### AWS CLI

Configure credentials or supply flags directly:

```bash
aws --endpoint-url http://localhost:8000 s3 mb s3://my-bucket
aws --endpoint-url http://localhost:8000 s3 cp myfile.txt s3://my-bucket/
aws --endpoint-url http://localhost:8000 s3 ls s3://my-bucket/
```
