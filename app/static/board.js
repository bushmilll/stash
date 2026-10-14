/* Moodboards: a free-form sheet of images and notes, exported as PNG or PDF.
   Loaded after app.js and shares its helpers ($, h, api, fill, toast, modal, downloadUrl, esc, pad). */
const Board = (() => {
  const W = 1600;            // logical board width; export scales this up
  const MARGIN = 48;
  const NUM_SPACE = 30;      // room under a numbered cell for its [01] label
  const SWATCHES = [["#eceae5", "Paper"], ["#ffffff", "White"], ["#8c8a84", "Concrete"], ["#1a1a1a", "Graphite"], ["#000000", "Black"]];
  let B = null, selected = null, scale = 1, lastLayout = "sheet", saveTimer = null, editing = false;

  const isText = (it) => it.type === "text";
  const ratio = (it) => (it.mw && it.mh ? it.mh / it.mw : 1.25);
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
  const bottomOf = (it) => it.y + (isText(it) ? (it._h || it.size * 1.3) : it.h + (it.num ? NUM_SPACE : 0));
  const contentH = () => Math.max(900, ...B.items.filter((it) => it.x != null).map((it) => bottomOf(it) + MARGIN));
  const numLabel = (it) => "[" + pad(it.num, 2) + "]";

  // ---------- open / close ----------
  async function open(id) {
    try { B = await api(`/api/boards/${id}`); } catch (e) { return toast(esc(e.message), { error: true }); }
    B.items.forEach((it, i) => { if (it.z == null) it.z = i + 1; });
    selected = null;
    lastLayout = B.items.some((it) => it.num) ? "sheet" : B.items.some((it) => !isText(it) && it.x != null) ? "masonry" : "sheet";
    markLayout();
    $("#board").hidden = false; document.body.style.overflow = "hidden";
    $("#boardTitle").value = B.name;
    $("#boardGap").value = B.gap;
    drawSwatches();
    placeNew();
    render();
    requestAnimationFrame(() => { measure(); fit(); });
  }
  async function close() {
    if (!B) return;
    await flush();
    $("#board").hidden = true; document.body.style.overflow = "";
    B = null; selected = null;
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
      name: $("#boardTitle").value.trim() || B.name, bg: B.bg, gap: B.gap, width: W,
      items: B.items.map((it) => isText(it)
        ? { type: "text", text: it.text, x: it.x, y: it.y, w: it.w, size: it.size, color: it.color, z: it.z, font: it.font, auto: it.auto, role: it.role }
        : { type: "media", media_id: it.media_id, x: it.x, y: it.y, w: it.w, h: it.h, z: it.z, fit: it.fit, num: it.num }),
    };
    try { await api(`/api/boards/${B.id}`, { method: "PUT", body: payload }); B.name = payload.name; }
    catch (e) { toast(esc(e.message), { error: true }); }
  }

  // ---------- layouts ----------
  const media = () => B.items.filter((it) => !isText(it));
  function plain(items) { for (const it of items) { delete it.fit; delete it.num; } }
  function dropAutoText() { B.items = B.items.filter((it) => !(isText(it) && it.auto)); }
  function sizeItem(it, w) { it.w = w; it.h = w * ratio(it); }

  function layoutMasonry(items, startY) {
    plain(items);
    const n = items.length; if (!n) return;
    const cols = Math.max(2, Math.min(6, Math.round(Math.sqrt(n * 0.9))));
    const g = B.gap, cw = (W - 2 * MARGIN - (cols - 1) * g) / cols;
    const ys = new Array(cols).fill(startY);
    for (const it of items) {
      let c = 0; for (let j = 1; j < cols; j++) if (ys[j] < ys[c] - 0.5) c = j;
      sizeItem(it, cw); it.x = MARGIN + c * (cw + g); it.y = ys[c]; ys[c] += it.h + g;
    }
  }
  function layoutRows(items, startY) {
    plain(items);
    const g = B.gap, avail = W - 2 * MARGIN, target = Math.max(220, Math.min(420, avail / Math.max(2, Math.sqrt(items.length) * 0.9)));
    let y = startY, row = [], sum = 0;
    const flushRow = (last) => {
      if (!row.length) return;
      let rh = (avail - (row.length - 1) * g) / sum;
      if (last && rh > target * 1.35) rh = target;
      let x = MARGIN;
      for (const it of row) { const a = 1 / ratio(it); it.h = rh; it.w = rh * a; it.x = x; it.y = y; x += it.w + g; }
      y += rh + g; row = []; sum = 0;
    };
    for (const it of items) {
      row.push(it); sum += 1 / ratio(it);
      if (sum * target + (row.length - 1) * g >= avail) flushRow(false);
    }
    flushRow(true);
  }
  // Catalog sheet: uniform numbered cells, then a caption list in columns underneath
  function layoutSheet(items, startY) {
    const n = items.length; if (!n) return;
    // title band, like the heading on a printed sheet
    let title = B.items.find((it) => isText(it) && it.role === "title");
    if (!title) { title = { type: "text", role: "title", text: B.name, size: 64, w: W - 2 * MARGIN, z: 1 }; B.items.push(title); }
    title.x = MARGIN; title.y = startY;
    startY += Math.max(title._h || 0, title.size * 1.12 * Math.max(1, String(title.text).split("\n").length)) + 36;
    const cols = n <= 3 ? n : n <= 8 ? 4 : n <= 18 ? 6 : 8;
    const g = Math.max(B.gap, 8), cw = (W - 2 * MARGIN - (cols - 1) * g) / cols, ch = cw * 1.25;
    items.forEach((it, i) => {
      const c = i % cols, r = Math.floor(i / cols);
      it.w = cw; it.h = ch; it.fit = true; it.num = i + 1;
      it.x = MARGIN + c * (cw + g); it.y = startY + r * (ch + NUM_SPACE + g);
    });
    dropAutoText();
    const rows = Math.ceil(n / cols);
    const y = startY + rows * (ch + NUM_SPACE + g) + 48;
    const listCols = Math.min(6, Math.max(2, cols)), lw = (W - 2 * MARGIN - (listCols - 1) * 24) / listCols;
    const per = Math.ceil(n / listCols);
    for (let k = 0; k < listCols; k++) {
      const chunk = items.slice(k * per, (k + 1) * per);
      if (!chunk.length) break;
      const text = chunk.map((it) => {
        const cap = (it.caption || "").replace(/\s+/g, " ").replace(/#\w+/g, "").trim();
        return `${numLabel(it)} ${it.username ? "@" + it.username : ""}${cap ? "\n" + (cap.length > 64 ? cap.slice(0, 62) + "…" : cap) : ""}`;
      }).join("\n\n");
      B.items.push({ type: "text", font: "mono", auto: true, size: 11, text, x: MARGIN + k * (lw + 24), y, w: lw, z: 1 });
    }
  }
  function arrange(kind, items, startY) { ({ rows: layoutRows, sheet: layoutSheet, masonry: layoutMasonry })[kind](items, startY); }
  function placeNew() {
    const fresh = media().filter((it) => it.x == null);
    if (!fresh.length) return;
    if (lastLayout === "sheet" || !media().some((it) => it.x != null)) { lastLayout = "sheet"; arrange("sheet", media(), MARGIN); }
    else {
      const placed = B.items.filter((it) => it.x != null);
      arrange(lastLayout, fresh, Math.max(...placed.map(bottomOf)) + B.gap * 3);
    }
    markLayout(); save();
  }
  function markLayout() {
    $("#lySheet").classList.toggle("on", lastLayout === "sheet");
    $("#lyMasonry").classList.toggle("on", lastLayout === "masonry");
    $("#lyRows").classList.toggle("on", lastLayout === "rows");
  }
  function relayout(kind) {
    lastLayout = kind; markLayout();
    if (kind !== "sheet") dropAutoText();
    arrange(kind, media(), MARGIN);
    render(); requestAnimationFrame(() => { measure(); fit(); }); save();
  }

  // ---------- rendering ----------
  function measure() {
    document.querySelectorAll(".bitem.text").forEach((el) => { el._item._h = el.offsetHeight; });
  }
  function fit() {
    if (!B) return;
    const avail = $("#boardScroll").clientWidth - (window.innerWidth < 860 ? 20 : 56);
    scale = Math.min(1, avail / W);
    const H = contentH();
    const paper = $("#paper");
    paper.style.width = W + "px"; paper.style.height = H + "px"; paper.style.transform = `scale(${scale})`;
    const wrap = $("#paperWrap"); wrap.style.width = W * scale + "px"; wrap.style.height = H * scale + "px";
    $("#boardInfo").textContent = `${pad(media().length)} images / ${W} × ${Math.round(H)} / export ${W * 2} px wide`;
  }
  function itemEl(it) {
    const el = h("div", { class: "bitem" + (isText(it) ? " text" + (it.font === "mono" ? " mono" : "") : "") + (it.fit ? " fit" : "") + (it === selected ? " sel" : "") });
    el._item = it;
    place(el, it);
    if (isText(it)) {
      el.textContent = it.text;
      el.style.fontSize = it.size + "px"; el.style.color = textColor(it);
    } else {
      const useFull = it.kind !== "video" && it.w * scale * (window.devicePixelRatio || 1) > 500;
      el.append(h("div", { class: "img", style: it.fit ? `background:${cellColor()}` : "" },
        h("img", { src: useFull ? it.src : (it.thumb_src || it.src), alt: "", draggable: "false", decoding: "async" })));
      if (it.kind === "video") el.append(h("span", { class: "vbadge" }, "Video"));
      if (it.num) el.append(h("span", { class: "num", style: `color:${inkColor()}` }, numLabel(it)));
    }
    el.append(h("span", { class: "grip" }));
    return el;
  }
  function place(el, it) {
    el.style.left = it.x + "px"; el.style.top = it.y + "px"; el.style.width = it.w + "px";
    if (!isText(it)) el.style.height = it.h + "px";
    el.style.zIndex = it.z || 1;
  }
  function render() {
    if (!B) return;
    const paper = $("#paper");
    paper.style.background = B.bg;
    fill(paper, ...B.items.filter((it) => it.x != null).map(itemEl),
      B.items.length ? null : h("div", { class: "board-empty", style: `color:${inkColor()}` },
        h("div", {}, h("div", { style: "font-weight:500;font-size:28px;letter-spacing:-.02em;margin-bottom:8px" }, "Empty board"),
          "Close this, select posts in your library, then choose Moodboard in the bar at the bottom.")));
    document.querySelectorAll("#swatches button").forEach((b) => b.classList.toggle("on", b.dataset.c === B.bg));
    fit();
  }
  function select(it) {
    selected = it;
    document.querySelectorAll(".bitem").forEach((e) => e.classList.toggle("sel", e._item === it));
  }
  function toFront(it) {
    it.z = Math.max(0, ...B.items.map((i) => i.z || 0)) + 1;
    document.querySelectorAll(".bitem").forEach((e) => { if (e._item === it) e.style.zIndex = it.z; });
  }
  function drawSwatches() {
    const custom = h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(B.bg) ? B.bg : "#eceae5", title: "Custom color", "aria-label": "Custom background" });
    custom.oninput = () => { B.bg = custom.value; render(); save(); };
    fill($("#swatches"), ...SWATCHES.map(([c, n]) => h("button", { "data-c": c, title: n, "aria-label": n, style: `background:${c}`, onclick: () => { B.bg = c; render(); save(); } })), custom);
  }

  // ---------- pointer interactions ----------
  function bindPaper() {
    const paper = $("#paper");
    let drag = null;
    paper.addEventListener("pointerdown", (e) => {
      if (editing) return;
      const el = e.target.closest(".bitem");
      if (!el) { select(null); return; }
      const it = el._item;
      select(it); toFront(it);
      drag = { it, el, mode: e.target.classList.contains("grip") ? "resize" : "move", sx: e.clientX, sy: e.clientY, x: it.x, y: it.y, w: it.w, h: it.h, size: it.size };
      paper.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    paper.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = (e.clientX - drag.sx) / scale, dy = (e.clientY - drag.sy) / scale, it = drag.it;
      if (drag.mode === "move") { it.x = Math.round(drag.x + dx); it.y = Math.round(Math.max(0, drag.y + dy)); }
      else {
        const nw = Math.max(60, drag.w + dx);
        if (isText(it)) { it.size = Math.max(8, Math.round(drag.size * nw / drag.w)); drag.el.style.fontSize = it.size + "px"; it.w = nw; }
        else if (it.fit) { it.w = nw; it.h = drag.h * nw / drag.w; }
        else sizeItem(it, nw);
      }
      place(drag.el, it);
    });
    const end = () => { if (drag) { drag = null; measure(); fit(); save(); } };
    paper.addEventListener("pointerup", end); paper.addEventListener("pointercancel", end);
    paper.addEventListener("dblclick", (e) => { const el = e.target.closest(".bitem.text"); if (el) edit(el); });
  }
  function edit(el) {
    editing = true; el.contentEditable = "true"; el.focus();
    const r = document.createRange(); r.selectNodeContents(el); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    el.addEventListener("blur", () => {
      editing = false; el.contentEditable = "false";
      el._item.text = el.innerText.replace(/\n$/, "") || "Note"; el._item.auto = false; el.textContent = el._item.text;
      el.append(h("span", { class: "grip" })); measure(); fit(); save();
    }, { once: true });
  }
  function addNote() {
    const it = { type: "text", text: "Note", x: MARGIN, y: Math.max(MARGIN, Math.round($("#boardScroll").scrollTop / scale) + 200), w: 520, size: 28, z: 0 };
    B.items.push(it); toFront(it); selected = it; render();
    const el = [...document.querySelectorAll(".bitem")].find((e) => e._item === it); if (el) edit(el);
    save();
  }
  function removeSelected() {
    if (!selected) return;
    B.items = B.items.filter((i) => i !== selected); selected = null; render(); save();
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
    measure();
    const H = contentH();
    const f = Math.min(2, 15000 / H, Math.sqrt(1e8 / (W * H)));
    const cv = document.createElement("canvas"); cv.width = Math.round(W * f); cv.height = Math.round(H * f);
    const ctx = cv.getContext("2d");
    ctx.fillStyle = B.bg; ctx.fillRect(0, 0, cv.width, cv.height);
    const items = [...B.items].filter((it) => it.x != null).sort((a, b) => (a.z || 0) - (b.z || 0));
    const imgs = await Promise.all(items.map(async (it) => {
      if (isText(it)) return null;
      if (it.kind === "video") { const r = await api(`/api/media/${it.media_id}/still`, { method: "POST", body: { t: 0.8 } }); return loadImg(r.url); }
      return loadImg(it.src);
    }));
    await (document.fonts?.ready || Promise.resolve());
    const mono = `"Plex Mono", "IBM Plex Mono", Menlo, monospace`, sans = `"Inter Tight", "Helvetica Neue", Arial, sans-serif`;
    ctx.textBaseline = "top";
    items.forEach((it, i) => {
      if (isText(it)) {
        const isMono = it.font === "mono";
        ctx.font = `${isMono ? 400 : 500} ${it.size * f}px ${isMono ? mono : sans}`;
        ctx.fillStyle = textColor(it);
        const lh = it.size * f * (isMono ? 1.3 : 1.12);
        let y = it.y * f;
        for (const line of wrapText(ctx, isMono ? it.text.toUpperCase() : it.text, it.w * f)) { ctx.fillText(line, it.x * f, y); y += lh; }
        return;
      }
      const img = imgs[i], x = it.x * f, y = it.y * f, w = it.w * f, hh = it.h * f;
      if (it.fit) {
        ctx.fillStyle = cellColor(); ctx.fillRect(x, y, w, hh);
        const s = Math.min(w / img.width, hh / img.height), dw = img.width * s, dh = img.height * s;
        ctx.drawImage(img, x + (w - dw) / 2, y + (hh - dh) / 2, dw, dh);
      } else {
        // cover-crop to the item box
        const s = Math.max(w / img.width, hh / img.height), sw = w / s, sh = hh / s;
        ctx.drawImage(img, (img.width - sw) / 2, (img.height - sh) / 2, sw, sh, x, y, w, hh);
      }
      if (it.num) { ctx.font = `400 ${11 * f}px ${mono}`; ctx.fillStyle = inkColor(); ctx.fillText(numLabel(it), x, y + hh + 6 * f); }
    });
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
    $("#lySheet").onclick = () => relayout("sheet");
    $("#lyMasonry").onclick = () => relayout("masonry");
    $("#lyRows").onclick = () => relayout("rows");
    $("#boardGap").oninput = () => { B.gap = Number($("#boardGap").value); relayout(lastLayout); };
    $("#addNote").onclick = addNote;
    $("#boardTitle").onchange = () => {
      const old = B.name; B.name = $("#boardTitle").value.trim() || B.name;
      const t = B.items.find((it) => isText(it) && it.role === "title");
      if (t && t.text === old) { t.text = B.name; render(); requestAnimationFrame(() => { measure(); fit(); }); }
      save();
    };
    $("#boardPng").onclick = (e) => exportAs("png", e.currentTarget);
    $("#boardPdf").onclick = (e) => exportAs("pdf", e.currentTarget);
    $("#boardDelete").onclick = () => modal({
      title: `Delete “${B.name}”?`, text: "The moodboard is removed. The posts in it stay in your library.",
      actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Delete moodboard", danger: true, fn: async (c) => {
        await api(`/api/boards/${B.id}`, { method: "DELETE" }); c(); B = null; $("#board").hidden = true; document.body.style.overflow = ""; loadSidebar();
      } }],
    });
    window.addEventListener("resize", () => { if (B) fit(); });
    document.addEventListener("keydown", (e) => {
      if (!B || $("#board").hidden || $("#modalRoot").children.length) return;
      const typing = editing || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
      if (e.key === "Escape") { if (typing) document.activeElement.blur(); else if (selected) select(null); else close(); return; }
      if (typing) return;
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
      else if (selected && e.key.startsWith("Arrow")) {
        e.preventDefault(); const d = e.shiftKey ? 10 : 1;
        selected.x += e.key === "ArrowRight" ? d : e.key === "ArrowLeft" ? -d : 0;
        selected.y = Math.max(0, selected.y + (e.key === "ArrowDown" ? d : e.key === "ArrowUp" ? -d : 0));
        render(); save();
      }
    }, true);
  }
  init();
  return { open, create, close };
})();
