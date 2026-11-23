"""HTTP 服务：REST API + 静态前端。仅依赖 Python 标准库。"""
import argparse
import json
import mimetypes
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .auth import COOKIE, SESSION_DAYS, Auth, parse_cookies
from .goodwe import GoodWeError
from .sync import GoodWeSync

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 20 * 1024 * 1024
PUBLIC_PATHS = {"/login.html", "/style.css"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class App:
    def __init__(self, db_path):
        self.conn = db.connect(db_path)
        self.lock = threading.Lock()
        self.sync = GoodWeSync.from_env(self.conn, self.lock)
        self.auth = Auth.from_env(self.conn)
        self.routes = [
            ("GET", r"/api/stations", self.stations_list),
            ("POST", r"/api/stations", self.stations_create),
            ("PUT", r"/api/stations/(\d+)", self.stations_update),
            ("DELETE", r"/api/stations/(\d+)", self.stations_delete),
            ("GET", r"/api/records", self.records_list),
            ("POST", r"/api/records", self.records_upsert),
            ("DELETE", r"/api/records/(\d+)", self.records_delete),
            ("POST", r"/api/import", self.records_import),
            ("GET", r"/api/export", self.records_export),
            ("GET", r"/api/stats/summary", self.stats_summary),
            ("GET", r"/api/stats/daily", self.stats_daily),
            ("GET", r"/api/stats/monthly", self.stats_monthly),
            ("GET", r"/api/stats/yearly", self.stats_yearly),
            ("POST", r"/api/demo", self.demo),
            ("GET", r"/api/sync/status", self.sync_status),
            ("POST", r"/api/sync/run", self.sync_run),
            ("POST", r"/api/sync/discover", self.sync_discover),
            ("POST", r"/api/sync/backfill", self.sync_backfill),
        ]

    # 每个 handler 返回 (status, body)；body 为 dict/list 时按 JSON 输出
    def stations_list(self, q, body):
        return 200, db.list_stations(self.conn)

    def stations_create(self, q, body):
        return 201, db.create_station(self.conn, body)

    def stations_update(self, q, body, sid):
        st = db.update_station(self.conn, int(sid), body)
        return (200, st) if st else (404, {"error": "电站不存在"})

    def stations_delete(self, q, body, sid):
        return (200, {"ok": True}) if db.delete_station(self.conn, int(sid)) else (404, {"error": "电站不存在"})

    def records_list(self, q, body):
        return 200, db.list_records(self.conn, _sid(q), q.get("start"), q.get("end"), int(q.get("limit") or 1000))

    def records_upsert(self, q, body):
        return 200, db.upsert_record(self.conn, int(body.get("station_id") or 0), body.get("day"),
                                     body.get("kwh"), body.get("note", ""))

    def records_delete(self, q, body, rid):
        return (200, {"ok": True}) if db.delete_record(self.conn, int(rid)) else (404, {"error": "记录不存在"})

    def records_import(self, q, body):
        ok, errors = db.import_csv(self.conn, int(body.get("station_id") or 0), body.get("csv", ""))
        return 200, {"imported": ok, "errors": errors[:50], "error_count": len(errors)}

    def records_export(self, q, body):
        return 200, ("text/csv; charset=utf-8", db.export_csv(self.conn, _sid(q)), "generation.csv")

    def stats_summary(self, q, body):
        return 200, db.summary(self.conn, _sid(q))

    def stats_daily(self, q, body):
        month = q.get("month")
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            raise db.ValidationError("month 格式应为 YYYY-MM")
        return 200, db.daily(self.conn, _sid(q), month)

    def stats_monthly(self, q, body):
        year = q.get("year")
        if year and not re.fullmatch(r"\d{4}", year):
            raise db.ValidationError("year 格式应为 YYYY")
        return 200, db.monthly(self.conn, _sid(q), year)

    def stats_yearly(self, q, body):
        return 200, db.yearly(self.conn, _sid(q))

    def demo(self, q, body):
        return 201, db.generate_demo(self.conn)

    def sync_status(self, q, body):
        return 200, self.sync.status

    def sync_run(self, q, body):
        return 200, self.sync.run_once()

    def sync_discover(self, q, body):
        self._need_sync()
        try:
            return 200, self.sync.discover()
        except GoodWeError as e:
            return 502, {"error": str(e)}

    def sync_backfill(self, q, body):
        self._need_sync()
        years = body.get("years")
        years = int(years) if str(years or "").isdigit() and 0 < int(years) <= 20 else None
        return 200, self.sync.start_backfill(years)

    def _need_sync(self):
        if not self.sync.client:
            raise db.ValidationError("未配置固德威账号（GOODWE_ACCOUNT / GOODWE_PASSWORD）")

    def dispatch(self, method, path, query, body):
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                if fn.__name__.startswith("sync_"):  # 同步会访问网络，内部自行加锁
                    return fn(query, body, *match.groups())
                with self.lock:
                    return fn(query, body, *match.groups())
        if any(re.fullmatch(p, path) for _, p, _ in self.routes):
            return 405, {"error": "方法不允许"}
        return 404, {"error": "接口不存在"}


def _sid(q):
    v = q.get("station_id")
    return int(v) if v and v.isdigit() else None


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SolarEMS/1.0"

        def log_message(self, fmt, *args):
            if os.environ.get("SOLAR_QUIET") != "1":
                super().log_message(fmt, *args)

        def _send(self, status, payload, content_type="application/json; charset=utf-8", filename=None,
                  headers=()):
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False).encode()
            elif isinstance(payload, str):
                data = payload.encode()
            else:
                data = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            if content_type.startswith(("application/json", "text/csv")):
                self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _handle(self):
            url = urlparse(self.path)
            auth = app.auth
            if url.path == "/api/auth":
                return self._send(200, {"enabled": auth.enabled, "logged_in": self._authed()})
            if url.path == "/api/login" and self.command == "POST":
                return self._login()
            if url.path == "/api/logout" and self.command == "POST":
                return self._send(200, {"ok": True}, headers=[("Set-Cookie", self._cookie("", 0))])
            if not self._authed() and url.path not in PUBLIC_PATHS:
                if url.path.startswith("/api/"):
                    return self._send(401, {"error": "请先登录"})
                return self._send(302, b"", "text/plain", headers=[("Location", "/login.html")])
            if url.path.startswith("/api/"):
                return self._api(url)
            return self._static(url.path)

        def _authed(self):
            return app.auth.verify(parse_cookies(self.headers.get("Cookie")).get(COOKIE))

        def _client(self):
            if os.environ.get("SOLAR_TRUST_PROXY") == "1" and self.headers.get("X-Forwarded-For"):
                return self.headers["X-Forwarded-For"].split(",")[-1].strip()
            return self.client_address[0]

        def _cookie(self, value, max_age):
            secure = (os.environ.get("SOLAR_SECURE_COOKIE") == "1"
                      or self.headers.get("X-Forwarded-Proto", "").lower() == "https")
            return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                    + ("; Secure" if secure else ""))

        def _login(self):
            if not app.auth.enabled:
                return self._send(200, {"ok": True})
            src = self._client()
            if app.auth.locked(src):
                return self._send(429, {"error": "密码错误次数过多，请 5 分钟后再试"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "请求格式错误"})
            if not isinstance(body, dict) or not app.auth.check_password(src, body.get("password")):
                time.sleep(1)  # 减慢暴力破解
                return self._send(401, {"error": "密码错误"})
            cookie = self._cookie(app.auth.issue(), SESSION_DAYS * 86400)
            return self._send(200, {"ok": True}, headers=[("Set-Cookie", cookie)])

        def _api(self, url):
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "请求体过大"})
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "JSON 格式错误"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "请求体必须是 JSON 对象"})
            try:
                status, result = app.dispatch(self.command, url.path, query, body)
            except db.ValidationError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self.log_error("internal error: %r", e)
                return self._send(500, {"error": "服务器内部错误"})
            if isinstance(result, tuple):  # 文件下载
                ctype, content, filename = result
                return self._send(status, content, ctype, filename)
            self._send(status, result)

        def _static(self, path):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "Not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

    return Handler


