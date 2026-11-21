"""占位当事人幂等：连点双占位收敛、双人同客争用、五处同态。

红测先行：
1. 假绿夹具中同档同客双占位并存 → 再占位必须拒写并落偏航审计（hold_idempotency_drift）。
2. 同一客人连点两次 → 第二次同事务拒写 + 幂等审计，同档同客只留一条占位中。
3. 两人同时为同一客人占同一档 → 档锁争用，后到 409 只留争用痕，只成一条占位。
五处同态：占位列表 / 当事人互证 / 余席 / 对账 / 审计。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
import db as dbmod  # noqa: E402
from db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user: str = "admin", password: str = "123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _party_holds(c, headers, slot_code, party):
    rows = c.get("/api/holds", headers=headers).json()["items"]
    return [h for h in rows if h["slot_code"] == slot_code and h["party_key"] == party]


def _audit_actions(c, headers):
    return [a["action"] for a in c.get("/api/audit", headers=headers).json()["items"]]


# ---------------------------------------------------------------------------
# 失败测 1：假绿夹具双占位并存 → 偏航拒写，审计与拒写同事务落痕
# ---------------------------------------------------------------------------
def test_fixture_double_hold_drift_must_reject_and_audit():
    with TestClient(app) as c:
        h = _login(c)
        # 夹具本身就是偏航现场：同档同客两条占位中并存
        existing = _party_holds(c, h, "S-PM-02", "P-DBL")
        assert len(existing) == 2

        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-PM-02", "party_key": "P-DBL", "seats": 1},
        )
        assert r.status_code == 409, r.text
        # 拒写：行数不增、席数不翻倍
        after = _party_holds(c, h, "S-PM-02", "P-DBL")
        assert len(after) == 2
        assert sum(x["seats"] for x in after) == 2

        # 偏航审计落痕（审计面）
        actions = _audit_actions(c, h)
        assert "hold_idempotency_drift" in actions

        # 对账面识别偏航
        recon = c.get("/api/reconcile/S-PM-02", headers=h).json()
        assert recon["hold_drift"] is True
        assert "P-DBL" in recon["duplicate_parties"]


# ---------------------------------------------------------------------------
# 失败测 2：连点双占位 → 甲路径同事务拒写 + 幂等审计，五处同态
# ---------------------------------------------------------------------------
def test_double_click_same_party_collapses_to_one_hold():
    with TestClient(app) as c:
        h = _login(c)
        payload = {"slot_code": "S-AM-01", "party_key": "P-CLICK", "seats": 2}

        r1 = c.post("/api/holds", headers=h, json=payload)
        assert r1.status_code == 200, r1.text
        assert r1.json()["disposition"] == "占位"

        r2 = c.post("/api/holds", headers=h, json=payload)
        assert r2.status_code == 409, r2.text

        # 面 1：占位列表只留一条
        holds = _party_holds(c, h, "S-AM-01", "P-CLICK")
        assert len(holds) == 1
        assert holds[0]["seats"] == 2
        assert holds[0]["status"] == "占位中"

        # 面 2：当事人互证同源只有一条占位
        proof = c.get("/api/proof/P-CLICK", headers=h).json()
        assert len(proof["holds"]) == 1

        # 面 3：余席只扣一次（容量 10，种子已确认 2，本次占位 2 → 余 6），拒写前后不变
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["free"] == 6
        assert slot["pending_seats"] == 2

        # 面 4：对账无偏航、占位行唯一
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["ok"] is True
        assert recon["hold_drift"] is False
        assert recon["open_hold_rows"] == 1

        # 面 5：幂等拒写审计与拒写同事务落痕
        assert "hold_idempotent_reject" in _audit_actions(c, h)


def test_reject_audit_rolls_back_with_failure(monkeypatch):
    """甲路径失败口径：审计写入后若同事务失败，审计与拒写同滚，不留半笔。"""
    import hold_svc

    real_write_audit = hold_svc.write_audit

    def flaky_write_audit(conn, actor, action, target, payload=None):
        # 先真实落审计（仍在同一事务内），再对幂等拒写注入故障
        real_write_audit(conn, actor, action, target, payload)
        if action == "hold_idempotent_reject":
            raise RuntimeError("injected failure after audit insert")

    monkeypatch.setattr(hold_svc, "write_audit", flaky_write_audit)

    with TestClient(app, raise_server_exceptions=False) as c:
        h = _login(c)
        r1 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-ROLL", "seats": 1},
        )
        assert r1.status_code == 200, r1.text
        assert "hold_idempotent_reject" not in _audit_actions(c, h)

        r2 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-ROLL", "seats": 1},
        )
        assert r2.status_code == 500, r2.text

        # 审计同滚：拒写审计不留痕，占位仍只有第一条、席数不翻倍
        assert "hold_idempotent_reject" not in _audit_actions(c, h)
        holds = _party_holds(c, h, "S-AM-01", "P-ROLL")
        assert len(holds) == 1
        assert holds[0]["seats"] == 1


# ---------------------------------------------------------------------------
# 失败测 3a：两人同时为同一客人占同一档 → 只成一条占位，席数不翻倍
# 后到者两种合法结局：持锁期撞锁（争用痕 409）或锁释放后撞幂等（拒写 409）
# ---------------------------------------------------------------------------
def test_two_users_same_party_contention_leaves_one_hold():
    with TestClient(app) as c:
        h1 = _login(c, "admin")
        h2 = _login(c, "clerk")
        barrier = threading.Barrier(2)
        statuses: dict[str, int] = {}
        bodies: dict[str, str] = {}

        def worker(tag, headers):
            barrier.wait()
            r = c.post(
                "/api/holds",
                headers=headers,
                json={"slot_code": "S-AM-01", "party_key": "P-RACE", "seats": 1},
            )
            statuses[tag] = r.status_code
            bodies[tag] = r.text

        t1 = threading.Thread(target=worker, args=("a", h1))
        t2 = threading.Thread(target=worker, args=("b", h2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(statuses.values()) == [200, 409], bodies

        # 只成一条占位，席数不翻倍
        holds = _party_holds(c, h1, "S-AM-01", "P-RACE")
        assert len(holds) == 1
        assert holds[0]["seats"] == 1

        # 后到 409 必留痕：争用痕或幂等拒写审计至少其一，且无第二条业务写入
        contention = c.get("/api/contention", headers=h1).json()["items"]
        slot_contention = [x for x in contention if x["lock_key"] == "slot:S-AM-01"]
        actions = _audit_actions(c, h1)
        assert slot_contention or "hold_idempotent_reject" in actions

        # 锁事后释放，不残留
        locks = c.get("/api/locks", headers=h1).json()["items"]
        assert all(x["lock_key"] != "slot:S-AM-01" for x in locks)

        # 同态：互证与对账都只见一条
        proof = c.get("/api/proof/P-RACE", headers=h1).json()
        assert len(proof["holds"]) == 1
        assert proof["hold_drift"] is False
        recon = c.get("/api/reconcile/S-AM-01", headers=h1).json()
        assert recon["hold_drift"] is False
        assert recon["open_hold_rows"] == 1


# ---------------------------------------------------------------------------
# 失败测 3b：档被他人持锁期间，后到者只留争用痕，不写任何业务数据
# ---------------------------------------------------------------------------
def test_held_slot_lock_rejects_second_user_with_contention_trace_only():
    with TestClient(app) as c:
        h1 = _login(c, "admin")
        h2 = _login(c, "clerk")
        # admin 先持档锁不释放
        r = c.post(
            "/api/locks/acquire", headers=h1, json={"lock_key": "slot:S-PM-02"}
        )
        assert r.status_code == 200

        before_holds = c.get("/api/holds", headers=h1).json()["items"]
        r2 = c.post(
            "/api/holds",
            headers=h2,
            json={"slot_code": "S-PM-02", "party_key": "P-LOCKED", "seats": 1},
        )
        assert r2.status_code == 409
        assert "争用" in r2.text

        # 只留争用痕
        contention = c.get("/api/contention", headers=h1).json()["items"]
        rows = [x for x in contention if x["lock_key"] == "slot:S-PM-02"]
        assert len(rows) == 1
        assert rows[0]["actor"] == "clerk"
        assert rows[0]["action"] == "争用"

        # 无任何业务写入：占位不增、无该当事人审计动作（争用痕不算业务审计）
        after_holds = c.get("/api/holds", headers=h1).json()["items"]
        assert len(after_holds) == len(before_holds)
        proof = c.get("/api/proof/P-LOCKED", headers=h1).json()
        assert proof["holds"] == []
        actions = _audit_actions(c, h1)
        assert "hold_create" not in actions

        # 释放锁后 clerk 可正常占位
        c.post("/api/locks/release", headers=h1, json={"lock_key": "slot:S-PM-02"})
        r3 = c.post(
            "/api/holds",
            headers=h2,
            json={"slot_code": "S-PM-02", "party_key": "P-LOCKED", "seats": 1},
        )
        assert r3.status_code == 200
        assert len(_party_holds(c, h1, "S-PM-02", "P-LOCKED")) == 1


# ---------------------------------------------------------------------------
# 失败测 4：候补晋升是 holds 写入旁路，必须同守甲路径幂等
# ---------------------------------------------------------------------------
def test_waitlist_promote_also_respects_party_idempotency():
    with TestClient(app) as c:
        h = _login(c)
        slot = "S-AM-01"  # 容量 10、种子 booked 2，空位充足

        # 人造一条候补中（容量充足场景下由历史/旁路留下的候补单）
        with dbmod.connect() as conn:
            sid = conn.execute(
                "SELECT id FROM slots WHERE code=?", (slot,)
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO waitlist(slot_id,party_key,seats,reason,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (sid, "P-WL", 1, "容量不足", "候补中", "2026-11-21T10:00:00"),
            )
            conn.commit()
        wid = [
            w
            for w in c.get("/api/waitlist", headers=h).json()["items"]
            if w["party_key"] == "P-WL"
        ][0]["id"]

        # 同档同客已有一条占位中
        r2 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": slot, "party_key": "P-WL", "seats": 1},
        )
        assert r2.status_code == 200 and r2.json()["disposition"] == "占位"
        existing = _party_holds(c, h, slot, "P-WL")
        assert len(existing) == 1

        # 晋升不得再插第二条占位：甲路径拒写留痕
        pr = c.post("/api/waitlist/promote", headers=h, json={"wait_id": wid})
        assert pr.status_code == 409, pr.text
        after = _party_holds(c, h, slot, "P-WL")
        assert len(after) == 1
        # 候补单未被晋升
        waits2 = [w for w in c.get("/api/waitlist", headers=h).json()["items"] if w["id"] == wid]
        assert waits2[0]["status"] == "候补中"
        assert "hold_idempotent_reject" in _audit_actions(c, h)
