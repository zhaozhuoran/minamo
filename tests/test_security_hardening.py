"""Security hardening and defense-in-depth verification tests."""
from __future__ import annotations

import pytest
from unittest.mock import patch
from botocore.exceptions import ClientError


def test_xml_bomb_and_doctype_rejected(s3, unique_bucket, raw):
    # Create a multipart upload
    upload = s3.create_multipart_upload(Bucket=unique_bucket, Key="xml-bomb")
    upload_id = upload["UploadId"]

    # Generate presigned URL for complete_multipart_upload
    url = s3.generate_presigned_url(
        ClientMethod="complete_multipart_upload",
        Params={"Bucket": unique_bucket, "Key": "xml-bomb", "UploadId": upload_id},
        ExpiresIn=300
    )

    # This is a classic Billion Laughs XML payload
    malicious_xml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<CompleteMultipartUpload>
  <Part>
    <PartNumber>1</PartNumber>
    <ETag>&lol2;</ETag>
  </Part>
</CompleteMultipartUpload>
"""
    # Send the request to the presigned URL
    resp = raw.post(url, content=malicious_xml, headers={"Content-Type": "application/xml"})

    # Check that it's rejected with 400 Bad Request
    assert resp.status_code == 400
    assert b"MalformedXML" in resp.content
    assert b"XML contains forbidden DOCTYPE or ENTITY" in resp.content


def test_malformed_xml_rejected(s3, unique_bucket, raw):
    upload = s3.create_multipart_upload(Bucket=unique_bucket, Key="malformed-xml")
    upload_id = upload["UploadId"]

    url = s3.generate_presigned_url(
        ClientMethod="complete_multipart_upload",
        Params={"Bucket": unique_bucket, "Key": "malformed-xml", "UploadId": upload_id},
        ExpiresIn=300
    )

    malformed_xml = "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber></CompleteMultipartUpload>" # missing closing Part tag

    resp = raw.post(url, content=malformed_xml, headers={"Content-Type": "application/xml"})

    assert resp.status_code == 400
    assert b"MalformedXML" in resp.content
    assert b"Invalid XML" in resp.content


def test_invalid_key_value_error_handled(s3, unique_bucket):
    # Attempting an operation with an invalid key that triggers a ValueError during validation
    # in local_disk.py (like starting/ending with a slash or path traversal sequences).
    # Since boto3 performs some client-side validation, we can use a key starting with "/"
    # or similar, but let's test a path traversal key if botocore allows it, or use simulated trigger.
    # Actually, a key like "dir/../file" has ".." in it, so it triggers path traversal ValueError.
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket=unique_bucket, Key="dir/../file", Body=b"data")
    assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 400
    assert exc.value.response["Error"]["Code"] == "InvalidArgument"


def test_unexpected_internal_error_fails_securely(s3, raw):
    # Mock list_buckets to raise an unexpected database / internal Exception
    with patch("minamo.service.s3_service.S3Service.list_buckets", side_effect=RuntimeError("Simulated database failure!")):
        with pytest.raises(ClientError) as exc:
            s3.list_buckets()
        assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 500
        assert exc.value.response["Error"]["Code"] == "InternalError"
        # Verify that no internal traceback details or internal server error messages leak to the client
        assert "Simulated database failure" not in exc.value.response["Error"]["Message"]
        assert "RuntimeError" not in exc.value.response["Error"]["Message"]
