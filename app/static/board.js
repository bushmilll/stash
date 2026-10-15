/* Moodboards: Sheet / Columns / Rows (structured, drag a post onto another to swap) and Collage (freeform).
   Video tiles can loop a section you pick, and show a still you pick. Exported as PNG, PDF or MP4.
   Loaded after app.js and shares its helpers ($, h, api, fill, toast, modal, downloadUrl, esc, pad). */
const Board = (() => {
  const W = 1600;            // logical board width; export scales this up
  const MARGIN_DEFAULT = 48;
  const MARGIN = () => (st && st.margin != null ? st.margin : MARGIN_DEFAULT);   // space around the edges (0 = images to the edge)
  const TITLE_IN = () => Math.max(MARGIN(), 28);   // the title keeps a little air even at zero margin
  const NUM_SPACE = 30;      // room under a numbered cell for its [01] label
  const TITLE_SIZE = 64, CAP_SIZE = 11;
  const SWATCHES = [["#eceae5", "Paper"], ["#ffffff", "White"], ["#8c8a84", "Concrete"], ["#1a1a1a", "Graphite"], ["#000000", "Black"]];
  const DEFAULTS = { layout: "sheet", show_title: true, title_pos: "top", captions: "full", numbered: true, tile_ar: "orig",
    fill: false, tilt: false, video_labels: false, motion: true, size: "free", pages: "flow", rows: 0, cols: 0, margin: 48 };
  const TILE = { orig: 1.25, "1:1": 1, "3:2": 2 / 3, "16:9": 9 / 16, "9:16": 16 / 9 };   // cell height / width
  const HINTS = {
    structured: "Drag a post onto another to swap / click a video to loop it / Delete removes / ⌘Z undo",
    collage: "Drag to move / corner to resize / [ ] layer order / click a video to loop it / Delete removes / ⌘Z undo",
  };
  let B = null, st = null, view = null, selected = null, scale = 1, saveTimer = null, editing = false;
  let els = new Map(), heights = {}, undo = [], lastToast = null;

  const isText = (it) => it.type === "text";
  const isVideo = (it) => it && !isText(it) && it.kind === "video";
  const looping = (it) => isVideo(it) && it.vin != null;
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
  const stillT = (it) => it.still ?? it.vin ?? null;
  const frameUrl = (it, t) => `/api/media/${it.media_id}/frame?t=${Number(t).toFixed(2)}`;

  // ---------- open / close ----------
  async function open(id) {
    try { B = await api(`/api/boards/${id}`); } catch (e) { return toast(esc(e.message), { error: true }); }
    st = { ...DEFAULTS, ...(B.settings || {}) };
    if (st.captions === true) st.captions = "full";
    if (st.captions === false) { st.captions = "off"; if (B.settings?.numbered == null) st.numbered = false; }
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
    syncControls(); clipPanel();
    render();
    requestAnimationFrame(() => render());   // fonts and sizes settle after the first paint
    loopTick();
  }
  async function close() {
    if (!B) return;
    await flush();
    $("#board").hidden = true; document.body.style.overflow = "";
    B = null; selected = null; els = new Map(); fill($("#paper")); clipPanel();
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
        : { type: "media", media_id: it.media_id, cx: it.cx, cy: it.cy, cw: it.cw, ch: it.ch, rot: it.rot, z: it.z, vin: it.vin, vout: it.vout, still: it.still }),
    };
    try { await api(`/api/boards/${B.id}`, { method: "PUT", body: payload }); B.name = payload.name; }
    catch (e) { toast(esc(e.message), { error: true }); }
  }

  // ---------- layouts: each places the posts from y0 down and returns where they end ----------
  function layoutSheet(ms, y0, v, o = {}) {
    const n = o.nCols || ms.length, r = TILE[st.tile_ar] || 1.25;
    let cols = n <= 3 ? n : n <= 8 ? 4 : n <= 18 ? 6 : 8;
    if (r < 1) cols = n <= 3 ? n : n <= 9 ? 3 : n <= 16 ? 4 : 5;          // wide tiles: fewer per row
    else if (r > 1.5) cols = n <= 4 ? n : n <= 12 ? 6 : 8;                  // tall tiles: more per row
    const g = Math.max(B.gap, 8), ns = st.numbered ? NUM_SPACE : 0;
    const maxW = r > 1.5 ? 420 : r < 1 ? 760 : 640;
    const cw = Math.min(maxW, (W - 2 * MARGIN() - (cols - 1) * g) / cols), ch = cw * r;
    ms.forEach((it, i) => {
      const c = i % cols, row = Math.floor(i / cols);
      v.media.set(it, { x: MARGIN() + c * (cw + g), y: y0 + row * (ch + ns + g), w: cw, h: ch, cell: true, fit: !st.fill, num: st.numbered ? (o.numBase || 0) + i + 1 : null });
    });
    return y0 + Math.ceil(ms.length / cols) * (ch + ns + g) - g;
  }
  const autoCols = (n) => (n === 1 ? 1 : Math.max(2, Math.min(6, Math.round(Math.sqrt(n * 0.9)))));
  function layoutMasonry(ms, y0, v) {
    const n = ms.length;
    const cols = Math.max(1, Math.min(n, st.cols || autoCols(n)));
    const g = B.gap, cw = Math.min(900, (W - 2 * MARGIN() - (cols - 1) * g) / cols);
    const colsItems = Array.from({ length: cols }, () => []), hs = new Array(cols).fill(0);
    for (const it of ms) {
      let c = 0; for (let j = 1; j < cols; j++) if (hs[j] < hs[c] - 0.5) c = j;
      colsItems[c].push(it); hs[c] += cw * ratio(it) + g;
    }
    // even bottoms: one shared height every column can reach with the least trimming (images crop from the centre)
    const used = colsItems.map((list) => list.length ? { list, nat: list.reduce((a, it) => a + cw * ratio(it), 0), gaps: (list.length - 1) * g } : null).filter(Boolean);
    const mean = used.reduce((a, c) => a + c.nat + c.gaps, 0) / (used.length || 1);
    let T = mean;
    for (const [lo, hi] of [[0.85, 1.15], [0.75, 1.25], [0.65, 1.4], [0.5, 1.7], [0.35, 2.5]]) {
      const a = Math.max(...used.map((c) => c.nat * lo + c.gaps)), b = Math.min(...used.map((c) => c.nat * hi + c.gaps));
      if (a <= b) { T = Math.min(b, Math.max(a, mean)); break; }
    }
    let end = y0;
    colsItems.forEach((list, c) => {
      if (!list.length) return;
      const natural = list.reduce((a, it) => a + cw * ratio(it), 0), gaps = (list.length - 1) * g;
      const f = (T - gaps) / natural;
      let y = y0;
      for (const it of list) { const hh = cw * ratio(it) * f; v.media.set(it, { x: MARGIN() + c * (cw + g), y, w: cw, h: hh }); y += hh + g; }
      end = Math.max(end, y - g);
    });
    return end;
  }
  const autoRows = (n) => Math.max(1, Math.min(n, Math.round(Math.sqrt(n / 2.2)) || 1));
  // split posts (in order) into R rows whose widths are as even as possible, then stretch every row edge to edge
  function partition(weights, R) {
    const n = weights.length, pre = [0];
    for (const w of weights) pre.push(pre[pre.length - 1] + w);
    const cost = Array.from({ length: R + 1 }, () => new Array(n + 1).fill(Infinity)), cut = Array.from({ length: R + 1 }, () => new Array(n + 1).fill(0));
    cost[0][0] = 0;
    for (let r = 1; r <= R; r++) for (let i = r; i <= n; i++) for (let j = r - 1; j < i; j++) {
      const c = Math.max(cost[r - 1][j], pre[i] - pre[j]);
      if (c < cost[r][i]) { cost[r][i] = c; cut[r][i] = j; }
    }
    const rows = []; let i = n;
    for (let r = R; r >= 1; r--) { const j = cut[r][i]; rows.unshift([j, i]); i = j; }
    return rows;
  }
  function layoutRows(ms, y0, v) {
    const n = ms.length, g = B.gap, avail = W - 2 * MARGIN();
    const R = Math.max(1, Math.min(n, st.rows || autoRows(n)));
    let y = y0;
    for (const [a, b] of partition(ms.map((it) => 1 / ratio(it)), R)) {
      const row = ms.slice(a, b); if (!row.length) continue;
      const rh = (avail - (row.length - 1) * g) / row.reduce((s_, it) => s_ + 1 / ratio(it), 0);
      let x = MARGIN();
      for (const it of row) { const w = rh / ratio(it); v.media.set(it, { x, y, w, h: rh }); x += w + g; }
      y += rh + g;
    }
    return y - g;
  }
  const cs = () => view?.cs || { s: 1, ox: 0, oy: view?.y0 || 0 };
  const collageP = (it) => { const c = cs(); return { x: c.ox + it.cx * c.s, y: c.oy + it.cy * c.s, w: it.cw * c.s, h: it.ch * c.s, rot: st.tilt ? it.rot || 0 : 0, free: true }; };
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
    const cell = (W - 2 * MARGIN()) / cols;
    const ys = Array.from({ length: cols }, () => base + rand(0, cell * 0.22));
    let z = Math.max(0, ...media().map((i) => i.z || 0));
    for (const it of list) {
      let c = 0; for (let j = 1; j < cols; j++) if (ys[j] < ys[c]) c = j;
      const w = Math.min(cell * rand(1.0, 1.22), W - 2 * MARGIN(), 760), hh = w * ratio(it);
      const x = Math.max(MARGIN() * 0.5, Math.min(W - MARGIN() * 0.5 - w, MARGIN() + c * cell + (cell - w) / 2 + rand(-0.08, 0.08) * cell));
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
  // layer order in Collage: one step at a time, or all the way
  function restack(it, how) {
    if (!it || isText(it) || structured()) return;
    const ms = media().sort((a, b) => (a.z || 0) - (b.z || 0));
    const i = ms.indexOf(it);
    ms.splice(i, 1);
    const at = how === "front" ? ms.length : how === "back" ? 0 : how === "up" ? Math.min(ms.length, i + 1) : Math.max(0, i - 1);
    ms.splice(at, 0, it);
    ms.forEach((m, k) => { m.z = k + 1; });
    render(); save();
  }

  function captions(ms, y, v) { return captionsFor(ms, y, v, 0, ""); }
  function captionsFor(ms, y, v, numBase, pageKey) {
    const n = ms.length, full = st.captions === "full";
    const listCols = Math.min(6, Math.max(2, Math.ceil(n / (full ? 4 : 8))));
    const lw = (W - 2 * MARGIN() - (listCols - 1) * 24) / listCols, per = Math.ceil(n / listCols);
    let end = y;
    for (let k = 0; k < listCols; k++) {
      const chunk = ms.slice(k * per, (k + 1) * per);
      if (!chunk.length) break;
      const text = chunk.map((it, j) => {
        const num = st.numbered ? numLabel(numBase + k * per + j + 1) + " " : "";
        const who = it.username ? "@" + it.username : it.shortcode || "";
        if (!full) return num + who;
        const cap = (it.caption || "").replace(/\s+/g, " ").replace(/#\w+/g, "").trim();
        return `${num}${who}${cap ? "\n" + (cap.length > 64 ? cap.slice(0, 62) + "…" : cap) : ""}`;
      }).join(full ? "\n\n" : "\n");
      const key = "cap" + pageKey + "_" + k;
      v.texts.push({ key, text, x: MARGIN() + k * (lw + 24), y, w: lw, size: CAP_SIZE, mono: true });
      end = Math.max(end, y + (heights[key] ?? chunk.length * (full ? 4 : 1) * CAP_SIZE * 1.3));
    }
    return end;
  }

  // fixed board sizes (height / width), for slides; "free" grows with what's on it
  const SIZES = { "16:9": 9 / 16, "4:3": 3 / 4, "3:2": 2 / 3, "1:1": 1, "4:5": 5 / 4, "9:16": 16 / 9 };
  const PAGE_GAP = 64;      // space between slides on screen (not exported)
  function compute() {
    const v = { media: new Map(), texts: [], pages: [] };
    const ms = media();
    const titleH = heights.title ?? TITLE_SIZE * 1.12;
    const title = (y) => ({ key: "title", title: true, text: B.name || "Untitled moodboard", x: TITLE_IN(), y, w: W - 2 * TITLE_IN(), size: TITLE_SIZE });
    const top = st.show_title && st.title_pos !== "bottom";
    if (SIZES[st.size]) { computePaged(v, ms, title, titleH, top); return v; }
    let y = MARGIN();
    if (top) { v.texts.push(title(TITLE_IN())); y = TITLE_IN() + titleH + 36; }
    v.y0 = y;
    let end = ms.length ? LAYOUTS[st.layout](ms, y, v) : y;
    if (st.layout === "sheet" && st.captions !== "off" && ms.length) end = captions(ms, end + 48, v);
    if (st.show_title && !top) { const ty = end + (ms.length ? 56 : 0); v.texts.push(title(ty)); end = ty + titleH + (TITLE_IN() - MARGIN()); }
    const noteEnd = notes().filter((it) => it.x != null).map((it) => it.y + (it._h || it.size * 1.3));
    v.H = Math.max(MARGIN() ? 900 : 300, end + MARGIN(), ...noteEnd.map((b) => b + MARGIN()));
    v.pages = [{ top: 0, h: v.H }];
    return v;
  }
  // lay a group of posts out on its own, from y = 0: -> { media, texts, h }
  function sandbox(chunk, numBase, key) {
    const t = { media: new Map(), texts: [] };
    let end = chunk.length ? LAYOUTS[st.layout](chunk, 0, t, { numBase, nCols: media().length }) : 0;
    if (st.layout === "sheet" && st.captions !== "off" && chunk.length) {
      const before = t.texts.length;
      end = captionsFor(chunk, end + 40, t, numBase, key);
      for (let i = before; i < t.texts.length; i++) t.texts[i].page = key;
    }
    return { ...t, h: end };
  }
  function computePaged(v, ms, title, titleH, top) {
    const PH = Math.round(W * SIZES[st.size]);
    const bottomTitle = st.show_title && !top;
    const area = (first, last) => PH - 2 * MARGIN() - (first && top ? titleH + 32 + Math.max(0, TITLE_IN() - MARGIN()) : 0) - (last && bottomTitle ? titleH + 32 + Math.max(0, TITLE_IN() - MARGIN()) : 0);
    // which posts go on which slide
    const chunks = [];
    if (st.pages === "fit" || st.layout === "collage" || !ms.length) chunks.push(ms);
    else {
      let s0 = 0;
      while (s0 < ms.length) {
        const avail = area(chunks.length === 0, false) * 1.02;
        const left = ms.length - s0;
        let k = 1;
        if (st.layout === "rows") {
          // rows: fewest posts that still fit the slide's height in the chosen number of rows
          const R = st.rows || 3;
          k = Math.min(left, R);
          while (k < left && sandbox(ms.slice(s0, s0 + k), s0, "p" + chunks.length).h > avail) k++;
        } else {
          // sheet / columns: as many posts as fit
          while (k < left && sandbox(ms.slice(s0, s0 + k + 1), s0, "p" + chunks.length).h <= avail) k++;
        }
        chunks.push(ms.slice(s0, s0 + k)); s0 += k;
      }
    }
    if (bottomTitle && chunks.length > 1) {   // keep room for the title on the last slide
      const last = chunks[chunks.length - 1];
      if (sandbox(last, ms.length - last.length, "p" + (chunks.length - 1)).h > area(false, true) * 1.02) chunks.push([]);
    }
    let base = 0;
    chunks.forEach((chunk, pi) => {
      const first = pi === 0, last = pi === chunks.length - 1;
      const pageTop = pi * (PH + PAGE_GAP);
      v.pages.push({ top: pageTop, h: PH });
      let y = pageTop + MARGIN();
      if (first && top) { v.texts.push(title(pageTop + TITLE_IN())); y = pageTop + TITLE_IN() + titleH + 32; }
      const avail = area(first, last);
      const sb = sandbox(chunk, base, "p" + pi);
      const sc = sb.h > avail ? avail / sb.h : 1;     // shrink to fit when needed, centred across
      const dx = (W - 2 * MARGIN()) * (1 - sc) / 2;
      const dy = st.layout === "sheet" || sc < 1 ? 0 : (avail - sb.h) / 2;
      const tx = (x) => MARGIN() + (x - MARGIN()) * sc + dx, ty = (yy) => y + dy + yy * sc;
      for (const [it, p] of sb.media) v.media.set(it, { ...p, x: tx(p.x), y: ty(p.y), w: p.w * sc, h: p.h * sc, page: pi });
      for (const t of sb.texts) v.texts.push({ ...t, x: tx(t.x), y: ty(t.y), w: t.w * sc, size: t.size * sc, _sc: sc });
      if (st.layout === "collage") v.cs = { s: sc, ox: dx + MARGIN() * (1 - sc), oy: y + dy };
      if (last && bottomTitle) v.texts.push(title(pageTop + PH - TITLE_IN() - titleH));
      base += chunk.length;
    });
    v.y0 = MARGIN() + (top ? titleH + 32 : 0);
    v.H = chunks.length * PH + (chunks.length - 1) * PAGE_GAP;
  }
  // ---------- rendering (elements are kept and moved, so swaps and reflows animate) ----------
  function mediaEl(it) {
    const el = h("div", { class: "bitem media" });
    el._item = it;
    el.append(h("div", { class: "img" }, h("img", { alt: "", draggable: "false", decoding: "async" })));
    if (it.kind === "video") el.append(h("span", { class: "vbadge" }, "Video"), h("span", { class: "loopbadge" }, "Loop"));
    el.append(h("span", { class: "num" }), h("span", { class: "grip" }));
    return el;
  }
  function placeMedia(el, it, p) {
    Object.assign(el.style, { left: p.x + "px", top: p.y + "px", width: p.w + "px", height: p.h + "px",
      transform: p.rot ? `rotate(${p.rot}deg)` : "", zIndex: p.free ? it.z || 1 : 1 });
    el.classList.toggle("fit", !!(p.cell && p.fit));
    el.classList.toggle("free", !!p.free);
    el.classList.toggle("labels", !!st.video_labels);
    el.classList.toggle("looping", looping(it));
    const box = el.querySelector(".img");
    box.style.background = p.cell && p.fit ? cellColor() : "";
    const num = el.querySelector(".num");
    num.textContent = p.num ? numLabel(p.num) : ""; num.style.color = inkColor();
    const img = el.querySelector("img");
    const wantFull = p.w * scale * (window.devicePixelRatio || 1) > 500;
    const t = stillT(it);
    const src = isVideo(it) && t != null ? frameUrl(it, t)
      : wantFull ? (it.kind === "video" ? (it.poster_src || it.thumb_src) : it.src) : (it.thumb_src || it.src);
    const cur = img.getAttribute("src");
    if (!cur || (t != null && cur !== src) || (t == null && el._still != null) || (t == null && wantFull && !el._full)) {
      img.src = src; el._full = wantFull; el._still = t;
    }
    // a picked section plays on the board, on a loop
    let vid = box.querySelector("video");
    if (looping(it) && st.motion) {
      if (!vid) {
        vid = h("video", { src: it.src, muted: true, playsinline: true, preload: "auto" });
        vid.muted = true;
        vid.addEventListener("loadedmetadata", () => { vid.currentTime = it.vin; vid.play().catch(() => {}); }, { once: true });
        box.append(vid);
      }
      vid._item = it;
    } else if (vid) vid.remove();
  }
  function loopTick() {   // keeps every board video inside its In/Out section
    if (!B) return;
    document.querySelectorAll("#paper video").forEach((v) => {
      const it = v._item; if (!it || v.readyState < 1) return;
      const end = it.vout ?? v.duration;
      if (v.currentTime >= end - 0.03 || v.currentTime < it.vin - 0.25) { v.currentTime = it.vin; if (v.paused) v.play().catch(() => {}); }
    });
    requestAnimationFrame(loopTick);
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
    const paged = !!SIZES[st.size];
    paper.style.background = paged ? "transparent" : B.bg;
    paper.classList.toggle("paged", paged);
    paper.classList.toggle("structured", structured());
    const live = new Set();
    const get = (key, make) => { let el = els.get(key); if (!el) { el = make(); els.set(key, el); paper.append(el); } live.add(el); return el; };
    if (paged) view.pages.forEach((pg, i) => {
      const el = get("page:" + i, () => h("div", { class: "page-bg" }, h("span", { class: "page-no" })));
      Object.assign(el.style, { top: pg.top + "px", height: pg.h + "px", background: B.bg });
      el.firstChild.textContent = view.pages.length > 1 ? `Slide ${pad(i + 1, 2)} / ${pad(view.pages.length, 2)}` : "";
    });
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
      if (typeof k === "string") {   // stored at full size, even when a slide shrank it to fit
        const key = k.slice(2), hv = hh / (el._item?._sc || 1);
        if (Math.abs((heights[key] ?? -99) - hv) > 1) { heights[key] = hv; changed = true; }
      }
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
    const loops = media().filter(looping).length;
    const slides = view.pages.length, ph = view.pages[0]?.h || H;
    $("#boardInfo").textContent = `${pad(media().length)} images${loops ? ` / ${loops} looping` : ""} / ` +
      (SIZES[st.size] ? `${slides} slide${slides > 1 ? "s" : ""} at ${st.size} / export ${W * 2} × ${Math.round(ph * 2)} px` : `${W} × ${Math.round(H)} / export ${W * 2} px wide`);
  }
  function select(it) {
    selected = it;
    for (const [k, el] of els) el.classList.toggle("sel", k === it);
    syncControls(); clipPanel();
  }
  function toFront(it) { it.z = Math.max(0, ...B.items.filter((i) => isText(i) === isText(it)).map((i) => i.z || 0)) + 1; }
  function drawSwatches() {
    const custom = h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(B.bg) ? B.bg : "#eceae5", title: "Custom color", "aria-label": "Custom background" });
    custom.oninput = () => { B.bg = custom.value; render(); save(); };
    fill($("#swatches"), ...SWATCHES.map(([c, n]) => h("button", { "data-c": c, title: n, "aria-label": n, style: `background:${c}`, onclick: () => { B.bg = c; render(); save(); } })), custom);
  }

  // ---------- video tile panel: loop a section, pick the still ----------
  let clip = null;   // { it, video }
  function clipPanel() {
    const panel = $("#clipPanel");
    const it = B && isVideo(selected) ? selected : null;
    if (!it) { panel.hidden = true; if (clip?.video) clip.video.pause(); clip = null; fill(panel); return; }
    if (clip?.it === it) { clipUpdate(); return; }
    const v = h("video", { src: it.src, muted: true, playsinline: true, preload: "auto", poster: it.poster_src || it.thumb_src });
    v.muted = true;
    clip = { it, video: v };
    const scrub = h("input", { type: "range", min: 0, max: 1, step: 0.01, value: 0, "aria-label": "Position" });
    scrub.oninput = () => { v.pause(); v.currentTime = Number(scrub.value); };
    v.addEventListener("loadedmetadata", () => { scrub.max = v.duration; v.currentTime = it.vin ?? it.still ?? 0; clipUpdate(); });
    v.addEventListener("timeupdate", () => {
      if (it.vin != null && !v.paused && v.currentTime >= (it.vout ?? v.duration) - 0.03) v.currentTime = it.vin;   // preview the loop
      clipUpdate();
    });
    v.addEventListener("play", clipUpdate); v.addEventListener("pause", clipUpdate);
    const btn = (label, title, fn, id) => h("button", { class: "cell", title, onclick: fn, id }, label);
    fill(panel,
      h("div", { class: "clip-video" }, v),
      h("div", { class: "clip-body" },
        h("div", { class: "clip-scrub" }, h("div", { class: "clip-range", id: "clipLoop" }), h("div", { class: "clip-still", id: "clipStill" }), scrub),
        h("div", { class: "clip-row" },
          btn("Play", "Play / pause (Space)", () => (v.paused ? v.play() : v.pause()), "clipPlay"),
          h("span", { class: "cell tc", id: "clipTc" }, "00:00.00"),
          btn("− Frame", "Back a frame (,)", () => { v.pause(); v.currentTime = Math.max(0, v.currentTime - 1 / 30); }),
          btn("+ Frame", "Forward a frame (.)", () => { v.pause(); v.currentTime = Math.min(v.duration, v.currentTime + 1 / 30); }),
          h("span", { class: "spacer" }),
          btn("In", "Loop starts here (I). Click again to clear", () => clipIn(true), "clipIn"),
          btn("Out", "Loop ends here (O). Click again to clear", () => clipOut(true), "clipOut"),
          btn("Preview", "Play just the loop (P)", clipPreview, "clipPreview"),
          btn("No loop", "Stop looping this tile", clipClear, "clipClear"),
          btn("Use as still", "Show this frame on the board and in PNG / PDF exports (S)", clipStill, "clipStillBtn")),
        h("p", { class: "clip-hint" }, "Set In and Out to loop that section on the board and in video exports. The still is what static exports show.")));
    panel.hidden = false;
    clipUpdate();
  }
  function clipUpdate() {
    if (!clip) return;
    const { it, video: v } = clip, d = v.duration || 1;
    const pct = (t) => `${(t / d) * 100}%`;
    const fmt = (t) => (t == null ? "–" : fmtTime(t));
    $("#clipTc").textContent = fmtTime(v.currentTime);
    $("#clipPlay").textContent = v.paused ? "Play" : "Pause";
    $("#clipIn").textContent = "In " + fmt(it.vin);
    $("#clipOut").textContent = "Out " + (it.vin != null ? fmt(it.vout ?? v.duration) : "–");
    $("#clipClear").disabled = it.vin == null;
    $("#clipPreview").disabled = it.vin == null;
    $("#clipPreview").textContent = clip.preview && !v.paused ? "Stop preview" : "Preview";
    $("#clipStillBtn").textContent = "Use as still" + (it.still != null ? " (" + fmt(it.still) + ")" : "");
    const scrub = $("#clipPanel input[type=range]"); if (scrub && document.activeElement !== scrub) scrub.value = v.currentTime;
    const r = $("#clipLoop");
    r.style.display = it.vin != null ? "block" : "none";
    if (it.vin != null) { r.style.left = pct(it.vin); r.style.width = pct((it.vout ?? d) - it.vin); }
    const s = $("#clipStill");
    s.style.display = it.still != null ? "block" : "none";
    if (it.still != null) s.style.left = pct(it.still);
  }
  function clipIn(fromButton) {
    if (!clip) return;
    const { it, video: v } = clip;
    if (fromButton === true && it.vin != null) { it.vin = it.vout = null; clip.preview = false; render(); clipUpdate(); save(); return; }   // click again: clear
    it.vin = Math.round(v.currentTime * 100) / 100;
    if (it.vout != null && it.vout <= it.vin + 0.1) it.vout = null;
    render(); clipUpdate(); save();
  }
  function clipOut(fromButton) {
    if (!clip) return;
    const { it, video: v } = clip;
    if (fromButton === true && it.vout != null) { it.vout = null; render(); clipUpdate(); save(); return; }   // click again: back to the end
    const t = Math.round(v.currentTime * 100) / 100;
    if (it.vin == null) it.vin = 0;
    if (t <= it.vin + 0.1) return toast("Out has to come after In", { error: true });
    it.vout = t;
    render(); clipUpdate(); save();
  }
  function clipPreview() {
    if (!clip || clip.it.vin == null) return;
    clip.preview = !clip.preview;
    if (clip.preview) { clip.video.currentTime = clip.it.vin; clip.video.play().catch(() => {}); } else clip.video.pause();
    clipUpdate();
  }
  function clipClear() { if (!clip) return; clip.it.vin = clip.it.vout = null; render(); clipUpdate(); save(); }
  function clipStill() {
    if (!clip) return;
    clip.it.still = Math.round(clip.video.currentTime * 100) / 100;
    render(); clipUpdate(); save();
  }

  // ---------- settings ----------
  function syncControls() {
    const on = (sel, val) => document.querySelectorAll(sel).forEach((b) => b.classList.toggle("on", b.dataset[Object.keys(b.dataset)[0]] === val));
    on("[data-ly]", st.layout);
    on("[data-title]", st.show_title ? st.title_pos : "off");
    on("[data-cap]", st.captions);
    on("[data-num]", st.numbered ? "1" : "0");
    on("[data-ar]", st.tile_ar);
    on("[data-fill]", st.fill ? "1" : "0");
    on("[data-tilt]", st.tilt ? "1" : "0");
    on("[data-vl]", st.video_labels ? "1" : "0");
    on("[data-motion]", st.motion ? "1" : "0");
    on("[data-pages]", st.pages);
    $("#boardMargin").value = st.margin ?? MARGIN_DEFAULT;
    document.querySelectorAll("[data-only]").forEach((g) => { g.hidden = !g.dataset.only.split(" ").includes(st.layout); });
    $("#pagesCtl").hidden = !SIZES[st.size] || st.layout === "collage";
    if (sizePick) sizePick.value = st.size;
    const n = media().length || 1;
    const rr = $("#rowsRange"), cr = $("#colsRange");
    rr.max = Math.max(1, Math.min(12, n)); rr.value = st.rows || (SIZES[st.size] && st.pages !== "fit" ? 3 : autoRows(n));
    cr.max = Math.max(1, Math.min(8, n)); cr.value = st.cols || autoCols(n);
    $("#rowsVal").textContent = rr.value; $("#colsVal").textContent = cr.value;
    const layerable = st.layout === "collage" && selected && !isText(selected);
    document.querySelectorAll("[data-layer]").forEach((b) => { b.disabled = !layerable; });
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
      if (selected !== it) select(it);
      drag = { it, el, free, isMedia, started: false, target: null, mode: free && e.target.classList.contains("grip") ? "resize" : "move",
        sx: e.clientX, sy: e.clientY, x: it.x, y: it.y, w: it.w, size: it.size, cx: it.cx, cy: it.cy, cw: it.cw, ch: it.ch, p: view.media.get(it) };
      paper.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    paper.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = (e.clientX - drag.sx) / scale, dy = (e.clientY - drag.sy) / scale, it = drag.it;
      if (!drag.started && Math.hypot(dx, dy) * scale < 4) return;
      if (!drag.started && !drag.isMedia) { toFront(it); }
      drag.started = true;
      if (!drag.isMedia) {   // notes: free everywhere
        if (drag.mode === "move") { it.x = Math.round(drag.x + dx); it.y = Math.round(Math.max(0, drag.y + dy)); }
        else { const nw = Math.max(60, drag.w + dx); it.size = Math.max(8, Math.round(drag.size * nw / drag.w)); it.w = nw; }
        placeText(drag.el, it, 1000 + it.z);
      } else if (drag.free) {   // collage: moving a piece doesn't change its layer; [ and ] do
        const k = cs().s;
        if (drag.mode === "move") { it.cx = Math.round(drag.cx + dx / k); it.cy = Math.round(Math.max(-view.y0, drag.cy + dy / k)); }
        else { const nw = Math.max(60, drag.cw + dx / k); it.cw = Math.round(nw); it.ch = Math.round(drag.ch * nw / drag.cw); }
        placeMedia(drag.el, it, collageP(it));
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
    const it = { type: "text", text: "Note", x: MARGIN(), y: Math.max(MARGIN(), Math.round($("#boardScroll").scrollTop / scale) + 200), w: 520, size: 28, z: 0 };
    B.items.push(it); toFront(it); render(); select(it);
    const el = els.get(it); if (el) edit(el);
    save();
  }
  function removeSelected() {
    if (!selected) return;
    const idx = B.items.indexOf(selected);
    if (idx < 0) return;
    const wasMedia = !isText(selected);
    B.items.splice(idx, 1);
    undo.push({ it: selected, idx }); if (undo.length > 50) undo.shift();
    select(null); render(); save();
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
  const MONO = `"Plex Mono", "IBM Plex Mono", Menlo, monospace`, SANS = `"Inter Tight", "Helvetica Neue", Arial, sans-serif`;
  // everything on the board, bottom layer first
  function drawList(pg) {
    const on = (y, hh) => !pg || (y < pg.top + pg.h && y + hh > pg.top);   // only what's on this slide
    const posts = [...view.media].filter(([, p]) => on(p.y, p.h)).sort((a, b) => (a[1].free ? a[0].z || 0 : 0) - (b[1].free ? b[0].z || 0 : 0));
    const texts = [...view.texts, ...notes().filter((it) => it.x != null).sort((a, b) => (a.z || 0) - (b.z || 0))]
      .filter((t) => on(t.y, t._h || t.size * 1.3));
    return [...posts.map(([it, p]) => ({ it, p })), ...texts.map((it) => ({ it, text: true }))];
  }
  function postSrc(it) {
    if (it.kind !== "video") return it.src;
    return frameUrl(it, stillT(it) ?? 0.8);
  }
  function drawPost(ctx, img, p, f, hole) {
    const x = p.x * f, y = p.y * f, w = p.w * f, hh = p.h * f;
    ctx.save();
    if (p.rot) { ctx.translate(x + w / 2, y + hh / 2); ctx.rotate(p.rot * Math.PI / 180); ctx.translate(-(x + w / 2), -(y + hh / 2)); }
    if (p.cell && p.fit) { ctx.fillStyle = cellColor(); ctx.fillRect(x, y, w, hh); }
    if (!hole) {
      if (p.cell && p.fit) {
        const s = Math.min(w / img.width, hh / img.height), dw = img.width * s, dh = img.height * s;
        ctx.drawImage(img, x + (w - dw) / 2, y + (hh - dh) / 2, dw, dh);
      } else {   // cover-crop to the box
        const s = Math.max(w / img.width, hh / img.height), sw = w / s, sh = hh / s;
        ctx.drawImage(img, (img.width - sw) / 2, (img.height - sh) / 2, sw, sh, x, y, w, hh);
      }
    }
    ctx.restore();
    if (p.num) { ctx.font = `400 ${11 * f}px ${MONO}`; ctx.fillStyle = inkColor(); ctx.textBaseline = "top"; ctx.fillText(numLabel(p.num), x, y + hh + 6 * f); }
  }
  function drawText(ctx, it, f) {
    const isMono = it.mono || it.font === "mono";
    ctx.font = `${isMono ? 400 : 500} ${it.size * f}px ${isMono ? MONO : SANS}`;
    ctx.fillStyle = textColor(it); ctx.textBaseline = "top";
    const lh = it.size * f * (isMono ? 1.3 : 1.12);
    let y = it.y * f;
    for (const line of wrapText(ctx, isMono ? it.text.toUpperCase() : it.text, it.w * f)) { ctx.fillText(line, it.x * f, y); y += lh; }
  }
  async function loadImages(list, skip) {
    return Promise.all(list.map(({ it, text }) => (text || skip?.(it) ? null : loadImg(postSrc(it)))));
  }
  async function renderCanvas(pg) {
    const H = pg.h;
    const f = Math.min(2, 15000 / H, Math.sqrt(1e8 / (W * H)));
    const cv = document.createElement("canvas"); cv.width = Math.round(W * f); cv.height = Math.round(H * f);
    const ctx = cv.getContext("2d");
    ctx.fillStyle = B.bg; ctx.fillRect(0, 0, cv.width, cv.height);
    const list = drawList(pg), imgs = await loadImages(list);
    await (document.fonts?.ready || Promise.resolve());
    ctx.translate(0, -pg.top * f);
    list.forEach((d, i) => (d.text ? drawText(ctx, d.it, f) : drawPost(ctx, imgs[i], d.p, f)));
    return cv;
  }
  async function exportAs(kind, btn) {
    if (!B.items.length) return toast("Add some images first", { error: true });
    await flush();
    const label = btn.textContent; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Rendering';
    try {
      render();
      const pages = view.pages, many = pages.length > 1;
      const name = (B.name || "moodboard").replace(/[^\w .-]+/g, "_").slice(0, 60);
      const blobs = [];
      for (let i = 0; i < pages.length; i++) {
        btn.innerHTML = `<span class="spinner"></span>Rendering${many ? ` ${i + 1}/${pages.length}` : ""}`;
        const cv = await renderCanvas(pages[i]);
        blobs.push(await new Promise((r) => cv.toBlob(r, "image/png")));
      }
      const fileName = (i) => name + (many ? `_slide${pad(i + 1, 2)}` : "") + ".png";
      if (kind === "png" && DESKTOP) {
        let last;
        for (let i = 0; i < blobs.length; i++) last = await api(`/api/desktop/save-blob?name=${encodeURIComponent(fileName(i))}`, { method: "POST", body: blobs[i], headers: { "Content-Type": "image/png" } });
        savedToast(many ? { ...last, name: `${blobs.length} slides (${name}_slide01…)` } : last);
      } else if (kind === "png") {
        blobs.forEach((b, i) => { const a = h("a", { href: URL.createObjectURL(b), download: fileName(i) }); document.body.append(a); a.click(); a.remove(); });
        toast(`Exported <b>${blobs.length > 1 ? blobs.length + " slides" : esc(fileName(0))}</b>`);
      } else {   // one PDF, a page per slide
        const fd = new FormData();
        blobs.forEach((b, i) => fd.append("page" + i, b, `page${i}.png`));
        const r = await api(`/api/boards/pdf?name=${encodeURIComponent(name)}`, { method: "POST", body: fd });
        downloadUrl(r.url); if (!DESKTOP) toast(`Exported <b>${esc(r.name)}</b>`);
      }
    } catch (e) { toast(esc(e.message), { error: true }); }
    btn.disabled = false; btn.textContent = label;
  }

  // video: the board is split into still layers and the looping tiles between them; the Mac stitches them into an MP4
  function videoDialog() {
    if (!media().some(looping)) return toast("Click a video tile and set an In point first. Tiles with a loop are what moves in the video.", { error: true, ms: 7000 });
    let secs = 15;
    const custom = h("input", { type: "number", min: 1, max: 300, step: 1, placeholder: "Seconds", style: "width:110px" });
    const opts = [[15, "15 sec"], [30, "30 sec"], [60, "1 min"]];
    const group = h("div", { class: "cell toggle", style: "border:1px solid var(--rule);height:34px" },
      ...opts.map(([v, l]) => h("button", { class: v === secs ? "on" : "", onclick: (e) => {
        secs = v; custom.value = ""; group.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === e.currentTarget)); } }, l)));
    custom.oninput = () => { group.querySelectorAll("button").forEach((b) => b.classList.remove("on")); secs = Number(custom.value) || 15; };
    custom.addEventListener("keydown", (e) => e.stopPropagation());
    modal({
      title: "Export video",
      text: "Tiles with a loop play their section, repeating to fill the length. Everything else stays still. MP4, no sound.",
      body: h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" }, group, custom, h("span", { class: "hint" }, "or type a length in seconds")),
      actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Export MP4", primary: true, fn: (c) => { c(); exportVideo(Math.max(1, Math.min(300, secs))); } }],
    });
  }
  async function exportVideo(secs) {
    await flush();
    const btn = $("#boardVideo"), label = btn.textContent;
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Preparing';
    try {
      render();
      // one MP4 per slide that has something moving on it
      const pages = view.pages.filter((pg) => drawList(pg).some((d) => !d.text && looping(d.it)));
      for (let pi = 0; pi < pages.length; pi++) {
        const pg = pages[pi], tag = pages.length > 1 ? ` ${pi + 1}/${pages.length}` : "";
        const H = pg.h;
        const f = Math.min(1920 / W, 4096 / H);
        const cw = Math.round(W * f / 2) * 2, chh = Math.round(H * f / 2) * 2;
        const list = drawList(pg), imgs = await loadImages(list, (it) => looping(it));
        await (document.fonts?.ready || Promise.resolve());
        const layers = [], spec = [];
        let cv = null, ctx = null, dirty = false;
        const newLayer = (opaque) => {
          cv = document.createElement("canvas"); cv.width = cw; cv.height = chh; ctx = cv.getContext("2d");
          if (opaque) { ctx.fillStyle = B.bg; ctx.fillRect(0, 0, cw, chh); }
          ctx.translate(0, -pg.top * f);
          dirty = opaque;
        };
        const pushLayer = async () => {
          if (!dirty) return;
          layers.push(await new Promise((r) => cv.toBlob(r, "image/png")));
          spec.push({ type: "png", file: layers.length - 1 });
        };
        newLayer(true);
        for (let i = 0; i < list.length; i++) {
          const d = list[i];
          if (d.text) { drawText(ctx, d.it, f); dirty = true; continue; }
          if (looping(d.it)) {
            drawPost(ctx, null, d.p, f, true);   // its cell color / number go in the still layer
            await pushLayer();
            const p = d.p;
            spec.push({ type: "video", media_id: d.it.media_id, x: p.x * f, y: (p.y - pg.top) * f, w: p.w * f, h: p.h * f, rot: p.rot || 0,
              fit: !!(p.cell && p.fit), bg: cellColor(), vin: d.it.vin, vout: d.it.vout });
            newLayer(false);
          } else { drawPost(ctx, imgs[i], d.p, f); dirty = true; }
        }
        await pushLayer();
        const fd = new FormData();
        const vname = (B.name || "moodboard") + (pages.length > 1 ? ` slide ${pad(view.pages.indexOf(pg) + 1, 2)}` : "");
        fd.append("spec", JSON.stringify({ width: cw, height: chh, duration: secs, name: vname, layers: spec }));
        layers.forEach((b, i) => fd.append("layer" + i, b, `layer${i}.png`));
        const { job } = await api("/api/boards/video", { method: "POST", body: fd });
        for (;;) {
          await new Promise((r) => setTimeout(r, 1000));
          const s = await api(`/api/boards/video/${job}`);
          if (s.status === "error") throw new Error(s.error || "The video export failed");
          if (s.status === "done") { downloadUrl(s.url); if (!DESKTOP) toast(`Exported <b>${esc(s.name)}</b>`); break; }
          btn.innerHTML = `<span class="spinner"></span>${esc(s.message || "Rendering")}${tag}`;
        }
      }
    } catch (e) { toast(esc(e.message), { error: true }); }
    btn.disabled = false; btn.textContent = label;
  }

  // ---------- wiring ----------
  let sizePick = null;
  function init() {
    bindPaper();
    // board size: free, or a fixed slide shape
    sizePick = pick({ label: "Size", value: "free", options: [{ value: "free", label: "Free" },
      ...Object.keys(SIZES).map((k) => ({ value: k, label: k }))], title: "Board shape. Fixed sizes export as slides" });
    sizePick.onchange = () => set({ size: sizePick.value });
    $("#sizeCtl").append(sizePick);
    document.querySelectorAll("[data-pages]").forEach((b) => { b.onclick = () => set({ pages: b.dataset.pages }); });
    $("#rowsRange").oninput = () => { $("#rowsVal").textContent = $("#rowsRange").value; set({ rows: Number($("#rowsRange").value) }); };
    $("#colsRange").oninput = () => { $("#colsVal").textContent = $("#colsRange").value; set({ cols: Number($("#colsRange").value) }); };
    $("#boardClose").onclick = close;
    document.querySelectorAll("[data-ly]").forEach((b) => { b.onclick = () => setLayout(b.dataset.ly); });
    document.querySelectorAll("[data-title]").forEach((b) => { b.onclick = () => set(b.dataset.title === "off" ? { show_title: false } : { show_title: true, title_pos: b.dataset.title }); });
    document.querySelectorAll("[data-cap]").forEach((b) => { b.onclick = () => set({ captions: b.dataset.cap }); });
    document.querySelectorAll("[data-num]").forEach((b) => { b.onclick = () => set({ numbered: b.dataset.num === "1" }); });
    document.querySelectorAll("[data-ar]").forEach((b) => { b.onclick = () => set({ tile_ar: b.dataset.ar }); });
    document.querySelectorAll("[data-fill]").forEach((b) => { b.onclick = () => set({ fill: b.dataset.fill === "1" }); });
    document.querySelectorAll("[data-tilt]").forEach((b) => { b.onclick = () => setTilt(b.dataset.tilt === "1"); });
    document.querySelectorAll("[data-vl]").forEach((b) => { b.onclick = () => set({ video_labels: b.dataset.vl === "1" }); });
    document.querySelectorAll("[data-motion]").forEach((b) => { b.onclick = () => set({ motion: b.dataset.motion === "1" }); });
    document.querySelectorAll("[data-layer]").forEach((b) => { b.onclick = () => restack(selected, b.dataset.layer); });
    $("#boardShuffle").onclick = shuffle;
    $("#boardMargin").oninput = () => { set({ margin: Number($("#boardMargin").value) }); };
    $("#boardGap").oninput = () => { B.gap = Number($("#boardGap").value); render(); save(); };
    $("#addNote").onclick = addNote;
    $("#boardTitle").onchange = () => { B.name = $("#boardTitle").value.trim() || B.name; render(); save(); };
    $("#boardPng").onclick = (e) => exportAs("png", e.currentTarget);
    $("#boardPdf").onclick = (e) => exportAs("pdf", e.currentTarget);
    $("#boardVideo").onclick = videoDialog;
    $("#boardDelete").onclick = () => modal({
      title: `Delete “${B.name}”?`, text: "The moodboard is removed. The posts in it stay in your library.",
      actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Delete moodboard", danger: true, fn: async (c) => {
        await api(`/api/boards/${B.id}`, { method: "DELETE" }); c(); B = null; els = new Map(); fill($("#paper")); clipPanel(); $("#board").hidden = true; document.body.style.overflow = ""; loadSidebar();
      } }],
    });
    window.addEventListener("resize", () => { if (B) render(); });
    document.addEventListener("keydown", (e) => {
      if (!B || $("#board").hidden || $("#modalRoot").children.length) return;
      const typing = editing || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) && document.activeElement.type !== "range";
      if (e.key === "Escape") { e.preventDefault(); if (typing) document.activeElement.blur(); else if (selected) select(null); else close(); return; }
      if (typing) return;
      const k = e.key.toLowerCase();
      if ((e.metaKey || e.ctrlKey) && k === "z" && !e.shiftKey) { e.preventDefault(); undoLast(); return; }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (k === "delete" || k === "backspace") { e.preventDefault(); removeSelected(); return; }
      if (e.code === "BracketRight" || e.code === "BracketLeft") {
        e.preventDefault();
        const up = e.code === "BracketRight";
        restack(selected, e.shiftKey ? (up ? "front" : "back") : (up ? "up" : "down"));
        return;
      }
      if (clip) {   // the selected video tile's panel
        const v = clip.video;
        const keys = { i: () => clipIn(true), o: () => clipOut(true), s: clipStill, p: clipPreview, " ": () => (v.paused ? v.play() : v.pause()),
          ",": () => { v.pause(); v.currentTime = Math.max(0, v.currentTime - 1 / 30); },
          ".": () => { v.pause(); v.currentTime = Math.min(v.duration, v.currentTime + 1 / 30); } };
        if (keys[k]) { e.preventDefault(); keys[k](); return; }
      }
      if (selected && k.startsWith("arrow") && (isText(selected) || st.layout === "collage")) {
        e.preventDefault(); const d = e.shiftKey ? 10 : 1;
        const ddx = k === "arrowright" ? d : k === "arrowleft" ? -d : 0, ddy = k === "arrowdown" ? d : k === "arrowup" ? -d : 0;
        if (isText(selected)) { selected.x += ddx; selected.y = Math.max(0, selected.y + ddy); }
        else { selected.cx += ddx; selected.cy += ddy; }
        render(); save();
      }
    }, true);
  }
  init();
  return { open, create, close };
})();
