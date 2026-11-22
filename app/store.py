"""SQLite persistence for a fixed-capacity shared block pool.

Accounting model
----------------
The pool has ``total_blocks`` slots.  Every live physical block occupies one
slot.  The snapshot may additionally hold a *reservation* of ``R`` slots which
are carved out of the public free space but not yet materialised:

    public_free = total - live_physical_blocks - reservation_remaining

* first write to an unallocated LBA      -> public free, one new block
* overwrite of a private block           -> in place, no space
* overwrite of a block shared w/ snapshot-> copy-on-write, charged to the
  reservation first and to public free only once the reservation is empty
* creating the snapshot                  -> requires ``R`` public free blocks
* deleting the snapshot                  -> releases blocks referenced only by
  the snapshot plus the unused reservation

Every mutation runs in a single ``BEGIN IMMEDIATE`` transaction; on any error
the transaction is rolled back, so a failed operation leaves the disk image and
the quota exactly as they were before it started.  WAL mode plus the write lock
also serialise concurrent writers, so two requests can never oversubscribe the
pool.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

BLOCK_SIZE = 4096
DISK_IDS = ("sys", "data")
DISK_NAMES = {"sys": "系统盘", "data": "数据盘"}
SNAPSHOT_ID = "snapshot"


class StoreError(Exception):
    """Domain error carrying an HTTP status."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


SCHEMA = """
CREATE TABLE IF NOT EXISTS pool (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    total_blocks INTEGER NOT NULL CHECK (total_blocks >= 0),
    block_size   INTEGER NOT NULL CHECK (block_size > 0)
);

-- A physical 4 KiB block.  Rows are only kept while referenced by a disk
-- mapping or by the snapshot; unreferenced rows are garbage-collected inside
-- the same transaction that detached them.
CREATE TABLE IF NOT EXISTS pblocks (
    id   INTEGER PRIMARY KEY,
    data BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS mappings (
    disk_id   TEXT NOT NULL CHECK (disk_id IN ('sys', 'data')),
    lba       INTEGER NOT NULL CHECK (lba >= 0),
    pblock_id INTEGER NOT NULL REFERENCES pblocks(id),
    PRIMARY KEY (disk_id, lba)
);
CREATE INDEX IF NOT EXISTS idx_mappings_pblock ON mappings(pblock_id);

-- At most one snapshot can exist; its id is pinned to 1.
CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    reserved_blocks INTEGER NOT NULL CHECK (reserved_blocks >= 0),
    used_blocks     INTEGER NOT NULL DEFAULT 0
                    CHECK (used_blocks >= 0 AND used_blocks <= reserved_blocks),
    created_at      REAL NOT NULL
);

-- Logical blocks of the snapshot, pointing at the same physical blocks the
-- system disk used at snapshot time.
CREATE TABLE IF NOT EXISTS snap_mappings (
    lba       INTEGER PRIMARY KEY CHECK (lba >= 0),
    pblock_id INTEGER NOT NULL REFERENCES pblocks(id)
);
CREATE INDEX IF NOT EXISTS idx_snap_mappings_pblock ON snap_mappings(pblock_id);
"""


