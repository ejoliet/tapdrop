"""Shared fixtures for discovery tests: never touch a real network.

``s3_endpoint`` starts a moto S3 server and uploads a couple of the
``tests/data`` fixtures to it. ``http_server`` serves ``tests/data`` itself
over plain HTTP (standing in for ``https://`` - TLS is not the thing under
test here).
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
import s3fs
from moto.server import ThreadedMotoServer

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def s3_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A moto S3 server at ``s3://tapdrop-test/`` holding a couple of fixtures."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
    # fsspec caches S3FileSystem instances by kwargs, not by the AWS_ENDPOINT_URL
    # env var - without clearing it, a filesystem created against a previous
    # moto server (now on a different port) would be reused and fail to connect.
    s3fs.S3FileSystem.clear_instance_cache()

    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    client.create_bucket(Bucket="tapdrop-test")
    for name in ("gaia.parquet", "stars.csv"):
        client.put_object(Bucket="tapdrop-test", Key=name, Body=(DATA_DIR / name).read_bytes())

    try:
        yield endpoint
    finally:
        server.stop()
        s3fs.S3FileSystem.clear_instance_cache()


@pytest.fixture
def http_server() -> Iterator[str]:
    """A local ``http.server`` rooted at ``tests/data``; returns its base URL."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DATA_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
