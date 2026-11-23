import base64
import hashlib
import json
import threading
import time
import unittest
from datetime import date

from solar import db
from solar.goodwe import GoodWeError
from solar.semsplus import GoodWeAuthError, SemsPlusClient, hash_password, signature
from solar.sync import BACKFILL_NOTE, GoodWeSync

API = "https://cn-gateway.semsportal.com/web/sems"


class FakeSemsPlus:
    """模拟 SEMS+：只接受 cn 主机 + isChinese=True 的登录，校验密码哈希与签名格式。"""

    def __init__(self, password="pw", daily=None, stations=None, expire_once=False):
        self.password = password
        self.daily = daily or {}            # {station_id: {YYYY-MM-DD: kwh}}
        self.stations = stations or []
        self.expire_once = expire_once
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body))
        if url.endswith("/auth/cross-login"):
            if not url.startswith("https://cn-semsplus.goodwe.com") or body["isChinese"] is not True:
                return {"code": "C0001", "description": "账号不存在"}
            if body["pwd"] != hash_password(self.password):
                return {"code": "100005", "description": "Email or password error."}
            return {"code": "00000", "data": {"uid": "u1", "token": "tk", "timestamp": "1",
                                              "api": API, "region": "cn"}}
        assert url.startswith(API), url
        tok = json.loads(headers["token"])
        assert tok["token"] == "tk"
        sig = base64.b64decode(headers["X-Signature"]).decode()
        digest, ms = sig.split("@")
        assert digest == hashlib.sha256(f"{ms}@u1@tk".encode()).hexdigest()
        if self.expire_once:
            self.expire_once = False
            return {"code": "100002", "description": "expired"}
        path = url[len(API):]
        if path.startswith("/sems-plant/api/portal/stations/page"):
            page, size = body["current"], body["size"]
            rows = self.stations[(page - 1) * size: page * size]
            return {"code": "00000", "data": {"total": len(self.stations), "dataList": rows}}
        if path.startswith("/sems-plant/api/stations/statistics"):
            sid = body["stationId"]
            start, end = body["startTime"][:10], body["endTime"][:10]
            assert start[:4] == end[:4], "区间不能跨年"
            pts = [{"date": d, "val": v} for d, v in sorted(self.daily.get(sid, {}).items()) if start <= d <= end]
            return {"code": "00000", "data": {"dataList": [
                {"item": "proSystemTotalStats", "statisticsList": pts},
                {"item": "proGridStats", "statisticsList": [{"date": p["date"], "val": p["val"] * 0.9} for p in pts]},
            ]}}
        if path.startswith("/sems-plant/api/stations/flow"):
            return {"code": "00000", "data": {"pSystem": 42.5}}
        return {"code": "404", "description": "not found"}


class SemsPlusClientTest(unittest.TestCase):
    def test_password_hash_and_signature(self):
        self.assertEqual(hash_password("abc"), base64.b64encode(hashlib.md5(b"abc").hexdigest().encode()).decode())
        sig = base64.b64decode(signature("u", "t", now_ms=123)).decode()
        self.assertEqual(sig, hashlib.sha256(b"123@u@t").hexdigest() + "@123")

    def test_login_tries_candidates_and_remembers(self):
        fake = FakeSemsPlus(daily={"s1": {"2026-11-23": 300.5, "2026-11-22": 200}})
        c = SemsPlusClient("a@b.c", "pw", region="cn", request=fake)
        r = c.station_today("s1", today=date(2026, 11, 23))
        self.assertEqual(r["today_kwh"], 300.5)
        self.assertEqual(r["month_kwh"], 500.5)
        self.assertEqual(r["power_w"], 42500)
        self.assertEqual(c.login_used[1], True)
        logins = sum(1 for _, u, _ in fake.calls if u.endswith("cross-login"))
        c.token = None
        c.station_today("s1", today=date(2026, 11, 23))
        self.assertEqual(sum(1 for _, u, _ in fake.calls if u.endswith("cross-login")), logins + 1)  # 直接用记住的地址

    def test_wrong_password(self):
        c = SemsPlusClient("a", "bad", request=FakeSemsPlus())
        with self.assertRaises(GoodWeAuthError):
            c.login()

    def test_relogin_on_expired(self):
        fake = FakeSemsPlus(daily={"s1": {"2026-11-23": 5}}, expire_once=True)
        c = SemsPlusClient("a", "pw", request=fake)
        c.login()
        self.assertEqual(c.station_today("s1", today=date(2026, 11, 23))["today_kwh"], 5)

    def test_list_stations_paging(self):
        stations = [{"id": f"id{i}", "name": f"站{i}", "installedPower": 10 + i} for i in range(250)]
        c = SemsPlusClient("a", "pw", request=FakeSemsPlus(stations=stations))
        got = c.list_stations()
        self.assertEqual(len(got), 250)
        self.assertEqual(got[249], {"id": "id249", "name": "站249", "capacity_kwp": 259.0})

    def test_history_splits_by_year(self):
        daily = {"s1": {"2024-12-31": 1, "2025-01-01": 2, "2025-06-01": 3}}
        c = SemsPlusClient("a", "pw", request=FakeSemsPlus(daily=daily))
        h = c.daily_history("s1", date(2024, 12, 1), date(2025, 12, 31))
        self.assertEqual(h, {"2024-12-31": 1, "2025-01-01": 2, "2025-06-01": 3})


class SemsPlusSyncTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        daily = {"g1": {"2025-03-01": 100, "2026-11-22": 80, "2026-11-23": 50},
                 "g2": {"2026-11-23": 9}}
        stations = [{"id": "g1", "name": "城关镇电站", "installedPower": 128},
                    {"id": "g2", "name": "二号站", "installedPower": 20}]
        self.fake = FakeSemsPlus(daily=daily, stations=stations)
        self.sync = GoodWeSync(self.conn, threading.Lock(), SemsPlusClient("a", "pw", request=self.fake))

    def test_discover_then_backfill_keeps_manual(self):
        r = self.sync.discover()
        self.assertEqual((r["found"], r["created"]), (2, 2))
        self.assertEqual(self.sync.discover()["created"], 0)  # 再次导入不重复
        st = {s["goodwe_station_id"]: s for s in db.list_stations(self.conn)}
        self.assertEqual(st["g1"]["capacity_kwp"], 128)
        db.upsert_record(self.conn, st["g1"]["id"], "2026-11-22", 77, "手动")
        from solar import clock
        orig = clock.today
        clock.today = lambda: date(2026, 11, 23)
        try:
            self.sync.start_backfill()
            for _ in range(100):
                if not self.sync.status["backfill"]["running"]:
                    break
                time.sleep(0.05)
        finally:
            clock.today = orig
        bf = self.sync.status["backfill"]
        self.assertEqual(bf["errors"], [])
        recs = {(r["station_id"], r["day"]): r for r in db.list_records(self.conn)}
        self.assertEqual(recs[(st["g1"]["id"], "2025-03-01")]["kwh"], 100)
        self.assertEqual(recs[(st["g1"]["id"], "2025-03-01")]["note"], BACKFILL_NOTE)
        self.assertEqual(recs[(st["g1"]["id"], "2026-11-22")]["kwh"], 77)  # 手动数据未被覆盖
        self.assertEqual(recs[(st["g2"]["id"], "2026-11-23")]["kwh"], 9)

    def test_auth_failure_pauses_auto_sync(self):
        db.create_station(self.conn, {"name": "x", "capacity_kwp": 1, "goodwe_station_id": "g1"})
        db.create_station(self.conn, {"name": "y", "capacity_kwp": 1, "goodwe_station_id": "g2"})
        self.sync.client.password = "wrong"
        st = self.sync.run_once(manual=False)
        self.assertTrue(st["paused"])
        self.assertEqual(len(st["stations"]), 1)  # 第一个失败后不再继续
        n = len(self.fake.calls)
        self.sync.run_once(manual=False)
        self.assertEqual(len(self.fake.calls), n)  # 暂停期间自动同步不再请求
        self.sync.client.password = "pw"
        st = self.sync.run_once(manual=True, today=date(2026, 11, 23))
        self.assertFalse(st["paused"])
        self.assertTrue(st["ok"])


if __name__ == "__main__":
    unittest.main()
