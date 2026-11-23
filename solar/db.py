"""数据存储与统计（SQLite，仅依赖 Python 标准库）。"""
import csv
import io
import math
import random
import sqlite3
from datetime import date, datetime, timedelta

from . import clock

# 中国电网平均排放因子 (kg CO2 / kWh)，生态环境部 2022 年度全国电网平均排放因子 0.5703
CO2_PER_KWH = 0.5703
# 每度电折合标准煤 (kg)
COAL_PER_KWH = 0.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    capacity_kwp REAL NOT NULL DEFAULT 0,
    location TEXT DEFAULT '',
    commission_date TEXT DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    goodwe_station_id TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS generation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    kwh REAL NOT NULL,
    note TEXT DEFAULT '',
    UNIQUE(station_id, day)
);
-- 覆盖索引：统计查询直接从索引读取，不必回表
CREATE INDEX IF NOT EXISTS idx_gen_day_cover ON generation(day, station_id, kwh);
CREATE INDEX IF NOT EXISTS idx_gen_station_cover ON generation(station_id, day, kwh);
"""

# 汇总表：由触发器自动维护，让“全部电站”的月/年/累计统计不必扫描全部日记录
ROLLUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_month (
    station_id INTEGER NOT NULL, month TEXT NOT NULL, kwh REAL NOT NULL DEFAULT 0, n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (station_id, month)
);
CREATE INDEX IF NOT EXISTS idx_station_month_month ON station_month(month, station_id, kwh, n);
CREATE TABLE IF NOT EXISTS day_total (
    day TEXT PRIMARY KEY, kwh REAL NOT NULL DEFAULT 0, n INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS trg_gen_ins AFTER INSERT ON generation BEGIN
    INSERT INTO station_month (station_id, month, kwh, n) VALUES (NEW.station_id, substr(NEW.day,1,7), NEW.kwh, 1)
        ON CONFLICT(station_id, month) DO UPDATE SET kwh = kwh + NEW.kwh, n = n + 1;
    INSERT INTO day_total (day, kwh, n) VALUES (NEW.day, NEW.kwh, 1)
        ON CONFLICT(day) DO UPDATE SET kwh = kwh + NEW.kwh, n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS trg_gen_del AFTER DELETE ON generation BEGIN
    UPDATE station_month SET kwh = kwh - OLD.kwh, n = n - 1
        WHERE station_id = OLD.station_id AND month = substr(OLD.day,1,7);
    UPDATE day_total SET kwh = kwh - OLD.kwh, n = n - 1 WHERE day = OLD.day;
END;
CREATE TRIGGER IF NOT EXISTS trg_gen_upd AFTER UPDATE OF station_id, day, kwh ON generation BEGIN
    UPDATE station_month SET kwh = kwh - OLD.kwh, n = n - 1
        WHERE station_id = OLD.station_id AND month = substr(OLD.day,1,7);
    UPDATE day_total SET kwh = kwh - OLD.kwh, n = n - 1 WHERE day = OLD.day;
    INSERT INTO station_month (station_id, month, kwh, n) VALUES (NEW.station_id, substr(NEW.day,1,7), NEW.kwh, 1)
        ON CONFLICT(station_id, month) DO UPDATE SET kwh = kwh + NEW.kwh, n = n + 1;
    INSERT INTO day_total (day, kwh, n) VALUES (NEW.day, NEW.kwh, 1)
        ON CONFLICT(day) DO UPDATE SET kwh = kwh + NEW.kwh, n = n + 1;
END;
"""

STATION_FIELDS = ("name", "capacity_kwp", "location", "commission_date", "price", "notes", "goodwe_station_id")


class ValidationError(ValueError):
    pass


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'station_month'").fetchone():
        conn.executescript(ROLLUP_SCHEMA)
        rebuild_rollups(conn)
    return conn


