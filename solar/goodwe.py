"""固德威 (GoodWe) SEMS 云平台对接。

使用 SEMS Portal 账号（与 SEMS App 登录的账号相同）获取电站当日发电量。
接口流程参考开源社区的 SEMS 集成（Home Assistant / openHAB）：
  1. POST {login_url}  v2/Common/CrossLogin  -> 返回 token 和所属区域的 API 地址
  2. POST {api}v2/PowerStation/GetMonitorDetailByPowerstationId -> kpi.power 为当日发电量 (kWh)

固德威未公开官方文档，若拿到官方 OpenAPI，只需替换本文件中的请求逻辑。
"""
import json
import urllib.error
import urllib.request

DEFAULT_LOGIN_URL = "https://www.semsportal.com/api/v2/Common/CrossLogin"
INITIAL_TOKEN = {"version": "v2.1.0", "client": "ios", "language": "en"}
TOKEN_EXPIRED_CODES = {100001, 100002}


class GoodWeError(Exception):
    pass


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise GoodWeError(f"SEMS 接口返回 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GoodWeError(f"无法连接 SEMS 服务器: {e}") from e
    except json.JSONDecodeError as e:
        raise GoodWeError("SEMS 返回的不是 JSON") from e


class GoodWeClient:
    def __init__(self, account, password, login_url=DEFAULT_LOGIN_URL, timeout=20, post=_post_json):
        if not account or not password:
            raise GoodWeError("未配置固德威账号或密码")
        self.account = account
        self.password = password
        self.login_url = login_url
        self.timeout = timeout
        self._post = post
        self.token = None
        self.api_base = None

    def login(self):
        resp = self._post(self.login_url, {"account": self.account, "pwd": self.password},
                          {"Token": json.dumps(INITIAL_TOKEN)}, self.timeout)
        if resp.get("hasError") or resp.get("code") not in (0, "0", None) or not resp.get("data"):
            raise GoodWeError(f"登录失败: {resp.get('msg') or resp.get('code')}")
        api = resp.get("api") or (resp.get("components") or {}).get("api")
        if not api:
            raise GoodWeError("登录成功但未返回 API 地址")
        self.token = resp["data"]
        self.api_base = api if api.endswith("/") else api + "/"

    def _call(self, path, payload, retry=True):
        if not self.token:
            self.login()
        resp = self._post(self.api_base + path, payload, {"Token": json.dumps(self.token)}, self.timeout)
        code = resp.get("code")
        try:
            code = int(code) if code is not None else 0
        except (TypeError, ValueError):
            code = -1
        if code in TOKEN_EXPIRED_CODES and retry:
            self.token = None
            return self._call(path, payload, retry=False)
        if resp.get("hasError") or code != 0:
            raise GoodWeError(f"接口 {path} 出错: {resp.get('msg') or code}")
        return resp.get("data") or {}

    def station_today(self, station_id):
        """返回 {'name', 'capacity_kwp', 'today_kwh', 'month_kwh', 'total_kwh', 'power_w'}。"""
        data = self._call("v2/PowerStation/GetMonitorDetailByPowerstationId", {"powerStationId": station_id})
        kpi = data.get("kpi") or {}
        info = data.get("info") or {}
        if "power" not in kpi:
            raise GoodWeError(f"电站 {station_id} 未返回发电数据，请检查电站 ID")
        return {
            "name": info.get("stationname"),
            "capacity_kwp": _num(info.get("capacity")),
            "today_kwh": _num(kpi.get("power")),
            "month_kwh": _num(kpi.get("month_generation")),
            "total_kwh": _num(kpi.get("total_power")),
            "power_w": _num(kpi.get("pac")),
        }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
