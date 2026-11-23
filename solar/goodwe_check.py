"""固德威连接诊断：在服务器上运行  sudo solar-goodwe-check  [电站ID]

逐步检查登录、电站列表、发电量统计，把结果（不含密码和令牌）打印出来，方便截图排查。
"""
import os
import sys
from datetime import date

ENV_FILE = "/etc/solar-ems.env"


def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def mask(s):
    s = str(s or "")
    return s[:3] + "***" + s[-2:] if len(s) > 6 else "***"


def main(argv):
    load_env(ENV_FILE)
    from . import clock
    from .goodwe import GoodWeError
    from .semsplus import SemsPlusClient

    account, pwd = os.environ.get("GOODWE_ACCOUNT"), os.environ.get("GOODWE_PASSWORD")
    region = os.environ.get("GOODWE_REGION") or "cn"
    print(f"账号: {mask(account)}   区域: {region}   时区今天: {clock.today()}")
    if not (account and pwd):
        print("❌ 未配置 GOODWE_ACCOUNT / GOODWE_PASSWORD（在 /etc/solar-ems.env 中填写）")
        return 1
    c = SemsPlusClient(account, pwd, region=region, item=os.environ.get("GOODWE_ITEM") or None,
                       login_url=os.environ.get("GOODWE_LOGIN_URL") or None)

    print("\n[1] 登录")
    try:
        tok = c.login()
        ok = True
    except GoodWeError as e:
        ok, err = False, e
    for url, chinese, code, msg in c.login_log:
        print(f"  {'✅' if code in ('0', '00000') else '✖'} {url.split('/web/')[0]} isChinese={chinese} -> {code} {msg}")
    if not ok:
        print(f"❌ 登录失败: {err}")
        return 1
    print(f"  数据网关: {tok.get('api')}   区域: {tok.get('region')}")

    print("\n[2] 电站列表")
    try:
        stations = c.list_stations()
        print(f"  共 {len(stations)} 座")
        for s in stations[:15]:
            print(f"  - {s['id']}  {s['name']}  {s['capacity_kwp']} kWp")
    except GoodWeError as e:
        stations = []
        print(f"  ✖ {e}")

    sid = argv[1] if len(argv) > 1 else (stations[0]["id"] if stations else None)
    if not sid:
        print("\n未指定电站 ID，结束。用法：sudo solar-goodwe-check 电站ID")
        return 0
    today = clock.today()
    print(f"\n[3] 电站 {sid} 本月每日统计（请对照 App 中的今日发电量，确认哪一项是发电量）")
    try:
        stats = c.statistics(sid, today.replace(day=1), today, "day")
        if not stats:
            print("  ✖ 没有返回任何统计项")
        for item, series in stats.items():
            mark = "  ← 当前用作发电量" if item == c.item else ""
            print(f"  {item}: 今日 {series.get(today.isoformat())}  本月合计 {round(sum(series.values()), 2)}"
                  f"  ({len(series)} 天){mark}")
    except GoodWeError as e:
        print(f"  ✖ {e}")

    print("\n[4] 当前功率")
    try:
        print(f"  {c.current_power_w(sid)} W")
    except GoodWeError as e:
        print(f"  ✖ {e}")

    print(f"\n[5] 去年 1 月历史数据（检查能否补录）")
    try:
        y = today.year - 1
        hist = c.daily_history(sid, date(y, 1, 1), date(y, 1, 31))
        vals = [v for v in hist.values() if v]
        print(f"  {len(hist)} 天有记录，合计 {round(sum(vals), 1)} kWh")
    except GoodWeError as e:
        print(f"  ✖ {e}")
    print("\n诊断完成。请把以上内容截图发给开发者。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
