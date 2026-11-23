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
        self.alarm_rows = []

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
        if path.startswith("/sems-alarm/api/v2/alarm/page"):
            rows = [a for a in self.alarm_rows if a["stationId"] in body["stationIds"]]
            return {"code": "00000", "data": {"dataList": rows, "total": len(rows)}}
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
        stations = [{"id": f"id{i}", "name": f"站{i}", "installedPower": 10 + i, "status": "1"} for i in range(250)]
        c = SemsPlusClient("a", "pw", request=FakeSemsPlus(stations=stations))
        got = c.list_stations()
        self.assertEqual(len(got), 250)
        self.assertEqual(got[249], {"id": "id249", "name": "站249", "capacity_kwp": 259.0, "status": 1})

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


class MonitorSyncTest(unittest.TestCase):
    def test_status_power_alarms_and_overview(self):
        from solar import monitor
        conn = db.connect(":memory:")
        stations = [{"id": "g1", "name": "一号", "installedPower": 100, "status": "1"},
                    {"id": "g2", "name": "二号", "installedPower": 100, "status": "2"},
                    {"id": "g3", "name": "三号", "installedPower": 100, "status": "0"}]
        t = date(2026, 11, 23).isoformat()
        fake = FakeSemsPlus(stations=stations, daily={"g1": {t: 500}, "g2": {t: 480}, "g3": {t: 100}})
        fake.alarm_rows = [
            {"warningid": "w1", "stationId": "g2", "stationname": "二号", "devicesn": "SN1", "deviceName": "逆变器1",
             "warning_code": "E01", "warningNameZh": "电网过压", "alarmLevel": "Total_FaultLevel_Fault",
             "status": 0, "happentimes": "2026-11-23 10:00:00", "recoverytimes": None},
            {"warningid": "w2", "stationId": "g1", "warningNameEn": "Fan fault", "status": 1,
             "happentime": "1763870400000", "recoverytimes": "2026-11-23 11:00:00"},
        ]
        sync = GoodWeSync(conn, threading.Lock(), SemsPlusClient("a", "pw", request=fake))
        sync.discover()
        from solar import clock
        orig = clock.today
        clock.today = lambda: date(2026, 11, 23)
        try:
            st = sync.run_once()
            ov = monitor.overview(conn)
        finally:
            clock.today = orig
        self.assertTrue(st["ok"], st)
        by = {r["name"]: r for r in ov["stations"]}
        self.assertEqual((by["一号"]["status"], by["二号"]["status"], by["三号"]["status"]),
                         ("running", "fault", "offline"))
        self.assertEqual(by["一号"]["power_w"], 42500)
        self.assertEqual(by["二号"]["active_alarms"], 1)
        self.assertEqual(by["一号"]["active_alarms"], 0)
        self.assertTrue(by["三号"]["low_eff"])          # 1 h vs 中位数 4.8 h
        self.assertFalse(by["一号"]["low_eff"])
        self.assertEqual(ov["summary"]["counts"], {"running": 1, "fault": 1, "offline": 1})
        self.assertEqual(ov["summary"]["today_kwh"], 1080)
        alarms = monitor.list_alarms(conn)
        self.assertEqual([a["warning_id"] for a in alarms], ["w1", "w2"])  # 进行中的排前面
        self.assertEqual(alarms[0]["station"], "二号")
        self.assertEqual(monitor.list_alarms(conn, active_only=True)[0]["name"], "电网过压")
        # 告警恢复后更新
        fake.alarm_rows[0]["status"] = 1
        fake.alarm_rows[0]["recoverytimes"] = "2026-11-23 12:00:00"
        sync.run_once(today=date(2026, 11, 23))
        self.assertEqual(monitor.list_alarms(conn, active_only=True), [])

    def test_report_csv(self):
        from solar import monitor
        conn = db.connect(":memory:")
        a = db.create_station(conn, {"name": "A", "capacity_kwp": 10, "price": 0.5})
        db.create_station(conn, {"name": "B", "capacity_kwp": 20, "price": 0.5})
        db.upsert_record(conn, a["id"], "2026-10-01", 40)
        db.upsert_record(conn, a["id"], "2026-10-02", 60)
        db.upsert_record(conn, a["id"], "2025-10-02", 99)
        rep = monitor.report_csv(conn, "2026-10")
        self.assertIn("A,10.0,100.0,10.0,0.5,50.0,2", rep)
        self.assertIn("B,20.0,0,0.0,0.5,0.0,0", rep)
        self.assertIn("合计,30.0,100.0", rep)
        self.assertIn("A,10.0,100.0", monitor.report_csv(conn, "2026"))
        with self.assertRaises(db.ValidationError):
            monitor.report_csv(conn, "26-1")


