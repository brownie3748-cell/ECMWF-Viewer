/* ECMWF 뷰어: static chart viewer for the manifest (schema 2) stored in Cloudflare R2.
 * Runs both in a browser (GitHub Pages <site>/app/) and inside the Capacitor Android app.
 */
"use strict";

// Chart images and index.json live in Cloudflare R2 (public bucket URL); `?data=<url>` overrides it.
const DATA_BASE = "https://pub-07aea3ace26c452cbe6112df32e49088.r2.dev/";
const SCHEMA = 2;
const PLAY_MS = 700;
const CMP_PLAY_MS = 1000;
const PREFETCH_PARALLEL = 4;      // background downloads at a time
const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

// Chart chips (top, horizontally scrollable): 500 hPa first, then the other levels from the surface up.
// Charts not listed are appended in manifest order.
const CHIPS = [
  ["gz500", "500"], ["gz500_wide", "500 광역"],
  ["msl", "지상·강수"],
  ["gz925", "925"], ["mfc925", "925 수분속"],
  ["gz850", "850"], ["mfc850", "850 수분속"],
  ["gz700", "700"], ["rh700", "700 습도"],
  ["gz300", "300"], ["gz200", "200"],
];
const LABELS = Object.fromEntries(CHIPS);

const $ = (id) => document.getElementById(id);

function dataBase() {
  const q = new URLSearchParams(location.search).get("data");
  if (q) return new URL(q, location.href).href;
  return DATA_BASE;
}

const state = {
  base: dataBase(),
  m: null,            // manifest
  model: localStorage.getItem("model") || "",
  runIdx: 0,
  chart: localStorage.getItem("chart") || "gz500",
  step: 0,
  wpsh: localStorage.getItem("wpsh") !== "0",
  timer: null,
  cmp: { frames: [], idx: 0, timer: null },
};

// ------------------------------------------------------------------ theme
function applyTheme() {
  const saved = localStorage.getItem("theme");
  const dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("theme").textContent = dark ? "☀" : "🌙";
  $("theme").setAttribute("aria-label", dark ? "밝은 테마로" : "어두운 테마로");
  document.querySelector('meta[name="theme-color"]').content = dark ? "#121417" : "#0f8f80";
}
function toggleTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  localStorage.setItem("theme", dark ? "light" : "dark");
  applyTheme();
}

