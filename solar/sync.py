"""把固德威云平台的发电量同步到本地数据库：定时同步当日、导入电站、补录历史。"""
import os
import threading
from datetime import date

from . import clock, db, monitor
from .goodwe import DEFAULT_LOGIN_URL, GoodWeClient, GoodWeError

SYNC_NOTE = "固德威自动同步"
BACKFILL_NOTE = "固德威历史补录"
MAX_BACKFILL_YEARS = 10


def make_client_from_env():
    account, pwd = os.environ.get("GOODWE_ACCOUNT"), os.environ.get("GOODWE_PASSWORD")
    if not (account and pwd):
        return None
    if (os.environ.get("GOODWE_API") or "semsplus").lower() == "legacy":
        return GoodWeClient(account, pwd, os.environ.get("GOODWE_LOGIN_URL") or DEFAULT_LOGIN_URL)
    from .semsplus import from_env
    return from_env()


class GoodWeSync:
    def __init__(self, conn, lock, client=None, interval_min=15):
        self.conn = conn
        self.lock = lock
        self.client = client
        self.interval = max(1, interval_min) * 60
        self.status = {"enabled": client is not None, "interval_min": interval_min,
                       "api": type(client).__name__ if client else None,
                       "last_run": None, "ok": None, "message": "尚未同步", "stations": [],
                       "paused": False, "backfill": None,
                       "can_discover": hasattr(client, "list_stations"),
                       "can_backfill": hasattr(client, "daily_history")}
        self._run_lock = threading.Lock()
        self._stop = threading.Event()

    @classmethod
    def from_env(cls, conn, lock):
        interval = int(os.environ.get("GOODWE_SYNC_MINUTES") or 15)
        return cls(conn, lock, make_client_from_env(), interval)

    def _linked_stations(self):
        with self.lock:
            return [s for s in db.list_stations(self.conn) if s.get("goodwe_station_id")]

    # ---------- 当日同步 ----------
    def run_once(self, today=None, manual=True):
        if not self.client:
            self.status.update(ok=False, message="未配置固德威账号（设置环境变量 GOODWE_ACCOUNT / GOODWE_PASSWORD）")
            return self.status
        if not manual and self.status["paused"]:
            return self.status  # 账号密码错误后暂停自动同步，避免账号被锁
        if not self._run_lock.acquire(blocking=False):
            return self.status
        try:
            today = today or clock.today()
            stations = self._linked_stations()
            results = []
            extra_errors = []
            # 电站运行状态（一次请求拿到所有电站）
            if stations and hasattr(self.client, "list_stations"):
                try:
                    remote = {r["id"]: r for r in self.client.list_stations()}
                    with self.lock:
                        for st in stations:
                            r = remote.get(st["goodwe_station_id"])
                            if r and r.get("status") is not None:
                                monitor.update_station_status(self.conn, st["goodwe_station_id"], status=r["status"])
                except GoodWeError as e:
                    extra_errors.append(f"读取电站状态失败: {e}")
            for st in stations:
                item = {"station": st["name"], "goodwe_station_id": st["goodwe_station_id"]}
                try:
                    data = self.client.station_today(st["goodwe_station_id"])
                    item.update(data)
                    with self.lock:
                        if data["today_kwh"] is not None:
                            db.upsert_record(self.conn, st["id"], today.isoformat(), data["today_kwh"], SYNC_NOTE)
                        monitor.update_station_status(self.conn, st["goodwe_station_id"], power_w=data.get("power_w"))
                    item["ok"] = True
                except GoodWeError as e:
                    item.update(ok=False, error=str(e))
                    if _is_auth_error(e):
                        results.append(item)
                        self.status["paused"] = True
                        break  # 密码错误时不再逐个电站重试
                except db.ValidationError as e:
                    item.update(ok=False, error=str(e))
                results.append(item)
            else:
                self.status["paused"] = False
            # 告警
            if stations and not self.status["paused"] and hasattr(self.client, "alarms"):
                try:
                    alarms = self.client.alarms([s["goodwe_station_id"] for s in stations])
                    with self.lock:
                        monitor.upsert_alarms(self.conn, alarms)
                except GoodWeError as e:
                    extra_errors.append(f"读取告警失败: {e}")
            self.status["extra_errors"] = extra_errors
            failed = [r for r in results if not r["ok"]]
            if self.status["paused"]:
                msg, ok = "固德威账号或密码错误，自动同步已暂停。修改配置后重启平台，或点“立即同步”重试", False
            elif not stations:
                msg, ok = "没有填写“固德威电站 ID”的电站（可点“从固德威导入电站”）", False
            elif failed:
                msg, ok = f"{len(results) - len(failed)}/{len(results)} 座电站同步成功；{failed[0]['error']}", False
            else:
                msg, ok = f"{len(results)} 座电站同步成功", True
            self.status.update(last_run=clock.now_iso(), ok=ok, message=msg, stations=results)
            return self.status
        finally:
            self._run_lock.release()

    # ---------- 导入电站 ----------
    def discover(self):
        """从固德威账号读取电站列表；本地没有的电站自动创建并关联。"""
        if not self.status["can_discover"]:
            raise db.ValidationError("当前接口不支持读取电站列表")
        remote = self.client.list_stations()
        created, linked = [], 0
        with self.lock:
            existing = {s["goodwe_station_id"] for s in db.list_stations(self.conn) if s.get("goodwe_station_id")}
            for r in remote:
                if not r.get("id"):
                    continue
                if r["id"] in existing:
                    linked += 1
                    continue
                db.create_station(self.conn, {"name": r.get("name") or r["id"], "capacity_kwp": r.get("capacity_kwp") or 0,
                                              "goodwe_station_id": r["id"],
                                              "price": db.default_price(self.conn)})
                created.append(r.get("name") or r["id"])
        return {"found": len(remote), "created": len(created), "already": linked, "names": created[:50]}

    # ---------- 补录历史 ----------
    def start_backfill(self, years=None):
        if not self.status["can_backfill"]:
            raise db.ValidationError("当前接口不支持读取历史数据")
        bf = self.status.get("backfill")
        if bf and bf.get("running"):
            return self.status
        self.status["backfill"] = {"running": True, "done": 0, "total": 0, "days": 0,
                                   "message": "准备中…", "errors": []}
        threading.Thread(target=self._backfill, args=(years,), name="goodwe-backfill", daemon=True).start()
        return self.status

    def _backfill(self, years):
        bf = self.status["backfill"]
        try:
            stations = self._linked_stations()
            bf["total"] = len(stations)
            today = clock.today()
            for st in stations:
                bf["message"] = f"正在补录：{st['name']}"
                try:
                    bf["days"] += self._backfill_station(st, today, years)
                except GoodWeError as e:
                    bf["errors"].append(f"{st['name']}: {e}")
                    if _is_auth_error(e):
                        break
                bf["done"] += 1
            bf["message"] = (f"补录完成：{bf['done']} 座电站，共写入 {bf['days']} 天数据"
                             + (f"；{len(bf['errors'])} 座失败" if bf["errors"] else ""))
        except Exception as e:  # noqa: BLE001
            bf["message"] = f"补录出错: {e}"
        finally:
            bf["running"] = False

    def _backfill_station(self, st, today, years):
        if st.get("commission_date"):
            first_year = int(st["commission_date"][:4])
        else:
            first_year = today.year - (years or MAX_BACKFILL_YEARS) + 1
        if years:
            first_year = max(first_year, today.year - years + 1)
        written = 0
        empty_streak = 0
        for year in range(today.year, first_year - 1, -1):  # 从今年往前，遇到连续两年无数据就停
            start, end = date(year, 1, 1), min(date(year, 12, 31), today)
            series = self.client.daily_history(st["goodwe_station_id"], start, end)
            rows = [(d, v) for d, v in series.items() if v and v > 0 and d <= today.isoformat()]
            if rows:
                empty_streak = 0
                with self.lock:
                    written += db.upsert_synced(self.conn, st["id"], rows, BACKFILL_NOTE)
            else:
                empty_streak += 1
                if empty_streak >= 2 and not st.get("commission_date"):
                    break
        return written

    # ---------- 后台定时 ----------
    def start(self):
        if not self.client:
            return

        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once(manual=False)
                except Exception as e:  # noqa: BLE001 - 后台线程不能崩
                    self.status.update(ok=False, message=f"同步异常: {e}")
                self._stop.wait(self.interval)
        threading.Thread(target=loop, name="goodwe-sync", daemon=True).start()

    def stop(self):
        self._stop.set()


def _is_auth_error(e):
    from .semsplus import GoodWeAuthError
    return isinstance(e, GoodWeAuthError) or "密码错误" in str(e) or "password error" in str(e).lower()
