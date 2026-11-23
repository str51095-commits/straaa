"""电站运行监控：运行状态、实时功率、告警、发电效率对比、月/年报表。"""
import csv
import io
import statistics

from . import clock, db

# SEMS+ 电站状态码（来源：开源项目对 SEMS+ 接口的实测记录）
STATUS = {0: ("offline", "离线"), 1: ("running", "正常运行"), 2: ("fault", "故障"),
          3: ("waiting", "等待"), 11: ("building", "建设中")}
LOW_EFF_RATIO = 0.6   # 今日等效小时低于同类电站中位数的 60% 视为“发电偏低”


def status_info(code, linked):
    if not linked:
        return "manual", "未接入"
    if code is None:
        return "unknown", "未知"
    return STATUS.get(int(code), ("unknown", "未知"))


# ---------- 写入（由同步任务调用） ----------

def update_station_status(conn, goodwe_id, status=None, power_w=None):
    sets, args = ["status_time = ?"], [clock.now_iso()]
    if status is not None:
        sets.append("status = ?")
        args.append(int(status))
    if power_w is not None:
        sets.append("power_w = ?")
        args.append(float(power_w))
    with conn:
        conn.execute(f"UPDATE stations SET {', '.join(sets)} WHERE goodwe_station_id = ?", (*args, goodwe_id))


def upsert_alarms(conn, rows):
    now = clock.now_iso()
    with conn:
        for a in rows:
            if not a.get("id"):
                continue
            conn.execute(
                """INSERT INTO alarms (warning_id, goodwe_station_id, station_name, device_sn, device_name, code,
                                       name, level, active, happened, recovered, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(warning_id) DO UPDATE SET active = excluded.active, recovered = excluded.recovered,
                       name = excluded.name, level = excluded.level, updated = excluded.updated""",
                (str(a["id"]), a.get("station_id") or "", a.get("station_name") or "", a.get("device_sn") or "",
                 a.get("device_name") or "", str(a.get("code") or ""), a.get("name") or "", a.get("level") or "",
                 1 if a.get("active") else 0, a.get("happened") or "", a.get("recovered") or "", now))
    return len(rows)


# ---------- 读取 ----------

def list_alarms(conn, active_only=False, goodwe_id=None, limit=200):
    sql = """SELECT a.*, s.id AS local_station_id, COALESCE(s.name, a.station_name) AS station
             FROM alarms a LEFT JOIN stations s ON s.goodwe_station_id = a.goodwe_station_id
             WHERE 1=1"""
    args = []
    if active_only:
        sql += " AND a.active = 1"
    if goodwe_id:
        sql += " AND a.goodwe_station_id = ?"
        args.append(goodwe_id)
    sql += " ORDER BY a.active DESC, a.happened DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def overview(conn, today=None):
    """全部电站的运行概况 + 每座电站的状态、功率、今日/本月发电与效率。"""
    today = today or clock.today()
    t = today.isoformat()
    month = t[:7]
    today_kwh = {r[0]: r[1] for r in conn.execute("SELECT station_id, kwh FROM generation WHERE day = ?", (t,))}
    month_kwh = {r[0]: r[1] for r in conn.execute(
        "SELECT station_id, kwh FROM station_month WHERE month = ? AND n > 0", (month,))}
    alarm_cnt = {r[0]: r[1] for r in conn.execute(
        "SELECT goodwe_station_id, COUNT(*) FROM alarms WHERE active = 1 GROUP BY goodwe_station_id")}
    rows = []
    for s in conn.execute("SELECT * FROM stations ORDER BY id"):
        s = dict(s)
        linked = bool(s.get("goodwe_station_id"))
        key, label = status_info(s.get("status"), linked)
        cap = s["capacity_kwp"] or 0
        kwh = today_kwh.get(s["id"])
        rows.append({
            "id": s["id"], "name": s["name"], "capacity_kwp": cap, "location": s.get("location") or "",
            "linked": linked, "status": key, "status_label": label, "status_time": s.get("status_time"),
            "power_w": s.get("power_w") if linked else None,
            "today_kwh": round(kwh, 2) if kwh is not None else None,
            "today_hours": round(kwh / cap, 2) if kwh is not None and cap else None,
            "month_kwh": round(month_kwh.get(s["id"], 0), 2),
            "month_hours": round(month_kwh.get(s["id"], 0) / cap, 1) if cap else None,
            "active_alarms": alarm_cnt.get(s.get("goodwe_station_id"), 0) if linked else 0,
            "low_eff": False,
        })
    hours = [r["today_hours"] for r in rows if r["today_hours"]]
    median = statistics.median(hours) if len(hours) >= 3 else None
    for r in rows:
        if median and median > 0.5 and r["today_hours"] is not None and r["today_hours"] < median * LOW_EFF_RATIO:
            r["low_eff"] = True
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "date": t,
        "summary": {
            "total": len(rows), "counts": counts,
            "active_alarms": sum(r["active_alarms"] for r in rows),
            "low_eff": sum(1 for r in rows if r["low_eff"]),
            "power_kw": round(sum((r["power_w"] or 0) for r in rows) / 1000, 2),
            "today_kwh": round(sum((r["today_kwh"] or 0) for r in rows), 2),
            "median_hours": round(median, 2) if median else None,
        },
        "stations": rows,
    }


def report_csv(conn, period):
    """月报（YYYY-MM）或年报（YYYY）：每座电站的发电量、等效小时、收益。"""
    import re
    if not re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2]))?", period or ""):
        raise db.ValidationError("报表周期格式应为 YYYY-MM 或 YYYY")
    if len(period) == 7:
        rows = conn.execute("""SELECT station_id, SUM(kwh), SUM(n) FROM station_month
                               WHERE month = ? AND n > 0 GROUP BY station_id""", (period,))
    elif len(period) == 4:
        rows = conn.execute("""SELECT station_id, SUM(kwh), SUM(n) FROM station_month
                               WHERE substr(month,1,4) = ? AND n > 0 GROUP BY station_id""", (period,))
    else:
        raise db.ValidationError("报表周期格式应为 YYYY-MM 或 YYYY")
    data = {sid: (kwh, n) for sid, kwh, n in rows}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"报表周期：{period}"])
    w.writerow(["电站", "装机容量(kWp)", "发电量(kWh)", "等效利用小时(h)", "电价(元/kWh)", "收益(元)", "有数据天数", "CO₂减排(kg)"])
    tot_kwh = tot_rev = tot_cap = 0.0
    for s in conn.execute("SELECT id, name, capacity_kwp, price FROM stations ORDER BY id"):
        kwh, n = data.get(s["id"], (0, 0))
        cap = s["capacity_kwp"] or 0
        rev = kwh * (s["price"] or 0)
        tot_kwh += kwh
        tot_rev += rev
        tot_cap += cap
        w.writerow([s["name"], cap, round(kwh, 2), round(kwh / cap, 2) if cap else "", s["price"],
                    round(rev, 2), n, round(kwh * db.CO2_PER_KWH, 1)])
    w.writerow(["合计", round(tot_cap, 2), round(tot_kwh, 2), round(tot_kwh / tot_cap, 2) if tot_cap else "",
                "", round(tot_rev, 2), "", round(tot_kwh * db.CO2_PER_KWH, 1)])
    return "﻿" + out.getvalue()
