#!/usr/bin/env python3
"""End-to-end drill against a running service (default http://127.0.0.1:8000).

Usage:
    python3 scripts/drill.py <base_url> [--phase A|B|C]

Both phases require an empty pool (fresh `docker compose down -v` /
new DB file).  Use POOL_TOTAL_BLOCKS=8.

Phase A — pool-full overwrite + snapshot deletion after full reservation use:
    1. fill 4 system blocks, snapshot with 2 reserved blocks,
    2. overwrite two shared blocks: both charged to the reservation,
    3. fill remaining public blocks with the data disk -> public=0, reserve=0,
    4. overwrite of a still-shared block -> 409, state unchanged,
       first write to an unallocated block -> 409, state unchanged,
    5. delete snapshot -> releases only snapshot-only blocks + unused reservation,
    6. the previously refused overwrite now succeeds (free, in place).

Phase B — deleting a snapshot that still has unused reservation:
    1. fill 4 system blocks, snapshot with 3 reserved blocks -> public = 1,
    2. data disk takes the last public block -> public = 0,
    3. a first write is refused (public = 0) and does not touch the reservation,
    4. two shared overwrites are charged to the reservation -> 1 reserved left,
    5. deletion frees the 2 snapshot-only copies and returns the 1 unused
       reserved block; public free becomes 3 (4 sys + 1 data live).

Phase C — packing with zero public free + both packed-overwrite outcomes:
    1. fill the whole pool with compressible data blocks -> public = 0,
    2. first write is refused (public = 0),
    3. pack the data-disk range -> pairs share slots, freed slots immediately
       return to public free; every LBA still reads back its full 4 KiB,
    4. overwrite a packed block with compressible data -> stays in its slot,
    5. overwrite a packed block with incompressible data -> split out using a
       public slot, partner keeps the original slot and its content,
    6. refill public free to 0, incompressible overwrite of a packed block
       -> 409, both blocks and the free space unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

FAILURES: list[str] = []


def call(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def status(base: str) -> dict:
    code, body = call(base, "GET", "/api/status")
    assert code == 200, body
    return body


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def show(label: str, st: dict) -> None:
    snap = st["snapshot"]
    reserved = "无" if snap is None else (
        f"总{snap['reserved_blocks']} 已用{snap['reservation_used_blocks']}"
    )
    print(f"  · {label}: 公共余量={st['public_free_blocks']} "
          f"预留余量={st['reserved_blocks']} "
          f"已占用={st['live_blocks']}/{st['total_blocks']} 快照={reserved}")


def require_empty(base: str) -> dict:
    st = status(base)
    print(f"池容量 {st['total_blocks']} 块 × {st['block_size']} 字节 @ {base}")
    if st["live_blocks"] or st["snapshot"]:
        print("!! 该实例已有数据，请用空库启动（新 DB 文件或 docker compose down -v）")
        sys.exit(2)
    if st["total_blocks"] < 8:
        print("!! 需要 POOL_TOTAL_BLOCKS >= 8")
        sys.exit(2)
    return st


def phase_a(base: str) -> None:
    total = require_empty(base)["total_blocks"]

    print("\n[A1] 写入 4 个系统盘块并创建预留 2 块的快照")
    for i in range(4):
        code, body = call(base, "PUT", f"/api/disks/sys/blocks/{i}",
                          {"text": f"sys-v{i}"})
        assert code == 200, body
    code, body = call(base, "POST", "/api/snapshot", {"reserved_blocks": 2})
    check("快照创建成功", code == 200, json.dumps(body, ensure_ascii=False))
    st = status(base)
    show("快照后", st)
    check("公共余量 = 总数-4-2", st["public_free_blocks"] == total - 6)
    check("预留余量 = 2", st["reserved_blocks"] == 2)

    print("\n[A2] 覆写两个与快照共用的块，应先消耗预留")
    for i in range(2):
        code, body = call(base, "PUT", f"/api/disks/sys/blocks/{i}",
                          {"text": f"sys-new{i}"})
        check(f"覆写 sys/{i} 成功", code == 200, body.get("charged", ""))
        check(f"sys/{i} 计费=reservation", body.get("charged") == "reservation")
    st = status(base)
    show("两次 COW 后", st)
    check("预留余量耗尽为 0", st["reserved_blocks"] == 0)
    code, old = call(base, "GET", "/api/disks/snapshot/blocks/0")
    check("快照仍读到旧值 sys-v0", code == 200 and old["text"] == "sys-v0",
          old.get("text", ""))

    print("\n[A3] 用数据盘填满公共余量")
    remaining = status(base)["public_free_blocks"]
    for i in range(remaining):
        code, body = call(base, "PUT", f"/api/disks/data/blocks/{i}",
                          {"text": f"data{i}"})
        assert code == 200, body
    st = status(base)
    show("池满后", st)
    check("公共余量为 0", st["public_free_blocks"] == 0)

    print("\n[A4] 池满后的写入必须被拒绝且不改状态")
    before = status(base)
    before_sys = call(base, "GET", "/api/disks/sys/blocks")[1]
    code, body = call(base, "PUT", "/api/disks/sys/blocks/2",
                      {"text": "should-fail"})
    check("覆写共用块返回 409", code == 409, json.dumps(body, ensure_ascii=False))
    check("错误码 pool_full", body.get("error", {}).get("code") == "pool_full")
    code, body = call(base, "PUT", f"/api/disks/data/blocks/{remaining}",
                      {"text": "nope"})
    check("首次写入未分配块返回 409", code == 409,
          json.dumps(body, ensure_ascii=False))
    after = status(base)
    check("配额状态完全未变", after == before)
    check("块表完全未变", call(base, "GET", "/api/disks/sys/blocks")[1] == before_sys)
    check("被拒块仍是旧值",
          call(base, "GET", "/api/disks/sys/blocks/2")[1]["text"] == "sys-v2")

    print("\n[A5] 删除快照，仅回收快照独占块和未用预留")
    code, body = call(base, "DELETE", "/api/snapshot")
    check("删除快照成功", code == 200, json.dumps(body, ensure_ascii=False))
    # Snapshot-only blocks: the two originals replaced by COW (old sys0, sys1).
    check("释放快照独占块 = 2", body.get("freed_blocks") == 2,
          f"实际 {body.get('freed_blocks')}")
    check("退还未用预留 = 0", body.get("released_reservation_blocks") == 0,
          f"实际 {body.get('released_reservation_blocks')}")
    st = status(base)
    show("删快照后", st)
    check("快照已不存在", st["snapshot"] is None)
    check("公共余量恢复到 2", st["public_free_blocks"] == 2)
    check("系统盘未被回滚",
          call(base, "GET", "/api/disks/sys/blocks/2")[1]["text"] == "sys-v2")

    print("\n[A6] 空间回收后，之前被拒的覆写应当成功")
    code, body = call(base, "PUT", "/api/disks/sys/blocks/2", {"text": "now-ok"})
    check("覆写 sys/2 成功", code == 200, json.dumps(body, ensure_ascii=False))
    # Snapshot is gone, so this block is private now: in-place, no charge.
    check("计费=none（已是私有块，无需新块）", body.get("charged") == "none")
    check("读取到新值",
          call(base, "GET", "/api/disks/sys/blocks/2")[1]["text"] == "now-ok")
    # And the freed public space really is usable for a first write.
    code, body = call(base, "PUT", f"/api/disks/data/blocks/{remaining}",
                      {"text": "after-free"})
    check("首次写入先前被拒的块成功", code == 200,
          json.dumps(body, ensure_ascii=False))
    check("该块计费=public", body.get("charged") == "public")

    print("\n[A7] 一致性自检")
    diag = call(base, "GET", "/api/diagnostics")[1]
    check("无孤儿块/悬空引用且余量非负", diag.get("ok") is True,
          json.dumps(diag, ensure_ascii=False))


def phase_b(base: str) -> None:
    require_empty(base)

    print("\n[B1] 填 4 个系统盘块，快照预留 3 块（公共余量剩 1）")
    for i in range(4):
        assert call(base, "PUT", f"/api/disks/sys/blocks/{i}",
                    {"text": f"sys-v{i}"})[0] == 200
    code, body = call(base, "POST", "/api/snapshot", {"reserved_blocks": 3})
    check("快照创建成功", code == 200, json.dumps(body, ensure_ascii=False))
    st = status(base)
    show("快照后", st)
    check("公共余量 = 1", st["public_free_blocks"] == 1)
    check("预留余量 = 3", st["reserved_blocks"] == 3)

    print("\n[B2] 数据盘占用最后 1 块公共空间")
    code, body = call(base, "PUT", "/api/disks/data/blocks/0", {"text": "d0"})
    check("首次写入成功", code == 200)
    st = status(base)
    show("池满后", st)
    check("公共余量 = 0", st["public_free_blocks"] == 0)
    check("预留余量 = 3", st["reserved_blocks"] == 3)

    print("\n[B3] 公共余量为 0 时首次写入被拒，且不动用预留")
    before = status(base)
    code, body = call(base, "PUT", "/api/disks/data/blocks/1", {"text": "d1"})
    check("首次写入 409", code == 409, json.dumps(body, ensure_ascii=False))
    check("预留余量仍为 3（未被首次写入挪用）",
          status(base)["reserved_blocks"] == 3)
    check("状态未变", status(base) == before)

    print("\n[B4] 覆写共用块消耗 2 块预留，剩 1")
    code, body = call(base, "PUT", "/api/disks/sys/blocks/0", {"text": "new0"})
    check("第 1 次 COW 走预留", body.get("charged") == "reservation")
    code, body = call(base, "PUT", "/api/disks/sys/blocks/1", {"text": "new1"})
    check("第 2 次 COW 走预留", body.get("charged") == "reservation")
    st = status(base)
    show("两次 COW 后", st)
    check("预留余量 = 1", st["reserved_blocks"] == 1)
    check("已占用 = 7/8", st["live_blocks"] == 7)

    print("\n[B5] 删除快照：释放 2 个快照独占块 + 1 块未用预留")
    code, body = call(base, "DELETE", "/api/snapshot")
    check("删除快照成功", code == 200, json.dumps(body, ensure_ascii=False))
    check("释放快照独占块 = 2", body.get("freed_blocks") == 2,
          f"实际 {body.get('freed_blocks')}")
    check("退还未用预留 = 1", body.get("released_reservation_blocks") == 1,
          f"实际 {body.get('released_reservation_blocks')}")
    st = status(base)
    show("删快照后", st)
    # live = 4 sys + 1 data, total 8 -> public 3
    check("公共余量 = 3", st["public_free_blocks"] == 3)
    check("已占用 = 5", st["live_blocks"] == 5)
    check("两次覆写内容都保留",
          all(call(base, "GET", f"/api/disks/sys/blocks/{i}")[1]["text"] == f"new{i}"
              for i in (0, 1)))
    check("未覆写的 sys/2 仍是快照前旧值",
          call(base, "GET", "/api/disks/sys/blocks/2")[1]["text"] == "sys-v2")
    diag = call(base, "GET", "/api/diagnostics")[1]
    check("一致性自检通过", diag.get("ok") is True,
          json.dumps(diag, ensure_ascii=False))


def phase_c(base: str) -> None:
    total = require_empty(base)["total_blocks"]

    print("\n[C1] 用可压缩块填满数据盘（公共余量归零）")
    for i in range(total):
        code, body = call(base, "PUT", f"/api/disks/data/blocks/{i}",
                          {"text": f"pack-{i}"})
        assert code == 200, body
    st = status(base)
    show("填满后", st)
    check("公共余量 = 0", st["public_free_blocks"] == 0)

    print("\n[C2] 公共余量为 0 时首次写入被拒")
    code, body = call(base, "PUT", f"/api/disks/data/blocks/{total}",
                      {"text": "nope"})
    check("首次写入 409 pool_full",
          code == 409 and body.get("error", {}).get("code") == "pool_full",
          json.dumps(body, ensure_ascii=False))

    print("\n[C3] 公共余量为 0 时合装数据盘整段连续块号")
    code, body = call(base, "POST", "/api/disks/data/pack",
                      {"start_lba": 0, "count": total})
    check("合装成功", code == 200, json.dumps(body, ensure_ascii=False))
    expect = total // 2
    check(f"合装 {expect} 对", body.get("packed_pairs") == expect,
          f"实际 {body.get('packed_pairs')}")
    check(f"实际释放 {expect} 槽", body.get("freed_slots") == expect,
          f"实际 {body.get('freed_slots')}")
    st = status(base)
    show("合装后", st)
    check("释放的槽立即归公共余量", st["public_free_blocks"] == expect)
    check("总览显示合装对数", st["packed_pairs"] == expect)

    print("\n[C4] 合装后每个块号仍读出完整 4 KiB 原文")
    ok_all = True
    for i in range(total):
        code, b = call(base, "GET", f"/api/disks/data/blocks/{i}")
        ok_all = (ok_all and code == 200 and b["text"] == f"pack-{i}"
                  and b["size"] == 4096 and b["packed"] is True)
    check("全部块号读回原文且标记合装", ok_all)

    print("\n[C5] 改写合装块：压缩后仍 ≤ 2 KiB → 留在原槽")
    slot0 = call(base, "GET", "/api/disks/data/blocks/0")[1]["pblock_id"]
    free_before = status(base)["public_free_blocks"]
    code, body = call(base, "PUT", "/api/disks/data/blocks/0",
                      {"text": "pack-0-v2"})
    check("改写成功", code == 200, json.dumps(body, ensure_ascii=False))
    check("动作 = overwrite_packed", body.get("action") == "overwrite_packed")
    check("留在原槽", body.get("pblock_id") == slot0)
    check("公共余量不变", status(base)["public_free_blocks"] == free_before)
    check("伙伴块内容照旧",
          call(base, "GET", "/api/disks/data/blocks/1")[1]["text"] == "pack-1")

    print("\n[C6] 改写合装块：压不回 2 KiB → 只用公共空槽拆开")
    raw = os.urandom(4096).hex()
    slot2 = call(base, "GET", "/api/disks/data/blocks/2")[1]["pblock_id"]
    code, body = call(base, "PUT", "/api/disks/data/blocks/2",
                      {"data_hex": raw})
    check("改写成功", code == 200, json.dumps(body, ensure_ascii=False))
    check("动作 = overwrite_packed_split",
          body.get("action") == "overwrite_packed_split")
    check("计费 = public", body.get("charged") == "public")
    check("换到公共新槽", body.get("pblock_id") != slot2)
    check("公共余量减 1",
          status(base)["public_free_blocks"] == free_before - 1)
    check("改写块读回新数据",
          call(base, "GET", "/api/disks/data/blocks/2")[1]["data_hex"] == raw)
    b3 = call(base, "GET", "/api/disks/data/blocks/3")[1]
    check("伙伴块留在原槽、内容照旧、恢复普通块",
          b3["pblock_id"] == slot2 and b3["text"] == "pack-3"
          and b3["packed"] is False)

    print("\n[C7] 重新占满公共余量后，拆不开的改写被拒且状态不变")
    for i in range(status(base)["public_free_blocks"]):
        code, body = call(base, "PUT", f"/api/disks/sys/blocks/{i}",
                          {"text": f"sys-{i}"})
        assert code == 200, body
    before = status(base)
    check("公共余量再次为 0", before["public_free_blocks"] == 0)
    code, body = call(base, "PUT", "/api/disks/data/blocks/4",
                      {"data_hex": os.urandom(4096).hex()})
    check("改写被拒 409 pool_full",
          code == 409 and body.get("error", {}).get("code") == "pool_full",
          json.dumps(body, ensure_ascii=False))
    check("余量与状态完全未变", status(base) == before)
    check("两块内容都保留",
          call(base, "GET", "/api/disks/data/blocks/4")[1]["text"] == "pack-4"
          and call(base, "GET", "/api/disks/data/blocks/5")[1]["text"] == "pack-5")
    check("合装状态保留",
          call(base, "GET", "/api/disks/data/blocks/4")[1]["packed"] is True)

    print("\n[C8] 一致性自检")
    diag = call(base, "GET", "/api/diagnostics")[1]
    check("一致性自检通过", diag.get("ok") is True,
          json.dumps(diag, ensure_ascii=False))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    phase = "A"
    for i, a in enumerate(sys.argv[1:]):
        if a == "--phase":
            phase = sys.argv[i + 2]
        elif a.startswith("--phase="):
            phase = a.split("=", 1)[1]
    base = args[0] if args else "http://127.0.0.1:8000"

    if phase.upper() == "A":
        phase_a(base)
    elif phase.upper() == "B":
        phase_b(base)
    elif phase.upper() == "C":
        phase_c(base)
    else:
        print("phase 只能是 A、B 或 C")
        return 2

    print()
    if FAILURES:
        print(f"演练失败 {len(FAILURES)} 项：{FAILURES}")
        return 1
    print(f"Phase {phase.upper()} 演练全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
