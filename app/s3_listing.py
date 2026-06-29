"""Resolve stream URLs from an S3 ListBucket XML listing URI."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
S3_LIST_TIMEOUT_S = 30


def parse_s3_object_keys(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    keys: list[str] = []
    for contents in root.findall(f"{{{S3_NS}}}Contents"):
        key_el = contents.find(f"{{{S3_NS}}}Key")
        if key_el is not None and key_el.text:
            keys.append(key_el.text)
    return keys


def object_keys_to_stream_urls(listing_uri: str, keys: list[str]) -> list[str]:
    parsed = urlparse(listing_uri.strip())
    base = f"{parsed.scheme}://{parsed.netloc}/"
    return [urljoin(base, key) for key in keys]


def fetch_stream_urls_from_s3_listing(listing_uri: str) -> list[str]:
    listing_uri = listing_uri.strip()
    if not listing_uri:
        return []

    req = Request(
        listing_uri,
        method="GET",
        headers={"Accept": "application/xml, text/xml, */*"},
    )
    with urlopen(req, timeout=S3_LIST_TIMEOUT_S) as resp:
        body = resp.read()

    keys = parse_s3_object_keys(body)
    return object_keys_to_stream_urls(listing_uri, keys)