// ------------------------------------------------------------------ time helpers
function parseRun(s) {
  return new Date(Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8), +s.slice(8, 10)));
}
function addH(d, h) { return new Date(d.getTime() + h * 3600e3); }
function kstParts(d) {
  const k = addH(d, state.m ? state.m.kst_offset_h : 9);
  return { mo: k.getUTCMonth() + 1, d: k.getUTCDate(), h: k.getUTCHours(), wd: WEEKDAY[k.getUTCDay()] };
}
function fmtKst(d) { const p = kstParts(d); return `${p.mo}/${p.d}(${p.wd}) ${p.h}시`; }
function fmtUtc(d) {
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${String(d.getUTCHours()).padStart(2, "0")}UTC`;
}
function runShort(s) {
  const d = parseRun(s), p = kstParts(d);
  return `${p.mo}/${p.d} ${String(p.h).padStart(2, "0")}시 발표 (${String(d.getUTCHours()).padStart(2, "0")}Z)`;
}
function runLabel(s) {
  const d = parseRun(s);
  return `${fmtKst(d)} 발표 (${String(d.getUTCHours()).padStart(2, "0")}UTC 런)`;
}

// ------------------------------------------------------------------ manifest helpers
const M = () => state.m.models.find((x) => x.id === state.model) || state.m.models[0];
const chartIn = (mod, id) => mod.charts.find((c) => c.id === id && !c.overlay_of);
const chartDef = (id) => M().charts.find((c) => c.id === id);
// WPSH line overlay for any chart on the same map domain (the overlay is drawn on the identical pixel frame).
function overlayOf(id) {
  const c = chartDef(id);
  if (!c) return null;
  return M().charts.find((o) => o.overlay_of && (chartDef(o.overlay_of) || {}).domain === c.domain) || null;
}
const run = () => M().runs[state.runIdx];
function stepsFor(r, chartId) {
  const min = chartDef(chartId).min_step || 0;
  return r.steps.filter((s) => s >= min);
}
function url(runId, chartId, step) {
  const p = state.m.path.replace("{model}", M().id).replace("{run}", runId).replace("{chart}", chartId)
    .replace("{step:03d}", String(step).padStart(3, "0"));
  return state.base + p;
}
function wpshFor(r, step) {
  if (!r.wpsh) return { drawn: false, why: "06·18UTC 런은 선을 그리지 않아요" };
  const v = r.wpsh[String(step)];
  if (!v) return { drawn: false, why: "이 시각엔 고기압 중심이 없어요" };
  return { drawn: true, H: v[0], line: v[1] };
}
function chartLabel(id) { return LABELS[id] || (chartDef(id) || {}).title || id; }

// ------------------------------------------------------------------ loading
async function loadManifest() {
  setStatus("");
  const res = await fetch(state.base + "index.json?t=" + Date.now(), { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const m = await res.json();
  if (m.schema_version !== SCHEMA) throw new Error("자료 형식이 앱과 달라요(" + m.schema_version + "). 앱을 업데이트해 주세요");
  if (!m.models || !m.models.some((x) => x.runs.length)) throw new Error("no runs");
  return m;
}

async function init(keepView) {
  try {
    const prev = state.m && run() ? run().run : null;
    const prevValid = state.m && prev ? addH(parseRun(prev), state.step) : null;
    state.m = await loadManifest();
    $("notice-link").href = state.base + "NOTICE.txt";
    buildModels();
    buildChartPicker();
    selectModel(state.model, keepView ? prev : null, prevValid);
  } catch (e) {
    console.error(e);
    setStatus("자료를 불러오지 못했어요. 인터넷 연결을 확인하고 ⟳ 를 눌러 주세요. (" + e.message + ")");
  }
}

// Switch model, keeping the run (if that model has it) and the valid time as close as possible.
function selectModel(id, keepRun, keepValid) {
  const has = (x) => x && x.runs.length;
  let mod = state.m.models.find((x) => x.id === id);
  if (!has(mod)) mod = state.m.models.find((x) => x.id === state.m.default_model && has(x)) || state.m.models.find(has);
  state.model = mod.id;
  localStorage.setItem("model", mod.id);
  $("attribution").textContent = mod.attribution || state.m.attribution;
  buildRuns();
  state.runIdx = mod.runs.length - 1;
  if (keepRun) {
    const i = mod.runs.findIndex((r) => r.run === keepRun);
    if (i >= 0) state.runIdx = i;
  }
  if (!chartIn(mod, state.chart)) state.chart = chartIn(mod, "gz500") ? "gz500" : mod.charts.find((c) => !c.overlay_of).id;
  $("run").value = String(state.runIdx);
  setStepNear(keepValid);
  render();
}

function selectChart(id) {
  stop();
  const v = currentValid();
  state.chart = id;
  localStorage.setItem("chart", id);
  setStepNear(v);
  render();
}

function setStatus(msg) { const s = $("status"); s.textContent = msg; s.hidden = !msg; }

// ------------------------------------------------------------------ UI builders
function buildModels() {
  const nav = $("models");
  nav.innerHTML = "";
  state.m.models.forEach((mod) => {
    const b = document.createElement("button");
    b.className = "model";
    b.textContent = mod.label;
    b.dataset.id = mod.id;
    b.onclick = () => {
      if (mod.id === state.model || b.disabled) return;
      stop();
      selectModel(mod.id, run() ? run().run : null, currentValid());
    };
    nav.appendChild(b);
  });
}

// A model button is disabled while it has no runs or does not offer the chart on screen.
function updateModels() {
  document.querySelectorAll(".model").forEach((b) => {
    const mod = state.m.models.find((x) => x.id === b.dataset.id);
    const ok = mod.runs.length && chartIn(mod, state.chart);
    b.disabled = !ok;
    b.title = ok ? "" : (mod.runs.length ? `${mod.label}에는 이 차트가 없어요` : "자료 준비 중");
    b.classList.toggle("on", mod.id === state.model);
  });
}

function buildRuns() {
  const sel = $("run");
  sel.innerHTML = "";
  M().runs.forEach((r, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = runShort(r.run) + (i === M().runs.length - 1 ? " 최신" : "");
    sel.appendChild(o);
  });
}

function buildChartPicker() {
  const nav = $("charts");
  nav.innerHTML = "";
  const all = [];
  state.m.models.forEach((mod) => mod.charts.forEach((c) => { if (!c.overlay_of && !all.includes(c.id)) all.push(c.id); }));
  const order = CHIPS.map(([id]) => id).filter((id) => all.includes(id)).concat(all.filter((id) => !LABELS[id]));
  order.forEach((id) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.dataset.id = id;
    b.textContent = LABELS[id] || id;
    b.onclick = () => { if (!b.disabled && id !== state.chart) selectChart(id); };
    nav.append(b);
  });
}

function updateChartPicker() {
  document.querySelectorAll(".chip").forEach((b) => {
    const ok = !!chartIn(M(), b.dataset.id);
    b.disabled = !ok;
    b.title = ok ? "" : `${M().label}에는 없는 차트`;
    b.classList.toggle("on", b.dataset.id === state.chart);
  });
  const on = document.querySelector(".chip.on");
  if (on) on.scrollIntoView({ inline: "nearest", block: "nearest" });
}

function currentValid() { return addH(parseRun(run().run), state.step); }

function setStepNear(valid) {
  const steps = stepsFor(run(), state.chart);
  if (!valid) { state.step = steps[0]; return; }
  const want = (valid - parseRun(run().run)) / 3600e3;
  let best = steps[0];
  for (const s of steps) if (Math.abs(s - want) < Math.abs(best - want)) best = s;
  state.step = best;
}

// ------------------------------------------------------------------ step grid + background prefetch
const loaded = new Set();                 // image URLs known to be in the browser cache
const pf = { gen: 0, queue: [], active: 0 };

// Every step any run of this model has (so a short 06/18Z run shows the later steps as unavailable).
function gridSteps() {
  const s = new Set();
  M().runs.forEach((r) => r.steps.forEach((x) => s.add(x)));
  return [...s].sort((a, b) => a - b);
}

function overlayUrl(r, step) {
  const ov = overlayOf(state.chart);
  return ov && state.wpsh && wpshFor(r, step).drawn ? url(r.run, ov.id, step) : null;
}

function buildStepGrid() {
  const grid = $("steps");
  grid.innerHTML = "";
  const r = run();
  const avail = new Set(stepsFor(r, state.chart));
  gridSteps().forEach((s) => {
    const b = document.createElement("button");
    b.className = "step";
    b.dataset.step = String(s);
    b.textContent = String(s).padStart(3, "0");
    if (!avail.has(s)) { b.classList.add("na"); b.disabled = true; }
    b.onclick = () => { if (!b.disabled) { stop(); state.step = s; render(); } };
    grid.append(b);
  });
  updateStepGrid();
}

function updateStepGrid() {
  const r = run();
  let n = 0, total = 0;
  document.querySelectorAll("#steps .step").forEach((b) => {
    const s = +b.dataset.step;
    b.classList.toggle("cur", s === state.step);
    if (b.disabled) return;
    total++;
    const ok = loaded.has(url(r.run, state.chart, s));
    b.classList.toggle("ok", ok);
    if (ok) n++;
  });
  $("steps-count").textContent = `${n} / ${total}`;
}

// Download every step of the chart on screen, nearest to the current step first.
function prefetch() {
  const gen = ++pf.gen;
  const r = run();
  const steps = stepsFor(r, state.chart).slice().sort((a, b) => Math.abs(a - state.step) - Math.abs(b - state.step));
  pf.queue = [];
  steps.forEach((s) => {
    pf.queue.push(url(r.run, state.chart, s));
    const o = overlayUrl(r, s);
    if (o) pf.queue.push(o);
  });
  pump(gen);
}
function pump(gen) {
  while (gen === pf.gen && pf.active < PREFETCH_PARALLEL && pf.queue.length) {
    const u = pf.queue.shift();
    if (loaded.has(u)) continue;
    pf.active++;
    const im = new Image();
    const done = (ok) => {
      pf.active--;
      if (ok) loaded.add(u);
      updateStepGrid();
      pump(pf.gen);
    };
    im.onload = () => done(true);
    im.onerror = () => done(false);
    im.src = u;
  }
}

// ------------------------------------------------------------------ main render
let gridKey = "";
function render() {
  const r = run();
  const steps = stepsFor(r, state.chart);
  const i = Math.max(0, steps.indexOf(state.step));
  state.step = steps[i];

  updateModels();
  updateChartPicker();

  const slider = $("slider");
  slider.max = String(steps.length - 1);
  slider.value = String(i);
  $("prev").disabled = i === 0;
  $("next").disabled = i === steps.length - 1;

  const valid = addH(parseRun(r.run), state.step);
  $("valid-kst").textContent = fmtKst(valid);
  $("valid-sub").textContent = `${fmtUtc(valid)} · +${state.step}h · ${M().label} ${chartLabel(state.chart)}`;

  // WPSH overlay (any chart on the same map domain as a WPSH overlay)
  const ov = overlayOf(state.chart);
  const wrap = $("wpsh-wrap");
  wrap.hidden = !ov;
  $("wpsh").checked = state.wpsh;
  let info = "";
  if (ov) {
    const w = wpshFor(r, state.step);
    info = w.drawn ? `H = ${w.H} gpm · 빨간 선 = ${w.line} gpm (500 hPa)` : w.why;
    wrap.classList.toggle("disabled", !w.drawn);
  }
  $("wpsh-info").textContent = ov ? info : "";
  const baseUrl = url(r.run, state.chart, state.step);
  showImages($("img-base"), $("img-overlay"), baseUrl, overlayUrl(r, state.step), true);

  const key = `${M().id}|${r.run}|${state.chart}|${state.wpsh}`;
  if (key !== gridKey) { gridKey = key; buildStepGrid(); prefetch(); } else updateStepGrid();
}

function showImages(base, overlay, baseUrl, ovUrl, withSpinner) {
  if (withSpinner) $("loading").hidden = loaded.has(baseUrl);
  base.onload = () => {
    loaded.add(baseUrl);
    if (withSpinner) { $("loading").hidden = true; updateStepGrid(); }
  };
  base.onerror = () => { if (withSpinner) $("loading").hidden = true; setStatus("이미지를 불러오지 못했어요."); };
  if (base.getAttribute("src") !== baseUrl) base.src = baseUrl; else if (withSpinner) $("loading").hidden = true;
  if (ovUrl) { overlay.src = ovUrl; overlay.hidden = false; } else { overlay.hidden = true; overlay.removeAttribute("src"); }
}

// ------------------------------------------------------------------ time controls
function move(d) {
  const steps = stepsFor(run(), state.chart);
  const i = steps.indexOf(state.step) + d;
  if (i < 0 || i >= steps.length) return false;
  state.step = steps[i];
  render();
  return true;
}
function stop() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  $("play").textContent = "▶ 재생";
}
function togglePlay() {
  if (state.timer) return stop();
  $("play").textContent = "⏸ 정지";
  state.timer = setInterval(() => {
    if (!move(1)) { state.step = stepsFor(run(), state.chart)[0]; render(); }
  }, PLAY_MS);
}

// ------------------------------------------------------------------ past-run comparison
function openCompare() {
  stop();
  const valid = currentValid();
  const frames = [];
  for (const r of M().runs) {
    const step = (valid - parseRun(r.run)) / 3600e3;
    if (stepsFor(r, state.chart).includes(step)) frames.push({ run: r, step });
  }
  state.cmp.frames = frames;                       // oldest -> newest
  state.cmp.idx = frames.length - 1;
  $("cmp-title").textContent = `과거 런 비교 · ${M().label} ${chartLabel(state.chart)}`;
  $("cmp-sub").textContent = `${fmtKst(valid)} (${fmtUtc(valid)}) 예상 · 런 ${frames.length}개`;
  const dots = $("cmp-dots");
  dots.innerHTML = "";
  frames.forEach((f, i) => {
    const b = document.createElement("button");
    b.className = "dot" + (i === frames.length - 1 ? " last" : "");
    b.setAttribute("aria-label", runLabel(f.run.run));
    b.onclick = () => { cmpStop(); state.cmp.idx = i; cmpRender(); };
    dots.appendChild(b);
  });
  frames.forEach((f) => {                         // download every frame up front
    preloadUrl(url(f.run.run, state.chart, f.step));
    const o = overlayUrl(f.run, f.step);
    if (o) preloadUrl(o);
  });
  $("cmp-slider").max = String(Math.max(0, frames.length - 1));
  cmpRender();                                     // opens on the newest run; play only when asked
  $("compare-dlg").showModal();
}

function preloadUrl(u) {
  if (loaded.has(u)) return;
  const im = new Image();
  im.onload = () => { loaded.add(u); if ($("compare-dlg").open) cmpRender(true); };
  im.src = u;
}
const cmpReady = (f) => loaded.has(url(f.run.run, state.chart, f.step));

function cmpRender(fromLoad) {
  const { frames, idx } = state.cmp;
  const f = frames[idx];
  if (!f) return;
  // Until the frame's image has arrived, dim the previous picture and say so (it is not this run).
  const ready = cmpReady(f);
  document.querySelector("#compare-dlg .viewer").classList.toggle("stale", !ready);
  $("cmp-loading").hidden = ready;
  if (fromLoad && !ready) return;
  const newest = idx === frames.length - 1;
  const ov = overlayOf(state.chart);
  const w = ov ? wpshFor(f.run, f.step) : null;
  showImages($("cmp-base"), $("cmp-overlay"), url(f.run.run, state.chart, f.step), overlayUrl(f.run, f.step), false);
  const lbl = $("cmp-label");
  lbl.className = "cmp-label" + (newest ? " newest" : "");
  lbl.innerHTML = "";
  lbl.append(`${runLabel(f.run.run)} · +${f.step}h`);
  if (newest) { const b = document.createElement("span"); b.className = "badge"; b.textContent = "최신"; lbl.append(b); }
  if (ov && state.wpsh) {
    const d = document.createElement("div");
    d.className = "cmp-wpsh";
    d.textContent = w.drawn ? `H−50 = ${w.line} gpm (H ${w.H})` : w.why;
    lbl.append(d);
  }
  document.querySelector("#compare-dlg .viewer").classList.toggle("newest", newest);
  document.querySelectorAll("#cmp-dots .dot").forEach((d, i) => d.classList.toggle("on", i === idx));
  $("cmp-slider").value = String(idx);
  $("cmp-prev").disabled = idx === 0;
  $("cmp-next").disabled = idx === frames.length - 1;
}
function cmpStop() {
  if (state.cmp.timer) { clearInterval(state.cmp.timer); state.cmp.timer = null; }
  $("cmp-play").textContent = "▶ 재생";
}
function cmpPlay() {
  if (state.cmp.timer) return cmpStop();
  $("cmp-play").textContent = "⏸ 정지";
  state.cmp.timer = setInterval(() => {
    const n = state.cmp.frames.length;
    if (state.cmp.idx >= n - 1) { cmpStop(); return; }   // stop on the newest run
    if (!cmpReady(state.cmp.frames[state.cmp.idx]) || !cmpReady(state.cmp.frames[state.cmp.idx + 1])) return;  // wait for images
    state.cmp.idx += 1;
    cmpRender();
  }, CMP_PLAY_MS);
}
function closeCompare() { cmpStop(); $("compare-dlg").close(); }

// ------------------------------------------------------------------ pinch / pan / double-tap zoom
function enableZoom(viewer, layer) {
  const pts = new Map();
  let s = 1, x = 0, y = 0, start = null, lastTap = 0;
  const apply = () => {
    const w = viewer.clientWidth, h = viewer.clientHeight;
    s = Math.min(5, Math.max(1, s));
    x = Math.min(0, Math.max(w - w * s, x));
    y = Math.min(0, Math.max(h - h * s, y));
    layer.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
  };
  const reset = () => { s = 1; x = 0; y = 0; apply(); };
  viewer.addEventListener("pointerdown", (e) => {
    viewer.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    start = snapshot();
    const now = Date.now();
    if (pts.size === 1 && now - lastTap < 300) {         // double tap
      const r = viewer.getBoundingClientRect();
      if (s > 1) reset(); else { const px = e.clientX - r.left, py = e.clientY - r.top; s = 2.5; x = px - px * s; y = py - py * s; apply(); }
    }
    lastTap = now;
  });
  const end = (e) => { pts.delete(e.pointerId); start = snapshot(); };
  viewer.addEventListener("pointerup", end);
  viewer.addEventListener("pointercancel", end);
  viewer.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const cur = snapshot();
    if (!start) return;
    if (pts.size === 1 && s > 1) { x = start.x + (cur.cx - start.cx); y = start.y + (cur.cy - start.cy); apply(); }
    if (pts.size === 2 && start.d > 0) {
      const r = viewer.getBoundingClientRect();
      const ns = start.s * (cur.d / start.d);
      const cx = start.cx - r.left, cy = start.cy - r.top;
      x = cx - (cx - start.x) * (ns / start.s) + (cur.cx - start.cx);
      y = cy - (cy - start.y) * (ns / start.s) + (cur.cy - start.cy);
      s = ns; apply();
    }
  });
  viewer.addEventListener("wheel", (e) => {          // desktop browsers
    e.preventDefault();
    const r = viewer.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const ns = Math.min(5, Math.max(1, s * (e.deltaY < 0 ? 1.2 : 1 / 1.2)));
    x = px - (px - x) * (ns / s); y = py - (py - y) * (ns / s); s = ns; apply();
  }, { passive: false });
  function snapshot() {
    const p = [...pts.values()];
    const cx = p.reduce((a, q) => a + q.x, 0) / (p.length || 1);
    const cy = p.reduce((a, q) => a + q.y, 0) / (p.length || 1);
    const d = p.length === 2 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0;
    return { s, x, y, cx, cy, d };
  }
  return reset;
}

// ------------------------------------------------------------------ wiring
function wire() {
  applyTheme();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  $("theme").onclick = toggleTheme;
  $("info").onclick = () => $("info-dlg").showModal();
  $("info-close").onclick = () => $("info-dlg").close();
  $("run").onchange = (e) => { stop(); const v = currentValid(); state.runIdx = +e.target.value; setStepNear(v); render(); };
  $("reload").onclick = () => { stop(); init(true); };
  $("prev").onclick = () => { stop(); move(-1); };
  $("next").onclick = () => { stop(); move(1); };
  $("play").onclick = togglePlay;
  $("slider").oninput = (e) => { stop(); state.step = stepsFor(run(), state.chart)[+e.target.value]; render(); };
  $("wpsh").onchange = (e) => { state.wpsh = e.target.checked; localStorage.setItem("wpsh", state.wpsh ? "1" : "0"); render(); };
  $("compare").onclick = openCompare;
  $("cmp-close").onclick = closeCompare;
  $("cmp-prev").onclick = () => { cmpStop(); state.cmp.idx -= 1; cmpRender(); };
  $("cmp-next").onclick = () => { cmpStop(); state.cmp.idx += 1; cmpRender(); };
  $("cmp-slider").oninput = (e) => { cmpStop(); state.cmp.idx = +e.target.value; cmpRender(); };
  $("cmp-play").onclick = () => { if (!state.cmp.timer && state.cmp.idx >= state.cmp.frames.length - 1) state.cmp.idx = 0; cmpPlay(); };
  $("compare-dlg").addEventListener("close", cmpStop);
  enableZoom($("viewer"), $("zoom"));
  enableZoom(document.querySelector("#compare-dlg .viewer"), $("cmp-zoom"));
  document.addEventListener("visibilitychange", () => { if (document.hidden) { stop(); cmpStop(); } });

  // Android back button (Capacitor App plugin): close an open sheet first, then leave the app.
  const App = window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.App;
  if (App) {
    App.addListener("backButton", () => {
      if ($("compare-dlg").open) closeCompare();
      else if ($("info-dlg").open) $("info-dlg").close();
      else App.exitApp();
    });
    App.addListener("resume", () => init(true));
  }
}

wire();
init(false);
