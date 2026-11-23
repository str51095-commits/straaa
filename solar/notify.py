"""微信通知：新告警 / 告警恢复 / 电站故障或离线 / 每日发电日报。

支持的推送渠道（都是把消息发到微信或企业微信）：
  pushplus  推送加    https://www.pushplus.plus/send            token        （微信渠道需实名认证，免费 200 条/天）
  wecom     企业微信群机器人 webhook                              key 或完整 webhook 地址
  serverchan Server酱 https://sctapi.ftqq.com/<SendKey>.send     SendKey
  wxpusher  WxPusher https://wxpusher.zjiecode.com/api/send/message  appToken + UID
配置保存在数据库 settings 表中，在网页「电站管理 → 微信通知」里设置。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import clock, db, monitor

CHANNELS = {
    "pushplus": "PushPlus 推送加（个人微信）",
    "wecom": "企业微信群机器人",
    "serverchan": "Server酱",
    "wxpusher": "WxPusher（免费，发到个人微信）",
}
DEFAULTS = {
    "notify_channel": "",
    "notify_token": "",
    "notify_uids": "",
    "notify_alarm": "1",        # 新告警
    "notify_recovery": "1",     # 告警恢复
    "notify_status": "1",       # 电站故障 / 离线 / 恢复正常
    "notify_daily": "1",        # 每日发电日报
    "notify_daily_time": "20:00",
    "notify_day_start": "08:00",  # 只在白天提醒离线（夜间逆变器休眠属正常）
    "notify_day_end": "18:00",
    "notify_mute": "DC侧SPD故障",  # 不推送的告警关键词，每行一个（仍会记录在告警中心）
}
MAX_LINES = 30
DAILY_CHUNK = 120   # 日报每条消息最多行数，电站很多时自动分多条发送
STALE_HOURS = 6


class NotifyError(Exception):
    pass


def http_post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise NotifyError(f"推送服务返回 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NotifyError(f"无法连接推送服务: {e}") from e
    except json.JSONDecodeError as e:
        raise NotifyError("推送服务返回的不是 JSON") from e


# ---------- 配置 ----------

def get_config(conn):
    return {k: db.get_setting(conn, k, v) for k, v in DEFAULTS.items()}


def public_config(conn):
    cfg = get_config(conn)
    tok = cfg.pop("notify_token")
    cfg["token_set"] = bool(tok)
    cfg["token_hint"] = (tok[:4] + "…" + tok[-4:]) if len(tok) > 10 else ("已设置" if tok else "")
    cfg["channels"] = CHANNELS
    cfg["last_result"] = db.get_setting(conn, "notify_last_result", "")
    return cfg


def save_config(conn, data):
    for k in DEFAULTS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, bool):
            v = "1" if v else "0"
        v = str(v if v is not None else "").strip()
        if k == "notify_channel" and v and v not in CHANNELS:
            raise db.ValidationError("不支持的推送方式")
        if k in ("notify_daily_time", "notify_day_start", "notify_day_end") and v:
            _parse_hm(v)
        if k == "notify_token" and v == "":
            continue  # 留空表示不修改已保存的 token
        db.set_setting(conn, k, v)
    return public_config(conn)


def _parse_hm(v):
    try:
        h, m = (int(x) for x in v.split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
        return h * 60 + m
    except Exception:  # noqa: BLE001
        raise db.ValidationError(f"时间格式应为 HH:MM: {v!r}")


# ---------- 发送 ----------

def send_long(conn, title, lines, post=http_post, chunk=DAILY_CHUNK):
    """内容较长（如日报列出所有电站）时分成几条发送，每条不超过 chunk 行。"""
    if len(lines) <= chunk:
        return send(conn, title, lines, post=post, max_lines=chunk)
    parts = [lines[i:i + chunk] for i in range(0, len(lines), chunk)]
    ok = False
    for i, part in enumerate(parts, 1):
        ok = send(conn, f"{title}（{i}/{len(parts)}）", part, post=post, max_lines=chunk) or ok
    return ok


def send(conn, title, lines, post=http_post, max_lines=MAX_LINES):
    """把标题 + 若干行内容发到配置的渠道。没配置时返回 False。"""
    cfg = get_config(conn)
    ch, token = cfg["notify_channel"], cfg["notify_token"]
    if not ch or not token:
        return False
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"……另有 {len(lines) - max_lines} 条，请登录平台查看"]
    md = "\n".join(lines)
    try:
        if ch == "pushplus":
            r = post("https://www.pushplus.plus/send",
                     {"token": token, "title": title, "content": md, "template": "markdown"})
            if r.get("code") != 200:
                raise NotifyError(f"PushPlus: {r.get('msg') or r}")
        elif ch == "wecom":
            url = token if token.startswith("http") else \
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + urllib.parse.quote(token)
            r = post(url, {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{md}"[:4000]}})
            if r.get("errcode") != 0:
                raise NotifyError(f"企业微信: {r.get('errmsg') or r}")
        elif ch == "serverchan":
            r = post(f"https://sctapi.ftqq.com/{urllib.parse.quote(token)}.send", {"title": title[:32], "desp": md})
            if r.get("code") != 0:
                raise NotifyError(f"Server酱: {r.get('message') or r}")
        elif ch == "wxpusher" and token.upper().startswith("SPT_"):
            # 极简推送：扫码关注即得 SPT，无需创建应用
            spts = [t.strip() for t in token.replace("，", ",").split(",") if t.strip()]
            r = post("https://wxpusher.zjiecode.com/api/send/message/simple-push",
                     {"summary": title[:90], "content": f"## {title}\n{md}", "contentType": 3,
                      **({"spt": spts[0]} if len(spts) == 1 else {"sptList": spts})})
            if not r.get("success"):
                raise NotifyError(f"WxPusher: {r.get('msg') or r}")
        elif ch == "wxpusher":
            uids = [u.strip() for u in cfg["notify_uids"].replace("，", ",").split(",") if u.strip()]
            if not uids:
                raise NotifyError("WxPusher 需要填写 UID（或改用以 SPT_ 开头的极简推送码）")
            r = post("https://wxpusher.zjiecode.com/api/send/message",
                     {"appToken": token, "summary": title[:90], "content": f"## {title}\n{md}",
                      "contentType": 3, "uids": uids})
            if not r.get("success"):
                raise NotifyError(f"WxPusher: {r.get('msg') or r}")
        db.set_setting(conn, "notify_last_result", f"{clock.now_iso()} 发送成功：{title}")
        return True
    except NotifyError as e:
        db.set_setting(conn, "notify_last_result", f"{clock.now_iso()} 发送失败：{e}")
        raise


# ---------- 事件检测（同步后调用） ----------

def mute_keywords(cfg):
    raw = cfg.get("notify_mute") or ""
    return [k.strip() for k in raw.replace("，", "\n").replace(",", "\n").splitlines() if k.strip()]


def toggle_mute(conn, name, mute=True):
    """把某类告警加入 / 移出不推送列表。"""
    name = (name or "").strip()
    if not name:
        raise db.ValidationError("告警名称不能为空")
    keys = mute_keywords(get_config(conn))
    if mute and name not in keys:
        keys.append(name)
    if not mute:
        keys = [k for k in keys if k != name]
    db.set_setting(conn, "notify_mute", "\n".join(keys))
    return keys


def _shift_iso(iso, hours):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(iso) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _in_daytime(cfg, now_hm):
    start, end = _parse_hm(cfg["notify_day_start"]), _parse_hm(cfg["notify_day_end"])
    return start <= now_hm <= end


def _now_hm():
    t = clock.now_iso()[11:16]
    return _parse_hm(t)


def collect_events(conn, prev_status, now_hm=None):
    """比较同步前后的电站状态与告警，返回 (标题, 行列表)；没有需要通知的事件返回 None。

    prev_status: {本地电站 id: 同步前的状态码}
    """
    cfg = get_config(conn)
    now_hm = _now_hm() if now_hm is None else now_hm
    lines = []
    n_alarm = n_status = 0

    # 屏蔽的告警：标记为已通知，后续不推送（包括恢复）
    mute = mute_keywords(cfg)
    if mute:
        muted = [r["id"] for r in conn.execute(
            "SELECT id, name, code FROM alarms WHERE notified = 0 OR notified_recovery = 0")
            if any(k in (r["name"] or "") or k == (r["code"] or "") for k in mute)]
        with conn:
            conn.executemany("UPDATE alarms SET notified = 1, notified_recovery = 1 WHERE id = ?",
                             [(i,) for i in muted])

    # 首次启用：把已有告警标记为已通知，只发一条汇总，避免刷屏
    if db.get_setting(conn, "alarm_baseline_done") != "1":
        active = conn.execute("SELECT COUNT(*) FROM alarms WHERE active = 1 AND notified = 0").fetchone()[0]
        with conn:
            conn.execute("UPDATE alarms SET notified = 1, notified_recovery = CASE WHEN active = 0 THEN 1 ELSE 0 END")
        db.set_setting(conn, "alarm_baseline_done", "1")
        if active and cfg["notify_alarm"] == "1":
            lines.append(f"当前共有 **{active}** 条进行中的告警，之后有新告警会实时通知。")
            n_alarm += active

    if cfg["notify_alarm"] == "1":
        rows = conn.execute("""SELECT a.*, COALESCE(s.name, a.station_name) AS station FROM alarms a
                               LEFT JOIN stations s ON s.goodwe_station_id = a.goodwe_station_id
                               WHERE a.active = 1 AND a.notified = 0 AND a.level NOT LIKE '%Prompt'
                               ORDER BY a.happened""").fetchall()
        # 发生超过 STALE_HOURS 的（例如新导入电站上早已存在的告警）只汇总计数，不逐条刷屏
        cutoff = _shift_iso(clock.now_iso(), -STALE_HOURS)
        stale = 0
        for a in rows:
            if a["happened"] and a["happened"].replace("T", " ") < cutoff:
                stale += 1
                continue
            lvl = "故障" if a["level"].endswith("_Fault") else "告警"
            lines.append(f"🔴 [{lvl}] {a['station']}：{a['name'] or a['code']}"
                         f"（{a['device_name'] or a['device_sn'] or '设备'}，{a['happened'][5:16]}）")
            n_alarm += 1
        if stale:
            lines.append(f"另有 **{stale}** 条较早发生、仍在进行中的告警，请在平台「告警」页查看。")
            n_alarm += stale
    with conn:
        conn.execute("UPDATE alarms SET notified = 1 WHERE active = 1 AND notified = 0")

    if cfg["notify_recovery"] == "1":
        rows = conn.execute("""SELECT a.*, COALESCE(s.name, a.station_name) AS station FROM alarms a
                               LEFT JOIN stations s ON s.goodwe_station_id = a.goodwe_station_id
                               WHERE a.active = 0 AND a.notified = 1 AND a.notified_recovery = 0""").fetchall()
        for a in rows:
            lines.append(f"🟢 [恢复] {a['station']}：{a['name'] or a['code']}（{(a['recovered'] or '')[5:16]}）")
    with conn:
        conn.execute("UPDATE alarms SET notified_recovery = 1 WHERE active = 0 AND notified_recovery = 0")

    if cfg["notify_status"] == "1":
        n_status += _status_events(conn, cfg, now_hm, lines)

    if not lines:
        return None
    if n_alarm or n_status:
        title = f"光伏告警：{n_alarm} 条告警" + (f"，{n_status} 座电站状态异常" if n_status else "") if n_alarm \
            else f"光伏电站状态变化：{n_status} 座"
    else:
        title = "光伏告警已恢复"
    lines.append(f"\n时间：{clock.now_iso().replace('T', ' ')}")
    return title, lines


FLEET_RUNNING_RATIO = 0.8   # 至少 80% 的电站在正常发电，才认为是“白天”，此时离线的电站才算异常
OFFLINE_CONFIRM = 2         # 连续 2 次同步（约 30 分钟）都满足条件才提醒离线，过滤日出日落的过渡期
MIN_FLEET = 3               # 电站少于 3 座时无法用整体判断，改用白天时段


def _status_events(conn, cfg, now_hm, lines):
    """基于当前状态（而非状态跳变）判断：
    - 故障：任何时间，出现即提醒一次；
    - 离线：只有在大部分电站正常发电时才提醒（日落、日出时全体离线属正常）；
      这样“昨晚还好、今早没有恢复上线”的电站也能被发现；
    - 恢复正常：只对之前提醒过的电站发送。
    """
    rows = conn.execute("""SELECT id, name, status, status_alerted, offline_streak FROM stations
                           WHERE goodwe_station_id != '' AND status IS NOT NULL""").fetchall()
    if not rows:
        return 0
    running = sum(1 for r in rows if r["status"] == 1)
    if len(rows) >= MIN_FLEET:
        fleet_day = running / len(rows) >= FLEET_RUNNING_RATIO
    else:
        fleet_day = _in_daytime(cfg, now_hm)
    n = 0
    updates = []
    for r in rows:
        st, alerted, streak = r["status"], r["status_alerted"] or 0, r["offline_streak"] or 0
        streak = streak + 1 if (st == 0 and fleet_day) else 0
        if st == 2 and alerted != 2:
            lines.append(f"⚠️ [故障] {r['name']}：电站状态为**故障**")
            alerted = 2
            n += 1
        elif st == 0 and alerted == 0 and streak >= OFFLINE_CONFIRM:
            extra = f"（{running}/{len(rows)} 座电站正在发电）" if len(rows) >= MIN_FLEET else ""
            lines.append(f"⚠️ [离线] {r['name']}：电站**离线**{extra}")
            alerted = 1
            n += 1
        elif st == 1 and alerted:
            lines.append(f"✅ [恢复] {r['name']}：已恢复正常运行")
            alerted = 0
            n += 1
        updates.append((alerted, streak, r["id"]))
    with conn:
        conn.executemany("UPDATE stations SET status_alerted = ?, offline_streak = ? WHERE id = ?", updates)
    return n


def daily_report(conn):
    ov = monitor.overview(conn)
    sm, rows = ov["summary"], ov["stations"]
    linked = [r for r in rows if r["today_kwh"] is not None]
    lines = [f"**今日总发电：{sm['today_kwh']:,.1f} kWh**（{len(linked)}/{sm['total']} 座电站有数据）"]
    cap = sum(r["capacity_kwp"] or 0 for r in linked)
    if cap:
        lines.append(f"平均等效小时：{sm['today_kwh'] / cap:.2f} h")
    c = sm["counts"]
    if c.get("fault"):  # 日报在晚上发送，此时电站离线属正常，只列出故障
        lines.append(f"故障电站：**{c['fault']}** 座")
    if sm["active_alarms"]:
        lines.append(f"进行中告警：**{sm['active_alarms']}** 条")
    if sm["low_eff"]:
        lines.append(f"发电偏低：**{sm['low_eff']}** 座（等效小时低于中位数 {sm['median_hours']} h 的 60%，下方标 ⬇）")
    month_total = sum(r["month_kwh"] or 0 for r in rows)
    lines.append(f"本月累计：{month_total:,.1f} kWh")
    s = db.summary(conn, None)
    lines.append(f"收益：今日 **¥{s['today']['revenue']:,.2f}**，本月 ¥{s['month']['revenue']:,.2f}，"
                 f"上月 ¥{s['last_month']['revenue']:,.2f}，本年 ¥{s['year']['revenue']:,.2f}")

    # 每座电站明细：按今日发电量从高到低
    lines.append("\n**各电站今日发电**（今日 kWh · 等效 h · 本月 kWh）：")
    ranked = sorted(linked, key=lambda r: -(r["today_kwh"] or 0))
    for i, r in enumerate(ranked, 1):
        hours = f" · {r['today_hours']:.2f} h" if r["today_hours"] is not None else ""
        mark = (" ⬇" if r["low_eff"] else "") + (" ⚠️故障" if r["status"] == "fault" else "") + \
               (f" 🔴告警{r['active_alarms']}" if r["active_alarms"] else "")
        lines.append(f"{i}. {r['name']}：**{r['today_kwh']:,.1f}**{hours} · {r['month_kwh']:,.0f}{mark}")
    missing = [r for r in rows if r["today_kwh"] is None]
    if missing:
        lines.append(f"\n今日无数据（{len(missing)} 座）：" + "、".join(r["name"] for r in missing))
    return f"光伏日报 {ov['date']}：{sm['today_kwh']:,.0f} kWh", lines


def maybe_send_daily(conn, post=http_post, now_hm=None):
    """到点且今天还没发过时发送日报。返回是否发送。"""
    cfg = get_config(conn)
    if cfg["notify_daily"] != "1" or not cfg["notify_channel"]:
        return False
    today = clock.today().isoformat()
    now_hm = _now_hm() if now_hm is None else now_hm
    if now_hm < _parse_hm(cfg["notify_daily_time"]) or db.get_setting(conn, "daily_report_date") == today:
        return False
    db.set_setting(conn, "daily_report_date", today)
    title, lines = daily_report(conn)
    send_long(conn, title, lines, post=post)
    return True
