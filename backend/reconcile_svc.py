"""对账：档期 booked 与正式册 seats 同源校验；占位偏航同态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _hold_drift(conn, slot_id: int) -> dict:
    """占位同态：统计占位中行数与同档同客重复占位（偏航）。"""
    rows = conn.execute(
        "SELECT party_key, COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中' GROUP BY party_key",
        (slot_id,),
    ).fetchall()
    open_rows = sum(int(r["c"]) for r in rows)
    dup = [r["party_key"] for r in rows if int(r["c"]) >= 2]
    return {
        "open_hold_rows": open_rows,
        "hold_drift": bool(dup),
        "duplicate_parties": dup,
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            booked_sum = conn.execute(
                """SELECT COALESCE(SUM(seats),0) s FROM bookings
                   WHERE slot_id=? AND status='已确认'""",
                (s["id"],),
            ).fetchone()["s"]
            ok = int(booked_sum) == int(s["booked"])
            drift = _hold_drift(conn, s["id"])
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": booked_sum,
                    "generation": s["generation"],
                    "status": s["status"],
                    "ok": ok,
                    **drift,
                }
            )
        return {
            "items": items,
            "all_ok": all(i["ok"] for i in items),
            "any_hold_drift": any(i["hold_drift"] for i in items),
        }


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        booked_sum = conn.execute(
            """SELECT COALESCE(SUM(seats),0) s FROM bookings
               WHERE slot_id=? AND status='已确认'""",
            (s["id"],),
        ).fetchone()["s"]
        drift = _hold_drift(conn, s["id"])
        pending = conn.execute(
            "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["s"]
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": booked_sum,
            "open_holds": drift["open_hold_rows"],
            "open_hold_rows": drift["open_hold_rows"],
            "pending_seats": int(pending),
            "free": int(s["capacity"]) - int(s["booked"]) - int(pending),
            "ok": int(booked_sum) == int(s["booked"]),
            "generation": s["generation"],
            "hold_drift": drift["hold_drift"],
            "duplicate_parties": drift["duplicate_parties"],
        }
