"""舞台提示执行台 —— FastAPI + SQLite 后端。

并发模型：所有写操作（确认 / 编辑 / 重排 / 归档 / 设置 / 重置）都在同一把锁内
串行完成；其中带版本号的操作（确认 / 编辑 / 重排 / 归档）在锁内完成
"检查版本号 -> 修改数据"两个步骤，因此同一版本号上并发的两个操作
只会有一个生效，另一个收到 409 及最新状态。归档的"写档案 + 清空提示"
在同一事务内提交，写档案失败时不会清空当前提示。
"""
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "cues.db"))

MIN_CUES = 2
MAX_CUES = 12

# 串行化所有写操作，保证版本检查与落库是原子的。
lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                status  TEXT NOT NULL DEFAULT 'setup'   -- setup | live | done
            );
            CREATE TABLE IF NOT EXISTS cues (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                number         TEXT NOT NULL,           -- 唯一编号
                content        TEXT NOT NULL,           -- 当前（可改写的）内容
                position       INTEGER NOT NULL,        -- 待发顺序
                status         TEXT NOT NULL DEFAULT 'pending',  -- pending | issued
                issued_content TEXT,                    -- 发出那一刻的内容快照
                issued_at      TEXT,
                issued_seq     INTEGER                  -- 发出顺序
            );
            CREATE TABLE IF NOT EXISTS archives (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,              -- 场次名称
                archived_at TEXT NOT NULL,              -- 归档时间（UTC ISO）
                status      TEXT NOT NULL,              -- 归档当时状态：live | done
                version     INTEGER NOT NULL,           -- 归档时携带的页面版本
                issued      TEXT NOT NULL,              -- 已发：编号/快照/时间（按发出顺序）
                pending     TEXT NOT NULL               -- 剩余：编号/内容（按待发顺序）
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (id, version, status) VALUES (1, 0, 'setup')"
        )


# ---------------------------------------------------------------- 状态组装

def build_state(conn: sqlite3.Connection) -> dict:
    meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
    rows = conn.execute(
        "SELECT * FROM cues ORDER BY position ASC, id ASC"
    ).fetchall()
    pending = [r for r in rows if r["status"] == "pending"]
    issued = sorted(
        (r for r in rows if r["status"] == "issued"),
        key=lambda r: r["issued_seq"],
    )

    def pending_json(r):
        return {"id": r["id"], "number": r["number"], "content": r["content"]}

    def issued_json(r):
        return {
            "id": r["id"],
            "number": r["number"],
            "content": r["issued_content"],
            "issued_at": r["issued_at"],
        }

    return {
        "version": meta["version"],
        "status": meta["status"],
        "current": pending_json(pending[0]) if pending else None,
        "next": pending_json(pending[1]) if len(pending) > 1 else None,
        "pending": [pending_json(r) for r in pending],
        "issued": [issued_json(r) for r in issued],
        "total": len(rows),
        "remaining": len(pending),
        "min_cues": MIN_CUES,
        "max_cues": MAX_CUES,
    }


def conflict(conn: sqlite3.Connection) -> JSONResponse:
    """409：请求已过期，附带最新状态供页面刷新。"""
    return JSONResponse(
        status_code=409,
        content={"error": "conflict", "message": "操作已过期", "state": build_state(conn)},
    )


