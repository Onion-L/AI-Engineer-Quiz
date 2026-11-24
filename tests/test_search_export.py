"""Filtering, sorting, paging and CSV export of the asset list, exercised over HTTP.

The scenario data is written straight into the throwaway database (no image
encoding) and kept in collections of its own, so the expected results are
exact and do not depend on what test_core.py ingested. Run with:

    python -m pytest -q
"""
import csv
import io
import os
import tempfile
import uuid as uuidlib

import pytest
from fastapi.testclient import TestClient

# Set before keepstack is imported, so this file also runs on its own without
# touching a real data directory. A previous module's choice is kept.
os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-"))
os.environ["KEEPSTACK_AI_ENABLED"] = "false"

from keepstack import ingest, search  # noqa: E402
from keepstack.app import app  # noqa: E402
from keepstack.config import config  # noqa: E402
from keepstack.db import transaction  # noqa: E402

PAGER_ROWS = 57
PAGE_SIZE = 10

# The README's business scenario: harbor assets cleared for a campaign.
SCENARIO = {"q": "harbor", "tags": ["approved", "campaign"], "exclude_tags": ["internal"]}


# ---------------------------------------------------------------------------
# Scenario data
# ---------------------------------------------------------------------------
def _collection(name):
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO collections (uuid, name, kind, created_at) VALUES (?, ?, 'folder', ?)",
            (str(uuidlib.uuid4()), name, "2026-01-01T00:00:00+00:00"))
    return cur.lastrowid


def _asset(title, *, tags=(), collection=None, created_at="2026-01-01T00:00:00+00:00",
           status="active", media_type="image", description=None, filename=None,
           conn=None, index=True):
    """Insert an asset, skipping the image pipeline. Returns its uuid.

    Pass ``conn`` to join a transaction of your own. ``index`` adds the asset
    to the full-text index, which only keyword searches need.
    """
    uid = str(uuidlib.uuid4())
    if conn is None:
        with transaction() as own:
            asset_id = _insert_asset(own, uid, title, tags, collection, created_at, status,
                                     media_type, description, filename)
    else:
        asset_id = _insert_asset(conn, uid, title, tags, collection, created_at, status,
                                 media_type, description, filename)
    if index:
        ingest.reindex_fts(asset_id)
    return uid


