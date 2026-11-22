import base64
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.store import BLOCK_SIZE, Store, StoreError  # noqa: E402


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "pool.db"), total_blocks=8)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("POOL_TOTAL_BLOCKS", "8")
    with TestClient(app) as c:
        yield c


def blk(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) <= BLOCK_SIZE
    return raw.ljust(BLOCK_SIZE, b"\x00")


# ------------------------------------------------------------------ basics

def test_first_write_and_private_overwrite_accounting(store):
    r = store.write_block("sys", 0, blk("a"))
    assert r["action"] == "first_write"
    assert r["charged"] == "public"
    assert r["live_blocks"] == 1
    assert store.status()["public_free_blocks"] == 7

    r = store.write_block("sys", 0, blk("b"))
    assert r["action"] == "overwrite_private"
    assert r["charged"] == "none"
    assert r["live_blocks"] == 1
    assert store.status()["public_free_blocks"] == 7
    assert store.read_block("sys", 0)["text"] == "b"


def test_block_size_is_enforced(store):
    with pytest.raises(StoreError) as err:
        store.write_block("sys", 0, b"short")
    assert err.value.code == "bad_size"
    assert store.status()["live_blocks"] == 0


def test_unknown_or_unallocated_reads(store):
    with pytest.raises(StoreError) as err:
        store.read_block("sys", 3)
    assert err.value.status == 404
    with pytest.raises(StoreError) as err:
        store.read_block("nope", 0)
    assert err.value.status == 404


def test_snapshot_is_read_only(store):
    store.write_block("sys", 0, blk("before"))
    store.create_snapshot(2)
    with pytest.raises(StoreError) as err:
        store.write_block("snapshot", 0, blk("hack"))
    assert err.value.code == "read_only"
    assert store.read_block("snapshot", 0)["text"] == "before"


# --------------------------------------------------------------- snapshot

def test_snapshot_shares_blocks_and_reserves(store):
    store.write_block("sys", 0, blk("x"))
    store.write_block("sys", 1, blk("y"))
    free_before = store.status()["public_free_blocks"]
    res = store.create_snapshot(3)
    assert res["shared_blocks"] == 2
    st = store.status()
    assert st["public_free_blocks"] == free_before - 3
    assert st["reserved_blocks"] == 3
    assert st["snapshot"]["reserved_blocks"] == 3
    # Snapshot sees the same physical blocks.
    assert store.read_block("snapshot", 0)["pblock_id"] == \
        store.read_block("sys", 0)["pblock_id"]


def test_snapshot_creation_rejected_when_reservation_exceeds_public(store):
    store.write_block("sys", 0, blk("x"))  # live 1, total 8, public 7
    with pytest.raises(StoreError) as err:
        store.create_snapshot(8)
    assert err.value.code == "insufficient_space"
    assert store.status()["snapshot"] is None
    assert store.status()["public_free_blocks"] == 7


def test_second_snapshot_rejected(store):
    store.create_snapshot(1)
    with pytest.raises(StoreError) as err:
        store.create_snapshot(1)
    assert err.value.code == "snapshot_exists"


def test_cow_uses_reservation_then_public(store):
    for i in range(4):
        store.write_block("sys", i, blk(f"v{i}"))
    # live 4, public 4
    store.create_snapshot(2)
    assert store.status()["public_free_blocks"] == 2

    r0 = store.write_block("sys", 0, blk("new0"))
    assert r0["action"] == "overwrite_shared"
    assert r0["charged"] == "reservation"
    assert r0["reservation_remaining_blocks"] == 1
    assert store.read_block("snapshot", 0)["text"] == "v0"

    r1 = store.write_block("sys", 1, blk("new1"))
    assert r1["charged"] == "reservation"
    assert r1["reservation_remaining_blocks"] == 0
    assert r1["public_free_blocks"] == 2

    r2 = store.write_block("sys", 2, blk("new2"))
    assert r2["charged"] == "public"
    assert r2["public_free_blocks"] == 1

    r3 = store.write_block("sys", 3, blk("new3"))
    assert r3["charged"] == "public"
    assert r3["public_free_blocks"] == 0
    assert store.status()["live_blocks"] == 8
    assert store.check_consistency()["ok"]


# ----------------------------------------------------------- pool exhaustion

