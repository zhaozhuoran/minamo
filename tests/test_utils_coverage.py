import pytest
from pathlib import Path
from xml.etree import ElementTree as ET
from minamo.utils.xml import build, element, render, parse
from minamo.utils.time import now, http_date, iso8601_date, amz_date, amz_date_short
from minamo.utils.temp_workspace import TempWorkspace
from minamo.utils.validation import validate_bucket_name
from datetime import datetime, timezone

def test_xml_helpers():
    # build
    xml_str = build("TestRoot", element("Child", "value"), attr="val")
    assert '<TestRoot attr="val">' in xml_str
    assert "<Child>value</Child>" in xml_str

    # render
    root = ET.Element("Root")
    child = ET.SubElement(root, "Child")
    child.text = "Hello"
    rendered = render(root)
    assert '<?xml version="1.0" encoding="UTF-8"?>' in rendered
    assert "<Child>Hello</Child>" in rendered

    # parse with valid XML
    parsed = parse(b"<Root><Child>123</Child></Root>")
    assert parsed.find("Child").text == "123"

    parsed_str = parse("<Root><Child>456</Child></Root>")
    assert parsed_str.find("Child").text == "456"

    # parse with forbidden DOCTYPE or ENTITY
    with pytest.raises(ValueError, match="forbidden DOCTYPE or ENTITY"):
        parse(b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><Root>&xxe;</Root>")

    with pytest.raises(ValueError, match="forbidden DOCTYPE or ENTITY"):
        parse("<!ENTITY xxe 'test'><Root>&xxe;</Root>")


def test_time_helpers():
    dt = datetime(2025, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert "Tue, 20 May 2025 12:00:00 GMT" == http_date(dt)
    assert "2025-05-20T12:00:00.000Z" == iso8601_date(dt)
    assert "20250520T120000Z" == amz_date(dt)
    assert "20250520" == amz_date_short(dt)

    # test naive datetime handling
    dt_naive = datetime(2025, 5, 20, 12, 0, 0)
    assert "Tue, 20 May 2025 12:00:00 GMT" == http_date(dt_naive)
    assert "2025-05-20T12:00:00.000Z" == iso8601_date(dt_naive)

    # default parameter
    assert isinstance(http_date(), str)
    assert isinstance(iso8601_date(), str)
    assert isinstance(amz_date(), str)
    assert isinstance(amz_date_short(), str)


def test_temp_workspace(tmp_path: Path):
    tw = TempWorkspace(root=tmp_path / "tmp", max_usage_bytes=100)
    assert tw.get_current_usage() == 0

    with tw.allocate_file(prefix="test_", suffix=".dat") as fpath:
        assert fpath.exists()
        fpath.write_bytes(b"A" * 50)
        assert tw.get_current_usage() == 50

    # Cleaned up after exit
    assert not fpath.exists()

    # Exceeding usage limit
    # Fill up workspace with a file
    dummy = tmp_path / "tmp" / "dummy.txt"
    dummy.write_bytes(b"X" * 150)
    assert tw.get_current_usage() >= 100

    with pytest.raises(OSError, match="exceeded maximum capacity limit"):
        with tw.allocate_file():
            pass

    # Clean all
    tw.clean_all()
    assert tw.get_current_usage() == 0


def test_validation():
    # Valid bucket name
    assert validate_bucket_name("my-valid-bucket-1") is True

    # Invalid cases
    assert validate_bucket_name("ab") is False
    assert validate_bucket_name("A"*64) is False
    assert validate_bucket_name("MyBucket") is False # uppercase
    assert validate_bucket_name("192.168.1.1") is False # IP address
    assert validate_bucket_name("a..b") is False # adjacent dots
