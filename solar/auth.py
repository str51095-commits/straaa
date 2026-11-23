"""单用户密码登录：设置环境变量 SOLAR_PASSWORD 即开启。

登录成功后下发 HMAC 签名的 Cookie（无需服务端存会话，重启不掉线）；
修改密码后旧 Cookie 自动失效。
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE = "solar_session"
SESSION_DAYS = 30
MAX_FAILS = 8          # 同一来源连续失败次数
LOCK_SECONDS = 300     # 超过后锁定时长


class Auth:
    def __init__(self, password, secret):
        self.password = password or ""
        self.enabled = bool(self.password)
        self._key = hashlib.sha256(f"{secret}\0{self.password}".encode()).digest()
        self._fails = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, conn):
        secret = os.environ.get("SOLAR_SECRET") or _stored_secret(conn)
        return cls(os.environ.get("SOLAR_PASSWORD"), secret)

    def _sign(self, payload):
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self):
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{exp}.{self._sign(exp)}"

    def verify(self, token):
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(exp))

    def locked(self, source):
        with self._lock:
            n, until = self._fails.get(source, (0, 0))
            return until > time.time()

    def check_password(self, source, password):
        ok = hmac.compare_digest(str(password or "").encode(), self.password.encode())
        with self._lock:
            if ok:
                self._fails.pop(source, None)
            else:
                n, _ = self._fails.get(source, (0, 0))
                n += 1
                self._fails[source] = (0, time.time() + LOCK_SECONDS) if n >= MAX_FAILS else (n, 0)
        return ok


def _stored_secret(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()
    if row:
        return row[0]
    value = secrets.token_hex(32)
    with conn:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('session_secret', ?)", (value,))
    return conn.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()[0]


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out