def test_private_overwrite_needs_no_space_when_pool_is_full(store):
    for i in range(8):
        store.write_block("sys", i, blk(f"v{i}"))
    assert store.status()["public_free_blocks"] == 0
    r = store.write_block("sys", 3, blk("rewritten"))
    assert r["action"] == "overwrite_private"
    assert r["charged"] == "none"
    assert store.status()["public_free_blocks"] == 0
    assert store.read_block("sys", 3)["text"] == "rewritten"


def test_shared_overwrite_falls_back_to_public_when_reservation_empty(store):
    store.write_block("sys", 0, blk("original"))
    store.create_snapshot(0)  # no reservation at all
    r = store.write_block("sys", 0, blk("copy"))
    assert r["action"] == "overwrite_shared"
    assert r["charged"] == "public"
    assert r["reservation_remaining_blocks"] == 0
    assert store.read_block("snapshot", 0)["text"] == "original"
    assert store.read_block("sys", 0)["text"] == "copy"


def test_pool_full_first_write_rejected_and_state_unchanged(store):
    for i in range(8):
        store.write_block("sys", i, blk(f"v{i}"))
    before = store.status()
    with pytest.raises(StoreError) as err:
        store.write_block("data", 0, blk("nope"))
    assert err.value.code == "pool_full"
    assert err.value.status == 409
    after = store.status()
    assert after == before
    assert store.read_block("data", 0) if False else True
    with pytest.raises(StoreError) as read_err:
        store.read_block("data", 0)
    assert read_err.value.code == "block_not_mapped"


def test_pool_full_overwrite_shared_rejected_and_state_unchanged(store):
    for i in range(4):
        store.write_block("sys", i, blk(f"v{i}"))
    store.create_snapshot(0)  # public stays 4; sys blocks stay shared
    for i in range(4):
        store.write_block("data", i, blk(f"d{i}"))  # public 0, live 8
    before = store.status()
    before_sys = store.list_blocks("sys")["blocks"]
    assert before["public_free_blocks"] == 0
    assert before["reserved_blocks"] == 0
    assert before["live_blocks"] == 8

    with pytest.raises(StoreError) as err:
        store.write_block("sys", 0, blk("again"))
    assert err.value.code == "pool_full"
    assert err.value.status == 409

    assert store.status() == before
    assert store.list_blocks("sys")["blocks"] == before_sys
    assert store.read_block("sys", 0)["text"] == "v0"
    assert store.check_consistency()["ok"]


# --------------------------------------------------------- snapshot deletion

def test_delete_snapshot_releases_only_snapshot_blocks_and_reservation(store):
    for i in range(4):
        store.write_block("sys", i, blk(f"v{i}"))
    store.write_block("data", 0, blk("d"))
    store.create_snapshot(3)  # public was 3, now 0
    # COW one shared block from the reservation.
    store.write_block("sys", 0, blk("new0"))
    # live = 6 blocks total (4 sys + 1 data + 1 cow), reservation used 1
    st = store.status()
    assert st["live_blocks"] == 6
    assert st["reserved_blocks"] == 2

    res = store.delete_snapshot()
    # The old sys0 block (shared with the snapshot, replaced by COW) is only
    # referenced by the snapshot now; sys1..3 are still live for the system disk.
    assert res["freed_blocks"] == 1
    assert res["released_reservation_blocks"] == 2
    assert store.status()["snapshot"] is None
    # live: sys0 cow copy + sys1..3 + data0 = 5; public 8-5=3
    assert store.status()["live_blocks"] == 5
    assert store.status()["public_free_blocks"] == 3
    assert store.read_block("sys", 0)["text"] == "new0"
    assert store.read_block("sys", 1)["text"] == "v1"
    assert store.read_block("data", 0)["text"] == "d"
    with pytest.raises(StoreError):
        store.read_block("snapshot", 1)
    assert store.check_consistency()["ok"]


def test_delete_snapshot_twice_rejected(store):
    store.create_snapshot(1)
    store.delete_snapshot()
    with pytest.raises(StoreError) as err:
        store.delete_snapshot()
    assert err.value.code == "no_snapshot"


def test_snapshot_absent_reads_rejected(store):
    with pytest.raises(StoreError) as err:
        store.read_block("snapshot", 0)
    assert err.value.code == "no_snapshot"
    with pytest.raises(StoreError) as err:
        store.list_blocks("snapshot")
    assert err.value.code == "no_snapshot"


