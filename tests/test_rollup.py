"""汇总表（触发器维护）必须与逐条记录计算的结果完全一致。"""
import random
import unittest
from collections import defaultdict
from datetime import date, timedelta

from solar import db


class RollupConsistencyTest(unittest.TestCase):
    def brute(self, conn, sid, start, end):
        prices = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM stations")}
        kwh = rev = 0.0
        days = set()
        for r in conn.execute("SELECT station_id, day, kwh FROM generation"):
            if (sid is None or r["station_id"] == sid) and start <= r["day"] <= end:
                kwh += r["kwh"]
                rev += r["kwh"] * prices[r["station_id"]]
                days.add(r["day"])
        return round(kwh, 2), round(rev, 2), len(days)

    def test_random_operations(self):
        rnd = random.Random(7)
        conn = db.connect(":memory:")
        stations = [db.create_station(conn, {"name": f"S{i}", "capacity_kwp": 10, "price": 0.3 + i / 10})["id"]
                    for i in range(4)]
        base = date(2024, 1, 1)
        for _ in range(1500):
            op = rnd.random()
            sid = rnd.choice(stations)
            day = (base + timedelta(days=rnd.randrange(800))).isoformat()
            if op < 0.6:
                db.upsert_record(conn, sid, day, round(rnd.uniform(0, 500), 1))  # 插入或覆盖
            elif op < 0.8:
                rows = db.list_records(conn, sid, limit=50)
                if rows:
                    db.delete_record(conn, rnd.choice(rows)["id"])
            else:
                ok, _ = db.import_csv(conn, sid, f"{day},{rnd.uniform(0, 300):.1f}\n")
        # 删除一个电站（级联删除其记录）
        db.delete_station(conn, stations.pop())

        for sid in [None, *stations]:
            for y in (2024, 2025, 2026):
                m = db.monthly(conn, sid, y)
                for item in m["items"]:
                    yy, mm = int(item["label"][:4]), int(item["label"][5:])
                    start = date(yy, mm, 1).isoformat()
                    end = db._month_end(yy, mm).isoformat()
                    kwh, rev, days = self.brute(conn, sid, start, end)
                    self.assertAlmostEqual(item["kwh"], kwh, places=1, msg=(sid, item["label"]))
                    self.assertAlmostEqual(item["revenue"], rev, places=1)
                    self.assertEqual(item["days"], days, (sid, item["label"]))
            y = db.yearly(conn, sid)
            for item in y["items"]:
                kwh, rev, days = self.brute(conn, sid, item["label"] + "-01-01", item["label"] + "-12-31")
                self.assertAlmostEqual(item["kwh"], kwh, places=1)
                self.assertEqual(item["days"], days)
            s = db.summary(conn, sid, today=date(2025, 3, 10))
            kwh, rev, days = self.brute(conn, sid, "0000-01-01", "9999-12-31")
            self.assertAlmostEqual(s["total"]["kwh"], kwh, places=1)
            self.assertAlmostEqual(s["total"]["revenue"], rev, places=1)
            self.assertEqual(s["total"]["days"], days)
            # 单日最高与逐条计算一致
            per_day = defaultdict(float)
            for r in conn.execute("SELECT station_id, day, kwh FROM generation"):
                if sid is None or r["station_id"] == sid:
                    per_day[r["day"]] += r["kwh"]
            self.assertAlmostEqual(s["best_day"]["kwh"], max(per_day.values()), places=1)

    def test_rebuild_matches_triggers(self):
        conn = db.connect(":memory:")
        db.generate_demo(conn, days=100, today=date(2025, 6, 1))
        before = [tuple(r) for r in conn.execute("SELECT * FROM station_month WHERE n > 0 ORDER BY 1, 2")]
        db.rebuild_rollups(conn)
        after = [tuple(r) for r in conn.execute("SELECT * FROM station_month ORDER BY 1, 2")]
        self.assertEqual([(a, b, round(c, 3), d) for a, b, c, d in before],
                         [(a, b, round(c, 3), d) for a, b, c, d in after])


if __name__ == "__main__":
    unittest.main()
