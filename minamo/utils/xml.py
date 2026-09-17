"""Small XML building helpers.

S3 responses are XML. We build them with explicit helpers (no external deps)
so the output is predictable and easy to match against AWS behaviour.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


def _text(tag: str, text: str, **attrs: str) -> str:
    attr_str = "".join(f' {k}="{escape(v)}"' for k, v in attrs.items())
    return f"<{tag}{attr_str}>{escape(text)}</{tag}>"


def build(tag: str, *children: str, **attrs: str) -> str:
    """Build an XML element from raw child strings."""
    attr_str = "".join(f' {k}="{escape(v)}"' for k, v in attrs.items())
    inner = "".join(children)
    return f'<?xml version="1.0" encoding="UTF-8"?><{tag}{attr_str}>{inner}</{tag}>'


def element(tag: str, value: str, **attrs: str) -> str:
    return _text(tag, value, **attrs)


def render(root: ET.Element) -> str:
    ET.register_namespace("", "http://s3.amazonaws.com/doc/2006-03-01/")
    raw = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>' + raw


def parse(body: bytes | str) -> ET.Element:
    """Parse an XML string/bytes safely while preventing XXE and entity expansion attacks."""
    if isinstance(body, bytes):
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("XML contains forbidden DOCTYPE or ENTITY declarations")
    else:
        if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
            raise ValueError("XML contains forbidden DOCTYPE or ENTITY declarations")
    return ET.fromstring(body)
