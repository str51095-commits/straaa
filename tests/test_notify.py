import unittest
from datetime import date

from solar import clock, db, monitor, notify


class FakePost:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def __call__(self, url, payload, timeout=15):
        self.calls.append((url, payload))
        if self.reply is not None:
            return self.reply
        if "pushplus" in url:
            return {"code": 200, "msg": "请求成功"}
        if "qyapi.weixin.qq.com" in url:
            return {"errcode": 0, "errmsg": "ok"}
        if "sctapi" in url:
            return {"code": 0}
        if "wxpusher" in url:
            return {"code": 1000, "success": True}
        return {}


def alarm(wid, gid, active, level="Total_FaultLevel_Fault", name="绝缘阻抗异常"):
    return {"id": wid, "station_id": gid, "station_name": gid, "device_name": "逆变器-1", "device_sn": "SN",
            "code": "1033", "name": name, "level": level, "active": active,
            "happened": "2026-11-23 09:12:40", "recovered": "" if active else "2026-11-23 10:00:00"}


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.a = db.create_station(self.conn, {"name": "一号站", "capacity_kwp": 100, "goodwe_station_id": "g1"})
        self.b = db.create_station(self.conn, {"name": "二号站", "capacity_kwp": 100, "goodwe_station_id": "g2"})
        notify.save_config(self.conn, {"notify_channel": "pushplus", "notify_token": "tok123456789"})

    def test_channels_payloads(self):
        for ch, tok, url_part in [("pushplus", "t1", "pushplus.plus/send"),
                                  ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "key=abc"),
                                  ("wecom", "abc-key", "key=abc-key"),
                                  ("serverchan", "SCT123", "sctapi.ftqq.com/SCT123.send"),
                                  ("wxpusher", "AT_x", "wxpusher")]:
            notify.save_config(self.conn, {"notify_channel": ch, "notify_token": tok, "notify_uids": "UID_1，UID_2"})
            fp = FakePost()
            self.assertTrue(notify.send(self.conn, "标题", ["第一行", "第二行"], post=fp))
            url, payload = fp.calls[0]
            self.assertIn(url_part, url)
            if ch == "pushplus":
                self.assertEqual(payload["token"], "t1")
                self.assertEqual(payload["template"], "markdown")
            if ch == "wecom":
                self.assertEqual(payload["msgtype"], "markdown")
                self.assertIn("第二行", payload["markdown"]["content"])
            if ch == "wxpusher":
                self.assertEqual(payload["uids"], ["UID_1", "UID_2"])

    def test_send_error_recorded(self):
        with self.assertRaises(notify.NotifyError):
            notify.send(self.conn, "t", ["x"], post=FakePost({"code": 900, "msg": "用户未实名"}))
        self.assertIn("用户未实名", notify.public_config(self.conn)["last_result"])
        self.assertNotIn("notify_token", notify.public_config(self.conn))  # 不向前端返回 token

    def test_token_blank_keeps_existing(self):
        notify.save_config(self.conn, {"notify_token": ""})
        self.assertEqual(notify.get_config(self.conn)["notify_token"], "tok123456789")

    def test_alarm_events_baseline_new_and_recovery(self):
        monitor.upsert_alarms(self.conn, [alarm("old1", "g1", True), alarm("old2", "g2", False)])
        ev = notify.collect_events(self.conn, {}, now_hm=600)
        self.assertIn("1", ev[1][0])  # 首次只发汇总：当前 1 条进行中
        self.assertEqual(len([line for line in ev[1] if line.startswith("🔴")]), 0)
        self.assertIsNone(notify.collect_events(self.conn, {}, now_hm=600))  # 没有变化不再发送

        monitor.upsert_alarms(self.conn, [alarm("new1", "g2", True, name="电网过压"),
                                          alarm("p1", "g2", True, level="Total_FaultLevel_Prompt")])
        title, lines = notify.collect_events(self.conn, {}, now_hm=600)
        self.assertIn("1 条告警", title)
        self.assertTrue(any("二号站" in line and "电网过压" in line for line in lines))
        self.assertFalse(any("p1" in line for line in lines))  # 提示级不推送

        monitor.upsert_alarms(self.conn, [alarm("new1", "g2", False, name="电网过压")])
        title, lines = notify.collect_events(self.conn, {}, now_hm=600)
        self.assertTrue(any(line.startswith("🟢") and "电网过压" in line for line in lines))
        self.assertIsNone(notify.collect_events(self.conn, {}, now_hm=600))

    def test_old_alarms_summarized(self):
        db.set_setting(self.conn, "alarm_baseline_done", "1")
        orig = clock.now_iso
        clock.now_iso = lambda: "2026-11-23T12:00:00"
        try:
            old = alarm("o1", "g1", True); old["happened"] = "2026-10-01 08:00:00"
            new = alarm("n1", "g2", True, name="电网过压"); new["happened"] = "2026-11-23 11:30:00"
            monitor.upsert_alarms(self.conn, [old, new])
            title, lines = notify.collect_events(self.conn, {}, now_hm=720)
        finally:
            clock.now_iso = orig
        self.assertEqual(len([line for line in lines if line.startswith("🔴")]), 1)
        self.assertTrue(any("另有 **1** 条较早" in line for line in lines))
        self.assertIn("2 条告警", title)

    def test_status_changes_respect_daytime(self):
        db.set_setting(self.conn, "alarm_baseline_done", "1")
        monitor.update_station_status(self.conn, "g1", status=0)
        monitor.update_station_status(self.conn, "g2", status=2)
        prev = {self.a["id"]: 1, self.b["id"]: 1}
        self.assertEqual(len(notify.collect_events(self.conn, prev, now_hm=10 * 60)[1]) - 1, 2)  # 白天：离线+故障
        ev = notify.collect_events(self.conn, prev, now_hm=22 * 60)  # 夜间：只报故障
        body = [line for line in ev[1] if line.startswith("⚠️")]
        self.assertEqual(len(body), 1)
        self.assertIn("故障", body[0])
        self.assertIsNone(notify.collect_events(self.conn, {self.a["id"]: 0, self.b["id"]: 2}, now_hm=600))

    def test_disabled_options(self):
        notify.save_config(self.conn, {"notify_alarm": False, "notify_status": False, "notify_recovery": False})
        monitor.upsert_alarms(self.conn, [alarm("x", "g1", True)])
        monitor.update_station_status(self.conn, "g1", status=2)
        self.assertIsNone(notify.collect_events(self.conn, {self.a["id"]: 1}, now_hm=600))

    def test_daily_report_once_per_day(self):
        orig = clock.today
        clock.today = lambda: date(2026, 11, 23)
        try:
            db.upsert_record(self.conn, self.a["id"], "2026-11-23", 450)
            db.upsert_record(self.conn, self.b["id"], "2026-11-23", 120)
            fp = FakePost()
            self.assertFalse(notify.maybe_send_daily(self.conn, post=fp, now_hm=19 * 60))  # 未到 20:00
            self.assertTrue(notify.maybe_send_daily(self.conn, post=fp, now_hm=20 * 60 + 5))
            self.assertFalse(notify.maybe_send_daily(self.conn, post=fp, now_hm=21 * 60))  # 今天已发
            self.assertEqual(len(fp.calls), 1)
            self.assertIn("570", fp.calls[0][1]["title"])
            self.assertIn("一号站", fp.calls[0][1]["content"])
        finally:
            clock.today = orig

    def test_bad_time_rejected(self):
        with self.assertRaises(db.ValidationError):
            notify.save_config(self.conn, {"notify_daily_time": "25:00"})
        with self.assertRaises(db.ValidationError):
            notify.save_config(self.conn, {"notify_channel": "sms"})


if __name__ == "__main__":
    unittest.main()
