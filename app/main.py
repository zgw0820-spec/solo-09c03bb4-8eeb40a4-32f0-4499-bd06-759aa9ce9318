"""FastAPI application for the shared block-pool demo."""

from __future__ import annotations

import base64
import binascii
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .store import BLOCK_SIZE, Store, StoreError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def build_store() -> Store:
    path = os.environ.get("POOL_DB_PATH", "/data/pool.db")
    raw = os.environ.get("POOL_TOTAL_BLOCKS", "64")
    try:
        total = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"POOL_TOTAL_BLOCKS 必须是整数，当前为 {raw!r}") from exc
    return Store(path, total_blocks=total, block_size=BLOCK_SIZE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = build_store()
    yield


app = FastAPI(title="虚拟机块池快照管理", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(StoreError)
async def store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Keep the error shape uniform: {"error": {"code", "message"}}."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_request", "message": "; ".join(parts)}},
    )


class WriteBody(BaseModel):
    text: Optional[str] = None
    data_hex: Optional[str] = None
    data_b64: Optional[str] = None
    fill: Optional[int] = None


class SnapshotBody(BaseModel):
    reserved_blocks: int


class PackBody(BaseModel):
    start_lba: int
    count: int


def _decode_block(body: WriteBody) -> bytes:
    provided = [
        name for name in ("text", "data_hex", "data_b64", "fill")
        if getattr(body, name) is not None
    ]
    if len(provided) != 1:
        raise StoreError(
            400, "bad_body",
            "text / data_hex / data_b64 / fill 必须且只能提供一个",
        )
    kind = provided[0]
    value = getattr(body, kind)
    if kind == "text":
        data = value.encode("utf-8")
        if len(data) > BLOCK_SIZE:
            raise StoreError(400, "bad_size", f"文本超过 {BLOCK_SIZE} 字节")
        return data.ljust(BLOCK_SIZE, b"\x00")
    if kind == "data_hex":
        try:
            data = bytes.fromhex(value)
        except ValueError as exc:
            raise StoreError(400, "bad_hex", f"十六进制数据非法：{exc}") from exc
        return _exact(data)
    if kind == "data_b64":
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StoreError(400, "bad_b64", f"Base64 数据非法：{exc}") from exc
        return _exact(data)
    if not 0 <= value <= 255:
        raise StoreError(400, "bad_fill", "fill 必须在 0..255 之间")
    return bytes([value]) * BLOCK_SIZE


def _exact(data: bytes) -> bytes:
    if len(data) != BLOCK_SIZE:
        raise StoreError(
            400, "bad_size",
            f"块数据必须恰好为 {BLOCK_SIZE} 字节，实际 {len(data)} 字节",
        )
    return data


def _store(request: Request) -> Store:
    return request.app.state.store


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
def get_status(request: Request) -> dict:
    return _store(request).status()


@app.get("/api/disks/{disk}/blocks")
def get_blocks(request: Request, disk: str, limit: int = 256) -> dict:
    return _store(request).list_blocks(disk, limit)


@app.get("/api/disks/{disk}/blocks/{lba}")
def get_block(request: Request, disk: str, lba: int) -> dict:
    return _store(request).read_block(disk, lba)


@app.put("/api/disks/{disk}/blocks/{lba}")
def put_block(request: Request, disk: str, lba: int, body: WriteBody) -> dict:
    return _store(request).write_block(disk, lba, _decode_block(body))


@app.post("/api/snapshot")
def create_snapshot(request: Request, body: SnapshotBody) -> dict:
    return _store(request).create_snapshot(body.reserved_blocks)


@app.post("/api/disks/{disk}/pack")
def pack_blocks(request: Request, disk: str, body: PackBody) -> dict:
    return _store(request).pack_blocks(disk, body.start_lba, body.count)


@app.delete("/api/snapshot")
def delete_snapshot(request: Request) -> dict:
    return _store(request).delete_snapshot()


@app.get("/api/diagnostics")
def diagnostics(request: Request) -> dict:
    return _store(request).check_consistency()
