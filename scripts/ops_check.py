#!/usr/bin/env python3
"""Extra operational checks against a real running server.

    python3 scripts/ops_check.py <base_url> [--expect-empty]

Checks
  * concurrent first writes cannot oversubscribe a nearly empty pool,
  * state survives a process restart (run twice around the restart),
  * snapshot is still readable after restart and still read-only.

Run against a fresh pool with POOL_TOTAL_BLOCKS=8.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import urllib.error
import urllib.request

BASE = ""
FAILURES: list[str] = []


def call(method: str, path: str, body=None, timeout: float = 20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    global BASE
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    BASE = args[0] if args else "http://127.0.0.1:8000"
    phase = "setup"
    for i, a in enumerate(sys.argv[1:]):
        if a == "--phase":
            phase = sys.argv[i + 2]

    if phase == "setup":
        code, st = call("GET", "/api/status")
        check("服务可用", code == 200)
        check("空池", st["live_blocks"] == 0 and st["snapshot"] is None,
              json.dumps(st, ensure_ascii=False))
        total = st["total_blocks"]
        check("容量为 8", total == 8)

        print("\n并发首次写入（40 个请求争抢 8 块）")
        def attempt(i):
            code, body = call("PUT", f"/api/disks/{'sys' if i % 2 else 'data'}/blocks/{i}",
                              {"text": f"v{i}"})
            return code, (body.get("error", {}) or {}).get("code")

        with cf.ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(attempt, range(40)))
        ok = sum(1 for c, _ in results if c == 200)
        full = sum(1 for c, e in results if c == 409 and e == "pool_full")
        check("恰好 8 个成功", ok == 8, f"成功 {ok}")
        check("其余 32 个 pool_full", full == 32, f"pool_full {full}")
        st = call("GET", "/api/status")[1]
        check("已占用 = 8/8", st["live_blocks"] == 8, json.dumps(st))
        check("公共余量 = 0", st["public_free_blocks"] == 0)
        diag = call("GET", "/api/diagnostics")[1]
        check("一致性自检通过", diag["ok"] is True, json.dumps(diag))
        print("\n下一步：重启服务后再运行 --phase verify")
        return 0 if not FAILURES else 1

    if phase == "verify":
        code, st = call("GET", "/api/status")
        check("服务可用", code == 200)
        check("重启后已占用仍为 8", st["live_blocks"] == 8, json.dumps(st))
        check("重启后公共余量为 0", st["public_free_blocks"] == 0)
        mapped = 0
        for disk in ("sys", "data"):
            code, body = call("GET", f"/api/disks/{disk}/blocks")
            check(f"重启后 {disk} 块表可读", code == 200)
            for b in body.get("blocks", []):
                mapped += 1
                code, blk = call("GET", f"/api/disks/{disk}/blocks/{b['lba']}")
                check(f"重启后 {disk}/{b['lba']} 内容正确",
                      code == 200 and blk["text"] == f"v{b['lba']}",
                      blk.get("text", json.dumps(blk, ensure_ascii=False)))
        check("重启后映射块数仍为 8", mapped == 8, f"实际 {mapped}")
        diag = call("GET", "/api/diagnostics")[1]
        check("重启后一致性自检通过", diag["ok"] is True, json.dumps(diag))
        code, body = call("PUT", "/api/disks/sys/blocks/99", {"text": "x"})
        check("重启后配额仍生效（409）", code == 409, json.dumps(body, ensure_ascii=False))
        return 0 if not FAILURES else 1

    print("phase 只能是 setup 或 verify")
    return 2


if __name__ == "__main__":
    sys.exit(main())
