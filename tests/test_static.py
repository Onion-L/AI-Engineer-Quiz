"""Static files are checked against the server on every load.

The web client's files have no content hash in their names, so a browser that
keeps its own copy can run last release's app.js behind a new page.
"""
import os
import tempfile

from fastapi.testclient import TestClient

# Set before keepstack is imported, so this file also runs on its own without
# touching a real data directory. A previous module's choice is kept.
os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-"))

from keepstack.app import app  # noqa: E402


def test_static_files_are_always_revalidated():
    for path in ("/", "/app.js", "/styles.css"):
        res = TestClient(app).get(path)
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