class BackfillBatchTest(unittest.TestCase):
    def setUp(self):
        import solar.sync as sync_mod
        self.sync_mod = sync_mod
        self.saved = (sync_mod.RATE_LIMIT_WAITS, sync_mod.BACKFILL_BATCH_PAUSE, sync_mod.BACKFILL_BATCH)
        sync_mod.RATE_LIMIT_WAITS, sync_mod.BACKFILL_BATCH_PAUSE, sync_mod.BACKFILL_BATCH = [0, 0, 0, 0], 0, 5
        from solar import clock
        self.clock, self.orig_today = clock, clock.today
        clock.today = lambda: date(2026, 11, 23)
        self.conn = db.connect(":memory:")
        stations = [{"id": f"s{i}", "name": f"站{i}", "installedPower": 10} for i in range(12)]
        daily = {f"s{i}": {"2026-11-01": 10 + i, "2025-06-01": 20} for i in range(12)}
        self.fake = FakeSemsPlus(stations=stations, daily=daily)
        # 模拟限流：前 3 次统计请求返回 429
        self.limited = 3
        orig = self.fake.__call__

        def limited_call(method, url, body, headers, timeout):
            if "statistics" in url and self.limited > 0:
                self.limited -= 1
                from solar.semsplus import GoodWeRateLimit
                raise GoodWeRateLimit("限流")
            return orig(method, url, body, headers, timeout)
        self.sync = GoodWeSync(self.conn, threading.Lock(), SemsPlusClient("a", "pw", request=limited_call))
        self.sync.discover()

    def tearDown(self):
        self.sync_mod.RATE_LIMIT_WAITS, self.sync_mod.BACKFILL_BATCH_PAUSE, self.sync_mod.BACKFILL_BATCH = self.saved
        self.clock.today = self.orig_today

    def wait(self):
        for _ in range(200):
            if not self.sync.status["backfill"]["running"]:
                return self.sync.status["backfill"]
            time.sleep(0.02)
        self.fail("backfill did not finish")

    def test_retries_rate_limit_and_skips_done(self):
        self.sync.start_backfill()
        bf = self.wait()
        self.assertEqual(bf["errors"], [])
        self.assertEqual(bf["done"], 12)
        n = self.conn.execute("SELECT COUNT(*) FROM generation").fetchone()[0]
        self.assertEqual(n, 24)  # 12 座 × 2 天
        self.assertEqual(self.sync.pending_backfill(), [])
        # 再点一次：没有待补录
        st = self.sync.start_backfill()
        self.assertTrue(st["backfill"]["nothing"])
        # 新导入一座：只补这一座
        self.fake.stations.append({"id": "s99", "name": "新站", "installedPower": 5})
        self.fake.daily["s99"] = {"2026-11-02": 7}
        self.sync.discover()
        self.sync.start_backfill()
        bf = self.wait()
        self.assertEqual((bf["total"], bf["done"]), (1, 1))
        # 强制全部重新补录
        self.sync.start_backfill(force=True)
        self.assertEqual(self.wait()["total"], 13)

    def test_gives_up_after_retries_and_retries_next_time(self):
        self.limited = 100   # 一直限流
        self.sync.start_backfill()
        bf = self.wait()
        self.assertEqual(len(bf["errors"]), 12)
        self.assertEqual(len(self.sync.pending_backfill()), 12)  # 失败的下次还会补
        self.limited = 0
        self.sync.start_backfill()
        self.assertEqual(self.wait()["errors"], [])
        self.assertEqual(self.sync.pending_backfill(), [])