def bad_request(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": message})


# ---------------------------------------------------------------- 档案组装

def archive_snapshots(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """读取归档快照：已发按发出顺序、剩余按待发顺序。"""
    rows = conn.execute("SELECT * FROM cues ORDER BY position ASC, id ASC").fetchall()
    issued = [
        {
            "number": r["number"],
            "content": r["issued_content"],
            "issued_at": r["issued_at"],
        }
        for r in sorted(
            (r for r in rows if r["status"] == "issued"),
            key=lambda r: r["issued_seq"],
        )
    ]
    pending = [
        {"number": r["number"], "content": r["content"]}
        for r in rows
        if r["status"] == "pending"
    ]
    return issued, pending


def archive_summary(row: sqlite3.Row) -> dict:
    issued = json.loads(row["issued"])
    pending = json.loads(row["pending"])
    return {
        "id": row["id"],
        "name": row["name"],
        "archived_at": row["archived_at"],
        "status": row["status"],
        "issued_count": len(issued),
        "pending_count": len(pending),
    }


def archive_detail(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "archived_at": row["archived_at"],
        "status": row["status"],
        "version": row["version"],
        "issued": json.loads(row["issued"]),
        "pending": json.loads(row["pending"]),
    }


# ---------------------------------------------------------------- 应用

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="舞台提示执行台", lifespan=lifespan)


class CueIn(BaseModel):
    number: str
    content: str


class SetupIn(BaseModel):
    cues: list[CueIn]


class VersionedIn(BaseModel):
    version: int


class ArchiveIn(BaseModel):
    version: int
    name: str


class EditIn(BaseModel):
    version: int
    cue_id: int
    content: str
    number: Optional[str] = None


class ReorderIn(BaseModel):
    version: int
    order: list[int]


@app.get("/api/state")
def get_state():
    with db() as conn:
        return build_state(conn)


@app.get("/api/archives")
def list_archives():
    """档案列表：按归档时间倒序、同时间按档案 ID 倒序。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM archives ORDER BY archived_at DESC, id DESC"
        ).fetchall()
        return {"archives": [archive_summary(r) for r in rows]}


@app.get("/api/archives/{archive_id}")
def get_archive(archive_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "archive_not_found"})
        return archive_detail(row)


@app.post("/api/setup")
def setup(body: SetupIn):
    """开场前录入：2~12 条、编号唯一、内容非空。会清空旧进度。"""
    cues = body.cues
    if not (MIN_CUES <= len(cues) <= MAX_CUES):
        return bad_request(f"提示数量需在 {MIN_CUES}～{MAX_CUES} 条之间")
    numbers = [c.number.strip() for c in cues]
    contents = [c.content.strip() for c in cues]
    if any(not n for n in numbers):
        return bad_request("编号不能为空")
    if len(set(numbers)) != len(numbers):
        return bad_request("编号必须唯一")
    if any(not c for c in contents):
        return bad_request("内容不能为空")

    with lock, db() as conn:
        conn.execute("DELETE FROM cues")
        for pos, (number, content) in enumerate(zip(numbers, contents), start=1):
            conn.execute(
                "INSERT INTO cues (number, content, position, status) "
                "VALUES (?, ?, ?, 'pending')",
                (number, content, pos),
            )
        conn.execute(
            "UPDATE meta SET version = version + 1, status = 'live' WHERE id = 1"
        )
        return build_state(conn)


@app.post("/api/confirm")
def confirm(body: VersionedIn):
    """确认发出当前提示：以服务端此刻的内容为准记入已发，并推进一条。"""
    with lock, db() as conn:
        meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
        if meta["status"] != "live" or body.version != meta["version"]:
            return conflict(conn)
        row = conn.execute(
            "SELECT * FROM cues WHERE status = 'pending' "
            "ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return conflict(conn)

        new_version = meta["version"] + 1
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE cues SET status = 'issued', issued_content = content, "
            "issued_at = ?, issued_seq = ? WHERE id = ?",
            (now, new_version, row["id"]),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM cues WHERE status = 'pending'"
        ).fetchone()["c"]
        conn.execute(
            "UPDATE meta SET version = ?, status = ? WHERE id = 1",
            (new_version, "done" if remaining == 0 else "live"),
        )
        return build_state(conn)


@app.post("/api/edit")
def edit(body: EditIn):
    """改写一条尚未发出的提示（内容，可选编号）。"""
    with lock, db() as conn:
        meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
        if body.version != meta["version"]:
            return conflict(conn)
        row = conn.execute("SELECT * FROM cues WHERE id = ?", (body.cue_id,)).fetchone()
        if row is None or row["status"] != "pending":
            return conflict(conn)

        content = body.content.strip()
        if not content:
            return bad_request("内容不能为空")
        number = row["number"] if body.number is None else body.number.strip()
        if not number:
            return bad_request("编号不能为空")
        dup = conn.execute(
            "SELECT 1 FROM cues WHERE number = ? AND id != ?", (number, row["id"])
        ).fetchone()
        if dup:
            return bad_request("编号必须唯一")

        conn.execute(
            "UPDATE cues SET content = ?, number = ? WHERE id = ?",
            (content, number, row["id"]),
        )
        conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")
        return build_state(conn)


@app.post("/api/reorder")
def reorder(body: ReorderIn):
    """重排尚未发出的提示：order 必须恰好是全部待发提示的 id。"""
    with lock, db() as conn:
        meta = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
        if body.version != meta["version"]:
            return conflict(conn)
        pending_ids = {
            r["id"]
            for r in conn.execute("SELECT id FROM cues WHERE status = 'pending'")
        }
        if len(body.order) != len(pending_ids) or set(body.order) != pending_ids:
            return conflict(conn)

        base = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS m FROM cues"
        ).fetchone()["m"]
        for offset, cue_id in enumerate(body.order, start=1):
            conn.execute(
                "UPDATE cues SET position = ? WHERE id = ?", (base + offset, cue_id)
            )
        conn.execute("UPDATE meta SET version = version + 1 WHERE id = 1")
        return build_state(conn)


@app.post("/api/reset")
def reset():
    """清空进度，回到开场前设置。"""
    with lock, db() as conn:
        conn.execute("DELETE FROM cues")
        conn.execute(
            "UPDATE meta SET version = version + 1, status = 'setup' WHERE id = 1"
        )
        return build_state(conn)


@app.post("/api/archive")
def archive(body: ArchiveIn):
    """归档本场并收场。

    仅演出中（live）或已完成（done）可归档；携带页面所见版本号。
    快照（已发按发出顺序、剩余按待发顺序）写入 archives 与清空 cues、
    回到 setup、版本号 +1 在同一事务内提交：写档案失败则整体回滚，
    当前提示不会被清空。同版本的重复归档因版本不再匹配而拿到 409，
    不会多建档案。
    """
    name = body.name.strip()
    if not name:
        return bad_request("场次名称不能为空")

    with lock, db() as conn:
        meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
        if meta["status"] not in ("live", "done") or body.version != meta["version"]:
            return conflict(conn)

        issued, pending = archive_snapshots(conn)
        now = datetime.now(timezone.utc).isoformat()
        new_version = meta["version"] + 1
        conn.execute(
            "INSERT INTO archives (name, archived_at, status, version, issued, pending) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                now,
                meta["status"],
                meta["version"],
                json.dumps(issued, ensure_ascii=False),
                json.dumps(pending, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM cues")
        conn.execute(
            "UPDATE meta SET version = ?, status = 'setup' WHERE id = 1",
            (new_version,),
        )
        return build_state(conn)


app.mount(
    "/",
    StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
    name="static",
)