def rebuild_rollups(conn):
    """根据日记录重新计算汇总表（首次升级或校正浮点累计误差时使用）。"""
    with conn:
        conn.execute("DELETE FROM station_month")
        conn.execute("DELETE FROM day_total")
        conn.execute("""INSERT INTO station_month (station_id, month, kwh, n)
                        SELECT station_id, substr(day,1,7), SUM(kwh), COUNT(*) FROM generation GROUP BY 1, 2""")
        conn.execute("""INSERT INTO day_total (day, kwh, n)
                        SELECT day, SUM(kwh), COUNT(*) FROM generation GROUP BY day""")


def _migrate(conn):
    """给旧版数据库补充新增的列、清理旧索引。"""
    with conn:
        conn.execute("DROP INDEX IF EXISTS idx_generation_day")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stations)")}
    if "goodwe_station_id" not in cols:
        with conn:
            conn.execute("ALTER TABLE stations ADD COLUMN goodwe_station_id TEXT DEFAULT ''")


def _parse_day(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        # 兼容 2024/1/5 之类写法
        try:
            return datetime.strptime(str(value).strip(), "%Y/%m/%d").date().isoformat()
        except ValueError:
            raise ValidationError(f"日期格式错误: {value!r}（应为 YYYY-MM-DD）")


def _parse_kwh(value):
    try:
        kwh = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"发电量不是有效数字: {value!r}")
    if math.isnan(kwh) or math.isinf(kwh) or kwh < 0:
        raise ValidationError(f"发电量必须是非负数: {value!r}")
    return round(kwh, 3)


def _clean_station(data, partial=False):
    out = {}
    for key in STATION_FIELDS:
        if key not in data:
            continue
        val = data[key]
        if key in ("capacity_kwp", "price"):
            try:
                val = float(val or 0)
            except (TypeError, ValueError):
                raise ValidationError(f"{key} 必须是数字")
            if val < 0:
                raise ValidationError(f"{key} 不能为负数")
        elif key == "commission_date" and val:
            val = _parse_day(val)
        else:
            val = str(val or "").strip()
        out[key] = val
    if not partial and not out.get("name"):
        raise ValidationError("电站名称不能为空")
    if "name" in out and not out["name"]:
        raise ValidationError("电站名称不能为空")
    return out


# ---------------- 电站 ----------------