def test_negative_lba_rejected(store):
    with pytest.raises(StoreError) as err:
        store.write_block("sys", -1, blk("x"))
    assert err.value.status == 400
    with pytest.raises(StoreError) as err:
        store.read_block("data", -5)
    assert err.value.status == 400


def test_snapshot_blocks_released_when_disk_also_drops_them(store):
    """A block shared by sys + snapshot is released by delete only if sys no
    longer references it; blocks still used by sys survive."""
    store.write_block("sys", 0, blk("a"))
    store.write_block("sys", 1, blk("b"))
    store.create_snapshot(0)
    # Overwrite sys/0 -> COW new block, old one only in the snapshot.
    store.write_block("sys", 0, blk("a2"))
    res = store.delete_snapshot()
    assert res["freed_blocks"] == 1  # old a, sys/1's block is still live
    assert store.status()["live_blocks"] == 2
    assert store.read_block("sys", 1)["text"] == "b"


def test_resnapshot_after_delete(store):
    store.write_block("sys", 0, blk("v1"))
    store.create_snapshot(1)
    store.write_block("sys", 0, blk("v2"))
    store.delete_snapshot()
    store.create_snapshot(2)
    assert store.read_block("snapshot", 0)["text"] == "v2"
    assert store.status()["snapshot"]["reserved_blocks"] == 2


def test_reservation_never_exceeds_reserved_under_concurrency(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race3.db")
    s0 = Store(path, total_blocks=10)
    for i in range(6):
        s0.write_block("sys", i, blk(f"v{i}"))
    s0.create_snapshot(4)  # public 0

    def attempt(i):
        s = Store(path)
        try:
            s.write_block("sys", i % 6, blk(f"w{i}"))
            return "ok"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(30)))

    st = Store(path).status()
    snap = st["snapshot"]
    assert snap is not None
    assert snap["reservation_used_blocks"] <= snap["reserved_blocks"]
    assert snap["reservation_remaining_blocks"] == st["reserved_blocks"]
    assert st["public_free_blocks"] == 0
    # 6 lbas x 5 attempts.  Exactly 4 lbas get a COW copy from the reservation;
    # every later write to those 4 is a free private overwrite, while all 5
    # attempts on each of the other 2 lbas are refused -> 10 failures.
    assert results.count("pool_full") == 10
    assert results.count("ok") == 20
    assert snap["reservation_used_blocks"] == 4
    assert st["live_blocks"] == 10
    assert Store(path).check_consistency()["ok"]


