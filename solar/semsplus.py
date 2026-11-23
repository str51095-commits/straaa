"""固德威 SEMS+（新平台，cn-semsplus.goodwe.com 等）接口客户端。

固德威未公开文档，接口细节参考开源项目 haids3/sems-plus-client（MIT）
与 TimSoethout/goodwe-sems-home-assistant：
  登录  POST https://{region}-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login
        body: account, pwd = base64(md5_hex(密码)), agreement, isChinese, isLocal
        返回 data: {token, uid, timestamp, api(数据网关地址), region}
  之后  请求头 token = 登录返回的 data(JSON)，X-Signature = base64(sha256(毫秒@uid@token)@毫秒)
  电站列表  POST {api}/sems-plant/api/portal/stations/page        {"current": 1, "size": 100}
  统计      POST {api}/sems-plant/api/stations/statistics          按日/月/年的发电量序列
  实时功率  GET  {api}/sems-plant/api/stations/flow?stationId=...   (kW)
中国区账号的登录方式尚未被社区完全确认，因此会依次尝试几个候选地址，并记住成功的那个。
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from .goodwe import GoodWeError

LOGIN_PATH = "/web/sems/sems-user/api/v1/auth/cross-login"
FALLBACK_API = "https://eu-gateway.semsportal.com/web/sems"
OK_CODES = {"0", "00000"}
RELOGIN_CODES = {"100001", "100002", "C0602"}
AUTH_FAIL_CODES = {"100005"}
RATE_LIMIT_CODES = {"GY0429"}
# 已在开源客户端中验证可用的统计项；proSystemTotalStats 为电站发电量
STAT_ITEMS = ["proConsumStats", "proGridStats", "proPurchaseStats", "proSelfConsumStats",
              "proDischarStats", "proCharStats", "proSystemTotalStats"]
DEFAULT_ITEM = "proSystemTotalStats"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class GoodWeAuthError(GoodWeError):
    """账号或密码被拒绝。"""


def hash_password(password):
    return base64.b64encode(hashlib.md5(password.encode()).hexdigest().encode()).decode()


def signature(uid, token, now_ms=None):
    ms = now_ms if now_ms is not None else round(time.time() * 1000)
    digest = hashlib.sha256(f"{ms}@{uid}@{token}".encode()).hexdigest()
    return base64.b64encode(f"{digest}@{ms}".encode()).decode()


def http_json(method, url, payload, headers, timeout):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise GoodWeError("固德威服务器限流（HTTP 429），请稍后再试") from e
        raise GoodWeError(f"固德威服务器返回 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GoodWeError(f"无法连接固德威服务器: {e}") from e
    except json.JSONDecodeError as e:
        raise GoodWeError("固德威服务器返回的不是 JSON") from e


def login_candidates(region):
    """依次尝试的 (登录地址, isChinese)。"""
    region = (region or "cn").lower()
    hosts = [f"https://{region}-semsplus.goodwe.com", "https://semsplus.goodwe.com"]
    chinese_first = region == "cn"
    out = []
    for host in hosts:
        for chinese in ((True, False) if chinese_first else (False, True)):
            out.append((host + LOGIN_PATH, chinese))
    return out


def _code(resp):
    c = resp.get("code")
    return "0" if c is None else str(c)


def _msg(resp):
    return resp.get("description") or resp.get("msg") or resp.get("message") or _code(resp)


class SemsPlusClient:
    def __init__(self, account, password, region="cn", timeout=20, request=http_json,
                 item=None, login_url=None):
        if not account or not password:
            raise GoodWeError("未配置固德威账号或密码")
        self.account = account
        self.password = password
        self.region = region or "cn"
        self.timeout = timeout
        self._request = request
        self.item = item or DEFAULT_ITEM
        self.token = None          # 登录返回的 data（含 api）
        self.login_used = None     # 成功的 (url, isChinese)
        self._candidates = ([(login_url, self.region == "cn")] if login_url
                            else login_candidates(self.region))
        self.login_log = []        # 诊断用：每次尝试的结果

    # ---------- 登录 ----------
    def login(self):
        tries = [self.login_used] if self.login_used else self._candidates
        auth_rejected = False
        last_err = None
        self.login_log = []
        for url, chinese in tries:
            host = url.split("/web/")[0]
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": host, "Referer": host + "/",
                "User-Agent": USER_AGENT,
                "Token": json.dumps({"uid": "", "timestamp": 0, "token": "", "client": "semsPlusWeb",
                                     "version": "", "language": "zh_CN" if chinese else "en"},
                                    separators=(",", ":")),
                "X-Signature": signature("", ""),
            }
            body = {"account": self.account, "pwd": hash_password(self.password),
                    "agreement": 1, "isChinese": chinese, "isLocal": False}
            try:
                resp = self._request("POST", url, body, headers, self.timeout)
            except GoodWeError as e:
                self.login_log.append((url, chinese, "网络错误", str(e)))
                last_err = e
                continue
            code = _code(resp)
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            self.login_log.append((url, chinese, code, _msg(resp)))
            if code in OK_CODES and data.get("token"):
                api = resp.get("api") or data.get("api") or FALLBACK_API
                self.token = {**data, "api": api.rstrip("/")}
                self.login_used = (url, chinese)
                return self.token
            if code in AUTH_FAIL_CODES:
                auth_rejected = True
            last_err = GoodWeError(f"登录失败: {_msg(resp)}")
        if self.login_used:  # 之前成功的地址失败了，下次重新全部尝试
            self.login_used = None
        if auth_rejected:
            raise GoodWeAuthError("固德威账号或密码错误")
        raise last_err or GoodWeError("登录失败")

    # ---------- 通用请求 ----------
    def _call(self, method, path, body=None, params=None, retry=True):
        if not self.token:
            self.login()
        url = self.token["api"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "token": json.dumps(self.token),
            "X-Signature": signature(str(self.token.get("uid", "")), str(self.token.get("token", ""))),
        }
        resp = self._request(method, url, body if method != "GET" else None, headers, self.timeout)
        code = _code(resp)
        if code in RELOGIN_CODES and retry:
            self.token = None
            return self._call(method, path, body, params, retry=False)
        if code in RATE_LIMIT_CODES:
            raise GoodWeError("固德威接口限流，请稍后再试")
        if code not in OK_CODES:
            raise GoodWeError(f"固德威接口出错（{code}）: {_msg(resp)}")
        return resp.get("data") if "data" in resp else resp

    # ---------- 业务接口 ----------
    def list_stations(self):
        out, page = [], 1
        while page <= 100:
            data = self._call("POST", "/sems-plant/api/portal/stations/page", {"current": page, "size": 100}) or {}
            rows = data.get("dataList") or data.get("records") or []
            for r in rows:
                out.append({"id": r.get("id") or r.get("stationId"), "name": r.get("name") or r.get("stationName"),
                            "capacity_kwp": _num(r.get("installedPower") or r.get("capacity")),
                            "status": _int(r.get("status"))})
            total = int(data.get("total") or 0)
            if not rows or len(out) >= total:
                break
            page += 1
        return out

    def statistics(self, station_id, start, end, dimension="day"):
        """返回 {统计项: {日期(YYYY-MM-DD/YYYY-MM/YYYY): kWh}}。区间不能跨自然年。"""
        items = list(STAT_ITEMS)
        if self.item not in items:
            items.append(self.item)
        body = {"stationId": station_id, "isReport": False, "items": items, "dimension": dimension,
                "startTime": f"{start.isoformat()} 00:00:00", "endTime": f"{end.isoformat()} 23:59:59"}
        data = self._call("POST", "/sems-plant/api/stations/statistics", body) or {}
        rows = data.get("dataList") if isinstance(data, dict) else None
        out = {}
        for row in rows or []:
            series = {}
            for p in row.get("statisticsList") or []:
                key = _norm_date(p.get("date"), start, dimension)
                val = _num(p.get("val"))
                if key and val is not None:
                    series[key] = val
            out[row.get("item")] = series
        return out

    def daily_history(self, station_id, start, end):
        """按自然年分段获取每日发电量 {YYYY-MM-DD: kWh}。"""
        result, cur = {}, start
        while cur <= end:
            year_end = min(date(cur.year, 12, 31), end)
            stats = self.statistics(station_id, cur, year_end, "day")
            result.update(stats.get(self.item) or {})
            cur = year_end + timedelta(days=1)
        return result

    def alarms(self, station_ids, max_pages=5, page_size=100):
        """最近的告警（含已恢复），按页读取。返回统一字段的列表。"""
        out = []
        ids = list(station_ids)
        for i in range(0, len(ids), 50):  # 每次最多 50 座电站
            chunk = ids[i:i + 50]
            for page in range(1, max_pages + 1):
                data = self._call("POST", "/sems-alarm/api/v2/alarm/page",
                                  {"pageIndex": page, "pageSize": page_size, "stationIds": chunk}) or {}
                rows = data.get("dataList") or []
                for r in rows:
                    out.append({
                        "id": r.get("warningid") or r.get("id"),
                        "station_id": r.get("stationId"),
                        "station_name": r.get("stationname"),
                        "device_sn": r.get("devicesn"),
                        "device_name": r.get("deviceName"),
                        "code": r.get("warning_code") or r.get("error_code"),
                        "name": r.get("warningNameZh") or r.get("warningNameEn") or r.get("warningname"),
                        "level": r.get("alarmLevel"),
                        "active": str(r.get("status")) == "0",
                        "happened": r.get("happentimes") or _ms_to_str(r.get("happentime")),
                        "recovered": r.get("recoverytimes") or _ms_to_str(r.get("recoverytime")),
                    })
                if len(rows) < page_size or page * page_size >= int(data.get("total") or 0):
                    break
        return out

    def current_power_w(self, station_id):
        data = self._call("GET", "/sems-plant/api/stations/flow", params={"stationId": station_id}) or {}
        kw = _num(data.get("pSystem") if data.get("pSystem") is not None else data.get("pAc"))
        return None if kw is None else round(kw * 1000, 1)

    def station_today(self, station_id, today=None):
        from . import clock
        today = today or clock.today()
        stats = self.statistics(station_id, today.replace(day=1), today, "day")
        if self.item not in stats:
            raise GoodWeError(f"电站 {station_id} 没有返回发电量数据，请检查电站 ID 或权限")
        series = stats[self.item]
        try:
            power = self.current_power_w(station_id)
        except GoodWeError:
            power = None
        return {
            "name": None, "capacity_kwp": None,
            "today_kwh": series.get(today.isoformat()),
            "month_kwh": round(sum(series.values()), 2),
            "total_kwh": None,
            "power_w": power,
        }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _ms_to_str(v):
    try:
        from datetime import datetime
        return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _norm_date(raw, start, dimension):
    if raw is None:
        return None
    s = str(raw).strip().replace("/", "-").replace(".", "-")
    if s.isdigit() and len(s) >= 12:  # 毫秒时间戳
        from datetime import datetime
        s = datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d")
    parts = s.split(" ")[0].split("T")[0].split("-")
    try:
        nums = [int(p) for p in parts if p]
    except ValueError:
        return None
    if dimension == "day":
        if len(nums) >= 3:
            y, m, d = nums[:3]
        elif len(nums) == 2:
            y, (m, d) = start.year, nums
        elif len(nums) == 1:
            y, m, d = start.year, start.month, nums[0]
        else:
            return None
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None
    if dimension == "month":
        if len(nums) >= 2:
            return f"{nums[0]:04d}-{nums[1]:02d}"
        return f"{start.year:04d}-{nums[0]:02d}" if nums else None
    return f"{nums[0]:04d}" if nums else None


def from_env():
    return SemsPlusClient(os.environ.get("GOODWE_ACCOUNT"), os.environ.get("GOODWE_PASSWORD"),
                          region=os.environ.get("GOODWE_REGION") or "cn",
                          item=os.environ.get("GOODWE_ITEM") or None,
                          login_url=os.environ.get("GOODWE_LOGIN_URL") or None)