def list_stations(conn):
    rows = conn.execute(
        """SELECT s.*, COALESCE(a.total_kwh, 0) AS total_kwh, COALESCE(a.record_days, 0) AS record_days,
                  a.last_day
           FROM stations s LEFT JOIN (
               SELECT station_id, SUM(kwh) AS total_kwh, COUNT(*) AS record_days, MAX(day) AS last_day
               FROM generation GROUP BY station_id
           ) a ON a.station_id = s.id
           ORDER BY s.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_station(conn, station_id):
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    return dict(row) if row else None


def create_station(conn, data):
    clean = _clean_station(data)
    cols = ", ".join(clean)
    marks = ", ".join("?" for _ in clean)
    with conn:
        cur = conn.execute(f"INSERT INTO stations ({cols}) VALUES ({marks})", tuple(clean.values()))
    return get_station(conn, cur.lastrowid)


def update_station(conn, station_id, data):
    clean = _clean_station(data, partial=True)
    if clean:
        sets = ", ".join(f"{k} = ?" for k in clean)
        with conn:
            conn.execute(f"UPDATE stations SET {sets} WHERE id = ?", (*clean.values(), station_id))
    return get_station(conn, station_id)


def delete_station(conn, station_id):
    with conn:
        cur = conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    return cur.rowcount > 0


# ---------------- 发电记录 ----------------

def upsert_record(conn, station_id, day, kwh, note=""):
    if not get_station(conn, station_id):
        raise ValidationError("电站不存在")
    day = _parse_day(day)
    kwh = _parse_kwh(kwh)
    with conn:
        conn.execute(
            """INSERT INTO generation (station_id, day, kwh, note) VALUES (?, ?, ?, ?)
               ON CONFLICT(station_id, day) DO UPDATE SET kwh = excluded.kwh, note = excluded.note""",
            (station_id, day, kwh, str(note or "")),
        )
    return dict(conn.execute(
        "SELECT * FROM generation WHERE station_id = ? AND day = ?", (station_id, day)
    ).fetchone())


def delete_record(conn, record_id):
    with conn:
        cur = conn.execute("DELETE FROM generation WHERE id = ?", (record_id,))
    return cur.rowcount > 0


def list_records(conn, station_id=None, start=None, end=None, limit=1000):
    sql = """SELECT g.*, s.name AS station_name FROM generation g
             JOIN stations s ON s.id = g.station_id WHERE 1=1"""
    args = []
    if station_id:
        sql += " AND g.station_id = ?"
        args.append(station_id)
    if start:
        sql += " AND g.day >= ?"
        args.append(_parse_day(start))
    if end:
        sql += " AND g.day <= ?"
        args.append(_parse_day(end))
    sql += " ORDER BY g.day DESC, g.station_id LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def import_csv(conn, station_id, text):
    """导入 CSV：每行 `日期,发电量kWh`，允许有表头。返回 (成功条数, 错误列表)。"""
    if not get_station(conn, station_id):
        raise ValidationError("电站不存在")
    ok, errors = 0, []
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = []
    for lineno, row in enumerate(reader, 1):
        row = [c.strip() for c in row]
        if not row or not any(row):
            continue
        if len(row) < 2:
            errors.append(f"第 {lineno} 行: 列数不足")
            continue
        try:
            rows.append((_parse_day(row[0]), _parse_kwh(row[1])))
        except ValidationError as e:
            if lineno == 1:  # 表头
                continue
            errors.append(f"第 {lineno} 行: {e}")
    with conn:
        for day, kwh in rows:
            conn.execute(
                """INSERT INTO generation (station_id, day, kwh) VALUES (?, ?, ?)
                   ON CONFLICT(station_id, day) DO UPDATE SET kwh = excluded.kwh""",
                (station_id, day, kwh),
            )
            ok += 1
    return ok, errors


def export_csv(conn, station_id=None):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["电站", "日期", "发电量(kWh)", "备注"])
    for r in reversed(list_records(conn, station_id, limit=10_000_000)):
        w.writerow([r["station_name"], r["day"], r["kwh"], r["note"]])
    return "﻿" + out.getvalue()  # BOM 方便 Excel 识别中文


# ---------------- 统计 ----------------

def _scope(conn, station_id):
    """返回 (WHERE 子句片段, 参数, 装机容量kWp)。station_id 为空表示全部电站。"""
    if station_id:
        st = get_station(conn, station_id)
        cap = st["capacity_kwp"] if st else 0
        return " AND g.station_id = ?", [station_id], cap
    cap = conn.execute("SELECT COALESCE(SUM(capacity_kwp),0) FROM stations").fetchone()[0]
    return "", [], cap


def _series(conn, where, args, start, end, label_len):
    """按时间粒度汇总：label_len 10=日 7=月 4=年 0=整段。

    收益按各电站电价分别计算（先按“时间+电站”汇总，再乘各自电价）。
    按月对齐的区间直接读汇总表 station_month / day_total，速度与日记录条数无关。
    返回 {label: {"kwh", "revenue", "days"}}。
    """
    prices = {r[0]: r[1] for r in conn.execute("SELECT id, price FROM stations")}
    params = [start, end, *args]
    month_aligned = label_len != 10 and start.endswith("-01-01" if label_len == 4 else "-01") and (
        end[5:] == "12-31" or end == _month_end(int(end[:4]), int(end[5:7])).isoformat())
    if month_aligned:
        m_where = where.replace("g.", "m.")
        rows = conn.execute(
            f"SELECT substr(m.month,1,{label_len}), m.station_id, SUM(m.kwh) FROM station_month m "
            f"WHERE m.month >= ? AND m.month <= ? AND m.n > 0{m_where} GROUP BY 1, 2",
            [start[:7], end[:7], *args])
        if args:  # 单个电站：每天最多一条记录，天数 = 记录数
            day_rows = conn.execute(
                f"SELECT substr(m.month,1,{label_len}), SUM(m.n) FROM station_month m "
                f"WHERE m.month >= ? AND m.month <= ? AND m.n > 0{m_where} GROUP BY 1", [start[:7], end[:7], *args])
        else:
            day_rows = conn.execute(
                f"SELECT substr(d.day,1,{label_len}), COUNT(*) FROM day_total d "
                f"WHERE d.day >= ? AND d.day <= ? AND d.n > 0 GROUP BY 1", [start, end])
    else:
        rng = "g.day >= ? AND g.day <= ?" + where
        rows = conn.execute(
            f"SELECT substr(g.day,1,{label_len}), g.station_id, SUM(g.kwh) FROM generation g "
            f"WHERE {rng} GROUP BY 1, 2", params)
        day_rows = conn.execute(
            f"SELECT substr(g.day,1,{label_len}), COUNT(DISTINCT g.day) FROM generation g "
            f"WHERE {rng} GROUP BY 1", params)
    out = {}
    for label, sid, kwh in rows:
        item = out.setdefault(label, {"kwh": 0.0, "revenue": 0.0, "days": 0})
        item["kwh"] += kwh
        item["revenue"] += kwh * prices.get(sid, 0)
    for label, days in day_rows:
        if label in out:
            out[label]["days"] = days
    return out


def _sum(conn, where, args, start, end):
    return _series(conn, where, args, start, end, 0).get("", {"kwh": 0, "revenue": 0, "days": 0})


def _decorate(item, capacity):
    kwh = item["kwh"]
    item["kwh"] = round(kwh, 2)
    item["revenue"] = round(item.get("revenue", 0), 2)
    item["hours"] = round(kwh / capacity, 2) if capacity else None  # 等效利用小时数
    return item


def _month_end(year, month):
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def summary(conn, station_id=None, today=None):
    today = today or clock.today()
    where, args, cap = _scope(conn, station_id)
    y, m = today.year, today.month
    yesterday = today - timedelta(days=1)
    periods = {
        "today": (today, today),
        "yesterday": (yesterday, yesterday),
        "month": (date(y, m, 1), _month_end(y, m)),
        "year": (date(y, 1, 1), date(y, 12, 31)),
        "last_year": (date(y - 1, 1, 1), date(y - 1, 12, 31)),
        "total": (date(1000, 1, 1), date(9999, 12, 31)),
    }
    out = {"capacity_kwp": cap, "date": today.isoformat()}
    for key, (s, e) in periods.items():
        out[key] = _decorate(_sum(conn, where, args, s.isoformat(), e.isoformat()), cap)
    total = out["total"]["kwh"]
    out["co2_kg"] = round(total * CO2_PER_KWH, 1)
    out["coal_kg"] = round(total * COAL_PER_KWH, 1)
    out["trees"] = round(total * CO2_PER_KWH / 18.3)  # 一棵树每年约吸收 18.3 kg CO2
    if args:
        best = conn.execute(
            "SELECT day, kwh FROM generation WHERE station_id = ? ORDER BY kwh DESC LIMIT 1", args).fetchone()
    else:
        best = conn.execute("SELECT day, kwh FROM day_total WHERE n > 0 ORDER BY kwh DESC LIMIT 1").fetchone()
    out["best_day"] = dict(best) if best else None
    return out


def daily(conn, station_id=None, month=None):
    """某月每日发电量。month 形如 2024-06。"""
    where, args, cap = _scope(conn, station_id)
    y, m = (int(x) for x in (month or clock.today().strftime("%Y-%m")).split("-"))
    end = _month_end(y, m)
    found = _series(conn, where, args, date(y, m, 1).isoformat(), end.isoformat(), 10)
    items = []
    for d in range(1, end.day + 1):
        key = date(y, m, d).isoformat()
        rec = found.get(key)
        item = {"label": key, "kwh": rec["kwh"] if rec else 0,
                "revenue": rec["revenue"] if rec else 0, "has_data": bool(rec)}
        items.append(_decorate(item, cap))
    return _wrap(items, cap, f"{y}-{m:02d}")


def monthly(conn, station_id=None, year=None):
    """某年每月发电量，附带上一年同期对比。"""
    where, args, cap = _scope(conn, station_id)
    y = int(year or clock.today().year)

    def by_month(yr):
        return _series(conn, where, args, f"{yr}-01-01", f"{yr}-12-31", 7)

    cur, prev = by_month(y), by_month(y - 1)
    items = []
    for m in range(1, 13):
        rec = cur.get(f"{y}-{m:02d}")
        prv = prev.get(f"{y - 1}-{m:02d}")
        item = {"label": f"{y}-{m:02d}", "kwh": rec["kwh"] if rec else 0,
                "revenue": rec["revenue"] if rec else 0, "days": rec["days"] if rec else 0,
                "has_data": bool(rec), "prev_kwh": round(prv["kwh"], 2) if prv else None}
        if rec and prv and prv["kwh"]:
            item["yoy"] = round((rec["kwh"] - prv["kwh"]) / prv["kwh"] * 100, 1)
        else:
            item["yoy"] = None
        items.append(_decorate(item, cap))
    return _wrap(items, cap, str(y))


def yearly(conn, station_id=None):
    where, args, cap = _scope(conn, station_id)
    series = _series(conn, where, args, "1000-01-01", "9999-12-31", 4)
    items = []
    prev = None
    for label in sorted(series):
        item = {"label": label, **series[label]}
        item["has_data"] = True
        item["yoy"] = round((item["kwh"] - prev) / prev * 100, 1) if prev else None
        prev = item["kwh"]
        items.append(_decorate(item, cap))
    return _wrap(items, cap, "all")


def _wrap(items, cap, period):
    total = sum(i["kwh"] for i in items)
    with_data = [i for i in items if i["has_data"]]
    best = max(with_data, key=lambda i: i["kwh"]) if with_data else None
    return {
        "period": period,
        "capacity_kwp": cap,
        "items": items,
        "total_kwh": round(total, 2),
        "total_revenue": round(sum(i["revenue"] for i in items), 2),
        "total_hours": round(total / cap, 2) if cap else None,
        "avg_kwh": round(total / len(with_data), 2) if with_data else 0,
        "best": best,
        "co2_kg": round(total * CO2_PER_KWH, 1),
    }


# ---------------- 演示数据 ----------------

def generate_demo(conn, days=800, seed=42, today=None):
    """创建两个示例电站并生成模拟的历史发电数据。"""
    rnd = random.Random(seed)
    today = today or clock.today()
    specs = [
        {"name": "屋顶光伏 1 号", "capacity_kwp": 50, "location": "江苏 苏州", "price": 0.391},
        {"name": "厂房分布式电站", "capacity_kwp": 300, "location": "浙江 嘉兴", "price": 0.4153},
    ]
    created = []
    for spec in specs:
        spec["commission_date"] = (today - timedelta(days=days)).isoformat()
        st = create_station(conn, spec)
        created.append(st)
        rows = []
        for i in range(days, -1, -1):
            d = today - timedelta(days=i)
            doy = d.timetuple().tm_yday
            # 季节因子：夏季高、冬季低（峰值约在 6 月）
            season = 3.4 + 1.3 * math.sin(2 * math.pi * (doy - 80) / 365)
            weather = rnd.choices([1.0, 0.75, 0.45, 0.15], [0.55, 0.25, 0.14, 0.06])[0]
            weather *= rnd.uniform(0.9, 1.05)
            rows.append((st["id"], d.isoformat(), round(spec["capacity_kwp"] * season * weather, 1)))
        with conn:
            conn.executemany(
                """INSERT INTO generation (station_id, day, kwh) VALUES (?, ?, ?)
                   ON CONFLICT(station_id, day) DO UPDATE SET kwh = excluded.kwh""", rows
            )
    return created
