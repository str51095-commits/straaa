"""定时把固德威云平台的当日发电量同步到本地数据库。"""
import os
import threading
import time
from . import clock, db
from .goodwe import DEFAULT_LOGIN_URL, GoodWeClient, GoodWeError

SYNC_NOTE = "固德威自动同步"


class GoodWeSync:
    def __init__(self, conn, lock, client=None, interval_min=15):
        self.conn = conn
        self.lock = lock
        self.client = client
        self.interval = max(1, interval_min) * 60
        self.status = {"enabled": client is not None, "interval_min": interval_min,
                       "last_run": None, "ok": None, "message": "尚未同步", "stations": []}
        self._run_lock = threading.Lock()
        self._stop = threading.Event()

    @classmethod
    def from_env(cls, conn, lock):
        account, pwd = os.environ.get("GOODWE_ACCOUNT"), os.environ.get("GOODWE_PASSWORD")
        interval = int(os.environ.get("GOODWE_SYNC_MINUTES") or 15)
        client = None
        if account and pwd:
            client = GoodWeClient(account, pwd, os.environ.get("GOODWE_LOGIN_URL") or DEFAULT_LOGIN_URL)
        return cls(conn, lock, client, interval)

    def run_once(self, today=None):
        if not self.client:
            self.status.update(ok=False, message="未配置固德威账号（设置环境变量 GOODWE_ACCOUNT / GOODWE_PASSWORD）")
            return self.status
        if not self._run_lock.acquire(blocking=False):
            return self.status  # 已有同步在进行
        try:
            today = today or clock.today()
            with self.lock:
                stations = [s for s in db.list_stations(self.conn) if s.get("goodwe_station_id")]
            results = []
            for st in stations:
                item = {"station": st["name"], "goodwe_station_id": st["goodwe_station_id"]}
                try:
                    data = self.client.station_today(st["goodwe_station_id"])
                    item.update(data)
                    if data["today_kwh"] is not None:
                        with self.lock:
                            db.upsert_record(self.conn, st["id"], today.isoformat(), data["today_kwh"], SYNC_NOTE)
                    item["ok"] = True
                except (GoodWeError, db.ValidationError) as e:
                    item.update(ok=False, error=str(e))
                results.append(item)
            failed = [r for r in results if not r["ok"]]
            if not stations:
                msg, ok = "没有填写了“固德威电站 ID”的电站", False
            elif failed:
                msg, ok = f"{len(results) - len(failed)}/{len(results)} 座电站同步成功；{failed[0]['error']}", False
            else:
                msg, ok = f"{len(results)} 座电站同步成功", True
            self.status.update(last_run=clock.now_iso(),
                               ok=ok, message=msg, stations=results)
            return self.status
        finally:
            self._run_lock.release()

    def start(self):
        if not self.client:
            return
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:  # noqa: BLE001 - 后台线程不能崩
                    self.status.update(ok=False, message=f"同步异常: {e}")
                self._stop.wait(self.interval)
        threading.Thread(target=loop, name="goodwe-sync", daemon=True).start()

    def stop(self):
        self._stop.set()