class Store:
    def __init__(self, path: str, total_blocks: Optional[int] = None,
                 block_size: int = BLOCK_SIZE):
        self.path = path
        self.block_size = block_size
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init(total_blocks)

    # ------------------------------------------------------------- plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One serialised write transaction; rollback on any error."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def _init(self, total_blocks: Optional[int]) -> None:
        conn = self._connect()
        try:
            # With several uvicorn workers starting at once this pragma can hit
            # a brief lock; retry rather than fail startup.
            for attempt in range(20):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT total_blocks, block_size FROM pool WHERE id=1"
                ).fetchone()
                if row is None:
                    if total_blocks is None:
                        raise StoreError(
                            500, "capacity_missing",
                            "数据库为空且未提供初始块池容量",
                        )
                    if total_blocks < 0:
                        raise StoreError(500, "bad_capacity", "块池容量不能为负数")
                    conn.execute(
                        "INSERT INTO pool(id, total_blocks, block_size) VALUES(1, ?, ?)",
                        (total_blocks, self.block_size),
                    )
                else:
                    self.block_size = row["block_size"]
                    self._gc(conn)
                    reserved = self._reserved_remaining(conn)
                    live = self._live_blocks(conn)
                    floor = live + reserved
                    if total_blocks is not None and total_blocks != row["total_blocks"]:
                        if total_blocks < floor:
                            raise StoreError(
                                500, "capacity_below_usage",
                                f"配置容量 {total_blocks} 块小于已用 {live} + "
                                f"预留 {reserved} 块",
                            )
                        conn.execute(
                            "UPDATE pool SET total_blocks=? WHERE id=1",
                            (total_blocks,),
                        )
                    elif total_blocks is None and row["total_blocks"] < floor:
                        raise StoreError(
                            500, "capacity_below_usage",
                            "持久化容量小于已用 + 预留块数，数据可能已损坏",
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # ------------------------------------------------------------- helpers

    def _pool_total(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT total_blocks FROM pool WHERE id=1").fetchone()
        return int(row["total_blocks"])

    def _live_blocks(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM pblocks").fetchone()[0])

    def _snapshot(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT id, reserved_blocks, used_blocks, created_at "
            "FROM snapshot WHERE id=1"
        ).fetchone()

    def _reserved_remaining(self, conn: sqlite3.Connection) -> int:
        snap = self._snapshot(conn)
        if snap is None:
            return 0
        return int(snap["reserved_blocks"] - snap["used_blocks"])

    def _public_free(self, conn: sqlite3.Connection) -> int:
        return (self._pool_total(conn) - self._live_blocks(conn)
                - self._reserved_remaining(conn))

    @staticmethod
    def _is_shared_with_snapshot(conn: sqlite3.Connection, pblock_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM snap_mappings WHERE pblock_id=? LIMIT 1", (pblock_id,)
        ).fetchone() is not None

    @classmethod
    def _is_shared_with_other_disk(cls, conn: sqlite3.Connection, disk: str,
                                   pblock_id: int) -> bool:
        """True when the block carrying this disk's data is also referenced by
        the snapshot (or by the other disk)."""
        if disk != SNAPSHOT_ID and cls._is_shared_with_snapshot(conn, pblock_id):
            return True
        return conn.execute(
            "SELECT 1 FROM mappings WHERE pblock_id=? AND disk_id<>? LIMIT 1",
            (pblock_id, disk),
        ).fetchone() is not None

    def _gc(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "DELETE FROM pblocks WHERE id NOT IN (SELECT pblock_id FROM mappings) "
            "AND id NOT IN (SELECT pblock_id FROM snap_mappings)"
        )

    @staticmethod
    def _norm_disk(disk: str) -> str:
        if disk not in DISK_IDS and disk != SNAPSHOT_ID:
            raise StoreError(404, "unknown_disk", f"未知磁盘 {disk}")
        return disk

    def _check_lba(self, lba: int) -> int:
        if not isinstance(lba, int) or isinstance(lba, bool) or lba < 0:
            raise StoreError(400, "bad_lba", "块号必须是非负整数")
        return lba

    # -------------------------------------------------------------- status

    def status(self) -> dict:
        with self._read() as conn:
            total = self._pool_total(conn)
            live = self._live_blocks(conn)
            snap = self._snapshot(conn)
            reserved_remaining = self._reserved_remaining(conn)
            snap_info = None
            if snap is not None:
                snap_info = {
                    "reserved_blocks": int(snap["reserved_blocks"]),
                    "reservation_used_blocks": int(snap["used_blocks"]),
                    "reservation_remaining_blocks": reserved_remaining,
                    "created_at": snap["created_at"],
                    "read_only": True,
                }
            return {
                "block_size": int(
                    conn.execute("SELECT block_size FROM pool WHERE id=1")
                    .fetchone()[0]
                ),
                "total_blocks": total,
                "live_blocks": live,
                "reserved_blocks": reserved_remaining,
                "public_free_blocks": total - live - reserved_remaining,
                "snapshot": snap_info,
                "disks": {
                    "sys": self._disk_summary(conn, "sys"),
                    "data": self._disk_summary(conn, "data"),
                },
            }

    def _disk_summary(self, conn: sqlite3.Connection, disk: str) -> dict:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(lba), -1) AS max_lba "
            "FROM mappings WHERE disk_id=?",
            (disk,),
        ).fetchone()
        return {
            "id": disk,
            "name": DISK_NAMES[disk],
            "allocated_blocks": int(row["n"]),
            "size_blocks": int(row["max_lba"]) + 1,
        }

    # --------------------------------------------------------------- reads

    def read_block(self, disk: str, lba: int) -> dict:
        self._norm_disk(disk)
        self._check_lba(lba)
        with self._read() as conn:
            if disk == SNAPSHOT_ID:
                if self._snapshot(conn) is None:
                    raise StoreError(404, "no_snapshot", "当前没有快照")
                row = conn.execute(
                    "SELECT p.id AS pblock_id, p.data FROM snap_mappings m "
                    "JOIN pblocks p ON p.id=m.pblock_id WHERE m.lba=?",
                    (lba,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT p.id AS pblock_id, p.data FROM mappings m "
                    "JOIN pblocks p ON p.id=m.pblock_id "
                    "WHERE m.disk_id=? AND m.lba=?",
                    (disk, lba),
                ).fetchone()
            if row is None:
                raise StoreError(
                    404, "block_not_mapped", f"{disk} 的块 {lba} 尚未分配"
                )
            data = bytes(row["data"])
            return {
                "disk": disk,
                "lba": lba,
                "pblock_id": int(row["pblock_id"]),
                "size": len(data),
                "data_b64": self._b64(data),
                "data_hex": data.hex(),
                "text": self._as_text(data),
                "shared_with_snapshot": self._is_shared_with_other_disk(
                    conn, disk, int(row["pblock_id"])
                ),
            }

    def list_blocks(self, disk: str, limit: int = 256) -> dict:
        self._norm_disk(disk)
        limit = max(1, min(int(limit), 2048))
        with self._read() as conn:
            if disk == SNAPSHOT_ID:
                if self._snapshot(conn) is None:
                    raise StoreError(404, "no_snapshot", "当前没有快照")
                rows = conn.execute(
                    "SELECT m.lba, m.pblock_id, length(p.data) AS size, "
                    "substr(p.data, 1, 64) AS preview FROM snap_mappings m "
                    "JOIN pblocks p ON p.id=m.pblock_id ORDER BY m.lba LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT m.lba, m.pblock_id, length(p.data) AS size, "
                    "substr(p.data, 1, 64) AS preview FROM mappings m "
                    "JOIN pblocks p ON p.id=m.pblock_id "
                    "WHERE m.disk_id=? ORDER BY m.lba LIMIT ?",
                    (disk, limit),
                ).fetchall()
            blocks = []
            for r in rows:
                preview = bytes(r["preview"] or b"")
                blocks.append({
                    "lba": int(r["lba"]),
                    "pblock_id": int(r["pblock_id"]),
                    "size": int(r["size"]),
                    "preview": self._as_text(preview)[:40],
                    "shared_with_snapshot": self._is_shared_with_other_disk(
                        conn, disk, int(r["pblock_id"])
                    ),
                })
            return {"disk": disk, "blocks": blocks, "count": len(blocks)}

    @staticmethod
    def _b64(data: bytes) -> str:
        import base64
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _as_text(data: bytes) -> str:
        return data.rstrip(b"\x00").decode("utf-8", "replace")

    # -------------------------------------------------------------- writes

    def write_block(self, disk: str, lba: int, data: bytes) -> dict:
        if disk not in DISK_IDS:
            if disk == SNAPSHOT_ID:
                raise StoreError(400, "read_only", "快照是只读的，不能写入")
            raise StoreError(404, "unknown_disk", f"未知磁盘 {disk}")
        self._check_lba(lba)
        if len(data) != self.block_size:
            raise StoreError(
                400, "bad_size",
                f"块数据必须恰好为 {self.block_size} 字节，实际 {len(data)} 字节",
            )
        with self._txn() as conn:
            mapping = conn.execute(
                "SELECT pblock_id FROM mappings WHERE disk_id=? AND lba=?",
                (disk, lba),
            ).fetchone()

            if mapping is None:
                # First write to an unallocated logical block: public space only.
                if self._public_free(conn) <= 0:
                    raise StoreError(
                        409, "pool_full",
                        "公共余量为 0，首次写入被拒绝（不会动用快照预留）",
                    )
                new_id = self._insert_pblock(conn, data)
                conn.execute(
                    "INSERT INTO mappings(disk_id, lba, pblock_id) VALUES(?,?,?)",
                    (disk, lba, new_id),
                )
                return self._write_result(conn, disk, lba, new_id,
                                          "first_write", "public")

            old_id = int(mapping["pblock_id"])
            if not self._is_shared_with_snapshot(conn, old_id):
                # Private block: overwrite in place, no extra space needed.
                conn.execute("UPDATE pblocks SET data=? WHERE id=?", (data, old_id))
                return self._write_result(conn, disk, lba, old_id,
                                          "overwrite_private", "none")

            # Shared with the snapshot: copy-on-write.  Reservation first.
            reserved_left = self._reserved_remaining(conn)
            if reserved_left > 0:
                charged = "reservation"
            elif self._public_free(conn) > 0:
                charged = "public"
            else:
                raise StoreError(
                    409, "pool_full",
                    "预留和公共余量均为 0，覆写共用块被拒绝",
                )
            new_id = self._insert_pblock(conn, data)
            conn.execute(
                "UPDATE mappings SET pblock_id=? WHERE disk_id=? AND lba=?",
                (new_id, disk, lba),
            )
            if charged == "reservation":
                conn.execute(
                    "UPDATE snapshot SET used_blocks = used_blocks + 1 WHERE id=1"
                )
            # The old block stays alive for the snapshot, so it is not GC'd.
            return self._write_result(conn, disk, lba, new_id,
                                      "overwrite_shared", charged)

    def _insert_pblock(self, conn: sqlite3.Connection, data: bytes) -> int:
        cur = conn.execute("INSERT INTO pblocks(data) VALUES(?)", (data,))
        return int(cur.lastrowid)

    def _write_result(self, conn: sqlite3.Connection, disk: str, lba: int,
                      pblock_id: int, action: str, charged: str) -> dict:
        return {
            "disk": disk,
            "lba": lba,
            "pblock_id": pblock_id,
            "action": action,
            "charged": charged,
            "public_free_blocks": self._public_free(conn),
            "reservation_remaining_blocks": self._reserved_remaining(conn),
            "live_blocks": self._live_blocks(conn),
        }

    # ------------------------------------------------------------ snapshots

    def create_snapshot(self, reserved_blocks: int) -> dict:
        if not isinstance(reserved_blocks, int) or isinstance(reserved_blocks, bool) \
                or reserved_blocks < 0:
            raise StoreError(400, "bad_reservation", "预留块数必须是非负整数")
        with self._txn() as conn:
            if self._snapshot(conn) is not None:
                raise StoreError(409, "snapshot_exists", "已存在快照，请先删除")
            public = self._public_free(conn)
            if reserved_blocks > public:
                raise StoreError(
                    409, "insufficient_space",
                    f"公共余量仅 {public} 块，不足以预留 {reserved_blocks} 块",
                )
            conn.execute(
                "INSERT INTO snapshot(id, reserved_blocks, used_blocks, created_at) "
                "VALUES(1, ?, 0, ?)",
                (reserved_blocks, time.time()),
            )
            # Share the system disk's current blocks read-only.
            conn.execute(
                "INSERT INTO snap_mappings(lba, pblock_id) "
                "SELECT lba, pblock_id FROM mappings WHERE disk_id='sys'"
            )
            count = conn.execute("SELECT COUNT(*) FROM snap_mappings").fetchone()[0]
            return {
                "snapshot": {
                    "reserved_blocks": reserved_blocks,
                    "reservation_used_blocks": 0,
                    "reservation_remaining_blocks": reserved_blocks,
                    "read_only": True,
                },
                "shared_blocks": int(count),
                "public_free_blocks": self._public_free(conn),
            }

    def delete_snapshot(self) -> dict:
        with self._txn() as conn:
            snap = self._snapshot(conn)
            if snap is None:
                raise StoreError(404, "no_snapshot", "当前没有快照")
            released_reservation = int(
                snap["reserved_blocks"] - snap["used_blocks"]
            )
            # Blocks referenced by the snapshot but by no disk afterwards.
            freed = int(conn.execute(
                "SELECT COUNT(*) FROM pblocks p WHERE "
                "EXISTS(SELECT 1 FROM snap_mappings m WHERE m.pblock_id=p.id) "
                "AND NOT EXISTS(SELECT 1 FROM mappings d WHERE d.pblock_id=p.id)"
            ).fetchone()[0])
            conn.execute("DELETE FROM snap_mappings")
            conn.execute("DELETE FROM snapshot WHERE id=1")
            self._gc(conn)
            return {
                "freed_blocks": freed,
                "released_reservation_blocks": released_reservation,
                "public_free_blocks": self._public_free(conn),
                "live_blocks": self._live_blocks(conn),
            }

    # ----------------------------------------------------------- diagnostics

    def check_consistency(self) -> dict:
        """Used by tests; verifies the accounting invariant from the DB."""
        with self._read() as conn:
            total = self._pool_total(conn)
            live = self._live_blocks(conn)
            reserved = self._reserved_remaining(conn)
            public = total - live - reserved
            orphan = int(conn.execute(
                "SELECT COUNT(*) FROM pblocks p WHERE "
                "NOT EXISTS(SELECT 1 FROM mappings m WHERE m.pblock_id=p.id) AND "
                "NOT EXISTS(SELECT 1 FROM snap_mappings s WHERE s.pblock_id=p.id)"
            ).fetchone()[0])
            dangling = int(conn.execute(
                "SELECT COUNT(*) FROM mappings m LEFT JOIN pblocks p "
                "ON p.id=m.pblock_id WHERE p.id IS NULL"
            ).fetchone()[0]) + int(conn.execute(
                "SELECT COUNT(*) FROM snap_mappings m LEFT JOIN pblocks p "
                "ON p.id=m.pblock_id WHERE p.id IS NULL"
            ).fetchone()[0])
            unused_snapshot = self._snapshot(conn)
            return {
                "total_blocks": total,
                "live_blocks": live,
                "reserved_remaining": reserved,
                "public_free": public,
                "orphan_blocks": orphan,
                "dangling_refs": dangling,
                "snapshot_present": unused_snapshot is not None,
                "ok": orphan == 0 and dangling == 0 and public >= 0,
            }
