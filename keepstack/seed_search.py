"""A deterministic catalog for the search and export scenario.

    python -m keepstack seed search

It is built for exercising combined filters, paging and the CSV export:

  * ``Spring Campaign 2026`` mixes harbor and other assets with every
    combination of the ``approved``, ``campaign`` and ``internal`` tags, so
    each filter condition changes the outcome
  * ``Archive Imports`` and ``Field Reports`` hold near-misses that only the
    collection filter tells apart
  * ``Localization set`` carries Chinese text, commas, quotes and newlines,
    plus a document and an audio file

Every run produces the same catalog: the bytes, timestamps and tags come
from fixed rules. Running it again changes nothing.
"""
from __future__ import annotations

import hashlib
import io
import math
import random
import struct
import uuid as uuidlib
import wave
from datetime import datetime, timedelta, timezone

from . import ingest, search
from .db import get_conn, init_db, transaction
from .seed import _make_image

BASE = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
CAMPAIGN = "Spring Campaign 2026"
LOCALIZATION = "Localization set"
HARBOR = ["Harbor sunrise", "Harbor ferry", "Harbor pier", "Harbor cranes", "Harbor night",
          "Harbor lighthouse", "Harbor market", "Harbor boardwalk", "Harbor boats", "Harbor fog",
          "Harbor warehouse", "Harbor skyline"]
OTHER = ["Coastline survey", "Ferry terminal", "Old town square", "River path"]
CAMPAIGN_ASSETS = 80
# Positions in the campaign collection that were deleted afterwards. Each
# would otherwise be a match for the scenario.
DELETED = {10, 40, 72}
# One tag set per position, repeating. Most combinations of the three review
# tags are present, and an asset's position also decides its subject, so the
# filters cross instead of lining up. Positions 0, 1, 2, 6 and 8 are cleared
# for the campaign.
CAMPAIGN_TAGS = [
    ["approved", "campaign"], ["approved", "campaign", "sunset"], ["approved", "campaign"],
    ["approved", "campaign", "internal"], ["approved"], ["campaign"],
    ["approved", "campaign", "draft"], ["internal"], ["approved", "campaign"], ["archive"],
]


def _image(title: str, idx: int) -> tuple[bytes, str]:
    """A generated picture with the title as its caption, and a file name."""
    return _make_image(idx, title), f"{title.lower().replace(' ', '_')}.jpg"


def _text_bytes() -> bytes:
    return "港口调研笔记\n\n潮位, 风速, 以及 \"引号\" 的测试。\n第三行。\n".encode("utf-8")


def _wav_bytes(seconds: float = 0.5) -> bytes:
    """A short, fixed tone: enough bytes for a real audio asset."""
    rate = 8000
    frames = (int(8000 * math.sin(2 * math.pi * 440 * t / rate)) for t in range(int(rate * seconds)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))
    return buf.getvalue()


