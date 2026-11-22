# 虚拟机块池 · 只读快照与空间预留

值班场景：升级前给系统盘留一份只读快照，同时保证另一块虚拟盘不会吃光升级所需的
空间。两块虚拟盘（`sys` 系统盘、`data` 数据盘）共享一个固定容量的块池，每块 4 KiB，
按块号读写。系统盘可创建一份快照，快照与原盘共享物理块，并从公共余量中预留指定
块数；空间不足时直接拒绝。

FastAPI + SQLite（WAL），前端为原生 HTML/JS，Docker Compose 一键启动。

## 启动

```bash
docker compose up -d --build
curl -s localhost:8000/api/status | python3 -m json.tool
```

浏览器打开 <http://localhost:8000/>。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POOL_TOTAL_BLOCKS` | `64` | 块池容量（块数 × 4 KiB） |
| `POOL_PORT` | `8000` | 宿主机端口（compose 插值） |

`/data/pool.db` 由命名卷 `pool-data` 持久化。`docker compose down` 保留数据，
`docker compose down -v` 才会清空。修改容量后重启即可；若新容量小于“已占用 +
预留余量”，服务拒绝启动（返回 `capacity_below_usage`），不会静默丢配额。

## 空间规则

```
公共余量 = 池总块数 − 已占用物理块 − 快照预留剩余
```

| 操作 | 计费 |
| --- | --- |
| 首次写入未分配的块 | 公共余量 1 块；公共余量为 0 则拒绝（绝不动用预留） |
| 覆写仅本盘引用的私有块 | 0（原地写） |
| 覆写与快照共用的块 | 写时复制：先扣预留，预留为 0 再用公共余量；两者皆 0 则拒绝 |
| 创建快照 | 共享系统盘当前所有块；要求公共余量 ≥ 预留块数，否则 409 |
| 删除快照 | 释放“仅快照引用”的物理块 + 未用完的预留 |

快照只读：对快照的写请求一律 `400 read_only`。快照读取的是创建时刻的块内容，
系统盘后续覆写不会影响快照。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 总容量、已占用、公共余量、预留余量、快照详情 |
| GET | `/api/disks/{sys\|data\|snapshot}/blocks?limit=256` | 块表（含共享标记） |
| GET | `/api/disks/{disk}/blocks/{lba}` | 读块：返回 `text`、`data_hex`、`data_b64` |
| PUT | `/api/disks/{sys\|data}/blocks/{lba}` | 写块，body 用 `text` / `data_hex` / `data_b64` / `fill` 之一 |
| POST | `/api/snapshot` | `{"reserved_blocks": N}` |
| DELETE | `/api/snapshot` | 删除并回收 |
| GET | `/api/diagnostics` | 引用完整性与配额自检 |

错误统一为 `{"error": {"code", "message"}}`。关键码：`pool_full`、
`insufficient_space`、`snapshot_exists`、`no_snapshot`、`read_only`、`bad_size`。

## 一致性与并发

- 所有写操作（写块、建/删快照）在单个 SQLite `BEGIN IMMEDIATE` 事务内完成：
  - `BEGIN IMMEDIATE` 在事务开始就拿到写锁，多个进程/线程的写请求被串行化，
    不存在“两个请求同时看到同一份空闲块”的窗口；
  - 任一步骤抛错即 `ROLLBACK`，失败操作不会留下半写状态；
  - 空间检查与分配在同一事务内，因此并发下也不会超卖。
- WAL + `synchronous=FULL` + `busy_timeout=30s`：已确认（COMMIT 返回）的数据
  在进程崩溃/强杀后仍可恢复，重启后盘面、映射、预留配额原样恢复。
- 物理块只要还被任一盘或快照引用就保留；失去全部引用的块在同一事务内回收。

## 本地测试与演练

```bash
# 单元 / 接口 / 并发 / 重启测试（34 个用例）
pip install fastapi uvicorn httpx pytest      # 或用镜像内固定版本
python3 -m pytest tests -q

# 端到端演练（需要空池，建议 POOL_TOTAL_BLOCKS=8）
POOL_TOTAL_BLOCKS=8 POOL_DB_PATH=/tmp/pool.db \
  uvicorn app.main:app --port 8000 &
python3 scripts/drill.py http://127.0.0.1:8000 --phase A   # 池满覆写 + 删快照
# 换一个空库再跑：
python3 scripts/drill.py http://127.0.0.1:8000 --phase B   # 删除时归还剩余预留

# 并发不超卖 + 重启持久化
python3 scripts/ops_check.py http://127.0.0.1:8000 --phase setup
# 重启服务（同一 DB）后：
python3 scripts/ops_check.py http://127.0.0.1:8000 --phase verify
```

## 目录

```
app/main.py          FastAPI 路由与请求解码
app/store.py         SQLite 块池、映射、快照、预留与事务
app/static/          网页（总览、快照、读写、块表、自检）
tests/test_pool.py   34 个用例：规则、失败回滚、并发、重启、API
scripts/drill.py     两阶段端到端演练
scripts/ops_check.py 并发超卖 + 重启持久化检查
Dockerfile / docker-compose.yml
```

## 已知边界 / 待办

- 池容量不可在运行中在线扩容；需改 `POOL_TOTAL_BLOCKS` 重启，且不能低于
  “已占用 + 预留”。
- 仅支持一份快照（按需求）；多快照需要把 `snapshot` 单行表改为多行并维护
  引用计数链。
- 块表接口默认最多返回 2048 条，尚无分页游标。
- 无鉴权、TLS、审计日志；公网部署前需加反向代理与访问控制。
- 未做块内容校验和；如需检测静默损坏可增加每块 CRC。
