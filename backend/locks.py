"""档期锁与争用登记。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """抢档锁：跨 holder 不可重入，同 holder 可重入。

    - 不同人并发抢同一档：锁键已存在即争用，后到只留争用痕并返回 False。
    - 同一人在同一连接上的重入（如连点）：放行，进入业务事务后由
      占位服务的当事人幂等检查拒写——锁层不替代幂等。
    争用痕在锁获取这一步即独立提交，保证后到者“只留争用痕”不随
    后续业务事务回滚而消失。
    """
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT holder FROM slot_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        # 以主键 INSERT 定胜负：并发下只有一个连接能插入成功
        try:
            conn.execute(
                "INSERT INTO slot_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT holder FROM slot_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            holder_now = row["holder"] if row else "未知"
            conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (lock_key, holder, "争用", f"持有者={holder_now}", now),
            )
            conn.commit()
            return False
    if row["holder"] == holder:
        # 同一文员重入：不在锁层制造争用，交由业务事务内的幂等检查收敛
        return True
    # 锁被他人持有：争用，禁止重入
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    conn.commit()
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM slot_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slot_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
