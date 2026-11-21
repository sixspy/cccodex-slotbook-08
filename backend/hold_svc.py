"""占位：容量校验、并发锁、候补分流、当事人幂等。

幂等口径（甲路径，唯一实现；禁止与乙路径合单混用）：
同事务内查到同档同客已有“占位中”占位 → 拒写并落审计，审计与拒写
同一事务提交；事务中途失败则整笔回滚，审计与拒写同滚，不留半笔。
检测到同档同客存在 >=2 条“占位中”（历史双占位偏航现场）→ 记
hold_idempotency_drift 并拒写，由人工处置，服务端绝不自动合单。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, require_lock

router = APIRouter(tags=["holds"])


class HoldIn(BaseModel):
    slot_code: str
    party_key: str
    seats: int = Field(ge=1, le=50)


def _open_holds_for_party(conn, slot_id: int, party_key: str):
    return conn.execute(
        """SELECT id, seats FROM holds
           WHERE slot_id=? AND party_key=? AND status='占位中'
           ORDER BY id""",
        (slot_id, party_key),
    ).fetchall()


@router.get("/api/holds")
def list_holds(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT h.*, s.code AS slot_code FROM holds h
               JOIN slots s ON s.id=h.slot_id ORDER BY h.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/holds")
def create_hold(body: HoldIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"slot:{body.slot_code}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            slot = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.slot_code,)
            ).fetchone()
            if not slot:
                raise HTTPException(404, "档期不存在")
            if slot["status"] != "开放":
                raise HTTPException(409, "档期未开放，禁止占位")

            # 当事人幂等（甲路径）：同事务查同档同客占位中，命中即拒写。
            # 多条命中是历史双占位偏航，记 drift 并拒写，绝不自动合单。
            existing = _open_holds_for_party(conn, slot["id"], body.party_key)
            if len(existing) >= 2:
                write_audit(
                    conn,
                    user,
                    "hold_idempotency_drift",
                    body.party_key,
                    {
                        "slot": body.slot_code,
                        "hold_ids": [r["id"] for r in existing],
                        "seats_sum": sum(int(r["seats"]) for r in existing),
                    },
                )
                conn.commit()
                raise HTTPException(
                    409,
                    "同档同客存在多条占位中（偏航现场），已拒写并留痕，待人工处置",
                )
            if len(existing) == 1:
                write_audit(
                    conn,
                    user,
                    "hold_idempotent_reject",
                    body.party_key,
                    {
                        "slot": body.slot_code,
                        "hold_id": existing[0]["id"],
                        "want_seats": body.seats,
                    },
                )
                conn.commit()
                raise HTTPException(409, "同档同客已有占位中，重复占位已拒写")

            remain = int(slot["capacity"]) - int(slot["booked"])
            pending = conn.execute(
                "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
                (slot["id"],),
            ).fetchone()["s"]
            free = remain - int(pending)
            now = datetime.now().isoformat(timespec="seconds")
            expire = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
            if body.seats > free:
                conn.execute(
                    """INSERT INTO waitlist(slot_id,party_key,seats,reason,status,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (slot["id"], body.party_key, body.seats, "容量不足", "候补中", now),
                )
                write_audit(
                    conn,
                    user,
                    "hold_to_waitlist",
                    body.party_key,
                    {"slot": body.slot_code, "free": free, "want": body.seats},
                )
                conn.commit()
                return {"disposition": "候补", "free": free}
            cur = conn.execute(
                """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (slot["id"], body.party_key, body.seats, "占位中", expire, now),
            )
            write_audit(
                conn,
                user,
                "hold_create",
                body.party_key,
                {"slot": body.slot_code, "seats": body.seats, "hold_id": cur.lastrowid},
            )
            conn.commit()
            return {"disposition": "占位", "hold_id": cur.lastrowid, "expire_at": expire}
        except HTTPException:
            # 409/404 等业务拒绝：正常返回前不回滚（拒写审计已在同事务提交）
            raise
        except Exception:
            # 任何中途故障：审计与拒写同滚，不留半笔
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
