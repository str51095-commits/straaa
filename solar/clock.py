"""“今天”按哪个时区算。云服务器通常是 UTC，需设置 SOLAR_TZ=Asia/Shanghai。"""
import os
from datetime import date, datetime


def today():
    tz = os.environ.get("SOLAR_TZ")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:  # noqa: BLE001 - 时区名错误或缺少 tzdata 时退回系统时间
            pass
    return date.today()


def now_iso():
    tz = os.environ.get("SOLAR_TZ")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).replace(tzinfo=None).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    return datetime.now().isoformat(timespec="seconds")
