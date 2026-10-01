"""舞台提示执行台 —— FastAPI + SQLite 后端。

并发模型：所有写操作（确认 / 编辑 / 重排 / 归档 / 设置 / 重置 / 以档开新场）
都在同一把锁内串行完成；其中带版本号的操作（确认 / 编辑 / 重排 / 归档 /
以档开新场）在锁内完成"检查版本号 -> 修改数据"两个步骤，因此同一版本号上
并发的两个操作只会有一个生效，另一个收到 409 及最新状态。归档的
"写档案 + 清空提示"、以档开新场的"写入新场提示 + 推进状态"都在同一事务内
提交，任一步失败整体回滚，不会出现档案没存上却清空提示、或状态已推进却
没写入提示的中间态。
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


def validate_cues(items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """校验 2~12 条 (编号, 内容)：数量、编号非空且唯一、内容非空。

    成功返回 (去空格后的编号列表, 去空格后的内容列表)；
    失败抛出 ValueError，消息可直接返回给调用方。
    """
    if not (MIN_CUES <= len(items) <= MAX_CUES):
        raise ValueError(f"提示数量需在 {MIN_CUES}～{MAX_CUES} 条之间")
    numbers = [str(n).strip() for n, _ in items]
    contents = [str(c).strip() for _, c in items]
    if any(not n for n in numbers):
        raise ValueError("编号不能为空")
    if len(set(numbers)) != len(numbers):
        raise ValueError("编号必须唯一")
    if any(not c for c in contents):
        raise ValueError("内容不能为空")
    return numbers, contents


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


class StartFromArchiveIn(BaseModel):
    version: int


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
    try:
        numbers, contents = validate_cues([(c.number, c.content) for c in body.cues])
    except ValueError as exc:
        return bad_request(str(exc))

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


@app.post("/api/archives/{archive_id}/start")
def start_from_archive(archive_id: int, body: StartFromArchiveIn):
    """以一条已归档演出开新场（仅开场前 setup 可生效）。

    新场的待发提示 = 档案中按发出顺序排列的已发（取编号与内容快照，
    不继承发出时间/进度）+ 按待发顺序排列的剩余编号与内容，全部作为
    新场待发提示，当前指向第一条，成功后进入演出中（live）、已发为空、
    版本号 +1。原档案不做任何修改。

    - 非开场前或版本过期：409 + 最新状态（优先判定，避免在演出中误建场）；
    - 档案不存在：404；
    - 合并后不满足 2~12 条 / 编号唯一 / 内容非空：400。
    所有提示写入与状态推进在同一事务内提交，任一步失败整体回滚，
    当前场次与档案都不变；同版本的重复/并发提交因版本不再匹配只有一个成功。
    """
    with lock, db() as conn:
        meta = conn.execute("SELECT version, status FROM meta WHERE id = 1").fetchone()
        if meta["status"] != "setup" or body.version != meta["version"]:
            return conflict(conn)

        row = conn.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(
                status_code=404, content={"error": "archive_not_found"}
            )

        issued = json.loads(row["issued"])
        pending = json.loads(row["pending"])
        items = [(c["number"], c["content"]) for c in issued]
        items += [(c["number"], c["content"]) for c in pending]
        try:
            numbers, contents = validate_cues(items)
        except ValueError as exc:
            return bad_request(str(exc))

        new_version = meta["version"] + 1
        conn.execute("DELETE FROM cues")
        for pos, (number, content) in enumerate(zip(numbers, contents), start=1):
            conn.execute(
                "INSERT INTO cues (number, content, position, status) "
                "VALUES (?, ?, ?, 'pending')",
                (number, content, pos),
            )
        conn.execute(
            "UPDATE meta SET version = ?, status = 'live' WHERE id = 1",
            (new_version,),
        )
        return build_state(conn)


app.mount(
    "/",
    StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
    name="static",
)
