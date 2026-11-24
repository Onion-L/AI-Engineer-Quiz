"""Run Keepstack: ``python -m keepstack`` (optionally ``... seed`` first).

``python -m keepstack seed`` loads the demo catalog. ``... seed search`` loads
the catalog made for the search and export scenario instead.

Environment:
  KEEPSTACK_HOST (default 0.0.0.0)   KEEPSTACK_PORT (default 8000)
"""
from __future__ import annotations

import json
import os
import sys

import uvicorn


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        dataset = sys.argv[2] if len(sys.argv) > 2 else "demo"
        if dataset == "search":
            from .seed_search import run as run_search_seed
            run_search_seed()
        elif dataset == "demo":
            from .seed import run_seed
            run_seed()
        else:
            sys.exit(f"Unknown seed dataset {dataset!r}. Use 'demo' or 'search'.")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "reindex":
        from . import vision
        from .db import get_conn, init_db
        from .ingest import reindex_embedding, reindex_fts

        init_db()
        if vision.runtime_available() and not vision.model_present():
            vision.ensure_model()
        conn = get_conn()
        ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE status != 'deleted'")]
        for i, asset_id in enumerate(ids, 1):
            reindex_embedding(asset_id)
            reindex_fts(asset_id)
            print(f"\rreindexing {i}/{len(ids)}", end="")
        print(f"\nReindexed {len(ids)} assets (local vision model: {'on' if vision.ready() else 'off'}).")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "gc":
        from .db import init_db
        from .storage import collect_orphan_blobs

        init_db()
        result = collect_orphan_blobs(dry_run="--dry-run" in sys.argv[2:])
        print(json.dumps(result, indent=2))
        return
    host = os.environ.get("KEEPSTACK_HOST", "0.0.0.0")
    port = int(os.environ.get("KEEPSTACK_PORT", "8000"))
    uvicorn.run("keepstack.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