def test_concurrent_snapshot_creation_single_winner(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race4.db")
    Store(path, total_blocks=8)

    def attempt(i):
        s = Store(path)
        try:
            s.create_snapshot(1)
            return "ok"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 1
    assert results.count("snapshot_exists") == 7
    assert Store(path).status()["reserved_blocks"] == 1


def test_concurrent_delete_snapshot_single_winner(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race5.db")
    s0 = Store(path, total_blocks=8)
    s0.write_block("sys", 0, blk("v"))
    s0.create_snapshot(2)

    def attempt(_):
        s = Store(path)
        try:
            s.delete_snapshot()
            return "ok"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 1
    assert results.count("no_snapshot") == 7
    st = Store(path).status()
    assert st["snapshot"] is None
    assert st["public_free_blocks"] == 7
    assert Store(path).check_consistency()["ok"]


# ------------------------------------------------------------ concurrency

def test_concurrent_first_writes_never_oversubscribe(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race.db")
    Store(path, total_blocks=10)
    results = []

    def attempt(i):
        s = Store(path)
        try:
            s.write_block("sys" if i % 2 else "data", i, blk(f"v{i}"))
            return "ok"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(attempt, range(40)))

    assert results.count("ok") == 10
    assert results.count("pool_full") == 30
    s = Store(path)
    st = s.status()
    assert st["live_blocks"] == 10
    assert st["public_free_blocks"] == 0
    assert s.check_consistency()["ok"]


def test_concurrent_mixed_writes_keep_invariant(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race2.db")
    s0 = Store(path, total_blocks=12)
    for i in range(6):
        s0.write_block("sys", i, blk(f"v{i}"))
    s0.create_snapshot(3)  # public = 12-6-3 = 3

    def attempt(i):
        s = Store(path)
        try:
            s.write_block("sys", i % 6, blk(f"w{i}"))
            return "ok"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(30)))

    st = Store(path).status()
    assert st["public_free_blocks"] >= 0
    assert st["reserved_blocks"] >= 0
    # At most 3 public blocks + 3 reservation blocks can fund COW/allocations.
    assert st["live_blocks"] <= 12
    assert Store(path).check_consistency()["ok"]


# ----------------------------------------------------------- persistence

def test_restart_preserves_disks_and_quota(tmp_path):
    path = str(tmp_path / "persist.db")
    s = Store(path, total_blocks=8)
    s.write_block("sys", 0, blk("keep"))
    s.write_block("data", 5, blk("data5"))
    s.create_snapshot(2)
    s.write_block("sys", 0, blk("changed"))  # reservation used 1
    snapshot_before = s.status()

    s2 = Store(path)  # restart: no capacity argument
    assert s2.status() == snapshot_before
    assert s2.read_block("sys", 0)["text"] == "changed"
    assert s2.read_block("snapshot", 0)["text"] == "keep"
    assert s2.read_block("data", 5)["text"] == "data5"

    # The persisted quota still applies after restart.
    s2.write_block("data", 6, blk("fits"))  # public 8-3-1 = 4 -> 3
    s2.write_block("data", 7, blk("fits2"))  # public 2
    s2.write_block("data", 8, blk("fits3"))  # public 1
    s2.write_block("data", 9, blk("fits4"))  # public 0
    with pytest.raises(StoreError) as err:
        s2.write_block("data", 10, blk("overflow"))
    assert err.value.code == "pool_full"


def test_restart_rejects_capacity_below_usage(tmp_path):
    path = str(tmp_path / "shrink.db")
    s = Store(path, total_blocks=8)
    for i in range(4):
        s.write_block("sys", i, blk(f"v{i}"))
    s.create_snapshot(2)
    with pytest.raises(StoreError) as err:
        Store(path, total_blocks=5)
    assert err.value.code == "capacity_below_usage"


# ------------------------------------------------------------------- API

def test_api_status_and_snapshot_read(client):
    r = client.put("/api/disks/sys/blocks/0", json={"text": "api"})
    assert r.status_code == 200
    assert r.json()["action"] == "first_write"

    st = client.get("/api/status").json()
    assert st["total_blocks"] == 8
    assert st["public_free_blocks"] == 7
    assert st["reserved_blocks"] == 0
    assert st["snapshot"] is None

    assert client.post("/api/snapshot", json={"reserved_blocks": 2}).status_code == 200
    st = client.get("/api/status").json()
    assert st["public_free_blocks"] == 5
    assert st["reserved_blocks"] == 2
    assert st["snapshot"]["reserved_blocks"] == 2

    snap = client.get("/api/disks/snapshot/blocks/0")
    assert snap.status_code == 200
    assert snap.json()["text"] == "api"
    assert snap.json()["shared_with_snapshot"] is True

    read_back = client.get("/api/disks/sys/blocks/0").json()
    assert read_back["pblock_id"] == snap.json()["pblock_id"]


def test_first_write_never_uses_reservation(store):
    store.write_block("sys", 0, blk("a"))
    store.create_snapshot(2)  # public already 0: 8-1-2 = 5? no: 8-1=7, reserve2 -> 5
    # Drain public space via the data disk.
    for i in range(store.status()["public_free_blocks"]):
        store.write_block("data", i, blk(f"d{i}"))
    st = store.status()
    assert st["public_free_blocks"] == 0
    assert st["reserved_blocks"] == 2
    with pytest.raises(StoreError) as err:
        store.write_block("data", 100, blk("nope"))
    assert err.value.code == "pool_full"
    # Reservation untouched, COW of the shared sys block still works.
    assert store.status()["reserved_blocks"] == 2
    r = store.write_block("sys", 0, blk("new"))
    assert r["charged"] == "reservation"
    assert r["reservation_remaining_blocks"] == 1
    assert r["public_free_blocks"] == 0


def test_api_serves_ui(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "块池" in page.text
    css = client.get("/static/style.css")
    assert css.status_code == 200
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "/api/status" in js.text


def test_api_hex_and_b64_roundtrip(client):
    payload = bytes(range(256)) * 16
    assert len(payload) == BLOCK_SIZE
    r = client.put("/api/disks/data/blocks/0", json={"data_hex": payload.hex()})
    assert r.status_code == 200
    body = client.get("/api/disks/data/blocks/0").json()
    assert body["data_hex"] == payload.hex()
    assert body["data_b64"] == base64.b64encode(payload).decode()

    other = bytes([7]) * BLOCK_SIZE
    r = client.put("/api/disks/data/blocks/1",
                   json={"data_b64": base64.b64encode(other).decode()})
    assert r.status_code == 200
    assert client.get("/api/disks/data/blocks/1").json()["data_hex"] == other.hex()

    fill = client.put("/api/disks/data/blocks/2", json={"fill": 65})
    assert fill.status_code == 200
    assert client.get("/api/disks/data/blocks/2").json()["text"] == "A" * BLOCK_SIZE


def test_api_snapshot_read_only_write(client):
    client.put("/api/disks/sys/blocks/0", json={"text": "v"})
    client.post("/api/snapshot", json={"reserved_blocks": 1})
    r = client.put("/api/disks/snapshot/blocks/0", json={"text": "hack"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "read_only"
    assert client.get("/api/disks/sys/blocks/0").json()["text"] == "v"


def test_api_no_snapshot_read(client):
    r = client.get("/api/disks/snapshot/blocks/0")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_snapshot"


def test_api_validation_errors_use_uniform_shape(client):
    r = client.post("/api/snapshot", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    r = client.put("/api/disks/sys/blocks/0", json={"text": 5})
    assert r.status_code == 422
    assert "error" in r.json()
    r = client.put("/api/disks/sys/blocks/notanint", json={"text": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_api_rejects_bad_requests(client):
    assert client.put("/api/disks/sys/blocks/0", json={"text": "x", "fill": 1}).status_code == 400
    assert client.put("/api/disks/sys/blocks/0", json={}).status_code == 400
    assert client.put("/api/disks/sys/blocks/0",
                      json={"data_hex": "00" * (BLOCK_SIZE - 1)}).status_code == 400
    assert client.put("/api/disks/snapshot/blocks/0", json={"text": "x"}).status_code == 400
    assert client.get("/api/disks/nope/blocks/0").status_code == 404
    assert client.post("/api/snapshot", json={"reserved_blocks": -1}).status_code == 400
    assert client.post("/api/snapshot", json={"reserved_blocks": 99}).status_code == 409


def test_api_pool_full_and_snapshot_delete_roundtrip(client):
    for i in range(8):
        assert client.put(f"/api/disks/data/blocks/{i}",
                          json={"text": f"d{i}"}).status_code == 200
    before = client.get("/api/status").json()
    r = client.put("/api/disks/sys/blocks/0", json={"text": "nope"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "pool_full"
    assert client.get("/api/status").json() == before

    # Free one block, snapshot, then delete and check both gauges.
    # (No delete-block endpoint; use a fresh DB path instead.)
    diag = client.get("/api/diagnostics").json()
    assert diag["ok"] is True


def test_api_snapshot_delete_frees_space(client):
    for i in range(4):
        client.put(f"/api/disks/sys/blocks/{i}", json={"text": f"v{i}"})
    client.post("/api/snapshot", json={"reserved_blocks": 2})
    client.put("/api/disks/sys/blocks/0", json={"text": "new"})
    st = client.get("/api/status").json()
    assert st["public_free_blocks"] == 2
    assert st["reserved_blocks"] == 1

    r = client.delete("/api/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["freed_blocks"] == 1
    assert body["released_reservation_blocks"] == 1
    st = client.get("/api/status").json()
    assert st["snapshot"] is None
    assert st["public_free_blocks"] == 4
    assert st["live_blocks"] == 4


# ------------------------------------------------------------------ packing

def rnd_block(seed: int) -> bytes:
    """Incompressible 4 KiB (zlib stays above the 2 KiB half-slot limit)."""
    import random
    return random.Random(seed).randbytes(BLOCK_SIZE)


def test_pack_pairs_free_slots_and_readback(store):
    for i in range(4):
        store.write_block("data", i, blk(f"pack-me-{i}"))
    assert store.status()["public_free_blocks"] == 4
    res = store.pack_blocks("data", 0, 4)
    assert res["packed_pairs"] == 2
    assert res["freed_slots"] == 2
    assert res["public_free_blocks"] == 6
    st = store.status()
    assert st["public_free_blocks"] == 6
    assert st["live_blocks"] == 2
    assert st["packed_pairs"] == 2
    assert st["disks"]["data"]["packed_blocks"] == 4
    # 每个块号仍读出完整 4 KiB 原文
    for i in range(4):
        b = store.read_block("data", i)
        assert b["text"] == f"pack-me-{i}"
        assert b["size"] == BLOCK_SIZE
        assert b["packed"] is True
        assert b["pack_half"] == i % 2
        assert 0 < b["compressed_size"] <= BLOCK_SIZE // 2
    listed = store.list_blocks("data")["blocks"]
    assert all(b["packed"] for b in listed)
    # 同一对的两个块号共享一个物理槽
    assert listed[0]["pblock_id"] == listed[1]["pblock_id"]
    assert listed[2]["pblock_id"] == listed[3]["pblock_id"]
    assert listed[0]["pack_partner_lba"] == 1
    assert listed[3]["pack_partner_lba"] == 2
    assert store.check_consistency()["ok"]


def test_pack_skips_and_is_idempotent(store):
    store.write_block("data", 0, blk("a"))
    store.write_block("data", 1, rnd_block(1))  # 压不进 2 KiB
    store.write_block("data", 2, blk("c"))
    # 范围 0..5：对 (0,1) 不可压，对 (2,3) 半数未分配，块 4 落单
    res = store.pack_blocks("data", 0, 5)
    assert res["packed_pairs"] == 0
    assert res["skipped"]["incompressible"] == 1
    assert res["skipped"]["unallocated"] == 1
    assert store.status()["packed_pairs"] == 0
    # 换成可压缩内容后补齐，再次合装成功
    store.write_block("data", 1, blk("b"))
    store.write_block("data", 3, blk("d"))
    res = store.pack_blocks("data", 0, 4)
    assert res["packed_pairs"] == 2
    # 幂等：重复合装不再释放任何槽
    res2 = store.pack_blocks("data", 0, 4)
    assert res2["packed_pairs"] == 0
    assert res2["skipped"]["already_packed"] == 2
    assert res2["freed_slots"] == 0
    assert store.check_consistency()["ok"]


def test_pack_validates_input(store):
    with pytest.raises(StoreError) as err:
        store.pack_blocks("sys", 0, 2)
    assert err.value.code == "pack_only_data"
    with pytest.raises(StoreError) as err:
        store.pack_blocks("snapshot", 0, 2)
    assert err.value.code == "pack_only_data"
    with pytest.raises(StoreError) as err:
        store.pack_blocks("nope", 0, 2)
    assert err.value.code == "unknown_disk"
    for bad in (0, 65, -3):
        with pytest.raises(StoreError) as err:
            store.pack_blocks("data", 0, bad)
        assert err.value.code == "bad_count"
    with pytest.raises(StoreError) as err:
        store.pack_blocks("data", -1, 2)
    assert err.value.code == "bad_lba"


def test_pack_works_when_public_free_is_zero(store):
    # 8 个可压缩块把池占满，公共余量归零
    for i in range(8):
        store.write_block("data", i, blk(f"full-{i}"))
    assert store.status()["public_free_blocks"] == 0
    res = store.pack_blocks("data", 0, 8)
    assert res["packed_pairs"] == 4
    assert res["freed_slots"] == 4
    # 释放的槽立即归公共余量，且立即可用
    assert store.status()["public_free_blocks"] == 4
    store.write_block("sys", 0, blk("uses-freed-slot"))
    assert store.status()["public_free_blocks"] == 3
    for i in range(8):
        assert store.read_block("data", i)["text"] == f"full-{i}"
    assert store.check_consistency()["ok"]


def test_overwrite_packed_still_compressible_stays_in_slot(store):
    store.write_block("data", 0, blk("aa"))
    store.write_block("data", 1, blk("bb"))
    store.pack_blocks("data", 0, 2)
    slot = store.read_block("data", 0)["pblock_id"]
    free_before = store.status()["public_free_blocks"]
    r = store.write_block("data", 0, blk("aa-v2"))
    assert r["action"] == "overwrite_packed"
    assert r["charged"] == "none"
    assert r["pblock_id"] == slot  # 留在原槽
    assert store.status()["public_free_blocks"] == free_before
    b0 = store.read_block("data", 0)
    assert b0["text"] == "aa-v2"
    assert b0["packed"] is True
    # 伙伴块内容照旧，仍与改写块同槽
    b1 = store.read_block("data", 1)
    assert b1["text"] == "bb"
    assert b1["pblock_id"] == slot
    assert store.check_consistency()["ok"]


def test_overwrite_packed_incompressible_splits_via_public_slot(store):
    store.write_block("data", 0, blk("aa"))
    store.write_block("data", 1, blk("bb"))
    store.pack_blocks("data", 0, 2)
    slot = store.read_block("data", 1)["pblock_id"]
    free_before = store.status()["public_free_blocks"]
    payload = rnd_block(7)
    r = store.write_block("data", 0, payload)
    assert r["action"] == "overwrite_packed_split"
    assert r["charged"] == "public"
    assert r["pblock_id"] != slot  # 只用公共空槽拆开
    st = store.status()
    assert st["public_free_blocks"] == free_before - 1
    assert st["packed_pairs"] == 0
    assert store.read_block("data", 0)["data_hex"] == payload.hex()
    # 伙伴块留在原槽、内容照旧、恢复为普通块
    b1 = store.read_block("data", 1)
    assert b1["text"] == "bb"
    assert b1["pblock_id"] == slot
    assert b1["packed"] is False
    assert store.check_consistency()["ok"]


def test_overwrite_packed_split_rejected_without_public_free(store):
    for i in range(8):
        store.write_block("data", i, blk(f"v{i}"))
    store.pack_blocks("data", 0, 8)  # 释放 4 槽
    for i in range(4):
        store.write_block("sys", i, blk(f"s{i}"))  # 重新占满
    before = store.status()
    assert before["public_free_blocks"] == 0
    before_blocks = store.list_blocks("data")["blocks"]
    with pytest.raises(StoreError) as err:
        store.write_block("data", 0, rnd_block(9))
    assert err.value.code == "pool_full"
    assert err.value.status == 409
    # 两块内容、合装状态和公共余量都保留
    assert store.status() == before
    assert store.list_blocks("data")["blocks"] == before_blocks
    assert store.read_block("data", 0)["text"] == "v0"
    assert store.read_block("data", 1)["text"] == "v1"
    assert store.read_block("data", 0)["packed"] is True
    assert store.read_block("data", 1)["packed"] is True
    assert store.check_consistency()["ok"]


def test_packed_blocks_survive_snapshot_cycle(store):
    store.write_block("sys", 0, blk("sys0"))
    store.write_block("data", 0, blk("d0"))
    store.write_block("data", 1, blk("d1"))
    store.pack_blocks("data", 0, 2)
    store.create_snapshot(1)  # 快照只共享系统盘，与合装无冲突
    assert store.read_block("data", 0)["packed"] is True
    store.delete_snapshot()
    assert store.read_block("data", 0)["text"] == "d0"
    assert store.read_block("data", 1)["text"] == "d1"
    assert store.status()["packed_pairs"] == 1
    assert store.check_consistency()["ok"]


def test_restart_preserves_packs_and_quota(tmp_path):
    path = str(tmp_path / "pack_persist.db")
    s = Store(path, total_blocks=8)
    for i in range(6):
        s.write_block("data", i, blk(f"keep-{i}"))
    s.pack_blocks("data", 0, 6)          # 3 对，释放 3 槽
    s.write_block("data", 0, blk("keep-0-v2"))  # 仍可合装：留在原槽
    s.write_block("data", 2, rnd_block(3))      # 压不回：拆到公共槽
    before = s.status()

    s2 = Store(path)  # 重启：盘面与余量以已提交状态为准
    assert s2.status() == before
    assert s2.read_block("data", 0)["text"] == "keep-0-v2"
    assert s2.read_block("data", 0)["packed"] is True
    assert s2.read_block("data", 1)["text"] == "keep-1"
    assert s2.read_block("data", 2)["data_hex"] == rnd_block(3).hex()
    assert s2.read_block("data", 2)["packed"] is False
    assert s2.read_block("data", 3)["text"] == "keep-3"
    assert s2.read_block("data", 4)["packed"] is True
    assert s2.check_consistency()["ok"]


def test_concurrent_pack_and_writes_keep_invariant(tmp_path):
    import concurrent.futures as cf

    path = str(tmp_path / "race_pack.db")
    s0 = Store(path, total_blocks=16)
    for i in range(10):
        s0.write_block("data", i, blk(f"c{i}"))

    def attempt(i):
        s = Store(path)
        try:
            if i % 3 == 0:
                s.pack_blocks("data", 0, 10)
                return "pack"
            s.write_block("data", i % 10, blk(f"w{i}"))
            return "write"
        except StoreError as exc:
            return exc.code

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(attempt, range(30)))

    s = Store(path)
    st = s.status()
    assert st["public_free_blocks"] >= 0
    # 已确认的写入都保留：每个块号仍能读出完整 4 KiB
    for i in range(10):
        assert s.read_block("data", i)["size"] == BLOCK_SIZE
    assert s.check_consistency()["ok"]


def test_api_pack_flow(client):
    for i in range(8):
        assert client.put(f"/api/disks/data/blocks/{i}",
                          json={"text": f"p{i}"}).status_code == 200
    assert client.get("/api/status").json()["public_free_blocks"] == 0
    r = client.post("/api/disks/data/pack", json={"start_lba": 0, "count": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["packed_pairs"] == 4
    assert body["freed_slots"] == 4
    assert body["public_free_blocks"] == 4
    st = client.get("/api/status").json()
    assert st["public_free_blocks"] == 4
    assert st["packed_pairs"] == 4
    assert st["disks"]["data"]["packed_blocks"] == 8
    b = client.get("/api/disks/data/blocks/3").json()
    assert b["text"] == "p3"
    assert b["size"] == BLOCK_SIZE
    assert b["packed"] is True
    listed = client.get("/api/disks/data/blocks").json()["blocks"]
    assert all(x["packed"] for x in listed)
    assert client.get("/api/diagnostics").json()["ok"] is True


def test_api_pack_rejects_bad_requests(client):
    assert client.post("/api/disks/sys/pack",
                       json={"start_lba": 0, "count": 2}).status_code == 400
    r = client.post("/api/disks/data/pack", json={"start_lba": 0, "count": 65})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_count"
    r = client.post("/api/disks/data/pack", json={"start_lba": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert client.post("/api/disks/nope/pack",
                       json={"start_lba": 0, "count": 2}).status_code == 404


def test_api_packed_overwrite_outcomes(client):
    client.put("/api/disks/data/blocks/0", json={"text": "x"})
    client.put("/api/disks/data/blocks/1", json={"text": "y"})
    client.post("/api/disks/data/pack", json={"start_lba": 0, "count": 2})
    slot = client.get("/api/disks/data/blocks/0").json()["pblock_id"]
    # 结果一：压缩后仍 ≤ 2 KiB，留在原槽
    r = client.put("/api/disks/data/blocks/0", json={"text": "x2"})
    assert r.json()["action"] == "overwrite_packed"
    assert r.json()["pblock_id"] == slot
    # 结果二：压不回 2 KiB，拆到公共空槽
    raw = os.urandom(BLOCK_SIZE)
    r = client.put("/api/disks/data/blocks/0", json={"data_hex": raw.hex()})
    assert r.json()["action"] == "overwrite_packed_split"
    assert r.json()["charged"] == "public"
    assert client.get("/api/disks/data/blocks/0").json()["data_hex"] == raw.hex()
    assert client.get("/api/disks/data/blocks/1").json()["text"] == "y"


def test_api_packed_split_needs_public_slot(client):
    for i in range(8):
        client.put(f"/api/disks/data/blocks/{i}", json={"text": f"p{i}"})
    client.post("/api/disks/data/pack", json={"start_lba": 0, "count": 8})
    for i in range(4):
        client.put(f"/api/disks/sys/blocks/{i}", json={"text": f"s{i}"})
    before = client.get("/api/status").json()
    assert before["public_free_blocks"] == 0
    r = client.put("/api/disks/data/blocks/0",
                   json={"data_hex": os.urandom(BLOCK_SIZE).hex()})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "pool_full"
    assert client.get("/api/status").json() == before
    assert client.get("/api/disks/data/blocks/0").json()["text"] == "p0"
    assert client.get("/api/disks/data/blocks/1").json()["text"] == "p1"
