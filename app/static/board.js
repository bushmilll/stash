/* Moodboards: Sheet / Columns / Rows (structured, drag a post onto another to swap) and Collage (freeform).
   Exported as PNG or PDF. Loaded after app.js and shares its helpers ($, h, api, fill, toast, modal, downloadUrl, esc, pad). */
const Board = (() => {
  const W = 1600;            // logical board width; export scales this up
  const MARGIN = 48;
  const NUM_SPACE = 30;      // room under a numbered cell for its [01] label
  const TITLE_SIZE = 64, CAP_SIZE = 11;
  const SWATCHES = [["#eceae5", "Paper"], ["#ffffff", "White"], ["#8c8a84", "Concrete"], ["#1a1a1a", "Graphite"], ["#000000", "Black"]];
  const DEFAULTS = { layout: "sheet", show_title: true, title_pos: "top", captions: true, tile_ar: "orig", fill: false, tilt: false };
  const TILE = { orig: 1.25, "3:2": 2 / 3, "16:9": 9 / 16, "9:16": 16 / 9 };   // cell height / width
  const HINTS = {
    structured: "Drag a post onto another to swap / Delete removes / ⌘Z undo / double-click text to edit",
    collage: "Drag to move / corner to resize / Delete removes / ⌘Z undo / double-click text to edit",
  };
  let B = null, st = null, view = null, selected = null, scale = 1, saveTimer = null, editing = false;
  let els = new Map(), heights = {}, undo = [], lastToast = null;

  const isText = (it) => it.type === "text";
  const media = () => B.items.filter((it) => !isText(it));
  const notes = () => B.items.filter(isText);
  const ratio = (it) => (it.mw && it.mh ? it.mh / it.mw : 1.25);
  const rand = (a, b) => a + Math.random() * (b - a);
  const lum = (hex) => {
    const n = parseInt(String(hex).replace("#", "").padEnd(6, "0").slice(0, 6), 16);
    return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  };
  const light = () => lum(B.bg) > 0.55;
  const inkColor = () => (light() ? "#0a0a0a" : "#edebe6");
  const textColor = (it) => it.color || inkColor();
  const cellColor = () => {   // image cells sit a step off the ground, like the reference sheets
    const n = parseInt(B.bg.replace("#", ""), 16), d = light() ? -12 : 18;
    const c = (v) => Math.max(0, Math.min(255, v + d)).toString(16).padStart(2, "0");
    return "#" + c(n >> 16) + c((n >> 8) & 255) + c(n & 255);
  };
  const numLabel = (n) => "[" + pad(n, 2) + "]";
  const structured = () => st.layout !== "collage";

  // ---------- open / close ----------
  async function open(id) {
    try { B = await api(`/api/boards/${id}`); } catch (e) { return toast(esc(e.message), { error: true }); }
    st = { ...DEFAULTS, ...(B.settings || {}) };
    if (!TILE[st.tile_ar]) st.tile_ar = "orig";
    if (!["sheet", "masonry", "rows", "collage"].includes(st.layout)) st.layout = "sheet";
    // older boards stored the title and caption list as text items: those are drawn by the layout now
    B.items = B.items.filter((it) => !(isText(it) && (it.auto || it.role === "title")));
    B.items.forEach((it, i) => { if (it.z == null) it.z = i + 1; });
    selected = null; undo = []; heights = {}; els = new Map();
    fill($("#paper"));
    $("#board").hidden = false; document.body.style.overflow = "hidden";
    $("#boardTitle").value = B.name;
    $("#boardGap").value = B.gap;
    drawSwatches();
    if (st.layout === "collage") ensureCollage();
    syncControls();
    render();
    requestAnimationFrame(() => render());   // fonts and sizes settle after the first paint
  }
  async function close() {
    if (!B) return;
    await flush();
    $("#board").hidden = true; document.body.style.overflow = "";
    B = null; selected = null; els = new Map(); fill($("#paper"));
    loadSidebar();
  }
  async function create() {
    const name = await askText("New moodboard", { value: "Untitled moodboard", label: "Create" });
    if (!name) return;
    try { const r = await api("/api/boards", { method: "POST", body: { name } }); await loadSidebar(); open(r.id); }
    catch (e) { toast(esc(e.message), { error: true }); }
  }

  // ---------- saving ----------
  function save() { clearTimeout(saveTimer); saveTimer = setTimeout(flush, 600); }
  async function flush() {
    clearTimeout(saveTimer);
    if (!B) return;
    const payload = {
      name: $("#boardTitle").value.trim() || B.name, bg: B.bg, gap: B.gap, width: W, settings: st,
      items: B.items.map((it) => isText(it)
        ? { type: "text", text: it.text, x: it.x, y: it.y, w: it.w, size: it.size, color: it.color, z: it.z, font: it.font }
        : { type: "media", media_id: it.media_id, cx: it.cx, cy: it.cy, cw: it.cw, ch: it.ch, rot: it.rot, z: it.z }),
    };
    try { await api(`/api/boards/${B.id}`, { method: "PUT", body: payload }); B.name = payload.name; }
    catch (e) { toast(esc(e.message), { error: true }); }
  }

  // ---------- layouts: each places the posts from y0 down and returns where they end ----------
  function layoutSheet(ms, y0, v) {
    const n = ms.length, r = TILE[st.tile_ar] || 1.25;
    let cols = n <= 3 ? n : n <= 8 ? 4 : n <= 18 ? 6 : 8;
    if (r < 1) cols = n <= 3 ? n : n <= 9 ? 3 : n <= 16 ? 4 : 5;          // wide tiles: fewer per row
    else if (r > 1.5) cols = n <= 4 ? n : n <= 12 ? 6 : 8;                  // tall tiles: more per row
    const g = Math.max(B.gap, 8), ns = st.captions ? NUM_SPACE : 0;
    const maxW = r > 1.5 ? 420 : r < 1 ? 760 : 640;
    const cw = Math.min(maxW, (W - 2 * MARGIN - (cols - 1) * g) / cols), ch = cw * r;
    ms.forEach((it, i) => {
      const c = i % cols, row = Math.floor(i / cols);
      v.media.set(it, { x: MARGIN + c * (cw + g), y: y0 + row * (ch + ns + g), w: cw, h: ch, cell: true, fit: !st.fill, num: st.captions ? i + 1 : null });
    });
    return y0 + Math.ceil(n / cols) * (ch + ns + g) - g;
  }
  function layoutMasonry(ms, y0, v) {
    const n = ms.length;
    const cols = n === 1 ? 1 : Math.max(2, Math.min(6, Math.round(Math.sqrt(n * 0.9))));
    const g = B.gap, cw = Math.min(900, (W - 2 * MARGIN - (cols - 1) * g) / cols);
    const ys = new Array(cols).fill(y0);
    for (const it of ms) {
      let c = 0; for (let j = 1; j < cols; j++) if (ys[j] < ys[c] - 0.5) c = j;
      const hh = cw * ratio(it);
      v.media.set(it, { x: MARGIN + c * (cw + g), y: ys[c], w: cw, h: hh });
      ys[c] += hh + g;
    }
    return Math.max(...ys) - g;
  }
  function layoutRows(ms, y0, v) {
    const g = B.gap, avail = W - 2 * MARGIN, target = Math.max(220, Math.min(420, avail / Math.max(2, Math.sqrt(ms.length) * 0.9)));
    let y = y0, row = [], sum = 0;
    const flushRow = (last) => {
      if (!row.length) return;
      let rh = (avail - (row.length - 1) * g) / sum;
      if (last && rh > target * 1.35) rh = target;
      let x = MARGIN;
      for (const it of row) { const w = rh / ratio(it); v.media.set(it, { x, y, w, h: rh }); x += w + g; }
      y += rh + g; row = []; sum = 0;
    };
    for (const it of ms) {
      row.push(it); sum += 1 / ratio(it);
      if (sum * target + (row.length - 1) * g >= avail) flushRow(false);
    }
    flushRow(true);
    return y - g;
  }
  function layoutCollage(ms, y0, v) {
    ensureCollage();
    let end = y0;
    for (const it of ms) {
      v.media.set(it, { x: it.cx, y: y0 + it.cy, w: it.cw, h: it.ch, rot: st.tilt ? it.rot || 0 : 0, free: true });
      end = Math.max(end, y0 + it.cy + it.ch);
    }
    return end;
  }
  const LAYOUTS = { sheet: layoutSheet, masonry: layoutMasonry, rows: layoutRows, collage: layoutCollage };

  // collage spots are stored on each post (cy is measured from where the posts start, under a top title)
  const tiltAngle = () => { const a = rand(1, 4.5); return Math.round((Math.random() < 0.5 ? -a : a) * 10) / 10; };
  function genCollage(list, base) {
    const n = list.length; if (!n) return;
    const cols = n === 1 ? 1 : n <= 4 ? n : Math.max(3, Math.min(7, Math.round(Math.sqrt(n * 1.8))));
    const cell = (W - 2 * MARGIN) / cols;
    const ys = Array.from({ length: cols }, () => base + rand(0, cell * 0.22));
    let z = Math.max(0, ...media().map((i) => i.z || 0));
    for (const it of list) {
      let c = 0; for (let j = 1; j < cols; j++) if (ys[j] < ys[c]) c = j;
      const w = Math.min(cell * rand(1.0, 1.22), W - 2 * MARGIN, 760), hh = w * ratio(it);
      const x = Math.max(MARGIN * 0.5, Math.min(W - MARGIN * 0.5 - w, MARGIN + c * cell + (cell - w) / 2 + rand(-0.08, 0.08) * cell));
      const y = ys[c] <= base + cell * 0.22 ? ys[c] : Math.max(base, ys[c] - rand(0.04, 0.12) * Math.min(hh, cell)); // tuck under the piece above
      Object.assign(it, { cx: Math.round(x), cy: Math.round(y), cw: Math.round(w), ch: Math.round(hh), rot: st.tilt ? tiltAngle() : 0, z: ++z });
      ys[c] = y + hh;
    }
  }
  function ensureCollage() {
    const ms = media(), fresh = ms.filter((it) => it.cx == null);
    if (!fresh.length) return;
    const placed = ms.filter((it) => it.cx != null);
    genCollage(fresh, placed.length ? Math.max(...placed.map((it) => it.cy + it.ch)) + Math.max(B.gap, 12) * 2 : 0);
  }
  function shuffle() {
    const ms = media();
    for (let i = ms.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [ms[i], ms[j]] = [ms[j], ms[i]]; }
    genCollage(ms, 0);
    render(); save();
  }

  function captions(ms, y, v) {
    const n = ms.length, listCols = Math.min(6, Math.max(2, Math.ceil(n / 4)));
    const lw = (W - 2 * MARGIN - (listCols - 1) * 24) / listCols, per = Math.ceil(n / listCols);
    let end = y;
    for (let k = 0; k < listCols; k++) {
      const chunk = ms.slice(k * per, (k + 1) * per);
      if (!chunk.length) break;
      const text = chunk.map((it, j) => {
        const cap = (it.caption || "").replace(/\s+/g, " ").replace(/#\w+/g, "").trim();
        return `${numLabel(k * per + j + 1)} ${it.username ? "@" + it.username : ""}${cap ? "\n" + (cap.length > 64 ? cap.slice(0, 62) + "…" : cap) : ""}`;
      }).join("\n\n");
      const key = "cap" + k;
      v.texts.push({ key, text, x: MARGIN + k * (lw + 24), y, w: lw, size: CAP_SIZE, mono: true });
      end = Math.max(end, y + (heights[key] ?? chunk.length * 4 * CAP_SIZE * 1.3));
    }
    return end;
  }

  function compute() {
    const v = { media: new Map(), texts: [] };
    const ms = media();
    const titleH = heights.title ?? TITLE_SIZE * 1.12;
    const title = (y) => ({ key: "title", title: true, text: B.name || "Untitled moodboard", x: MARGIN, y, w: W - 2 * MARGIN, size: TITLE_SIZE });
    const top = st.show_title && st.title_pos !== "bottom";
    let y = MARGIN;
    if (top) { v.texts.push(title(y)); y += titleH + 36; }
    v.y0 = y;
    let end = ms.length ? LAYOUTS[st.layout](ms, y, v) : y;
    if (st.layout === "sheet" && st.captions && ms.length) end = captions(ms, end + 48, v);
    if (st.show_title && !top) { const ty = end + (ms.length ? 56 : 0); v.texts.push(title(ty)); end = ty + titleH; }
    const noteEnd = notes().filter((it) => it.x != null).map((it) => it.y + (it._h || it.size * 1.3));
    v.H = Math.max(900, end + MARGIN, ...noteEnd.map((b) => b + MARGIN));
    return v;
  }

  // ---------- rendering (elements are kept and moved, so swaps and reflows animate) ----------
  function mediaEl(it) {
    const el = h("div", { class: "bitem media" });
    el._item = it;
    el.append(h("div", { class: "img" }, h("img", { alt: "", draggable: "false", decoding: "async" })));
    if (it.kind === "video") el.append(h("span", { class: "vbadge" }, "Video"));
    el.append(h("span", { class: "num" }), h("span", { class: "grip" }));
    return el;
  }
  function placeMedia(el, it, p) {
    Object.assign(el.style, { left: p.x + "px", top: p.y + "px", width: p.w + "px", height: p.h + "px",
      transform: p.rot ? `rotate(${p.rot}deg)` : "", zIndex: p.free ? it.z || 1 : 1 });
    el.classList.toggle("fit", !!(p.cell && p.fit));
    el.classList.toggle("free", !!p.free);
    el.querySelector(".img").style.background = p.cell && p.fit ? cellColor() : "";
    const num = el.querySelector(".num");
    num.textContent = p.num ? numLabel(p.num) : ""; num.style.color = inkColor();
    const img = el.querySelector("img");
    const wantFull = p.w * scale * (window.devicePixelRatio || 1) > 500;
    if (!img.getAttribute("src") || (wantFull && !el._full)) {
      const full = it.kind === "video" ? (it.poster_src || it.thumb_src) : it.src;
      img.src = wantFull ? full : (it.thumb_src || full); el._full = wantFull;
    }
  }
  function textEl(it, derived) {
    const el = h("div", { class: "bitem text" + (derived ? " derived" : "") + (it.mono || it.font === "mono" ? " mono" : "") + (it.title ? " title" : "") });
    el._item = it;
    el.append(h("span", { class: "t" }));
    if (!derived) el.append(h("span", { class: "grip" }));
    return el;
  }
  function placeText(el, it, z) {
    Object.assign(el.style, { left: it.x + "px", top: it.y + "px", width: it.w + "px", fontSize: it.size + "px", color: textColor(it), zIndex: z });
    const t = el.querySelector(".t");
    if (t.contentEditable !== "true" && t.textContent !== it.text) t.textContent = it.text;
  }
  function paint() {
    const paper = $("#paper");
    paper.style.background = B.bg;
    paper.classList.toggle("structured", structured());
    const live = new Set();
    const get = (key, make) => { let el = els.get(key); if (!el) { el = make(); els.set(key, el); paper.append(el); } live.add(el); return el; };
    for (const [it, p] of view.media) placeMedia(get(it, () => mediaEl(it)), it, p);
    for (const it of notes()) { if (it.x == null) continue; const el = get(it, () => textEl(it, false)); el._item = it; placeText(el, it, 1000 + (it.z || 0)); }
    for (const t of view.texts) { const el = get("k:" + t.key, () => textEl(t, true)); el._item = t; placeText(el, t, 999); }
    if (!B.items.length) {
      get("empty", () => h("div", { class: "board-empty" },
        h("div", {}, h("div", { style: "font-weight:500;font-size:28px;letter-spacing:-.02em;margin-bottom:8px" }, "Empty board"),
          "Close this, select posts in your library, then choose Moodboard in the bar at the bottom."))).style.color = inkColor();
    }
    for (const [k, el] of els) if (!live.has(el)) { el.remove(); els.delete(k); }
    for (const [k, el] of els) el.classList.toggle("sel", k === selected);
    document.querySelectorAll("#swatches button").forEach((b) => b.classList.toggle("on", b.dataset.c === B.bg));
  }
  function measure() {   // real heights of text, so titles and caption lists push things down by the right amount
    let changed = false;
    for (const [k, el] of els) {
      if (!el.classList?.contains("text")) continue;
      const hh = el.offsetHeight;
      if (typeof k === "string") { const key = k.slice(2); if (Math.abs((heights[key] ?? -99) - hh) > 1) { heights[key] = hh; changed = true; } }
      else k._h = hh;
    }
    return changed;
  }
  function render() {
    if (!B) return;
    view = compute(); paint();
    if (measure()) { view = compute(); paint(); }
    fit();
  }
  function fit() {
    if (!B || !view) return;
    const avail = $("#boardScroll").clientWidth - (window.innerWidth < 860 ? 20 : 56);
    scale = Math.min(1, avail / W);
    const H = view.H, paper = $("#paper");
    paper.style.width = W + "px"; paper.style.height = H + "px"; paper.style.transform = `scale(${scale})`;
    const wrap = $("#paperWrap"); wrap.style.width = W * scale + "px"; wrap.style.height = H * scale + "px";
    $("#boardInfo").textContent = `${pad(media().length)} images / ${W} × ${Math.round(H)} / export ${W * 2} px wide`;
  }
  function select(it) {
    selected = it;
    for (const [k, el] of els) el.classList.toggle("sel", k === it);
  }
  function toFront(it) { it.z = Math.max(0, ...B.items.map((i) => i.z || 0)) + 1; }
  function drawSwatches() {
    const custom = h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(B.bg) ? B.bg : "#eceae5", title: "Custom color", "aria-label": "Custom background" });
    custom.oninput = () => { B.bg = custom.value; render(); save(); };
    fill($("#swatches"), ...SWATCHES.map(([c, n]) => h("button", { "data-c": c, title: n, "aria-label": n, style: `background:${c}`, onclick: () => { B.bg = c; render(); save(); } })), custom);
  }

  // ---------- settings ----------
  function syncControls() {
    const on = (sel, val) => document.querySelectorAll(sel).forEach((b) => b.classList.toggle("on", b.dataset[Object.keys(b.dataset)[0]] === val));
    on("[data-ly]", st.layout);
    on("[data-title]", st.show_title ? st.title_pos : "off");
    on("[data-cap]", st.captions ? "1" : "0");
    on("[data-ar]", st.tile_ar);
    on("[data-fill]", st.fill ? "1" : "0");
    on("[data-tilt]", st.tilt ? "1" : "0");
    document.querySelectorAll("[data-only]").forEach((g) => { g.hidden = g.dataset.only !== st.layout; });
    $("#boardHint").textContent = HINTS[structured() ? "structured" : "collage"];
  }
  function set(patch) {
    Object.assign(st, patch);
    if (st.layout === "collage") ensureCollage();
    syncControls(); render(); save();
  }
  function setLayout(kind) { select(null); set({ layout: kind }); }
  function setTilt(on) {
    for (const it of media()) it.rot = on ? tiltAngle() : 0;
    set({ tilt: on });
  }

  // ---------- pointer interactions ----------
  function boardPoint(e) {
    const r = $("#paper").getBoundingClientRect();
    return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
  }
  function postAt(pt, except) {
    for (const [it, p] of view.media) {
      if (it !== except && pt.x >= p.x && pt.x <= p.x + p.w && pt.y >= p.y && pt.y <= p.y + p.h + (p.num ? NUM_SPACE : 0)) return it;
    }
    return null;
  }
  function bindPaper() {
    const paper = $("#paper");
    let drag = null;
    paper.addEventListener("pointerdown", (e) => {
      if (editing) return;
      const el = e.target.closest(".bitem");
      if (!el || el.classList.contains("derived") || !el._item) { select(null); return; }
      const it = el._item, isMedia = !isText(it), free = !isMedia || st.layout === "collage";
      select(it);
      if (free) { toFront(it); el.style.zIndex = isMedia ? it.z : 1000 + it.z; }
      drag = { it, el, free, isMedia, started: false, target: null, mode: free && e.target.classList.contains("grip") ? "resize" : "move",
        sx: e.clientX, sy: e.clientY, x: it.x, y: it.y, w: it.w, size: it.size, cx: it.cx, cy: it.cy, cw: it.cw, ch: it.ch, p: view.media.get(it) };
      paper.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    paper.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = (e.clientX - drag.sx) / scale, dy = (e.clientY - drag.sy) / scale, it = drag.it;
      if (!drag.started && Math.hypot(dx, dy) * scale < 4) return;
      drag.started = true;
      if (!drag.isMedia) {   // notes: free everywhere
        if (drag.mode === "move") { it.x = Math.round(drag.x + dx); it.y = Math.round(Math.max(0, drag.y + dy)); }
        else { const nw = Math.max(60, drag.w + dx); it.size = Math.max(8, Math.round(drag.size * nw / drag.w)); it.w = nw; }
        placeText(drag.el, it, 1000 + it.z);
      } else if (drag.free) {   // collage
        if (drag.mode === "move") { it.cx = Math.round(drag.cx + dx); it.cy = Math.round(Math.max(-view.y0, drag.cy + dy)); }
        else { const nw = Math.max(60, drag.cw + dx); it.cw = Math.round(nw); it.ch = Math.round(drag.ch * nw / drag.cw); }
        placeMedia(drag.el, it, { x: it.cx, y: view.y0 + it.cy, w: it.cw, h: it.ch, rot: st.tilt ? it.rot || 0 : 0, free: true });
      } else {   // structured: lift the post and show where it would land
        drag.el.classList.add("lifting");
        drag.el.style.left = drag.p.x + dx + "px"; drag.el.style.top = drag.p.y + dy + "px";
        const target = postAt(boardPoint(e), it);
        if (target !== drag.target) {
          if (drag.target) els.get(drag.target)?.classList.remove("drop");
          if (target) els.get(target)?.classList.add("drop");
          drag.target = target;
        }
      }
    });
    const end = () => {
      if (!drag) return;
      const d = drag; drag = null;
      if (!d.started) return;
      if (d.isMedia && !d.free) {
        d.el.classList.remove("lifting");
        if (d.target) {
          els.get(d.target)?.classList.remove("drop");
          const i = B.items.indexOf(d.it), j = B.items.indexOf(d.target);
          [B.items[i], B.items[j]] = [B.items[j], B.items[i]];
          save();
        }
        render();
      } else { render(); save(); }
    };
    paper.addEventListener("pointerup", end); paper.addEventListener("pointercancel", end);
    paper.addEventListener("dblclick", (e) => { const el = e.target.closest(".bitem.text"); if (el) edit(el); });
  }
  function edit(el) {
    const t = el.querySelector(".t"), it = el._item;
    editing = true; t.contentEditable = "true"; t.focus();
    const r = document.createRange(); r.selectNodeContents(t); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    t.addEventListener("blur", () => {
      editing = false; t.contentEditable = "false";
      const text = t.innerText.replace(/\n$/, "").trim();
      if (it.title) { B.name = text || B.name; $("#boardTitle").value = B.name; }
      else if (el.classList.contains("derived")) { /* caption list: rebuilt from the posts */ }
      else it.text = text || "Note";
      t.textContent = "";
      render(); save();
    }, { once: true });
  }
  function addNote() {
    const it = { type: "text", text: "Note", x: MARGIN, y: Math.max(MARGIN, Math.round($("#boardScroll").scrollTop / scale) + 200), w: 520, size: 28, z: 0 };
    B.items.push(it); toFront(it); render(); select(it);
    const el = els.get(it); if (el) edit(el);
    save();
  }
  function removeSelected() {
    if (!selected || isText(selected) && !B.items.includes(selected)) return;
    const idx = B.items.indexOf(selected);
    if (idx < 0) return;
    const wasMedia = !isText(selected);
    B.items.splice(idx, 1);
    undo.push({ it: selected, idx }); if (undo.length > 50) undo.shift();
    selected = null; render(); save();
    lastToast?.remove();
    lastToast = toast(wasMedia ? "Removed from this board (it stays in your library)" : "Note removed", { ms: 5000, action: { label: "Undo", fn: undoLast } });
  }
  function undoLast() {
    const u = undo.pop();
    if (!u || !B) return;
    B.items.splice(Math.min(u.idx, B.items.length), 0, u.it);
    lastToast?.remove(); lastToast = null;
    render(); select(u.it); save();
  }

  // ---------- export ----------
  const loadImg = (src) => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("An image failed to load")); i.src = src; });
  function wrapText(ctx, text, maxW) {
    const lines = [];
    for (const para of String(text).split("\n")) {
      let line = "";
      for (const word of para.split(" ")) {
        const test = line ? line + " " + word : word;
        if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = word; } else line = test;
      }
      lines.push(line);
    }
    return lines;
  }
  async function renderCanvas() {
    render();
    const H = view.H;
    const f = Math.min(2, 15000 / H, Math.sqrt(1e8 / (W * H)));
    const cv = document.createElement("canvas"); cv.width = Math.round(W * f); cv.height = Math.round(H * f);
    const ctx = cv.getContext("2d");
    ctx.fillStyle = B.bg; ctx.fillRect(0, 0, cv.width, cv.height);
    const posts = [...view.media].sort((a, b) => (a[1].free ? a[0].z || 0 : 0) - (b[1].free ? b[0].z || 0 : 0));
    const imgs = await Promise.all(posts.map(async ([it]) => {
      if (it.kind === "video") { const r = await api(`/api/media/${it.media_id}/still`, { method: "POST", body: { t: 0.8 } }); return loadImg(r.url); }
      return loadImg(it.src);
    }));
    await (document.fonts?.ready || Promise.resolve());
    const mono = `"Plex Mono", "IBM Plex Mono", Menlo, monospace`, sans = `"Inter Tight", "Helvetica Neue", Arial, sans-serif`;
    ctx.textBaseline = "top";
    posts.forEach(([it, p], i) => {
      const img = imgs[i], x = p.x * f, y = p.y * f, w = p.w * f, hh = p.h * f;
      ctx.save();
      if (p.rot) { ctx.translate(x + w / 2, y + hh / 2); ctx.rotate(p.rot * Math.PI / 180); ctx.translate(-(x + w / 2), -(y + hh / 2)); }
      if (p.cell && p.fit) {
        ctx.fillStyle = cellColor(); ctx.fillRect(x, y, w, hh);
        const s = Math.min(w / img.width, hh / img.height), dw = img.width * s, dh = img.height * s;
        ctx.drawImage(img, x + (w - dw) / 2, y + (hh - dh) / 2, dw, dh);
      } else {   // cover-crop to the box
        const s = Math.max(w / img.width, hh / img.height), sw = w / s, sh = hh / s;
        ctx.drawImage(img, (img.width - sw) / 2, (img.height - sh) / 2, sw, sh, x, y, w, hh);
      }
      ctx.restore();
      if (p.num) { ctx.font = `400 ${11 * f}px ${mono}`; ctx.fillStyle = inkColor(); ctx.fillText(numLabel(p.num), x, y + hh + 6 * f); }
    });
    const texts = [...view.texts, ...notes().filter((it) => it.x != null).sort((a, b) => (a.z || 0) - (b.z || 0))];
    for (const it of texts) {
      const isMono = it.mono || it.font === "mono";
      ctx.font = `${isMono ? 400 : 500} ${it.size * f}px ${isMono ? mono : sans}`;
      ctx.fillStyle = textColor(it);
      const lh = it.size * f * (isMono ? 1.3 : 1.12);
      let y = it.y * f;
      for (const line of wrapText(ctx, isMono ? it.text.toUpperCase() : it.text, it.w * f)) { ctx.fillText(line, it.x * f, y); y += lh; }
    }
    return cv;
  }
  async function exportAs(kind, btn) {
    if (!B.items.length) return toast("Add some images first", { error: true });
    await flush();
    const label = btn.textContent; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Rendering';
    try {
      const cv = await renderCanvas();
      const blob = await new Promise((r) => cv.toBlob(r, "image/png"));
      const name = (B.name || "moodboard").replace(/[^\w .-]+/g, "_").slice(0, 60);
      if (kind === "png" && DESKTOP) {
        savedToast(await api(`/api/desktop/save-blob?name=${encodeURIComponent(name + ".png")}`, { method: "POST", body: blob, headers: { "Content-Type": "image/png" } }));
      } else if (kind === "png") {
        const a = h("a", { href: URL.createObjectURL(blob), download: name + ".png" }); document.body.append(a); a.click(); a.remove();
        toast(`Exported <b>${esc(name)}.png</b> / ${cv.width} × ${cv.height}`);
      } else {
        const r = await api(`/api/boards/pdf?name=${encodeURIComponent(name)}`, { method: "POST", body: blob, headers: { "Content-Type": "image/png" } });
        downloadUrl(r.url); if (!DESKTOP) toast(`Exported <b>${esc(r.name)}</b>`);
      }
    } catch (e) { toast(esc(e.message), { error: true }); }
    btn.disabled = false; btn.textContent = label;
  }

  // ---------- wiring ----------
  function init() {
    bindPaper();
    $("#boardClose").onclick = close;
    document.querySelectorAll("[data-ly]").forEach((b) => { b.onclick = () => setLayout(b.dataset.ly); });
    document.querySelectorAll("[data-title]").forEach((b) => { b.onclick = () => set(b.dataset.title === "off" ? { show_title: false } : { show_title: true, title_pos: b.dataset.title }); });
    document.querySelectorAll("[data-cap]").forEach((b) => { b.onclick = () => set({ captions: b.dataset.cap === "1" }); });
    document.querySelectorAll("[data-ar]").forEach((b) => { b.onclick = () => set({ tile_ar: b.dataset.ar }); });
    document.querySelectorAll("[data-fill]").forEach((b) => { b.onclick = () => set({ fill: b.dataset.fill === "1" }); });
    document.querySelectorAll("[data-tilt]").forEach((b) => { b.onclick = () => setTilt(b.dataset.tilt === "1"); });
    $("#boardShuffle").onclick = shuffle;
    $("#boardGap").oninput = () => { B.gap = Number($("#boardGap").value); render(); save(); };
    $("#addNote").onclick = addNote;
    $("#boardTitle").onchange = () => { B.name = $("#boardTitle").value.trim() || B.name; render(); save(); };
    $("#boardPng").onclick = (e) => exportAs("png", e.currentTarget);
    $("#boardPdf").onclick = (e) => exportAs("pdf", e.currentTarget);
    $("#boardDelete").onclick = () => modal({
      title: `Delete “${B.name}”?`, text: "The moodboard is removed. The posts in it stay in your library.",
      actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Delete moodboard", danger: true, fn: async (c) => {
        await api(`/api/boards/${B.id}`, { method: "DELETE" }); c(); B = null; els = new Map(); fill($("#paper")); $("#board").hidden = true; document.body.style.overflow = ""; loadSidebar();
      } }],
    });
    window.addEventListener("resize", () => { if (B) render(); });
    document.addEventListener("keydown", (e) => {
      if (!B || $("#board").hidden || $("#modalRoot").children.length) return;
      const typing = editing || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
      if (e.key === "Escape") { if (typing) document.activeElement.blur(); else if (selected) select(null); else close(); return; }
      if (typing) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && !e.shiftKey) { e.preventDefault(); undoLast(); return; }
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
      else if (selected && e.key.startsWith("Arrow") && (isText(selected) || st.layout === "collage")) {
        e.preventDefault(); const d = e.shiftKey ? 10 : 1;
        const ddx = e.key === "ArrowRight" ? d : e.key === "ArrowLeft" ? -d : 0, ddy = e.key === "ArrowDown" ? d : e.key === "ArrowUp" ? -d : 0;
        if (isText(selected)) { selected.x += ddx; selected.y = Math.max(0, selected.y + ddy); }
        else { selected.cx += ddx; selected.cy += ddy; }
        render(); save();
      }
    }, true);
  }
  init();
  return { open, create, close };
})();