def main():
    parser = argparse.ArgumentParser(description="光伏电站能源管理平台")
    parser.add_argument("--host", default=os.environ.get("SOLAR_HOST"),
                        help="监听地址；未设密码时默认仅本机 127.0.0.1，设了密码默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SOLAR_PORT", 8000)))
    parser.add_argument("--db", default=os.environ.get("SOLAR_DB", "solar.db"))
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--allow-no-password", action="store_true",
                        help="允许在未设置密码时对外网/局域网开放（不安全）")
    args = parser.parse_args()
    app = App(args.db)
    if not args.host:
        args.host = "0.0.0.0" if app.auth.enabled else "127.0.0.1"
    if args.host not in LOOPBACK and not app.auth.enabled and not args.allow_no_password:
        raise SystemExit("拒绝启动：对外开放时必须设置登录密码（环境变量 SOLAR_PASSWORD）。"
                         "如确需无密码开放，请加 --allow-no-password")
    print("登录密码保护：" + ("已开启" if app.auth.enabled else "未开启（仅本机可访问）"))
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    if app.sync.client:
        print(f"固德威自动同步已开启，每 {app.sync.status['interval_min']} 分钟一次")
        app.sync.start()
    print(f"光伏能源管理平台已启动: http://127.0.0.1:{args.port}  (数据库: {os.path.abspath(args.db)})")
    print("关闭此窗口或按 Ctrl+C 即可停止平台")
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