def _plan() -> list[dict]:
    """Every asset to create, with the fields the scenario depends on."""
    items = []

    def add(title, data, filename, tags, collection, created_at,
            description="", status="active"):
        items.append({"title": title, "data": data, "filename": filename, "tags": tags,
                      "collection": collection, "created_at": created_at,
                      "description": description, "status": status})

    # The campaign collection. Every fifth asset is not about the harbor, and
    # five assets share each created_at, so ties fall inside a page.
    for i in range(CAMPAIGN_ASSETS):
        about_harbor = i % 5 != 4
        subject = HARBOR[i % len(HARBOR)] if about_harbor else OTHER[i % len(OTHER)]
        title = f"{subject} #{i + 1:02d}"
        # A few assets mention the harbor in their description only.
        description = ("Alternate view of the harbor entrance" if i % 20 == 19
                      else "Shot for the spring outreach campaign")
        data, filename = _image(title, i)
        add(title, data, filename, CAMPAIGN_TAGS[i % len(CAMPAIGN_TAGS)], CAMPAIGN,
            BASE + timedelta(hours=i // 5), description,
            "deleted" if i in DELETED else "active")

    # Near-misses: cleared for the campaign, but filed somewhere else.
    for j in range(16):
        title = f"{HARBOR[j % 6]} A{j + 1:02d}" if j % 2 == 0 else f"Ledger scan A{j + 1:02d}"
        tags = ["approved", "campaign"] if j % 2 == 0 else ["archive"]
        data, filename = _image(title, CAMPAIGN_ASSETS + j)
        add(title, data, filename, tags, "Archive Imports", BASE + timedelta(days=2, hours=j))
    for k in range(16):
        title = (f"Field report: harbor dredging F{k + 1:02d}" if k == 7
                 else f"Field report F{k + 1:02d}")
        tags = ["approved", "campaign"] if k % 3 else ["draft"]
        data, filename = _image(title, CAMPAIGN_ASSETS + 16 + k)
        add(title, data, filename, tags, "Field Reports", BASE + timedelta(days=3, hours=k))

    # Fields that are hard for a CSV: Chinese, commas, quotes, newlines.
    special = [
        ('港口夜景, "一号" 码头', '第一行\n第二行, with "quotes"\n', ["approved", "campaign", "夜景"]),
        ("渡轮时刻表（草案）", '包含逗号, 引号 " 和换行。\n第二行。', ["approved", "internal"]),
        ("Harbor 海港 黄昏", "日落时分的海港。", ["approved", "campaign"]),
        ("仓库清点单", "春季库存清点。", ["campaign"]),
    ]
    for n, (title, description, tags) in enumerate(special):
        data, filename = _image(title, n)
        add(title, data, filename, tags, LOCALIZATION, BASE + timedelta(days=4, hours=n), description)
    # Not pictures, so the media type filter has something else to pick.
    add("港口调研笔记", _text_bytes(), "港口调研笔记.txt", ["approved", "campaign"], LOCALIZATION,
        BASE + timedelta(days=4, hours=4))
    add("港口环境录音", _wav_bytes(), "港口环境录音.wav", ["approved", "campaign"], LOCALIZATION,
        BASE + timedelta(days=4, hours=5))
    return items


def _collection_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, description, kind, created_at) VALUES (?, ?, ?, 'folder', ?)",
        (str(uuidlib.uuid4()), name, "Seeded for the search and export scenario", BASE.isoformat()))
    return cur.lastrowid


def run() -> None:
    init_db()
    random.seed(11)

    conn = get_conn()
    plan = _plan()
    collections = {}
    for item in plan:
        if item["collection"] not in collections:
            collections[item["collection"]] = _collection_id(conn, item["collection"])
        # Ingest unless the same bytes are already catalogued, which is what
        # keeps a second run from adding anything.
        sha = hashlib.sha256(item["data"]).hexdigest()
        row = conn.execute("SELECT id FROM assets WHERE sha256 = ?", (sha,)).fetchone()
        asset_id = row["id"] if row else ingest.ingest_stream(
            io.BytesIO(item["data"]), item["filename"], run_ai=False, title=item["title"])["id"]

        stamp = item["created_at"].isoformat()
        with transaction():
            for tag in item["tags"]:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
                             (asset_id, tag_id))
            conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?, ?, ?)",
                         (collections[item["collection"]], asset_id, stamp))
            conn.execute(
                "UPDATE assets SET title = ?, description = ?, status = ?, created_at = ?, updated_at = ? "
                "WHERE id = ?",
                (item["title"], item["description"], item["status"], stamp, stamp, asset_id))
        ingest.reindex_fts(asset_id)
        ingest.reindex_embedding(asset_id)

    # What the README's scenario should produce, so it can be checked by eye.
    campaign = search.search(q="harbor", collection_id=collections[CAMPAIGN],
                             tags=["approved", "campaign"], exclude_tags=["internal"])
    print(f"Seeded {len(plan)} assets in {len(collections)} collections.")
    print(f"Try it: collection \"{CAMPAIGN}\", keyword \"harbor\", tags approved and campaign, "
          f"without internal -> {campaign['total']} assets.")
    print("Sign in as admin/admin.")
