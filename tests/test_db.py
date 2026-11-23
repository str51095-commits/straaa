import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from solar import db
from solar.server import App, make_handler


class DbTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.st = db.create_station(self.conn, {"name": "A", "capacity_kwp": 10, "price": 0.5})

    def test_upsert_overwrites(self):
        db.upsert_record(self.conn, self.st["id"], "2024-06-01", 30)
        db.upsert_record(self.conn, self.st["id"], "2024-06-01", 40)
        self.assertEqual(len(db.list_records(self.conn)), 1)
        self.assertEqual(db.list_records(self.conn)[0]["kwh"], 40)

    def test_validation(self):
        with self.assertRaises(db.ValidationError):
            db.upsert_record(self.conn, self.st["id"], "2024-13-01", 1)
        with self.assertRaises(db.ValidationError):
            db.upsert_record(self.conn, self.st["id"], "2024-06-01", -1)
        with self.assertRaises(db.ValidationError):
            db.create_station(self.conn, {"name": ""})

    def test_daily_monthly_yearly(self):
        sid = self.st["id"]
        db.upsert_record(self.conn, sid, "2023-06-01", 20)
        db.upsert_record(self.conn, sid, "2024-06-01", 30)
        db.upsert_record(self.conn, sid, "2024-06-30", 10)
        db.upsert_record(self.conn, sid, "2024-07-01", 50)

        d = db.daily(self.conn, sid, "2024-06")
        self.assertEqual(len(d["items"]), 30)
        self.assertEqual(d["total_kwh"], 40)
        self.assertEqual(d["total_hours"], 4)
        self.assertEqual(d["total_revenue"], 20)

        m = db.monthly(self.conn, sid, 2024)
        self.assertEqual(len(m["items"]), 12)
        june = m["items"][5]
        self.assertEqual(june["kwh"], 40)
        self.assertEqual(june["prev_kwh"], 20)
        self.assertEqual(june["yoy"], 100.0)
        self.assertEqual(m["total_kwh"], 90)

        y = db.yearly(self.conn, sid)
        self.assertEqual([i["label"] for i in y["items"]], ["2023", "2024"])
        self.assertEqual(y["items"][1]["kwh"], 90)
        self.assertEqual(y["items"][1]["yoy"], 350.0)

    def test_all_stations_aggregate(self):
        b = db.create_station(self.conn, {"name": "B", "capacity_kwp": 30, "price": 1})
        db.upsert_record(self.conn, self.st["id"], "2024-06-01", 10)
        db.upsert_record(self.conn, b["id"], "2024-06-01", 30)
        s = db.summary(self.conn, None, today=date(2024, 6, 1))
        self.assertEqual(s["capacity_kwp"], 40)
        self.assertEqual(s["today"]["kwh"], 40)
        self.assertEqual(s["today"]["hours"], 1)
        self.assertEqual(s["today"]["revenue"], 35)
        self.assertEqual(s["total"]["kwh"], 40)
        one = db.summary(self.conn, b["id"], today=date(2024, 6, 1))
        self.assertEqual(one["today"]["kwh"], 30)

    def test_import_csv(self):
        ok, errors = db.import_csv(self.conn, self.st["id"], "日期,发电量\n2024-01-01,5\n2024/1/2,6\nbad,1\n")
        self.assertEqual(ok, 2)
        self.assertEqual(len(errors), 1)
        out = db.export_csv(self.conn)
        self.assertIn("2024-01-02", out)

    def test_delete_station_cascades(self):
        db.upsert_record(self.conn, self.st["id"], "2024-01-01", 5)
        db.delete_station(self.conn, self.st["id"])
        self.assertEqual(db.list_records(self.conn), [])

    def test_demo(self):
        db.generate_demo(self.conn, days=60, today=date(2024, 6, 30))
        y = db.yearly(self.conn)
        self.assertGreater(y["total_kwh"], 0)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["SOLAR_QUIET"] = "1"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(":memory:")))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_flow(self):
        code, raw = self.call("POST", "/api/stations", {"name": "X", "capacity_kwp": 5})
        self.assertEqual(code, 201)
        sid = json.loads(raw)["id"]
        code, _ = self.call("POST", "/api/records", {"station_id": sid, "day": "2024-05-05", "kwh": 22.5})
        self.assertEqual(code, 200)
        code, raw = self.call("GET", f"/api/stats/monthly?station_id={sid}&year=2024")
        self.assertEqual(json.loads(raw)["items"][4]["kwh"], 22.5)
        code, raw = self.call("POST", "/api/records", {"station_id": sid, "day": "x", "kwh": 1})
        self.assertEqual(code, 400)
        code, _ = self.call("GET", "/api/stats/daily?month=bad")
        self.assertEqual(code, 400)
        code, raw = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("光伏".encode(), raw)
        code, _ = self.call("GET", "/../solar/db.py")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()


class AuthApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["SOLAR_QUIET"] = "1"
        os.environ["SOLAR_PASSWORD"] = "s3cret-pw"
        try:
            app = App(":memory:")
        finally:
            del os.environ["SOLAR_PASSWORD"]
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, method, path, body=None, cookie=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(self.base + path, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            resp = opener.open(r)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers, resp.read()

    def test_requires_login(self):
        code, _, _ = self.req("GET", "/api/stations")
        self.assertEqual(code, 401)
        code, headers, _ = self.req("GET", "/")
        self.assertEqual(code, 302)
        self.assertEqual(headers["Location"], "/login.html")
        code, _, _ = self.req("GET", "/login.html")
        self.assertEqual(code, 200)

    def test_login_flow(self):
        code, _, _ = self.req("POST", "/api/login", {"password": "wrong"})
        self.assertEqual(code, 401)
        code, headers, _ = self.req("POST", "/api/login", {"password": "s3cret-pw"})
        self.assertEqual(code, 200)
        set_cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        cookie = set_cookie.split(";")[0]
        code, _, _ = self.req("GET", "/api/stations", cookie=cookie)
        self.assertEqual(code, 200)
        code, _, _ = self.req("GET", "/api/stations", cookie="solar_session=9999999999.deadbeef")
        self.assertEqual(code, 401)


class AuthUnitTest(unittest.TestCase):
    def test_lockout_and_password_change(self):
        from solar.auth import Auth, MAX_FAILS
        a = Auth("pw", "secret")
        tok = a.issue()
        self.assertTrue(a.verify(tok))
        self.assertFalse(Auth("other", "secret").verify(tok))  # 改密码后旧会话失效
        for _ in range(MAX_FAILS):
            a.check_password("1.2.3.4", "bad")
        self.assertTrue(a.locked("1.2.3.4"))
        self.assertFalse(a.locked("5.6.7.8"))


class PriceTest(unittest.TestCase):
    def test_set_all_prices_and_default(self):
        import threading
        from solar.sync import GoodWeSync
        conn = db.connect(":memory:")
        a = db.create_station(conn, {"name": "A", "capacity_kwp": 10, "price": 0.4})
        db.upsert_record(conn, a["id"], "2024-06-01", 100)
        self.assertEqual(db.set_all_prices(conn, "0.2829"), 1)
        self.assertEqual(db.summary(conn, None, today=date(2024, 6, 1))["total"]["revenue"], 28.29)
        self.assertEqual(db.default_price(conn), 0.2829)
        with self.assertRaises(db.ValidationError):
            db.set_all_prices(conn, "abc")

        class C:
            def list_stations(self):
                return [{"id": "g1", "name": "新站", "capacity_kwp": 50}]
        s = GoodWeSync(conn, threading.Lock(), C())
        s.discover()
        new = [x for x in db.list_stations(conn) if x["goodwe_station_id"] == "g1"][0]
        self.assertEqual(new["price"], 0.2829)  # 导入的电站使用统一电价


class RevenuePeriodsTest(unittest.TestCase):
    def test_last_month_including_january(self):
        conn = db.connect(":memory:")
        a = db.create_station(conn, {"name": "A", "capacity_kwp": 10, "price": 0.2829})
        b = db.create_station(conn, {"name": "B", "capacity_kwp": 10, "price": 0.5})
        db.upsert_record(conn, a["id"], "2025-12-31", 100)
        db.upsert_record(conn, b["id"], "2025-12-01", 10)
        db.upsert_record(conn, a["id"], "2026-01-05", 200)
        s = db.summary(conn, None, today=date(2026, 1, 10))
        self.assertEqual(s["last_month"]["kwh"], 110)
        self.assertEqual(s["last_month"]["revenue"], 33.29)   # 100*0.2829 + 10*0.5
        self.assertEqual(s["month"]["revenue"], 56.58)
        self.assertEqual(s["year"]["revenue"], 56.58)
        self.assertEqual(s["total"]["revenue"], 89.87)
        s = db.summary(conn, None, today=date(2026, 3, 1))
        self.assertEqual(s["last_month"]["kwh"], 0)
