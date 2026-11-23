import json
import threading
import unittest
from datetime import date

from solar import db
from solar.goodwe import GoodWeClient, GoodWeError
from solar.sync import GoodWeSync


class FakeSems:
    """模拟 SEMS 接口。"""
    def __init__(self, stations, expire_once=False, bad_login=False):
        self.stations = stations
        self.expire_once = expire_once
        self.bad_login = bad_login
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, json.loads(headers["Token"])))
        if url.endswith("CrossLogin"):
            if self.bad_login:
                return {"hasError": True, "code": 100005, "msg": "Email or password error."}
            return {"hasError": False, "code": 0, "msg": "Successful",
                    "data": {"uid": "u", "timestamp": 1, "token": "tok", "client": "ios",
                             "version": "v2.1.0", "language": "en"},
                    "api": "https://eu.semsportal.com/api/"}
        assert url == "https://eu.semsportal.com/api/v2/PowerStation/GetMonitorDetailByPowerstationId", url
        if self.expire_once:
            self.expire_once = False
            return {"hasError": True, "code": 100002, "msg": "token expired"}
        sid = payload["powerStationId"]
        if sid not in self.stations:
            return {"hasError": False, "code": 0, "data": {"kpi": {}, "info": {}}}
        return {"hasError": False, "code": 0, "data": {
            "info": {"stationname": "My PV", "capacity": 10.5},
            "kpi": {"power": self.stations[sid], "month_generation": 300.0, "total_power": 9000.0, "pac": 4200},
        }}


class GoodWeClientTest(unittest.TestCase):
    def test_login_and_fetch(self):
        fake = FakeSems({"abc": 23.4})
        c = GoodWeClient("me@x.com", "pw", post=fake)
        r = c.station_today("abc")
        self.assertEqual(r["today_kwh"], 23.4)
        self.assertEqual(r["capacity_kwp"], 10.5)
        self.assertEqual(fake.calls[0][1], {"account": "me@x.com", "pwd": "pw"})
        self.assertEqual(fake.calls[1][2]["token"], "tok")  # 后续请求带上登录 token

    def test_token_expired_relogin(self):
        fake = FakeSems({"abc": 5}, expire_once=True)
        c = GoodWeClient("a", "b", post=fake)
        self.assertEqual(c.station_today("abc")["today_kwh"], 5)
        self.assertEqual(sum(1 for u, *_ in fake.calls if u.endswith("CrossLogin")), 2)

    def test_errors(self):
        with self.assertRaises(GoodWeError):
            GoodWeClient("a", "b", post=FakeSems({}, bad_login=True)).station_today("abc")
        with self.assertRaises(GoodWeError):
            GoodWeClient("a", "b", post=FakeSems({})).station_today("missing")
        with self.assertRaises(GoodWeError):
            GoodWeClient("", "")


class SyncTest(unittest.TestCase):
    def test_sync_writes_today(self):
        conn = db.connect(":memory:")
        a = db.create_station(conn, {"name": "A", "capacity_kwp": 10, "goodwe_station_id": "abc"})
        db.create_station(conn, {"name": "Manual", "capacity_kwp": 5})
        db.create_station(conn, {"name": "Bad", "capacity_kwp": 5, "goodwe_station_id": "nope"})
        client = GoodWeClient("a", "b", post=FakeSems({"abc": 31.2}))
        s = GoodWeSync(conn, threading.Lock(), client)
        st = s.run_once(today=date(2024, 6, 1))
        self.assertFalse(st["ok"])
        self.assertEqual(len(st["stations"]), 2)
        recs = db.list_records(conn)
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["station_id"], recs[0]["day"], recs[0]["kwh"]), (a["id"], "2024-06-01", 31.2))
        # 再次同步覆盖为最新值
        client._post.stations["abc"] = 40
        s.run_once(today=date(2024, 6, 1))
        self.assertEqual(db.list_records(conn)[0]["kwh"], 40)

    def test_disabled(self):
        conn = db.connect(":memory:")
        st = GoodWeSync(conn, threading.Lock(), None).run_once()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["ok"])

    def test_migration_adds_column(self):
        import os, sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT, capacity_kwp REAL, location TEXT,"
                    " commission_date TEXT, price REAL, notes TEXT, created_at TEXT)")
        old.commit(); old.close()
        conn = db.connect(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stations)")}
        self.assertIn("goodwe_station_id", cols)


if __name__ == "__main__":
    unittest.main()