def _insert_asset(conn, uid, title, tags, collection, created_at, status, media_type,
                  description, filename):
    cur = conn.execute(
        "INSERT INTO assets (uuid, sha256, filename, media_type, title, description, status, "
        "storage_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, uuidlib.uuid4().hex, filename or f"{uid}.jpg",
         media_type, title, description, status, "missing", created_at, created_at))
    asset_id = cur.lastrowid
    for name in tags:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) VALUES (?, ?)",
                     (asset_id, tag_id))
    if collection is not None:
        conn.execute("INSERT INTO collection_assets (collection_id, asset_id) VALUES (?, ?)",
                     (collection, asset_id))
    return asset_id


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        token = c.post("/api/auth/login", json={"username": config.admin_user,
                                                "password": config.admin_password}).json()["token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def _get(client, **params):
    res = client.get("/api/assets", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def _uuids(client, **params):
    return [i["uuid"] for i in _get(client, **params)["items"]]


@pytest.fixture(scope="module")
def harbor(client):
    """A campaign collection of harbor assets, plus what should not match."""
    col = _collection("Harbor campaign")
    day = "2026-03-0{}T09:00:00+00:00"
    return {
        "collection": col,
        "a": _asset("Harbor sunrise", tags=["approved", "campaign"], collection=col, created_at=day.format(1)),
        "b": _asset("Harbor ferry", tags=["approved", "campaign", "internal"], collection=col, created_at=day.format(2)),
        "c": _asset("Harbor pier", tags=["approved"], collection=col, created_at=day.format(3)),
        "d": _asset("Harbor cranes", tags=["campaign"], collection=col, created_at=day.format(4)),
        "e": _asset("Harbor night", tags=["approved", "campaign"], collection=col, created_at=day.format(5)),
        # Deleted: never a match.
        "f": _asset("Harbor bridge", tags=["approved", "campaign"], collection=col,
                    created_at=day.format(6), status="deleted"),
        # Not about the harbor, but cleared for the campaign.
        "g": _asset("City park", tags=["approved", "campaign"], collection=col, created_at=day.format(7)),
        # Outside the collection.
        "h": _asset("Harbor lighthouse", tags=["approved", "campaign"], created_at=day.format(8)),
    }


@pytest.fixture(scope="module")
def pager(client):
    """Many rows that share created_at values, for paging and tie-breaking."""
    col = _collection("Pager")
    uuids = []
    for i in range(PAGER_ROWS):
        # Four rows per timestamp, so ties straddle page boundaries.
        uuids.append(_asset(f"pager row {i:02d}", collection=col,
                            created_at=f"2026-01-01T00:{i // 4:02d}:00+00:00"))
    return {"collection": col, "uuids": uuids}


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------
def test_scenario_combines_keyword_collection_and_tags(client, harbor):
    page = _get(client, **SCENARIO, collection_id=harbor["collection"])
    assert page["total"] == 2
    # Newest first. b is internal, f is deleted, the others miss a tag or the keyword.
    assert [i["uuid"] for i in page["items"]] == [harbor["e"], harbor["a"]]


def test_each_condition_narrows_the_result(client, harbor):
    col = harbor["collection"]
    harbor_ids = {harbor[k] for k in "abcde"}
    assert set(_uuids(client, collection_id=col)) == harbor_ids | {harbor["g"]}
    assert set(_uuids(client, q="harbor", collection_id=col)) == harbor_ids
    with_approved = harbor_ids - {harbor["d"]}
    assert set(_uuids(client, q="harbor", collection_id=col, tags=["approved"])) == with_approved
    with_both = with_approved - {harbor["c"]}
    assert set(_uuids(client, q="harbor", collection_id=col, tags=["approved", "campaign"])) == with_both
    assert set(_uuids(client, **SCENARIO, collection_id=col)) == with_both - {harbor["b"]}


def test_included_tags_are_all_required(client, harbor):
    col = harbor["collection"]
    cleared = {harbor["a"], harbor["b"], harbor["e"], harbor["g"]}
    assert set(_uuids(client, collection_id=col, tags=["approved"])) == cleared | {harbor["c"]}
    assert set(_uuids(client, collection_id=col, tags=["campaign"])) == cleared | {harbor["d"]}
    assert set(_uuids(client, collection_id=col, tags=["approved", "campaign"])) == cleared


def test_any_excluded_tag_drops_an_asset(client, harbor):
    col = harbor["collection"]
    everything = set(_uuids(client, collection_id=col))
    assert set(_uuids(client, collection_id=col, exclude_tags=["internal"])) == everything - {harbor["b"]}
    assert set(_uuids(client, collection_id=col, exclude_tags=["internal", "campaign"])) == {harbor["c"]}


def test_only_active_assets_match(client, harbor):
    # f carries both tags too, but it is deleted. h sits outside the collection.
    assert set(_uuids(client, tags=["approved", "campaign"])) == {harbor[k] for k in "abegh"}


def test_tag_conditions_ignore_case_whitespace_and_duplicates(client, harbor):
    col = harbor["collection"]
    expected = set(_uuids(client, collection_id=col, tags=["approved", "campaign"],
                          exclude_tags=["internal"]))
    assert expected == {harbor["a"], harbor["e"], harbor["g"]}
    assert set(_uuids(client, collection_id=col, tags=["Approved", "APPROVED", " campaign "],
                      exclude_tags=["Internal", "internal"])) == expected


def test_same_tag_included_and_excluded_matches_nothing(client, harbor):
    page = _get(client, collection_id=harbor["collection"], tags=["approved"], exclude_tags=["approved"])
    assert page["total"] == 0 and page["items"] == []


def test_tags_match_whole_names_only(client, harbor):
    # "approv" is a prefix of "approved", not a tag of its own.
    assert _uuids(client, collection_id=harbor["collection"], tags=["approv"]) == []


def test_blank_keyword_does_not_restrict(client, harbor):
    col = harbor["collection"]
    assert set(_uuids(client, q="   ", collection_id=col)) == set(_uuids(client, collection_id=col))


def test_special_characters_in_the_keyword_do_not_error(client, harbor):
    col = harbor["collection"]
    everything = set(_uuids(client, collection_id=col))
    for q in ('"harbor"', "harbor's", "harbor OR (x)", "*-", "\\", "港口"):
        page = _get(client, q=q, collection_id=col)
        assert page["total"] == len(page["items"])
    # Quotes are not syntax here: the keyword still means "harbor".
    assert set(_uuids(client, q='"harbor"', collection_id=col)) == {harbor[k] for k in "abcde"}
    # Punctuation leaves nothing to search for, so it does not restrict.
    assert set(_uuids(client, q="*-", collection_id=col)) == everything


def test_media_type_narrows_like_the_other_conditions(client):
    col = _collection("Media types")
    image = _asset("harbor poster", collection=col)
    video = _asset("harbor clip", collection=col, media_type="video")
    assert set(_uuids(client, collection_id=col)) == {image, video}
    assert _uuids(client, collection_id=col, media_type="video") == [video]
    assert _uuids(client, q="harbor", collection_id=col, media_type="video") == [video]


# ---------------------------------------------------------------------------
# Sorting and paging
# ---------------------------------------------------------------------------
def test_sort_is_honored_with_a_keyword(client, harbor):
    col = harbor["collection"]
    by_date = [harbor[k] for k in "abcde"]
    assert _uuids(client, q="harbor", collection_id=col, sort="oldest") == by_date
    assert _uuids(client, q="harbor", collection_id=col, sort="newest") == by_date[::-1]
    assert _uuids(client, q="harbor", collection_id=col, sort="name") == [harbor[k] for k in "dbeca"]


def test_pages_tile_the_results(client, pager):
    col = pager["collection"]
    for sort in ("oldest", "newest"):
        expected = pager["uuids"] if sort == "oldest" else pager["uuids"][::-1]
        for q in (None, "pager"):
            seen, sizes = [], []
            for offset in range(0, PAGER_ROWS, PAGE_SIZE):
                page = _get(client, q=q, collection_id=col, sort=sort, limit=PAGE_SIZE, offset=offset)
                assert page["total"] == PAGER_ROWS
                seen += [i["uuid"] for i in page["items"]]
                sizes.append(len(page["items"]))
            # Every row exactly once, in one global order; the last page is short.
            assert seen == expected
            assert sizes == [PAGE_SIZE] * 5 + [PAGER_ROWS - 5 * PAGE_SIZE]


def test_tied_created_at_do_not_repeat_or_skip_across_pages(client, pager):
    col = pager["collection"]
    whole = _uuids(client, collection_id=col, sort="newest", limit=200)
    assert whole == pager["uuids"][::-1]
    # Ties are four rows long: page size 4 lands on their edges, 3, 7 and 10 cut across them.
    for size in (3, 4, 7, 10):
        paged = []
        for offset in range(0, PAGER_ROWS, size):
            paged += _uuids(client, collection_id=col, sort="newest", limit=size, offset=offset)
        assert paged == whole


def test_offset_past_the_end_is_an_empty_page_with_the_total(client, pager):
    page = _get(client, collection_id=pager["collection"], limit=10, offset=1000)
    assert page["total"] == PAGER_ROWS and page["items"] == []


def test_invalid_paging_parameters_are_rejected(client):
    for params in ({"limit": 0}, {"limit": -1}, {"limit": 201}, {"offset": -1},
                   {"collection_id": 0}, {"collection_id": "abc"}):
        assert client.get("/api/assets", params=params).status_code == 422


def test_the_asset_list_requires_login():
    assert TestClient(app).get("/api/assets").status_code == 401


# ---------------------------------------------------------------------------
# Edits are visible to the next query
# ---------------------------------------------------------------------------
def test_edits_show_up_in_the_next_query(client):
    col = _collection("Edits")
    uid = _asset("Harbor edit me", tags=["approved", "campaign"], collection=col)

    def hits(**extra):
        return set(_uuids(client, collection_id=col, **extra))

    assert hits(q="harbor") == {uid}
    assert hits(tags=["approved", "campaign"]) == {uid}

    # Title: the old keyword stops matching, the new title starts.
    assert client.patch(f"/api/assets/{uid}", json={"title": "Pier renamed"}).status_code == 200
    assert hits(q="harbor") == set()
    assert hits(q="renamed") == {uid}

    # Description is a search field too.
    assert client.patch(f"/api/assets/{uid}", json={"description": "A quiet harbor at dawn"}).status_code == 200
    assert hits(q="harbor") == {uid}

    # Tags, through the existing add and remove endpoints.
    assert client.post(f"/api/assets/{uid}/tags", json={"name": "Internal"}).status_code == 200
    assert hits(exclude_tags=["internal"]) == set()
    assert client.delete(f"/api/assets/{uid}/tags/internal").status_code == 200
    assert hits(exclude_tags=["internal"]) == {uid}


def test_edits_show_up_in_the_next_export(client):
    col = _collection("Edit export")
    uid = _asset("Harbor export me", tags=["approved", "campaign"], collection=col, description="before")

    def exported(**extra):
        # uuid, title, description, tags
        return [tuple(r[i] for i in (0, 1, 2, 5)) for r in _csv(client, collection_id=col, **extra)[1:]]

    assert exported(q="harbor") == [(uid, "Harbor export me", "before", "approved;campaign")]

    after = "after, with\na newline"
    assert client.patch(f"/api/assets/{uid}",
                        json={"title": "Pier renamed", "description": after}).status_code == 200
    assert exported(q="harbor") == []
    assert exported(q="renamed") == [(uid, "Pier renamed", after, "approved;campaign")]

    assert client.post(f"/api/assets/{uid}/tags", json={"name": "Internal"}).status_code == 200
    assert exported(exclude_tags=["internal"]) == []
    assert exported(tags=["approved", "campaign", "internal"]) == [
        (uid, "Pier renamed", after, "approved;campaign;internal")]


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
EXPORT = "/api/assets/export.csv"


def _csv(client, **params):
    res = client.get(EXPORT, params=params)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"].startswith("attachment; filename=")
    return list(csv.reader(io.StringIO(res.text)))


def test_export_starts_with_the_required_columns(client):
    header = ["uuid", "title", "description", "filename", "media_type", "tags", "created_at"]
    assert search.CSV_COLUMNS == tuple(header)
    assert _csv(client, q="no-such-thing")[0] == header


def test_export_with_no_matches_still_has_the_header(client):
    assert _csv(client, q="no-such-thing", tags=["approved"]) == [list(search.CSV_COLUMNS)]


def test_export_covers_every_page_in_the_list_order(client, pager):
    col = pager["collection"]
    for sort in ("newest", "oldest"):
        listed = []
        for offset in range(0, PAGER_ROWS, PAGE_SIZE):
            listed += _uuids(client, collection_id=col, sort=sort, limit=PAGE_SIZE, offset=offset)
        rows = _csv(client, collection_id=col, sort=sort)
        # The whole result, not one page of it.
        assert len(rows) == PAGER_ROWS + 1
        assert [r[0] for r in rows[1:]] == listed


def test_export_applies_the_same_filters_and_sort_as_the_list(client, harbor):
    col = harbor["collection"]
    for sort, expected in (("newest", [harbor["e"], harbor["a"]]),
                           ("oldest", [harbor["a"], harbor["e"]])):
        assert _uuids(client, **SCENARIO, collection_id=col, sort=sort) == expected
        assert [r[0] for r in _csv(client, **SCENARIO, collection_id=col, sort=sort)[1:]] == expected


def test_export_applies_the_media_type_filter(client):
    col = _collection("Export media types")
    _asset("harbor poster", collection=col)
    video = _asset("harbor clip", collection=col, media_type="video")
    rows = _csv(client, collection_id=col, media_type="video")
    assert len(rows) == 2
    assert rows[1][0] == video and rows[1][4] == "video"


def test_export_quotes_commas_quotes_newlines_and_keeps_chinese(client):
    title = '港口夜景, "一号" 码头'
    description = '第一行\n第二行, with "quotes"\n'
    filename = "港口, 夜景.jpg"
    tags = ["night,shift", 'say "hi"', "夜景"]
    created_at = "2026-04-01T12:30:00+00:00"
    uid = _asset(title, tags=tags, collection=_collection("Quoting"), description=description,
                 filename=filename, created_at=created_at)
    assert _csv(client, q="港口")[1:] == [
        [uid, title, description, filename, "image", 'night,shift;say "hi";夜景', created_at]]
    # A quote inside a field is doubled, as RFC 4180 asks.
    assert 'say ""hi""' in client.get(EXPORT, params={"q": "港口"}).text


def test_export_keeps_tags_past_the_first_batch(client):
    # Tags are looked up in batches of 500 ids, so the file must stay whole beyond that.
    col = _collection("Large export")
    with transaction() as conn:
        uuids = [_asset(f"bulk row {i:04d}", tags=["bulk"], collection=col, conn=conn, index=False)
                 for i in range(1050)]
    rows = _csv(client, collection_id=col)
    assert sorted(r[0] for r in rows[1:]) == sorted(uuids)
    assert all(r[5] == "bulk" for r in rows[1:])


def test_the_export_requires_login():
    assert TestClient(app).get(EXPORT).status_code == 401


def test_filters_never_return_deleted_assets(client):
    # Whatever status is asked for, only active assets come back.
    col = _collection("Soft deleted")
    live = _asset("harbor kept", tags=["approved"], collection=col)
    _asset("harbor gone", tags=["approved"], collection=col, status="deleted")
    for asked in ("deleted", "anything", "ACTIVE"):
        assert client.get("/api/assets", params={"status": asked}).status_code == 422
        assert client.get(EXPORT, params={"status": asked}).status_code == 422
    assert _uuids(client, collection_id=col) == [live]
    assert [r[0] for r in _csv(client, collection_id=col)[1:]] == [live]

    # A viewer, the role with the least access, gets the same answers.
    assert client.post("/api/users", json={"username": "reader", "password": "reader-pass",
                                            "role": "viewer"}).status_code == 200
    viewer = TestClient(app)
    token = viewer.post("/api/auth/login",
                        json={"username": "reader", "password": "reader-pass"}).json()["token"]
    viewer.headers["Authorization"] = f"Bearer {token}"
    assert viewer.get("/api/assets", params={"status": "deleted"}).status_code == 422
    assert viewer.get(EXPORT, params={"status": "deleted"}).status_code == 422
    assert [r[0] for r in _csv(viewer, collection_id=col)[1:]] == [live]
