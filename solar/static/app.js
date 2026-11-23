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
  }

  // ---------- 电站 ----------
  async function loadStations() {
    state.stations = await api("/api/stations");
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
    await Promise.all([loadSummary(), loadChart()]);
    if ($("#view-entry").classList.contains("active")) await loadRecords();
  }

  // ---------- 事件 ----------
  function bind() {
    $$(".tab").forEach(b => b.onclick = () => showView(b.dataset.view));
    $$("[data-goto]").forEach(b => b.onclick = () => showView(b.dataset.goto));

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
