/* 光伏能源管理平台 前端（原生 JS，无外部依赖） */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const state = {
    stations: [],
    stationId: localGet("stationId") || "",
    period: "daily",
    month: ymd(new Date()).slice(0, 7),
    monFilter: localGet("monFilter") || "all",
    monitor: null,
    alarmActive: "1",
    year: new Date().getFullYear(),
  };

  // ---------- 工具 ----------
  function localGet(k) { try { return localStorage.getItem("solar." + k); } catch { return null; } }
  function localSet(k, v) { try { localStorage.setItem("solar." + k, v); } catch { /* ignore */ } }
  function ymd(d) { const z = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`; }
  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  const nf = (v, d = 1) => v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: d, minimumFractionDigits: 0 });

  /** 自动换算单位：kWh / MWh / GWh（中文：度 / 千度 / 百万度） */
  function energy(kwh) {
    const v = Number(kwh) || 0;
    if (Math.abs(v) >= 1e6) return [nf(v / 1e6, 2), "GWh"];
    if (Math.abs(v) >= 1e4) return [nf(v / 1e3, 2), "MWh"];
    return [nf(v, 1), "kWh"];
  }
  const energyStr = kwh => energy(kwh).join(" ");
  const money = v => "¥" + Number(v || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function mass(kg) { return kg >= 1000 ? [nf(kg / 1000, 2), "吨"] : [nf(kg, 1), "kg"]; }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) { location.href = "/login.html"; throw new Error("请先登录"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
    return data;
  }
  const sidQuery = () => state.stationId ? `station_id=${state.stationId}` : "";

  let toastTimer;
  function toast(msg, isErr = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isErr ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3000);
  }
  const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

  // ---------- 主题 ----------
  function initTheme() {
    const saved = localGet("theme");
    if (saved) document.documentElement.dataset.theme = saved;
    $("#themeBtn").onclick = () => {
      const cur = document.documentElement.dataset.theme ||
        (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localSet("theme", next);
      loadChart();
    };
  }

  // ---------- 导航 ----------
  function showView(name) {
    $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
    if (name === "entry") { loadRecords(); loadSync().catch(() => {}); }
    if (name === "stations") loadNotify().catch(e => toast(e.message, true));
    if (name === "monitor") loadMonitor().catch(e => toast(e.message, true));
    if (name === "alarms") loadAlarms().catch(e => toast(e.message, true));
  }

  // ---------- 电站 ----------
  async function loadStations() {
    const [stations, settings] = await Promise.all([api("/api/stations"), api("/api/settings")]);
    state.stations = stations;
    state.defaultPrice = settings.default_price;
    if (!$("#stationForm").elements.id.value) $("#stationForm").elements.price.value = state.defaultPrice;
    if (document.activeElement !== $("#priceForm").elements.price) $("#priceForm").elements.price.value = state.defaultPrice;
    if (state.stationId && !state.stations.some(s => String(s.id) === String(state.stationId))) state.stationId = "";
    const sel = $("#stationSelect");
    sel.innerHTML = `<option value="">全部电站（${state.stations.length}）</option>` +
      state.stations.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    sel.value = state.stationId;
    const opts = state.stations.map(s => `<option value="${s.id}">${esc(s.name)}（${nf(s.capacity_kwp, 2)} kWp）</option>`).join("");
    $$("#recordForm select[name=station_id], #importForm select[name=station_id]").forEach(el => {
      const prev = el.value;
      el.innerHTML = opts;
      el.value = prev || state.stationId || (state.stations[0] && state.stations[0].id) || "";
    });
    $("#exportLink").href = "/api/export?" + sidQuery();
    $("#emptyState").classList.toggle("hidden", state.stations.length > 0);
    renderStationsTable();
  }

  function renderStationsTable() {
    const t = $("#stationsTable");
    if (!state.stations.length) { t.innerHTML = `<tr><td class="hint">暂无电站</td></tr>`; return; }
    t.innerHTML = `<thead><tr><th>名称</th><th class="num">容量 kWp</th><th class="num">电价</th><th>地址</th><th>并网日期</th><th>固德威同步</th><th class="num">累计发电</th><th class="num">记录天数</th><th>最近数据</th><th></th></tr></thead><tbody>` +
      state.stations.map(s => `<tr>
        <td>${esc(s.name)}</td><td class="num">${nf(s.capacity_kwp, 2)}</td><td class="num">${nf(s.price, 4)}</td>
        <td>${esc(s.location) || "—"}</td><td>${esc(s.commission_date) || "—"}</td>
        <td title="${esc(s.goodwe_station_id)}">${s.goodwe_station_id ? "已关联" : "—"}</td>
        <td class="num">${energyStr(s.total_kwh)}</td><td class="num">${s.record_days}</td><td>${s.last_day || "—"}</td>
        <td><button class="btn small" data-edit="${s.id}">编辑</button> <button class="btn small danger" data-del="${s.id}">删除</button></td>
      </tr>`).join("") + "</tbody>";
  }

  function fillStationForm(s) {
    const f = $("#stationForm");
    f.reset();
    if (!s) f.elements.price.value = state.defaultPrice ?? 0.4;
    for (const k of ["id", "name", "capacity_kwp", "price", "location", "commission_date", "notes", "goodwe_station_id"]) {
      if (s && s[k] != null) f.elements[k].value = s[k];
    }
    $("#stationFormTitle").textContent = s ? `编辑电站：${s.name}` : "添加电站";
    $("#stationCancel").classList.toggle("hidden", !s);
  }

  // ---------- 概览 ----------
  async function loadSummary() {
    const s = await api("/api/stats/summary?" + sidQuery());
    const tile = (label, kwh, sub, minor) => {
      const [v, u] = energy(kwh);
      return `<div class="kpi${minor ? " minor" : ""}"><div class="label">${label}</div><div class="value">${v}<small>${u}</small></div><div class="sub">${sub}</div></div>`;
    };
    const delta = (cur, prev) => {
      if (!prev) return "";
      const p = (cur - prev) / prev * 100;
      return ` · <span class="${p >= 0 ? "delta-up" : "delta-down"}">${p >= 0 ? "▲" : "▼"} ${nf(Math.abs(p), 1)}%</span> 较昨日`;
    };
    const hrs = h => h == null ? "" : `等效 ${nf(h, 2)} h`;
    const [co2, co2u] = mass(s.co2_kg);
    const [coal, coalu] = mass(s.coal_kg);
    $("#kpis").innerHTML =
      tile("今日发电", s.today.kwh, (s.today.days ? hrs(s.today.hours) : "今日未录入") + (s.today.days ? delta(s.today.kwh, s.yesterday.kwh) : "")) +
      tile("本月发电", s.month.kwh, `${hrs(s.month.hours)} · ${money(s.month.revenue)}`) +
      tile("本年发电", s.year.kwh, `${hrs(s.year.hours)} · ${money(s.year.revenue)}`) +
      tile("累计发电", s.total.kwh, `${s.total.days} 天 · ${money(s.total.revenue)}`) +
      `<div class="kpi minor"><div class="label">装机容量</div><div class="value">${nf(s.capacity_kwp, 2)}<small>kWp</small></div><div class="sub">${state.stationId ? "当前电站" : `${state.stations.length} 座电站合计`}</div></div>` +
      `<div class="kpi minor"><div class="label">CO₂ 减排</div><div class="value">${co2}<small>${co2u}</small></div><div class="sub">约等于种树 ${nf(s.trees, 0)} 棵/年</div></div>` +
      `<div class="kpi minor"><div class="label">节约标准煤</div><div class="value">${coal}<small>${coalu}</small></div><div class="sub">按 0.3 kg/kWh 折算</div></div>` +
      `<div class="kpi minor"><div class="label">单日最高</div><div class="value">${s.best_day ? energy(s.best_day.kwh).join("<small>") + "</small>" : "—"}</div><div class="sub">${s.best_day ? s.best_day.day : "暂无数据"}</div></div>`;
  }

  async function loadChart() {
    const p = state.period;
    $("#monthInput").classList.toggle("hidden", p !== "daily");
    $("#yearInput").classList.toggle("hidden", p !== "monthly");
    $("#prevBtn").classList.toggle("hidden", p === "yearly");
    $("#nextBtn").classList.toggle("hidden", p === "yearly");
    $("#monthInput").value = state.month;
    $("#yearInput").value = state.year;

    let url = `/api/stats/${p}?${sidQuery()}`;
    if (p === "daily") url += `&month=${state.month}`;
    if (p === "monthly") url += `&year=${state.year}`;
    const d = await api(url);

    const name = state.stationId ? (state.stations.find(s => String(s.id) === String(state.stationId)) || {}).name : "全部电站";
    const [y, m] = state.month.split("-");
    $("#chartTitle").textContent =
      p === "daily" ? `${name} · ${y} 年 ${+m} 月每日发电量 (kWh)` :
      p === "monthly" ? `${name} · ${state.year} 年每月发电量 (kWh)` : `${name} · 历年发电量 (kWh)`;

    const unit = p === "daily" ? "日" : p === "monthly" ? "月" : "年";
    const bestLabel = d.best ? fmtLabel(d.best.label, p) : "—";
    $("#periodStats").innerHTML =
      `<span>合计 <b>${energyStr(d.total_kwh)}</b></span>` +
      `<span>${unit}均 <b>${energyStr(d.avg_kwh)}</b></span>` +
      `<span>最高 <b>${d.best ? energyStr(d.best.kwh) : "—"}</b>（${bestLabel}）</span>` +
      (d.total_hours != null ? `<span>等效利用小时 <b>${nf(d.total_hours, 1)} h</b></span>` : "") +
      `<span>收益 <b>${money(d.total_revenue)}</b></span>` +
      `<span>CO₂ 减排 <b>${mass(d.co2_kg).join(" ")}</b></span>`;

    const compare = p === "monthly" && d.items.some(i => i.prev_kwh != null);
    $("#legend").innerHTML = compare
      ? `<span><i style="background:var(--series-1)"></i>${state.year} 年</span><span><i style="background:var(--series-2)"></i>${state.year - 1} 年</span>`
      : "";
    drawBars($("#chart"), d.items, p, compare);
    renderPeriodTable(d.items, p, compare);
  }

  function fmtLabel(label, p) {
    if (p === "daily") return `${+label.slice(5, 7)}月${+label.slice(8)}日`;
    if (p === "monthly") return `${+label.slice(5)}月`;
    return `${label}年`;
  }

  function renderPeriodTable(items, p, compare) {
    const head = { daily: "日期", monthly: "月份", yearly: "年份" }[p];
    const cols = `<th>${head}</th><th class="num">发电量 kWh</th><th class="num">等效小时 h</th><th class="num">收益 ¥</th>` +
      (p !== "daily" ? `<th class="num">记录天数</th>` : "") +
      (compare ? `<th class="num">去年同期 kWh</th>` : "") + (p !== "daily" ? `<th class="num">同比</th>` : "");
    const rows = items.map(i => `<tr class="${i.has_data ? "" : "nodata"}">
      <td>${fmtLabel(i.label, p)}</td><td class="num">${i.has_data ? nf(i.kwh, 2) : "—"}</td>
      <td class="num">${i.has_data && i.hours != null ? nf(i.hours, 2) : "—"}</td><td class="num">${i.has_data ? nf(i.revenue, 2) : "—"}</td>
      ${p !== "daily" ? `<td class="num">${i.days ?? "—"}</td>` : ""}
      ${compare ? `<td class="num">${i.prev_kwh != null ? nf(i.prev_kwh, 2) : "—"}</td>` : ""}
      ${p !== "daily" ? `<td class="num">${i.yoy != null ? (i.yoy >= 0 ? "+" : "") + nf(i.yoy, 1) + "%" : "—"}</td>` : ""}
    </tr>`).join("");
    $("#periodTable").innerHTML = `<thead><tr>${cols}</tr></thead><tbody>${rows}</tbody>`;
  }

  // ---------- SVG 柱状图 ----------
  function niceMax(v) {
    if (v <= 0) return 10;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    for (const s of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (s * pow >= v) return s * pow;
    return 10 * pow;
  }
  function roundTopRect(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  function drawBars(el, items, p, compare) {
    const W = el.clientWidth || 800, H = el.clientHeight || 320;
    if (!items.length || items.every(i => !i.has_data && !i.prev_kwh)) {
      el.innerHTML = `<div class="chart-empty">该时间段暂无发电数据</div>`;
      return;
    }
    const narrow = W < 520;
    const m = { t: 16, r: 8, b: 26, l: narrow ? 40 : 52 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const maxV = niceMax(Math.max(...items.map(i => Math.max(i.kwh, compare ? i.prev_kwh || 0 : 0))));
    const yS = v => m.t + ih - (v / maxV) * ih;
    const band = iw / items.length;
    const gap = 2;
    const groupW = Math.min(band * (compare ? 0.78 : 0.7), compare ? 44 : 36);
    const barW = compare ? (groupW - gap) / 2 : groupW;
    const ticks = 4;
    const fmtTick = v => v === 0 ? "0" : maxV >= 1e4 ? nf(v / 1000, 1) + "k" : nf(v, 0);

    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc($("#chartTitle").textContent)}"><g class="grid">`;
    for (let i = 1; i <= ticks; i++) {
      const v = maxV * i / ticks, y = yS(v);
      svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/>`;
    }
    svg += `</g><g class="axis">`;
    for (let i = 0; i <= ticks; i++) {
      const v = maxV * i / ticks;
      svg += `<text x="${m.l - 8}" y="${yS(v) + 4}" text-anchor="end">${fmtTick(v)}</text>`;
    }
    // x 轴标签：根据宽度抽稀
    const every = Math.max(1, Math.ceil(items.length / Math.floor(iw / (p === "daily" ? 26 : 38))));
    items.forEach((it, i) => {
      const show = p === "daily" ? (i % every === 0 || i === items.length - 1) && !(i === items.length - 1 && i % every !== 0 && (i % every) < every / 2) : i % every === 0;
      if (!show) return;
      const lbl = p === "daily" ? String(+it.label.slice(8)) : p === "monthly" ? `${+it.label.slice(5)}月` : it.label;
      svg += `<text x="${m.l + band * i + band / 2}" y="${H - 6}" text-anchor="middle">${lbl}</text>`;
    });
    svg += `</g><line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${yS(0)}" y2="${yS(0)}"/>`;

    // 柱子
    let bestIdx = -1;
    items.forEach((it, i) => { if (it.has_data && (bestIdx < 0 || it.kwh > items[bestIdx].kwh)) bestIdx = i; });
    items.forEach((it, i) => {
      const cx = m.l + band * i + band / 2;
      const x0 = cx - groupW / 2;
      let bars = "";
      if (it.has_data && it.kwh > 0) {
        const y = yS(it.kwh);
        bars += `<path d="${roundTopRect(x0, y, barW, yS(0) - y, 4)}" fill="var(--series-1)"/>`;
      }
      if (compare && it.prev_kwh) {
        const y = yS(it.prev_kwh);
        bars += `<path d="${roundTopRect(x0 + barW + gap, y, barW, yS(0) - y, 4)}" fill="var(--series-2)"/>`;
      }
      svg += `<g class="bar" data-i="${i}">${bars}</g>`;
    });
    // 只给最高值做直接标注
    if (bestIdx >= 0) {
      const it = items[bestIdx];
      const cx = m.l + band * bestIdx + band / 2 - (compare ? (groupW - barW) / 2 : 0);
      svg += `<text class="dlabel" x="${cx}" y="${yS(it.kwh) - 5}" text-anchor="middle">${nf(it.kwh, it.kwh >= 1000 ? 0 : 1)}</text>`;
    }
    // 交互热区（比柱子大）
    items.forEach((it, i) => {
      svg += `<rect class="hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}"/>`;
    });
    svg += "</svg>";
    el.innerHTML = svg;

    const tip = $("#tooltip");
    const bars = $$(".bar", el);
    const show = (i, ev) => {
      const it = items[i];
      el.classList.add("hovering");
      bars.forEach(b => b.classList.toggle("hot", +b.dataset.i === i));
      let html = `<div class="t-head">${fmtLabel(it.label, p)}</div>`;
      html += it.has_data
        ? `<div class="t-row"><span>${compare ? `<i style="background:var(--series-1)"></i>${state.year} 年` : "发电量"}</span><b>${nf(it.kwh, 2)} kWh</b></div>` +
          (it.hours != null ? `<div class="t-row"><span>等效小时</span><b>${nf(it.hours, 2)} h</b></div>` : "") +
          `<div class="t-row"><span>收益</span><b>${money(it.revenue)}</b></div>` +
          (it.days != null && p !== "daily" ? `<div class="t-row"><span>记录天数</span><b>${it.days}</b></div>` : "")
        : `<div class="t-row"><span>未录入数据</span></div>`;
      if (compare) html += `<div class="t-row"><span><i style="background:var(--series-2)"></i>${state.year - 1} 年</span><b>${it.prev_kwh != null ? nf(it.prev_kwh, 2) + " kWh" : "—"}</b></div>`;
      if (it.yoy != null) html += `<div class="t-row"><span>同比</span><b>${it.yoy >= 0 ? "+" : ""}${nf(it.yoy, 1)}%</b></div>`;
      tip.innerHTML = html;
      tip.classList.remove("hidden");
      const r = tip.getBoundingClientRect();
      let x = ev.clientX + 14, y = ev.clientY - r.height - 10;
      if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
      if (y < 8) y = ev.clientY + 16;
      tip.style.left = Math.max(8, x) + "px";
      tip.style.top = y + "px";
    };
    const hide = () => { el.classList.remove("hovering"); bars.forEach(b => b.classList.remove("hot")); tip.classList.add("hidden"); };
    $$(".hit", el).forEach(h => {
      h.addEventListener("pointermove", ev => show(+h.dataset.i, ev));
      h.addEventListener("pointerdown", ev => show(+h.dataset.i, ev));
    });
    el.onpointerleave = hide;
  }

  // ---------- 记录 ----------
  async function loadRecords() {
    const mon = $("#recordsMonth").value || state.month;
    $("#recordsMonth").value = mon;
    const [y, m] = mon.split("-").map(Number);
    const last = new Date(y, m, 0).getDate();
    const rows = await api(`/api/records?${sidQuery()}&start=${mon}-01&end=${mon}-${String(last).padStart(2, "0")}`);
    const t = $("#recordsTable");
    if (!rows.length) { t.innerHTML = `<tr><td class="hint">${y} 年 ${m} 月暂无记录</td></tr>`; return; }
    t.innerHTML = `<thead><tr><th>日期</th><th>电站</th><th class="num">发电量 kWh</th><th>备注</th><th></th></tr></thead><tbody>` +
      rows.map(r => `<tr><td>${r.day}</td><td>${esc(r.station_name)}</td><td class="num">${nf(r.kwh, 2)}</td><td>${esc(r.note)}</td>
        <td><button class="btn small" data-fill='${esc(JSON.stringify({ station_id: r.station_id, day: r.day, kwh: r.kwh, note: r.note }))}'>修改</button>
        <button class="btn small danger" data-delrec="${r.id}">删除</button></td></tr>`).join("") + "</tbody>";
  }

  // ---------- 电站监控 ----------
  const STATUS_ORDER = { fault: 0, offline: 1, waiting: 2, building: 3, unknown: 4, running: 5, manual: 6 };
  const STATUS_LABEL = { running: "正常运行", fault: "故障", offline: "离线", waiting: "等待", building: "建设中", unknown: "未知", manual: "未接入" };
  const stBadge = (key, label) => `<span class="st st-${key}">${esc(label || STATUS_LABEL[key] || key)}</span>`;
  const powerStr = w => w == null ? "—" : (Math.abs(w) >= 1e6 ? nf(w / 1e6, 2) + " MW" : nf(w / 1000, 1) + " kW");

  async function loadMonitor() {
    const m = state.monitor = await api("/api/monitor");
    const sm = m.summary, c = sm.counts;
    const badge = $("#alarmBadge");
    badge.textContent = sm.active_alarms;
    badge.classList.toggle("hidden", !sm.active_alarms);
    const linked = m.stations.some(x => x.linked);
    const strip = $("#statusStrip");
    strip.classList.toggle("hidden", !linked);
    if (linked) {
      const parts = ["running", "fault", "offline", "waiting", "building", "unknown"].filter(k => c[k])
        .map(k => `<span>${stBadge(k)} <b>${c[k]}</b></span>`);
      if (sm.active_alarms) parts.push(`<span class="tag crit">告警 ${sm.active_alarms}</span>`);
      if (sm.low_eff) parts.push(`<span class="tag warn">发电偏低 ${sm.low_eff}</span>`);
      strip.innerHTML = parts.join("") + `<span>实时功率 <b>${powerStr(sm.power_kw * 1000)}</b></span><span class="more">电站监控 ›</span>`;
    }
    if ($("#view-monitor").classList.contains("active")) renderMonitor();
  }

  function renderMonitor() {
    const m = state.monitor; if (!m) return;
    const sm = m.summary, c = sm.counts;
    const [tv, tu] = energy(sm.today_kwh);
    const pw = powerStr(sm.power_kw * 1000).split(" ");
    $("#monTotals").innerHTML =
      `<div class="kpi"><div class="label">电站总数</div><div class="value">${sm.total}<small>座</small></div><div class="sub">正常运行 ${c.running || 0} 座</div></div>` +
      `<div class="kpi"><div class="label">实时总功率</div><div class="value">${pw[0]}<small>${pw[1] || ""}</small></div><div class="sub">最近一次同步</div></div>` +
      `<div class="kpi"><div class="label">今日总发电</div><div class="value">${tv}<small>${tu}</small></div><div class="sub">${sm.median_hours != null ? `等效小时中位数 ${nf(sm.median_hours, 2)} h` : "　"}</div></div>` +
      `<div class="kpi"><div class="label">进行中告警</div><div class="value">${sm.active_alarms}<small>条</small></div><div class="sub">${sm.low_eff ? `${sm.low_eff} 座发电偏低` : "无发电偏低电站"}</div></div>`;
    const chips = [["all", "全部", sm.total]];
    ["fault", "offline", "waiting", "building", "unknown", "running", "manual"].forEach(k => { if (c[k]) chips.push([k, STATUS_LABEL[k], c[k]]); });
    if (sm.active_alarms) chips.push(["alarm", "有告警", m.stations.filter(x => x.active_alarms).length]);
    if (sm.low_eff) chips.push(["loweff", "发电偏低", sm.low_eff]);
    if (!chips.some(x => x[0] === state.monFilter)) state.monFilter = "all";
    $("#monChips").innerHTML = chips.map(([k, l, n]) =>
      `<button class="chip${state.monFilter === k ? " active" : ""}" data-f="${k}">${["all", "alarm", "loweff"].includes(k) ? esc(l) : stBadge(k)} <b>${n}</b></button>`).join("");

    const q = $("#monSearch").value.trim().toLowerCase();
    const sort = $("#monSort").value;
    const rows = m.stations.filter(r => {
      const f = state.monFilter;
      if (f === "alarm" && !r.active_alarms) return false;
      if (f === "loweff" && !r.low_eff) return false;
      if (!["all", "alarm", "loweff"].includes(f) && r.status !== f) return false;
      return !q || r.name.toLowerCase().includes(q) || (r.location || "").toLowerCase().includes(q);
    });
    const num = v => v == null ? -Infinity : v;
    const asc = v => v == null ? Infinity : v;
    rows.sort((a, b) => sort === "name" ? a.name.localeCompare(b.name, "zh")
      : sort === "status" ? (STATUS_ORDER[a.status] - STATUS_ORDER[b.status]) || (b.active_alarms - a.active_alarms) || (asc(a.today_hours) - asc(b.today_hours))
      : sort === "today_hours" ? asc(a.today_hours) - asc(b.today_hours)
      : num(b[sort]) - num(a[sort]));
    $("#monHint").textContent = `${rows.length} 座电站` + (sort === "today_hours" ? "（今日等效小时从低到高，排在前面的需重点关注）" : "") +
      (m.stations.some(x => x.status_time) ? "" : "。运行状态和实时功率在固德威同步后显示");
    $("#monList").innerHTML = rows.length ? rows.map(r => `<button class="mon-card" data-sid="${r.id}">
      <div class="top">${stBadge(r.status, r.status_label)}<span class="name" title="${esc(r.name)}">${esc(r.name)}</span>
        ${r.active_alarms ? `<span class="tag crit">告警 ${r.active_alarms}</span>` : ""}${r.low_eff ? `<span class="tag warn">发电偏低</span>` : ""}</div>
      <div class="grid">
        <div><div class="k">实时功率</div><div class="v">${powerStr(r.power_w)}</div></div>
        <div><div class="k">今日发电</div><div class="v">${r.today_kwh == null ? "—" : energyStr(r.today_kwh)}</div></div>
        <div><div class="k">今日等效</div><div class="v">${r.today_hours == null ? "—" : nf(r.today_hours, 2) + " h"}</div></div>
        <div><div class="k">本月发电</div><div class="v">${energyStr(r.month_kwh)}</div></div>
      </div>
      <div class="foot"><span>${nf(r.capacity_kwp, 2)} kWp</span>${r.location ? `<span>${esc(r.location)}</span>` : ""}${r.status_time ? `<span>更新于 ${esc(r.status_time.slice(5, 16).replace("T", " "))}</span>` : ""}</div>
    </button>`).join("") : `<p class="hint">没有符合条件的电站</p>`;
  }

  async function loadAlarms() {
    const [rows, nc] = await Promise.all([api(`/api/alarms?active=${state.alarmActive}&${sidQuery()}`), api("/api/notify")]);
    const muteList = (nc.notify_mute || "").split(/[\n,，]/).map(x => x.trim()).filter(Boolean);
    const muted = a => muteList.some(k => (a.name || "").includes(k) || k === a.code);
    const lvl = l => /_Fault$/i.test(l || "") ? ["fault", "故障"] : /_alarm$/i.test(l || "") ? ["alarm", "告警"] : /_Prompt$/i.test(l || "") ? ["prompt", "提示"] : ["", l || ""];
    $("#alarmList").innerHTML = rows.length ? rows.map(a => {
      const [lk, ll] = lvl(a.level);
      return `<div class="alarm${a.active ? " on" : ""} lv-${lk}">
        <div class="t">${a.active ? `<span class="tag ${lk === "fault" ? "crit" : "warn"}">${esc(ll || "告警")} · 进行中</span>` : `<span class="tag" style="color:var(--muted)">已恢复</span>`}${esc(a.name || a.code || "未知告警")}</div>
        <div class="m">${esc(a.station || "")}${a.device_name || a.device_sn ? ` · ${esc(a.device_name || "")} ${esc(a.device_sn || "")}` : ""}${a.code ? ` · 代码 ${esc(a.code)}` : ""}</div>
        <div class="m">发生 ${esc(a.happened || "—")}${a.recovered ? ` · 恢复 ${esc(a.recovered)}` : ""}</div>
        <div class="m">${muted(a) ? `<span class="tag" style="color:var(--muted)">不推送</span> <button class="linkbtn" data-unmute="${esc(a.name)}">恢复推送</button>`
          : a.name ? `<button class="linkbtn" data-mute="${esc(a.name)}">不推送此类告警</button>` : ""}</div>
      </div>`;
    }).join("") : `<p class="hint">${state.alarmActive === "1" ? "当前没有进行中的告警" : "暂无告警记录"}</p>`;
  }

  // ---------- 微信通知 ----------
  const NOTIFY_HELP = {
    "": ["Token", "选择推送方式后，告警和日报会推送到微信。"],
    pushplus: ["PushPlus Token", "微信搜索公众号「pushplus 推送加」关注并登录，完成实名认证后在「一对一推送」页复制 Token（微信渠道免费 200 条/天）。"],
    wecom: ["机器人 Webhook 地址或 key", "在企业微信群里点右上角「…」→「消息推送 / 群机器人」→ 添加机器人，复制 Webhook 地址粘贴到这里。完全免费，可多人一起接收。"],
    serverchan: ["SendKey", "登录 sct.ftqq.com 用微信扫码，复制 SendKey（免费版每天条数有限）。"],
    wxpusher: ["appToken", "在 wxpusher.zjiecode.com 创建应用获取 appToken，扫码关注后得到自己的 UID。"],
  };
  function renderNotifyHelp() {
    const f = $("#notifyForm"), ch = f.elements.notify_channel.value;
    const [label, help] = NOTIFY_HELP[ch] || NOTIFY_HELP[""];
    $("#tokenLabel").textContent = label;
    $("#notifyHelp").textContent = help;
    $("#uidsRow").classList.toggle("hidden", ch !== "wxpusher");
  }
  async function loadNotify() {
    const c = await api("/api/notify"), f = $("#notifyForm");
    f.elements.notify_channel.value = c.notify_channel || "";
    f.elements.notify_token.value = "";
    f.elements.notify_token.placeholder = c.token_set ? `已保存（${c.token_hint}），不修改请留空` : "粘贴 Token";
    f.elements.notify_uids.value = c.notify_uids || "";
    f.elements.notify_mute.value = c.notify_mute || "";
    for (const k of ["notify_alarm", "notify_recovery", "notify_status", "notify_daily"]) f.elements[k].checked = c[k] === "1";
    for (const k of ["notify_daily_time", "notify_day_start", "notify_day_end"]) f.elements[k].value = c[k];
    $("#notifyLast").textContent = c.last_result ? "最近一次：" + c.last_result.replace("T", " ") : "";
    renderNotifyHelp();
  }
  async function saveNotify() {
    const f = $("#notifyForm"), body = {};
    for (const k of ["notify_channel", "notify_token", "notify_uids", "notify_mute", "notify_daily_time", "notify_day_start", "notify_day_end"]) body[k] = f.elements[k].value;
    for (const k of ["notify_alarm", "notify_recovery", "notify_status", "notify_daily"]) body[k] = f.elements[k].checked;
    await api("/api/notify", { method: "PUT", body });
  }

  // ---------- 固德威同步 ----------
  function renderSync(st) {
    const msg = $("#syncMsg");
    msg.className = "sync-msg " + (st.ok === true ? "ok" : st.ok === false ? "err" : "");
    if (!st.enabled) {
      msg.innerHTML = `<span class="dot"></span>未开启。启动前设置环境变量 <code>GOODWE_ACCOUNT</code> 和 <code>GOODWE_PASSWORD</code>（SEMS 账号密码），并在「电站管理」中填写固德威电站 ID。`;
      ["#syncBtn", "#discoverBtn", "#backfillBtn"].forEach(id => $(id).disabled = true);
      $("#syncTable").innerHTML = "";
      return;
    }
    $("#syncBtn").disabled = false;
    $("#discoverBtn").classList.toggle("hidden", !st.can_discover);
    $("#backfillBtn").classList.toggle("hidden", !st.can_backfill);
    const bf = st.backfill;
    $("#backfillMsg").classList.toggle("hidden", !bf);
    $("#backfillBtn").disabled = !!(bf && bf.running);
    if (bf) {
      $("#backfillMsg").innerHTML = `<span class="dot"></span>${bf.running ? `历史补录中 ${bf.done}/${bf.total} · ` : ""}${esc(bf.message)}` +
        (bf.errors && bf.errors.length ? `<br>${bf.errors.slice(0, 3).map(esc).join("<br>")}` : "");
      $("#backfillMsg").className = "sync-msg " + (bf.running ? "" : bf.errors && bf.errors.length ? "err" : "ok");
      if (bf.running) { clearTimeout(renderSync.t); renderSync.t = setTimeout(() => loadSync().then(() => { if (!(state.lastBf && state.lastBf.running)) refreshAll(); }).catch(() => {}), 3000); }
    }
    state.lastBf = bf;
    msg.innerHTML = `<span class="dot"></span>${esc(st.message)}` +
      (st.last_run ? ` · 上次同步 ${esc(st.last_run.replace("T", " "))}` : "") + ` · 每 ${st.interval_min} 分钟自动同步`;
    const rows = st.stations || [];
    $("#syncTable").innerHTML = rows.length ? `<thead><tr><th>电站</th><th>状态</th><th class="num">今日 kWh</th><th class="num">本月 kWh</th><th class="num">累计 kWh</th><th class="num">当前功率 W</th></tr></thead><tbody>` +
      rows.map(r => `<tr><td>${esc(r.station)}</td><td>${r.ok ? "成功" : "失败：" + esc(r.error)}</td>
        <td class="num">${nf(r.today_kwh, 2)}</td><td class="num">${nf(r.month_kwh, 1)}</td><td class="num">${nf(r.total_kwh, 1)}</td><td class="num">${nf(r.power_w, 0)}</td></tr>`).join("") + "</tbody>" : "";
  }
  async function loadSync() { renderSync(await api("/api/sync/status")); }

  async function refreshAll() {
    await loadStations();
    await Promise.all([loadSummary(), loadChart(), loadMonitor().catch(() => {})]);
    if ($("#view-alarms").classList.contains("active")) await loadAlarms();
    if ($("#view-entry").classList.contains("active")) await loadRecords();
  }

  // ---------- 事件 ----------
  function bind() {
    $$(".tab").forEach(b => b.onclick = () => showView(b.dataset.view));
    $$("[data-goto]").forEach(b => b.onclick = () => showView(b.dataset.goto));
    $("#stationSelect").addEventListener("change", () => { if ($("#view-alarms").classList.contains("active")) loadAlarms().catch(() => {}); });

    $("#stationSelect").onchange = guard(async e => {
      state.stationId = e.target.value;
      localSet("stationId", state.stationId);
      $("#exportLink").href = "/api/export?" + sidQuery();
      $$("#recordForm select[name=station_id], #importForm select[name=station_id]").forEach(el => { if (state.stationId) el.value = state.stationId; });
      await Promise.all([loadSummary(), loadChart()]);
      if ($("#view-entry").classList.contains("active")) await loadRecords();
    });

    $$("#periodSeg button").forEach(b => b.onclick = guard(async () => {
      $$("#periodSeg button").forEach(x => x.classList.toggle("active", x === b));
      state.period = b.dataset.period;
      await loadChart();
    }));
    $("#monthInput").onchange = guard(async e => { if (e.target.value) { state.month = e.target.value; await loadChart(); } });
    $("#yearInput").onchange = guard(async e => { const v = +e.target.value; if (v >= 2000 && v <= 2100) { state.year = v; await loadChart(); } });
    const step = dir => guard(async () => {
      if (state.period === "daily") {
        const [y, m] = state.month.split("-").map(Number);
        const d = new Date(y, m - 1 + dir, 1);
        state.month = ymd(d).slice(0, 7);
      } else state.year += dir;
      await loadChart();
    });
    $("#prevBtn").onclick = step(-1);
    $("#nextBtn").onclick = step(1);

    $("#notifyForm").elements.notify_channel.onchange = renderNotifyHelp;
    $("#notifyForm").onsubmit = guard(async e => {
      e.preventDefault(); await saveNotify(); toast("通知设置已保存"); await loadNotify();
    });
    const notifySend = kind => guard(async () => {
      await saveNotify();
      await api("/api/notify/test", { method: "POST", body: { kind } });
      toast(kind === "daily" ? "日报已发送，请查看微信" : "测试消息已发送，请查看微信");
      await loadNotify();
    });
    $("#notifyTest").onclick = notifySend("test");
    $("#notifyDaily").onclick = notifySend("daily");

    $("#monChips").onclick = e => {
      const b = e.target.closest(".chip"); if (!b) return;
      state.monFilter = b.dataset.f; localSet("monFilter", state.monFilter); renderMonitor();
    };
    $("#monSearch").oninput = () => renderMonitor();
    $("#monSort").onchange = () => { localSet("monSort", $("#monSort").value); renderMonitor(); };
    const savedSort = localGet("monSort"); if (savedSort) $("#monSort").value = savedSort;
    $("#monList").onclick = guard(async e => {
      const card = e.target.closest(".mon-card"); if (!card) return;
      state.stationId = card.dataset.sid; localSet("stationId", state.stationId);
      $("#stationSelect").value = state.stationId;
      $("#exportLink").href = "/api/export?" + sidQuery();
      showView("dashboard");
      await Promise.all([loadSummary(), loadChart()]);
      scrollTo({ top: 0 });
    });
    $("#alarmList").onclick = guard(async e => {
      const name = e.target.dataset.mute || e.target.dataset.unmute;
      if (!name) return;
      const mute = !!e.target.dataset.mute;
      if (mute && !confirm(`以后「${name}」类告警不再推送到微信（仍会记录在告警页）？`)) return;
      await api("/api/notify/mute", { method: "POST", body: { name, mute } });
      toast(mute ? `已设置：「${name}」不再推送` : `已恢复推送「${name}」`);
      await loadAlarms();
    });
    $$("#alarmSeg button").forEach(b => b.onclick = guard(async () => {
      $$("#alarmSeg button").forEach(x => x.classList.toggle("active", x === b));
      state.alarmActive = b.dataset.active; await loadAlarms();
    }));
    const reportMonth = $("#reportMonth");
    const setReport = () => {
      const v = reportMonth.value || state.month;
      $("#reportMonthLink").href = `/api/export/report?period=${v}`;
      $("#reportYearLink").href = `/api/export/report?period=${v.slice(0, 4)}`;
      $("#reportYearLink").textContent = `导出 ${v.slice(0, 4)} 年报`;
    };
    reportMonth.value = state.month; reportMonth.onchange = setReport; setReport();

    $("#demoBtn").onclick = guard(async () => {
      await api("/api/demo", { method: "POST" });
      toast("已生成演示电站和发电数据");
      await refreshAll();
    });

    // 电站表单
    $("#stationForm").onsubmit = guard(async e => {
      e.preventDefault();
      const f = e.target, body = Object.fromEntries(new FormData(f));
      const id = body.id; delete body.id;
      if (id) await api(`/api/stations/${id}`, { method: "PUT", body });
      else await api("/api/stations", { method: "POST", body });
      toast(id ? "电站已更新" : "电站已添加");
      fillStationForm(null);
      await refreshAll();
    });
    $("#stationCancel").onclick = () => fillStationForm(null);
    $("#priceForm").onsubmit = guard(async e => {
      e.preventDefault();
      const price = e.target.elements.price.value;
      if (!confirm(`把全部 ${state.stations.length} 座电站的电价设为 ${price} 元/kWh？以后导入的电站也会使用这个电价。`)) return;
      const r = await api("/api/stations/price", { method: "POST", body: { price } });
      toast(`已将 ${r.updated} 座电站电价设为 ${r.price} 元/kWh`);
      await refreshAll();
    });
    $("#stationsTable").onclick = guard(async e => {
      const edit = e.target.dataset.edit, del = e.target.dataset.del;
      if (edit) fillStationForm(state.stations.find(s => String(s.id) === edit));
      if (del) {
        const s = state.stations.find(x => String(x.id) === del);
        if (!confirm(`确定删除电站「${s.name}」及其全部 ${s.record_days} 条发电记录？此操作不可恢复。`)) return;
        await api(`/api/stations/${del}`, { method: "DELETE" });
        toast("电站已删除");
        await refreshAll();
      }
    });

    // 录入
    const rf = $("#recordForm");
    rf.elements.day.value = ymd(new Date());
    rf.onsubmit = guard(async e => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(rf));
      await api("/api/records", { method: "POST", body });
      toast(`已保存 ${body.day} 发电量 ${body.kwh} kWh`);
      rf.elements.kwh.value = ""; rf.elements.note.value = "";
      // 日期自动 +1 方便连续录入
      const d = new Date(body.day + "T00:00:00"); d.setDate(d.getDate() + 1);
      if (d <= new Date()) rf.elements.day.value = ymd(d);
      rf.elements.kwh.focus();
      await refreshAll();
    });

    $("#importForm").onsubmit = guard(async e => {
      e.preventDefault();
      const f = e.target;
      let csv = f.elements.csv.value;
      const file = f.elements.file.files[0];
      if (file) csv = await file.text();
      if (!csv.trim()) throw new Error("请选择文件或粘贴数据");
      const r = await api("/api/import", { method: "POST", body: { station_id: f.elements.station_id.value, csv } });
      toast(`成功导入 ${r.imported} 条` + (r.error_count ? `，${r.error_count} 行有误：${r.errors[0]}` : ""), r.error_count > 0 && !r.imported);
      f.elements.csv.value = ""; f.elements.file.value = "";
      await refreshAll();
    });

    $("#recordsMonth").onchange = guard(loadRecords);
    $("#discoverBtn").onclick = guard(async () => {
      const r = await api("/api/sync/discover", { method: "POST" });
      toast(`固德威账号下共 ${r.found} 座电站，新导入 ${r.created} 座` + (r.already ? `，${r.already} 座已存在` : ""));
      await refreshAll(); await loadSync();
    });
    $("#backfillBtn").onclick = guard(async () => {
      if (!confirm("从固德威读取所有已关联电站的历史每日发电量（最多 10 年），手动录入的数据不会被覆盖。电站多时需要几分钟，确定开始？")) return;
      renderSync(await api("/api/sync/backfill", { method: "POST", body: {} }));
    });
    $("#syncBtn").onclick = guard(async () => {
      $("#syncBtn").disabled = true; $("#syncBtn").textContent = "同步中…";
      try {
        const st = await api("/api/sync/run", { method: "POST" });
        renderSync(st);
        toast(st.message, st.ok === false);
        await refreshAll();
      } finally { $("#syncBtn").textContent = "立即同步"; $("#syncBtn").disabled = false; }
    });
    $("#recordsTable").onclick = guard(async e => {
      if (e.target.dataset.delrec) {
        if (!confirm("确定删除这条记录？")) return;
        await api(`/api/records/${e.target.dataset.delrec}`, { method: "DELETE" });
        toast("记录已删除");
        await refreshAll();
      }
      if (e.target.dataset.fill) {
        const r = JSON.parse(e.target.dataset.fill);
        for (const k of ["station_id", "day", "kwh", "note"]) rf.elements[k].value = r[k] ?? "";
        rf.scrollIntoView({ behavior: "smooth" });
        rf.elements.kwh.focus();
      }
    });

    let rz;
    addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => loadChart().catch(() => {}), 150); });
  }

  async function initAuth() {
    const a = await api("/api/auth").catch(() => ({}));
    if (!a.enabled) return;
    const b = document.createElement("button");
    b.className = "btn small"; b.textContent = "退出";
    b.onclick = async () => { await fetch("/api/logout", { method: "POST" }); location.href = "/login.html"; };
    $(".topbar-actions").appendChild(b);
  }

  initTheme();
  initAuth();
  bind();
  refreshAll().catch(e => toast(e.message, true));
})();
