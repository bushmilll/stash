/* Stash front end: plain JS, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
const api = async (url, opts = {}) => {
  const isForm = opts.body instanceof FormData || opts.body instanceof Blob;
  let res;
  try {
    res = await fetch(url, {
      ...opts,
      headers: opts.body && !isForm ? { "Content-Type": "application/json" } : opts.headers,
      body: opts.body && !isForm ? JSON.stringify(opts.body) : opts.body,
    });
  } catch {
    throw new Error("Can't reach Stash. Make sure the app is still running.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
};
const icon = (d, extra = "") => `<svg viewBox="0 0 20 20" ${extra}>${d}</svg>`;
const ICON = {
  video: '<path d="M6 4.5v11l9-5.5z" fill="currentColor" stroke="none"/>',
  stack: '<rect x="3" y="6" width="11" height="11" rx="1"/><path d="M6 3h10a1 1 0 0 1 1 1v10"/>',
  star: '<path d="m10 2.8 2.2 4.6 5 .6-3.7 3.4 1 5-4.5-2.5-4.5 2.5 1-5L2.8 8l5-.6z"/>',
  pause: '<path d="M7 4v12M13 4v12" stroke-width="2.4"/>',
  play: '<path d="M6 4v12l10-6z"/>',
};
const fmtTime = (s) => {
  if (s == null || isNaN(s)) return "–";
  const m = Math.floor(s / 60), sec = s - m * 60;
  return `${String(m).padStart(2, "0")}:${sec.toFixed(2).padStart(5, "0")}`;
};
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : null;
const pad = (n, w = 3) => String(n ?? 0).padStart(w, "0");
const fmtDay = (ts) => ts ? new Date(ts * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }).toUpperCase() : "–";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function toast(msg, opts = {}) {
  const t = h("div", { class: "toast" + (opts.error ? " err" : "") });
  t.append(h("span", { html: msg }));
  if (opts.action) t.append(h("button", { class: "ghost", style: "color:inherit;padding:2px 8px", onclick: () => { t.remove(); opts.action.fn(); } }, opts.action.label));
  $("#toasts").append(t);
  setTimeout(() => t.remove(), opts.ms || (opts.error ? 7000 : opts.action ? 9000 : 4500));
  return t;
}

// ---------------- theme ----------------
const THEME_ICON = {
  auto: '<circle cx="10" cy="10" r="6.5"/><path d="M10 3.5v13a6.5 6.5 0 0 0 0-13z" fill="currentColor"/>',
  light: '<circle cx="10" cy="10" r="3.5"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"/>',
  dark: '<path d="M16 11.5A6.5 6.5 0 0 1 8.5 4a6.5 6.5 0 1 0 7.5 7.5z"/>',
};
function getTheme() { try { return localStorage.getItem("sf-theme") || "auto"; } catch { return "auto"; } }
function applyTheme(t) {
  const r = document.documentElement;
  if (t === "auto") delete r.dataset.theme; else r.dataset.theme = t;
  try { localStorage.setItem("sf-theme", t); } catch {}
  const b = $("#themeBtn");
  b.textContent = { auto: "Auto", light: "Light", dark: "Dark" }[t];
  b.title = "Theme. Click to switch between Auto (match your Mac), Light and Dark.";
}

// ---------------- modal ----------------
function modal({ title, text, body, actions }) {
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const scrim = h("div", { class: "modal-scrim", onclick: (e) => { if (e.target === scrim) close(); } },
    h("div", { class: "modal", role: "dialog", "aria-label": title },
      h("h2", {}, title), text ? h("p", {}, text) : null, body ? h("div", { class: "body" }, body) : null,
      h("div", { class: "row" }, ...actions.map((a) => h("button", { class: a.primary ? "primary" : a.danger ? "danger" : "ghost", onclick: () => a.fn(close, scrim) }, a.label)))));
  $("#modalRoot").append(scrim);
  document.addEventListener("keydown", onKey, true);
  scrim.querySelector("button.primary, button.danger")?.focus();
  return close;
}
// in-app dialogs (the Mac app window has no browser pop-ups)
function askText(title, { text, value = "", label = "Save" } = {}) {
  return new Promise((resolve) => {
    const input = h("input", { value, style: "width:100%;font-size:15px" });
    let done = false;
    const finish = (v, close) => { if (done) return; done = true; close(); resolve(v); };
    const close = modal({ title, text, body: input, actions: [
      { label: "Cancel", fn: (c) => finish(null, c) },
      { label, primary: true, fn: (c) => finish(input.value.trim() || null, c) },
    ] });
    input.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") finish(input.value.trim() || null, close); if (e.key === "Escape") finish(null, close); });
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}
function askConfirm(title, { text, label = "OK", danger = false } = {}) {
  return new Promise((resolve) => modal({ title, text, actions: [
    { label: "Cancel", fn: (c) => { c(); resolve(false); } },
    { label, primary: !danger, danger, fn: (c) => { c(); resolve(true); } },
  ] }));
}
// ---------------- menus ----------------
// Stash's own dropdown (instead of the Mac's pop-up menus). Works like a <select>: .value, onchange.
// options: [{ value, label, count?, group? }]; multi: several values, kept as "a,b".
let openMenu = null;
// a menu that's open while its bar redraws (counts update after every pick) moves over to the new button
function reattachMenu(root) {
  if (!openMenu?.key || document.body.contains(openMenu.btn)) return;
  const nb = root.querySelector(`[data-menu-key="${openMenu.key}"]`);
  const top = openMenu.pop.scrollTop;
  if (nb && nb._open) nb._open(top); else closeMenu();
}
function closeMenu() { if (openMenu) { openMenu.pop.remove(); openMenu.btn.classList.remove("open"); openMenu = null; } }
document.addEventListener("pointerdown", (e) => { if (openMenu && !openMenu.pop.contains(e.target) && !openMenu.btn.contains(e.target)) closeMenu(); }, true);
document.addEventListener("keydown", (e) => { if (openMenu && e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); } }, true);
window.addEventListener("resize", closeMenu);
function pick({ options = [], value = "", multi = false, label = "", placeholder = "", title = "", cls = "", plain = false, align = "left", key = "", compact = false } = {}) {
  const btn = h("button", { type: "button", class: "pick" + (plain ? " plain" : "") + (compact ? " compact" : "") + (cls ? " " + cls : ""), title, "aria-haspopup": "listbox" });
  if (key) btn.dataset.menuKey = key;
  let cur = multi ? String(value || "").split(",").filter(Boolean) : String(value ?? "");
  const opts = () => btn._options;
  const text = () => {
    if (multi) {
      if (!cur.length) return compact ? "" : (placeholder || "Any");
      const names = cur.map((v) => opts().find((o) => String(o.value) === v)?.label || v);
      if (compact) return names.length > 1 ? `${names[0]} +${names.length - 1}` : names[0];
      return names.length > 2 ? `${names[0]} +${names.length - 1}` : names.join(", ");
    }
    const o = opts().find((o) => String(o.value) === cur);
    return o ? o.label : placeholder;
  };
  const draw = () => {
    const t = text();
    fill(btn, label ? h("span", { class: "lbl" }, label) : null, t ? h("span", { class: "pv" }, t) : null, h("span", { class: "caret" }));
    btn.classList.toggle("on", multi && cur.length > 0);
  };
  const fire = () => btn.dispatchEvent(new Event("change"));
  const open = () => {
    if (openMenu?.btn === btn) return closeMenu();
    doOpen();
  };
  const doOpen = (scrollTop = 0) => {
    closeMenu();
    const pop = h("div", { class: "menu-pop", role: "listbox" });
    const rows = () => {
      const out = []; let g = null;
      for (const o of opts()) {
        if (o.group && o.group !== g) { g = o.group; out.push(h("div", { class: "menu-group" }, g)); }
        const on = multi ? cur.includes(String(o.value)) : String(o.value) === cur;
        out.push(h("button", { type: "button", class: "menu-row" + (on ? " on" : "") + (o.count === 0 && !on ? " dim" : ""), role: "option", "aria-selected": on ? "true" : "false",
          onclick: () => {
            const v = String(o.value);
            if (multi) { cur = on ? cur.filter((x) => x !== v) : [...cur, v]; draw(); fill(pop, ...rows()); fire(); }
            else { cur = v; draw(); closeMenu(); fire(); }
          } },
          multi ? h("span", { class: "box" }) : null, h("span", { class: "ml" }, o.label), o.count != null ? h("span", { class: "mc" }, pad(o.count)) : null));
      }
      if (multi && cur.length) out.push(h("button", { type: "button", class: "menu-clear", onclick: () => { cur = []; draw(); closeMenu(); fire(); } }, "Clear"));
      if (!out.length) out.push(h("div", { class: "menu-group" }, "Nothing here yet"));
      return out;
    };
    fill(pop, ...rows());
    document.body.append(pop);
    const r = btn.getBoundingClientRect();
    pop.style.minWidth = Math.max(r.width, 200) + "px";
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = align === "right" ? r.right - pw : r.left;
    left = Math.max(8, Math.min(left, innerWidth - pw - 8));
    const below = innerHeight - r.bottom - 8, above = r.top - 8;
    if (ph > below && above > below) { pop.style.bottom = innerHeight - r.top + "px"; pop.style.maxHeight = above + "px"; }
    else { pop.style.top = r.bottom + "px"; pop.style.maxHeight = below + "px"; }
    pop.style.left = left + "px";
    btn.classList.add("open");
    pop.scrollTop = scrollTop;
    openMenu = { btn, pop, key };
  };
  btn._open = doOpen;
  btn.addEventListener("click", open);
  Object.defineProperty(btn, "value", {
    get: () => (multi ? cur.join(",") : cur),
    set: (v) => { cur = multi ? String(v || "").split(",").filter(Boolean) : String(v ?? ""); draw(); },
  });
  btn.setOptions = (o) => { btn._options = o; draw(); };
  btn.setOptions(options);
  return btn;
}

const DESKTOP = new URLSearchParams(location.search).has("app");
{   // the Sort menu in the top bar
  const sort = pick({ label: "Sort", value: "saved", options: [
    { value: "saved", label: "Newest saves" }, { value: "saved_old", label: "Oldest saves" },
    { value: "posted", label: "Newest posts" }, { value: "random", label: "Shuffle" }] });
  sort.id = "sort"; sort.classList.add("plain-on");
  const again = h("button", { id: "reshuffle", class: "shuf", title: "Shuffle again", hidden: true }, "↻");
  const back = h("button", { id: "unshuffle", class: "shuf", title: "Back to the last shuffle", hidden: true }, "↶");
  document.getElementById("sortCell").append(sort, again, back);
}
function openExternal(url) {
  if (DESKTOP) api("/api/desktop/open-url", { method: "POST", body: { url } }).catch((e) => toast(esc(e.message), { error: true }));
  else window.open(url, "_blank", "noopener");
}
function savedToast(r) {
  toast(`Saved <b>${esc(r.name)}</b> to Downloads`, { action: { label: "Show in Finder", fn: () => api("/api/desktop/reveal", { method: "POST", body: { path: r.path } }) } });
}

function exportDialog({ ids, collection, name, count }) {
  const chk = (label, on = true) => { const i = h("input", { type: "checkbox", checked: on }); return [i, h("label", { class: "check" }, i, label)]; };
  const [img, imgL] = chk("Photos"), [vid, vidL] = chk("Videos"), [meta, metaL] = chk("Spreadsheet of creators, links and captions");
  modal({
    title: `Export ${collection ? "“" + collection + "”" : count ? count + " posts" : "selection"}`,
    text: "Saves the original files, full quality, into one zip.",
    body: h("div", {}, imgL, vidL, metaL),
    actions: [
      { label: "Cancel", fn: (close) => close() },
      { label: "Export zip", primary: true, fn: async (close, scrim) => {
        const btn = scrim.querySelector("button.primary");
        btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Preparing…';
        try {
          const r = await api("/api/export", { method: "POST", body: { ids, collection, name, images: img.checked, videos: vid.checked, metadata: meta.checked } });
          downloadUrl(r.url);
          if (!DESKTOP) toast(`Exported ${r.files} files (${(r.size / 1048576).toFixed(0)} MB) to <b>${esc(r.name)}</b>`);
          close();
        } catch (e) { toast(esc(e.message), { error: true }); btn.disabled = false; btn.textContent = "Export zip"; }
      } },
    ],
  });
}

// ---------------- state ----------------
const S = {
  filter: {}, sort: "saved", seed: Math.floor(Math.random() * 1e6),
  items: [], offset: 0, more: true, loading: false, total: null, gen: 0,
  selected: new Set(), side: null, viewerIndex: -1, post: null, slide: 0,
  inPt: null, outPt: null, settings: null, jobs: [], view: "frames", facets: null, queryImg: null,
};
const NAV_KEYS = ["collection", "tag", "creator", "kind", "favorite", "uncollected", "status", "trashed"];
const LOOK_KEYS = ["tech", "content", "color", "tone", "sat", "temp", "contrast", "ar"];
const CONTENT = [["film", "Film frames"], ["notfilm", "Not film frames"], ["talking", "Talking to camera"], ["bts", "Behind the scenes"], ["gear", "Gear & products"], ["text", "Text & graphics"]];
const RANK_KEYS = ["similar", "simimg"];
const LOOK = {
  tone: ["Tone", [["low", "Low key"], ["mid", "Mid key"], ["high", "High key"]]],
  sat: ["Saturation", [["saturated", "Saturated"], ["muted", "Muted"], ["bw", "Black & white"]]],
  temp: ["Temperature", [["warm", "Warm"], ["neutral", "Neutral"], ["cool", "Cool"]]],
  contrast: ["Contrast", [["high", "High"], ["normal", "Normal"], ["low", "Low"]]],
  ar: ["Aspect", ["2.39:1", "2.20:1", "2:1", "1.85:1", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "2:3", "9:16"].map((a) => [a, a])],
};
const SWATCHES = ["#c8102e", "#ff5a1f", "#f2b134", "#e9dcb8", "#5a8f3c", "#1fb5ad", "#1d4ed8", "#6d28d9", "#e64fb5", "#7a4b2a", "#ffffff", "#000000"];

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  S.filter = {};
  for (const k of ["q", "chips", ...NAV_KEYS, ...LOOK_KEYS, ...RANK_KEYS]) if (p.get(k)) S.filter[k] = p.get(k);
  $("#q").value = S.filter.q || "";
  renderChips();
}
// ---------------- stacked search ----------------
// Enter turns what you typed into a chip; every chip has to match, so each one narrows the results
const chipList_ = () => (S.filter.chips || "").split("|").filter(Boolean);
const searchTerms = () => [...chipList_(), ...(S.filter.q ? [S.filter.q] : [])];
const searchActive = () => searchTerms().length > 0;
const searchLabel = () => searchTerms().join(" + ");
function setChips(list) {
  S.filter.chips = list.join("|");
  if (!S.filter.chips) delete S.filter.chips;
  renderChips();
}
function renderChips() {
  const el = $("#chips");
  if (!el) return;
  fill(el, ...chipList_().map((t, i) => h("span", { class: "schip" }, t,
    h("button", { type: "button", "aria-label": "Remove " + t, title: "Remove", onclick: (e) => {
      e.preventDefault(); const l = chipList_(); l.splice(i, 1); setChips(l); writeHash(); reload(); $("#q").focus();
    } }, "×"))));
  $("#q").placeholder = chipList_().length ? "And…" : (S.aiOn ? "Search anything: flower, red neon, overhead table…" : "Captions, creators, tags, notes");
}
function writeHash() {
  const p = new URLSearchParams(S.filter);
  history.replaceState(null, "", "#" + p.toString());
}
function setFilter(f, keepQuery = true) {
  // switching what you're looking at keeps the search box and the look filters
  const keep = Object.fromEntries(Object.entries(S.filter).filter(([k]) => LOOK_KEYS.includes(k) || (keepQuery && (k === "q" || k === "chips"))));
  S.filter = { ...keep, ...f };
  for (const k in S.filter) if (!S.filter[k]) delete S.filter[k];
  writeHash();
  clearSelection();
  $("#side").classList.remove("open");
  reload();
  navCommit();
}

// ---------------- sidebar ----------------
async function loadSidebar() {
  loadFacets();
  if (!S.tech) api("/api/techniques").then((t) => { S.tech = t; renderFilters(); }).catch(() => {});
  api("/api/ai").then((a) => {
    S.aiOn = a.enabled && a.installed;
    renderChips();
  }).catch(() => {});
  S.side = await api("/api/sidebar");
  renderSidebar();
  fill($("#collectionList"), ...S.side.collections.map((c) => h("option", { value: c.name })));
}
function updateFilter(patch) {
  S.filter = { ...S.filter, ...patch };
  for (const k in S.filter) if (!S.filter[k]) delete S.filter[k];
  writeHash(); clearSelection(); reload(); navCommit();
}
function navItem(label, n, f, extra = {}, cls = "") {
  const on = JSON.stringify(f) === JSON.stringify(Object.fromEntries(Object.entries(S.filter).filter(([k]) => NAV_KEYS.includes(k))));
  return h("button", { class: "nav-item " + cls + (on ? " on" : ""), onclick: () => setFilter(f), ...extra },
    h("span", { class: "label" }, label), n != null ? h("span", { class: "n" }, pad(n)) : null);
}
function renderSidebar() {
  if (document.activeElement?.classList.contains("side-search")) return;   // don't yank the box away mid-typing
  const sd = S.side; if (!sd) return;
  const c = sd.counts;
  const tagsShown = S.showAllTags ? sd.tags : sd.tags.slice(0, 30);
  fill($("#nav"),
    h("h3", { style: "margin-top:0;border-top:0" }, "Library"),
    navItem("Everything", c.ready, {}),
    navItem("Favorites", c.favorite, { favorite: "1" }),
    navItem("Videos", null, { kind: "video" }),
    navItem("Images", null, { kind: "image" }),
    navItem("Not in a collection", c.uncollected, { uncollected: "1" }),
    c.pending ? navItem("Waiting for media", c.pending, { status: "pending" }) : null,
    c.gone ? navItem("No longer on Instagram", c.gone, { status: "gone" }) : null,
    c.trash ? navItem("Trash", c.trash, { trashed: "1" }) : null,
    h("h3", {}, "Collections", h("button", { title: "New collection", "aria-label": "New collection", onclick: newCollection }, "+")),
    ...sd.collections.map((col) => {
      const el = navItem(col.name, col.n, { collection: col.name }, { title: col.source === "instagram" ? "From Instagram" : "Your collection" });
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop"));
      el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("drop"); dropInto(col.name, e); });
      return el;
    }),
    sd.collections.length ? null : h("p", { class: "hint", style: "padding:8px 16px" }, "Collections from Instagram show up here after you import or sync."),
    h("h3", {}, "Moodboards", h("button", { title: "New moodboard", "aria-label": "New moodboard", onclick: () => Board.create() }, "+")),
    ...(sd.boards || []).map((b) => h("button", { class: "nav-item board-link", onclick: () => Board.open(b.id) },
      h("span", { class: "label" }, b.name), h("span", { class: "n" }, pad(b.n)))),
    sd.creators.length ? h("h3", {}, "Creators") : null,
    sd.creators.length ? creatorsBlock(sd.creators) : null,
    sd.tags.length ? h("h3", {}, "Tags") : null,
    sd.tags.length ? h("div", { class: "tag-cloud" }, ...tagsShown.map((t) =>
      h("button", { class: S.filter.tag === t.name ? "on" : "", onclick: () => setFilter(S.filter.tag === t.name ? {} : { tag: t.name }) }, t.name))) : null,
    sd.tags.length > 30 ? h("button", { class: "more-link", onclick: () => { S.showAllTags = !S.showAllTags; renderSidebar(); } }, S.showAllTags ? "Fewer tags" : `All ${sd.tags.length} tags`) : null,
  );
}
// creators: type a letter or any part of a handle; best matches first (starts with, then contains, then loose)
function creatorMatches(list, q) {
  q = q.toLowerCase().replace(/^@/, "").replace(/\s+/g, "");
  if (!q) return list;
  const loose = (name) => { let i = 0; for (const ch of name) if (ch === q[i]) i++; return i === q.length; };
  const tier = (name) => {
    const n = name.toLowerCase(), plain = n.replace(/[._]/g, "");
    if (n.startsWith(q) || plain.startsWith(q)) return 0;
    if (n.split(/[._]/).some((part) => part.startsWith(q))) return 1;
    if (n.includes(q) || plain.includes(q)) return 2;
    if (q.length > 2 && loose(plain)) return 3;
    return 9;
  };
  return list.map((c) => [tier(c.name), c]).filter(([t]) => t < 9).sort((a, b) => a[0] - b[0] || b[1].n - a[1].n).map(([, c]) => c);
}
function creatorsBlock(all) {
  const list = h("div", {});
  const input = h("input", { type: "search", class: "side-search", placeholder: "Find a creator", value: S.creatorQ || "", "aria-label": "Find a creator", autocomplete: "off" });
  const draw = () => {
    const q = input.value.trim();
    const found = creatorMatches(all, q);
    const shown = q ? found.slice(0, 60) : found.slice(0, S.showAllCreators ? 60 : 8);
    fill(list, ...shown.map((cr) => navItem("@" + cr.name, cr.n, { creator: cr.name }, {}, "handle")),
      q && !found.length ? h("div", { class: "side-empty" }, "No creator matches") : null,
      !q && all.length > 8 ? h("button", { class: "more-link", onclick: () => { S.showAllCreators = !S.showAllCreators; draw(); } }, S.showAllCreators ? "Fewer" : "More creators") : null);
  };
  input.addEventListener("input", () => { S.creatorQ = input.value; draw(); });
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { const first = creatorMatches(all, input.value.trim())[0]; if (first) setFilter({ creator: first.name }); }
    if (e.key === "Escape") { input.value = ""; S.creatorQ = ""; draw(); input.blur(); }
  });
  draw();
  return h("div", { class: "creators" }, h("div", { class: "side-search-row" }, input), list);
}
async function newCollection() {
  const name = await askText("New collection", { label: "Create" });
  if (!name) return;
  const ids = [...S.selected];
  try {
    await api("/api/collections", { method: "POST", body: { name } });
    if (ids.length) {
      await api("/api/posts/bulk", { method: "POST", body: { ids, add_collections: [name] } });
      toast(`Added ${ids.length} to <b>${esc(name)}</b>`);
      clearSelection();
    } else toast(`Created <b>${esc(name)}</b>. Drag posts onto it, or select some and choose Add.`);
    loadSidebar();
  } catch (e) { toast(esc(e.message), { error: true }); }
}
async function dropInto(name, e) {
  const id = Number(e.dataTransfer.getData("text/stash-id"));
  const ids = S.selected.size && S.selected.has(id) ? [...S.selected] : [id];
  await api("/api/posts/bulk", { method: "POST", body: { ids, add_collections: [name] } });
  toast(`Added ${ids.length > 1 ? ids.length + " posts" : "post"} to <b>${esc(name)}</b>`);
  clearSelection(); loadSidebar();
}

// ---------------- scope header ----------------
function renderScope() {
  const fres = document.querySelector("#filters .fres");
  if (fres) fres.textContent = S.total != null ? `${pad(S.total)} results` : "";
  const f = S.filter;
  let title = "Everything";
  if (f.collection) title = f.collection;
  else if (f.tag) title = "#" + f.tag;
  else if (f.creator) title = "@" + f.creator;
  else if (f.favorite) title = "Favorites";
  else if (f.kind === "video") title = "Videos";
  else if (f.kind === "image") title = "Images";
  else if (f.uncollected) title = "Not in a collection";
  else if (f.status === "pending") title = "Waiting for media";
  else if (f.status === "gone") title = "No longer on Instagram";
  else if (f.trashed) title = "Trash";
  if (f.content && !f.collection && !f.tag && !f.creator) title = CONTENT.find(([v]) => v === f.content)?.[1] || title;
  if (f.similar) title = "Similar shots";
  if (f.simimg) title = "Looks like this";
  const h1 = h("h1", {}, title);
  if (f.creator || f.tag) h1.style.textTransform = "none";
  const meta = [h("div", { class: "stat" }, h("span", { class: "lbl" }, "Posts"), h("b", {}, S.total != null ? pad(S.total) : "···"))];
  if (searchActive()) meta.push(h("div", { class: "stat" }, h("span", { class: "lbl" }, "Search"), h("b", {}, searchLabel())));
  if (searchActive() && S.aiOn) meta.push(h("div", { class: "stat", title: "Looks at every image and about one frame a second of every video, plus captions" }, h("span", { class: "lbl" }, "Matching"), h("b", {}, "what's in the frame + captions")));
  if (f.simimg && S.queryImg) meta.unshift(h("img", { class: "query-img", src: S.queryImg, alt: "Your search image" }));
  if (f.similar || f.simimg) meta.push(h("div", { class: "stat" }, h("span", { class: "lbl" }, "Ranked by"), h("b", {}, "composition + color")));
  meta.push(h("span", { class: "spacer" }));
  if (S.total) meta.push(h("button", { title: "Select everything in this view, then add to a collection, export or trash", onclick: async () => {
    const p = new URLSearchParams({ ...S.filter, sort: S.sort, ids_only: 1 });
    const r = await api("/api/posts?" + p);
    r.ids.forEach((id) => S.selected.add(id));
    document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("sel", S.selected.has(Number(t.dataset.id))));
    renderSelbar();
  } }, `Select all ${S.total.toLocaleString()}`));
  if (f.collection) meta.push(
    h("button", { onclick: boardFromCollection }, "Open as moodboard"),
    h("button", { onclick: () => exportDialog({ collection: f.collection, count: S.total }) }, "Export"),
    h("button", { onclick: renameCollection }, "Rename"),
    h("button", { class: "danger", onclick: deleteCollection }, "Delete"));
  if (f.uncollected && S.total) meta.push(h("span", { class: "note" }, "Saves that aren't in any collection"),
    h("button", { class: "danger", onclick: async () => {
      if (!(await askConfirm(`Move ${S.total} saves to the trash?`, { text: "Everything that isn't in a collection goes to the trash. You can restore it until you empty the trash, and sync won't bring it back.", label: "Move to trash", danger: true }))) return;
      const r = await api("/api/posts/trash-uncollected", { method: "POST", body: {} });
      toast(`Moved ${r.count} to trash`, { action: { label: "Undo", fn: async () => { await api("/api/posts/trash", { method: "POST", body: { ids: r.ids, restore: true } }); loadSidebar(); reload(); } } });
      loadSidebar(); reload();
    } }, "Move all to trash"));
  if (f.trashed) meta.push(h("span", { class: "note" }, "Nothing is deleted until you empty the trash"),
    h("button", { class: "danger", onclick: emptyTrash }, "Empty trash"));
  if (f.creator) meta.push(h("button", { onclick: () => openExternal(`https://www.instagram.com/${encodeURIComponent(f.creator)}/`) }, "Open on Instagram"));
  if (f.similar || f.simimg) meta.push(h("button", { onclick: () => updateFilter({ similar: null, simimg: null }) }, "Back to library"));
  fill($("#scope"), h1, h("div", { class: "scope-meta" }, ...meta));
  renderFilters();
}
function renderFilters() {
  const f = S.filter, fc = S.facets || {};
  const cell = (el) => h("div", { class: "cell menu-cell" }, el);
  const menu = (label, key, options, extra = {}) => {
    const m = pick({ label, key, multi: true, compact: true, value: f[key] || "", options, ...extra });
    m.onchange = () => updateFilter({ [key]: m.value || null });
    return cell(m);
  };
  // techniques: picks in one menu widen, menus narrow each other
  const tc = fc.tech || {}, techSel = (f.tech || "").split(",").filter(Boolean);
  const groupTags = (g) => (S.tech?.tags || []).filter((t) => t.group === g);
  const techOpts = (g) => groupTags(g).filter((t) => tc[t.tag] || techSel.includes(t.tag)).map((t) => ({ value: t.tag, label: t.label, count: tc[t.tag] || 0 }));
  const setTech = (g, vals) => {
    const others = techSel.filter((t) => !groupTags(g).some((x) => x.tag === t));
    updateFilter({ tech: [...others, ...vals].join(",") || null });
  };
  const techMenu = (g, label = g) => {
    const tags = groupTags(g);
    const m = pick({ label, key: "tech:" + g, multi: true, compact: true, value: techSel.filter((t) => tags.some((x) => x.tag === t)).join(","),
      title: "Tagged on your Mac. Results start at the moment it shows up.", options: techOpts(g) });
    m.onchange = () => setTech(g, m.value.split(",").filter(Boolean));
    return cell(m);
  };
  const lookOpts = (k) => { const counts = fc[k] || {}; return LOOK[k][1].map(([v, l]) => ({ v, l, count: counts[v] || 0 })); };
  // Look: the look tags, plus Tone / Saturation / Contrast tucked under "More"
  const lookMenu = (() => {
    const mine = [...techSel.filter((t) => groupTags("Look").some((x) => x.tag === t)).map((t) => "tech:" + t),
      ...["tone", "sat", "contrast"].flatMap((k) => (f[k] || "").split(",").filter(Boolean).map((v) => k + ":" + v))];
    const options = [
      ...techOpts("Look").map((o) => ({ ...o, value: "tech:" + o.value, group: "Look" })),
      ...["tone", "sat", "contrast"].flatMap((k) => lookOpts(k).map((o) => ({ value: k + ":" + o.v, label: o.l + (k === "contrast" ? " contrast" : ""), count: o.count, group: "More / " + LOOK[k][0] }))),
    ];
    const m = pick({ label: "Look", key: "look", multi: true, compact: true, value: mine.join(","), options });
    m.onchange = () => {
      const vals = m.value.split(",").filter(Boolean), by = (p) => vals.filter((v) => v.startsWith(p + ":")).map((v) => v.slice(p.length + 1));
      const others = techSel.filter((t) => !groupTags("Look").some((x) => x.tag === t));
      updateFilter({ tech: [...others, ...by("tech")].join(",") || null, tone: by("tone").join(",") || null, sat: by("sat").join(",") || null, contrast: by("contrast").join(",") || null });
    };
    return cell(m);
  })();
  const cc = fc.content || {};
  const ccTotal = Object.values(cc).reduce((a, b) => a + b, 0);
  const counts = (k) => fc[k] || {};
  const aspectOpts = LOOK.ar[1].filter(([v]) => counts("ar")[v] || (f.ar || "").split(",").includes(v)).map(([v, l]) => ({ value: v, label: l, count: counts("ar")[v] || 0 }));
  const active = LOOK_KEYS.some((k) => f[k]);
  const res = h("span", { class: "fres" }, S.total != null ? `${pad(S.total)} results` : "");
  const filters = $("#filters");
  filters.classList.add("fbar-a");   // the "spec sheet" look; remove this class to go back to the previous bar
  fill(filters,
    h("span", { class: "fhead" }, "Filter"),
    menu("Content", "content", CONTENT.map(([v, l]) => ({ value: v, label: l, count: v === "notfilm" ? ccTotal - (cc.film || 0) : cc[v] || 0 })), { title: "What each save shows, tagged by AI on your Mac" }),
    menu("Aspect", "ar", aspectOpts),
    gstart(techMenu("Frame Size")), techMenu("Shot Type"), techMenu("Composition"), techMenu("Movement"),
    gstart(lookMenu),
    menu("Temperature", "temp", lookOpts("temp").map((o) => ({ value: o.v, label: o.l, count: o.count }))),
    cell(colorButton()),
    h("span", { class: "spacer" }),
    fc.pending ? h("span", { class: "analyzing" }, `Analyzing ${fc.pending}…`) : null,
    res,
    active ? h("button", { class: "cell clear-cell", onclick: () => updateFilter(Object.fromEntries(LOOK_KEYS.map((k) => [k, null]))) }, "Clear") : null);
  reattachMenu($("#filters"));
}
const gstart = (el) => { el.classList.add("gstart"); return el; };   // a heavier rule between groups of filters
// Color: one button that opens the swatches (a rainbow square until a color is picked)
function colorButton() {
  const f = S.filter;
  const btn = h("button", { type: "button", class: "pick compact" + (f.color ? " on" : ""), "data-menu-key": "color", title: "Frames featuring a color" },
    h("span", { class: "lbl" }, "Color"),
    h("span", { class: "chip-swatch" + (f.color ? "" : " rainbow"), style: f.color ? `background:${f.color}` : "" }),
    f.color ? h("span", { class: "pv mono-hex" }, f.color.toUpperCase()) : null,
    h("span", { class: "caret" }));
  btn._open = () => {
    closeMenu();
    const custom = h("input", { type: "color", value: f.color || "#c89723", title: "Pick any color", "aria-label": "Pick any color" });
    custom.onchange = () => { closeMenu(); updateFilter({ color: custom.value }); };
    // or type a hex code
    const hex = h("input", { type: "text", class: "hex-in", placeholder: "#HEX", value: f.color ? f.color.toUpperCase() : "", maxlength: 7, spellcheck: "false", "aria-label": "Hex color" });
    const applyHex = () => {
      let v = hex.value.trim().replace(/^#?/, "#");
      if (/^#[0-9a-f]{3}$/i.test(v)) v = "#" + [...v.slice(1)].map((c) => c + c).join("");
      if (!/^#[0-9a-f]{6}$/i.test(v)) { hex.classList.add("bad"); return; }
      closeMenu(); updateFilter({ color: v.toLowerCase() });
    };
    hex.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") applyHex(); if (e.key === "Escape") closeMenu(); hex.classList.remove("bad"); });
    const pop = h("div", { class: "menu-pop color-pop" },
      h("div", { class: "swatch-row" }, ...SWATCHES.map((c) => h("button", { class: f.color === c ? "on" : "", style: `background:${c}`, title: c, "aria-label": `Color ${c}`,
        onclick: () => { closeMenu(); updateFilter({ color: f.color === c ? null : c }); } })), custom),
      h("div", { class: "hex-row" }, hex, h("button", { type: "button", class: "menu-clear", onclick: applyHex }, "Apply")),
      f.color ? h("button", { type: "button", class: "menu-clear", onclick: () => { closeMenu(); updateFilter({ color: null }); } }, "Clear color") : null);
    document.body.append(pop);
    const r = btn.getBoundingClientRect();
    pop.style.top = r.bottom + "px"; pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8)) + "px";
    btn.classList.add("open");
    openMenu = { btn, pop, key: "color" };
    setTimeout(() => hex.focus(), 0);
  };
  btn.addEventListener("click", () => (openMenu?.btn === btn ? closeMenu() : btn._open()));
  return btn;
}
// counts follow what you've already picked, so every menu only offers what's actually there
let facetGen = 0;
async function loadFacets() {
  const gen = ++facetGen;
  const p = new URLSearchParams(S.filter);
  try { const fc = await api("/api/facets?" + p); if (gen === facetGen) { S.facets = fc; renderFilters(); } } catch {}
}
async function boardFromCollection() {
  const name = S.filter.collection;
  try {
    const r = await api("/api/boards", { method: "POST", body: { name, collection: name, limit: 60 } });
    if (S.total > 60) toast("Started with the 60 newest. Select more posts and use Moodboard to add them.");
    loadSidebar(); Board.open(r.id);
  } catch (e) { toast(esc(e.message), { error: true }); }
}
async function emptyTrash() {
  modal({ title: "Empty the trash?", text: "This permanently deletes these posts and their files from your computer. It can't be undone.",
    actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Delete forever", danger: true, fn: async (c) => {
      const r = await api("/api/trash/purge", { method: "POST", body: {} });
      toast(`Deleted ${r.count} posts and freed ${(r.freed / 1048576).toFixed(0)} MB`); c(); loadSidebar(); setFilter({});
    } }] });
}
async function renameCollection() {
  const old = S.filter.collection;
  const name = await askText(`Rename “${old}”`, { text: "Use the name of another collection to merge them.", value: old, label: "Rename" });
  if (!name || name === old) return;
  await api("/api/collections/rename", { method: "POST", body: { old, new: name } });
  await loadSidebar(); setFilter({ collection: name });
}
async function deleteCollection() {
  const old = S.filter.collection;
  if (!(await askConfirm(`Delete “${old}”?`, { text: "The collection goes away. The posts stay in your library.", label: "Delete collection", danger: true }))) return;
  await api("/api/collections/rename", { method: "POST", body: { old, new: "" } });
  await loadSidebar(); setFilter({});
}

// ---------------- grid ----------------
let cols = [], colHeights = [];
const fr = { pending: [], tail: null, width: 0 };
function colCount() {
  const w = $("#grid").clientWidth || window.innerWidth;
  const d = 11 - Number($("#density").value);          // slider: right = bigger tiles = fewer columns
  const ideal = Math.max(2, Math.round(w / (1600 / d)));
  return window.innerWidth < 600 ? Math.min(ideal, 2) : ideal;
}
function frameHeight() {
  const d = 11 - Number($("#density").value);         // slider right = bigger rows
  const hgt = Math.round(470 - (d - 2) * 46);
  return window.innerWidth < 600 ? Math.min(hgt, 150) : hgt;
}
function aspectOf(p) {
  const m = p.media[0] || {};
  const a = m.ar || (m.width && m.height ? m.width / m.height : 0.8);
  return Math.max(0.45, Math.min(3.2, a));
}
function framesFlush(final) {
  const grid = $("#grid"), W = grid.clientWidth - 12, H = frameHeight(), g = 6;
  fr.width = grid.clientWidth;
  if (fr.tail) { fr.tail.remove(); fr.tail = null; }
  let row = [], sum = 0;
  const queue = fr.pending; fr.pending = [];
  for (const it of queue) {
    row.push(it); sum += aspectOf(it[0]);
    if (sum * H + g * (row.length - 1) >= W) {
      grid.append(frameRow(row, (W - g * (row.length - 1)) / sum));
      row = []; sum = 0;
    }
  }
  fr.pending = row;
  if (row.length) { fr.tail = frameRow(row, H); grid.append(fr.tail); }
}
function frameRow(items, rh) {
  return h("div", { class: "frow", style: `height:${rh}px` }, ...items.map(([p, i]) => {
    const el = tile(p, i), w = aspectOf(p) * rh;
    el.style.width = w + "px"; el.style.height = rh + "px";
    const img = el.querySelector("img"); if (img) img.sizes = Math.ceil(w) + "px";
    return el;
  }));
}
function layout(reset) {
  const grid = $("#grid"), n = colCount();
  grid.className = "grid " + S.view + (S.selected.size ? " selecting" : "");
  $("#viewIndex").classList.toggle("on", S.view === "index");
  $("#viewFrames").classList.toggle("on", S.view === "frames");
  if (S.view === "frames") {
    if (reset || Math.abs(grid.clientWidth - fr.width) > 2) {
      fill(grid); fr.pending = S.items.map((p, i) => [p, i]); fr.tail = null; framesFlush();
    }
    return;
  }
  if (S.view === "index") {
    grid.style.setProperty("--cols", n);
    if (reset) { fill(grid); cols = []; S.items.forEach((p, i) => place(p, i)); }
    return;
  }
}
// rebuild the grid without losing your place (removing a tile shouldn't send you back to the top)
function relayoutInPlace(gone = []) {
  // anchor on a tile you're looking at (one that's staying), so it stays put on screen while the rows re-flow
  const tiles = [...document.querySelectorAll("#grid .tile")].filter((t) => !gone.includes(Number(t.dataset.id)));
  const anchor = tiles.find((t) => t.getBoundingClientRect().bottom > 80);
  const id = anchor?.dataset.id, off = anchor ? anchor.getBoundingClientRect().top : 0;
  const grid = $("#grid");
  grid.style.minHeight = grid.offsetHeight + "px";
  layout(true);
  const settle = () => {
    const el = id && document.querySelector(`#grid .tile[data-id="${id}"]`);
    if (!el) return;
    const d = el.getBoundingClientRect().top - off;
    if (Math.abs(d) < 1) return;
    const main = document.querySelector(".main");
    const sc = main && main.scrollHeight > main.clientHeight ? main : document.scrollingElement;
    sc.scrollTop += d;
  };
  settle();
  requestAnimationFrame(() => { grid.style.minHeight = ""; settle(); setTimeout(settle, 120); });
}
function place(p, i) {
  if (S.view === "frames") return fr.pending.push([p, i]);
  const el = tile(p, i), img = el.querySelector("img");
  if (img) img.sizes = Math.ceil(($("#grid").clientWidth || 1200) / colCount()) + "px";
  $("#grid").append(el);
}
function kindLabel(p) {
  const m = p.media[0] || {};
  if (m.kind === "video") return "Video " + fmtTime(m.duration).replace(/\.\d\d$/, "");
  if (p.media_count > 1) return p.media_count + " img";
  return m.width ? `${m.width}×${m.height}` : "";
}
// small preview for small tiles, the full-res file when the tile (or a Retina screen) needs more pixels
function sharpImg(m, sizes = "420px") {
  const full = m.kind === "video" ? m.poster_src : m.src;
  const fullW = m.width || 1080;
  const attrs = { src: m.thumb_src, loading: "lazy", decoding: "async", alt: "", sizes };
  if (full && fullW > 560) attrs.srcset = `${m.thumb_src} 520w, ${full} ${fullW}w`;
  return h("img", attrs);
}
function tile(p, i, ratio) {
  const m = p.media[0] || {};
  const isVideo = m.kind === "video";
  const num = "[" + pad(i + 1, Math.max(3, String(S.total || 0).length)) + "]";
  const hit = p.hit;
  const box = h("span", { class: "box" }, m.thumb_src ? (m.hit_src ? h("img", { src: m.hit_src, loading: "lazy", decoding: "async", alt: "" }) : sharpImg(m)) : null);
  if (hit && m.kind === "video" && hit.t >= 0.5) box.append(h("span", { class: "hitchip", title: "Where your search shows up" }, "▶ " + fmtTime(hit.t).replace(/\.\d\d$/, "")));
  else if (hit && p.media_count > 1) box.append(h("span", { class: "hitchip" }, `${hit.idx + 1} / ${p.media_count}`));
  const el = h("button", { class: "tile" + (S.selected.has(p.id) ? " sel" : "") + (p.favorite ? " fav" : ""), "data-id": p.id, draggable: "true", "aria-label": `Post ${i + 1} by ${p.username || "unknown"}` }, box);
  if (S.view === "index") {
    el.append(h("span", { class: "cap" }, h("span", { class: "n" }, num), h("span", { class: "k" }, (p.favorite ? "★ " : "") + kindLabel(p))),
      h("span", { class: "who" }, p.username ? "@" + p.username : p.shortcode));
  } else {
    el.append(h("span", { class: "meta" }, h("span", {}, num + " " + (p.username ? "@" + p.username : "")),
      h("span", {}, [(m.ar_label || ""), m.kind === "video" ? fmtTime(m.duration).replace(/\.\d\d$/, "") : p.media_count > 1 ? p.media_count + " img" : ""].filter(Boolean).join(" / "))));
  }
  el.addEventListener("click", (e) => {
    if (e.shiftKey || e.metaKey || e.ctrlKey || S.selected.size) return toggleSelect(p.id, el, e.shiftKey ? i : null);
    if (searchActive() && S.aiOn) {
      S.opened = S.opened || new Set();
      const skipped = S.items.slice(Math.max(0, i - 12), i).filter((x) => !S.opened.has(x.id)).map((x) => x.id);
      S.opened.add(p.id);
      learnSignal("open", p, { skipped });
    }
    openViewer(i);
  });
  box.append(h("span", { class: "selbox", role: "checkbox", "aria-label": "Select", title: "Select (or ⌘-click the tile)",
    onclick: (e) => { e.stopPropagation(); e.preventDefault(); toggleSelect(p.id, el, e.shiftKey ? i : null); } }));
  const star = h("span", { class: "favstar", role: "button", title: "Favorite (F)", "aria-label": "Favorite",
    onclick: (e) => { e.stopPropagation(); e.preventDefault(); const it = S.items.find((x) => x.id === p.id) || p; setFavorite([p.id], !it.favorite); } });
  star.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2.8l2.75 5.9 6.45.75-4.78 4.4 1.3 6.37L12 17.02l-5.72 3.2 1.3-6.37L2.8 9.45l6.45-.75z"/></svg>';
  box.append(star);
  if (searchActive() && S.aiOn) {
    box.append(h("span", { class: "nomatch", role: "button", title: "Not what I searched for: hide it and teach search", onclick: (e) => { e.stopPropagation(); notAMatch(p); } }, "Not a match"));
  }
  el.addEventListener("mouseenter", () => { S.hoverId = p.id; });
  el.addEventListener("mouseleave", () => { if (S.hoverId === p.id) S.hoverId = null; });
  el.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/stash-id", p.id));
  if (isVideo) {
    let v, timer;
    el.addEventListener("mouseenter", () => {
      timer = setTimeout(() => {
        v = h("video", { src: m.src, muted: true, loop: true, playsinline: true, preload: "auto" });
        v.muted = true;
        if (hit && hit.t >= 0.5) v.addEventListener("loadedmetadata", () => { v.currentTime = hit.t; }, { once: true });
        box.append(v); v.play().catch(() => {});
      }, 180);
    });
    el.addEventListener("mouseleave", () => { clearTimeout(timer); if (v) { v.pause(); v.remove(); v = null; } });
  }
  return el;
}
async function loadMore() {
  if (S.loading || !S.more) return;
  S.loading = true;
  const gen = S.gen;
  const p = new URLSearchParams({ ...S.filter, sort: S.sort, offset: S.offset, limit: 80 });
  if (S.sort === "random") p.set("seed", S.seed);
  try {
    const data = await api("/api/posts?" + p);
    if (gen !== S.gen) return;
    if (data.total != null) { S.total = data.total; renderScope(); }
    const start = S.items.length;
    S.items.push(...data.items);
    S.offset += data.items.length;
    S.more = data.more;
    data.items.forEach((post, j) => place(post, start + j));
    if (S.view === "frames") framesFlush();
    renderEmpty();
  } catch (e) { toast(esc(e.message), { error: true }); }
  finally { S.loading = false; }
  // keep filling if the sentinel is still on screen
  requestAnimationFrame(() => {
    if (S.more && $("#sentinel").getBoundingClientRect().top < window.innerHeight + 800) loadMore();
  });
}
function reload() {
  S.opened = new Set();
  loadFacets();
  S.gen++; S.items = []; S.offset = 0; S.more = true; S.total = null; S.loading = false;
  layout(true); renderScope(); loadMore();
}
function renderEmpty() {
  const el = $("#empty");
  if (S.items.length || S.more) { el.hidden = true; return; }
  el.hidden = false;
  const lib = S.side?.counts || {};
  if (!lib.ready && !lib.pending && !Object.keys(S.filter).length) {
    fill(el, 
      h("h2", {}, "Bring in your saves"),
      h("p", {}, "Import the data export from Instagram to pull in your whole backlog and its collections, then connect sync so new saves arrive on their own."),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => openDrawer("import") }, "Import Instagram export"),
        h("button", { onclick: () => openDrawer("sync") }, "Set up sync")));
  } else if (S.filter.status === "pending" || (lib.pending && !lib.ready)) {
    fill(el, h("h2", {}, `${(lib.pending || 0).toLocaleString()} posts are waiting for their media`),
      h("p", {}, "Your export lists what you saved, but not the photos and videos themselves. Run a sync to download them. Collections you've linked download fastest."),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: () => openDrawer("sync") }, "Open sync")));
  } else if (S.filter.trashed) {
    fill(el, h("h2", {}, "The trash is empty"), h("p", {}, "Posts you move to the trash wait here until you empty it, so nothing is lost by accident."));
  } else {
    fill(el, h("h2", {}, "Nothing here"),
      h("p", {}, searchActive() ? "No saves match that search. Try fewer words, or search everything." : "No saves match this view yet."),
      searchActive() || Object.keys(S.filter).length ? h("div", { class: "row" }, h("button", { onclick: () => { $("#q").value = ""; setFilter({}, false); renderChips(); } }, "Show everything")) : null);
  }
}

// ---------------- selection ----------------
let lastSelIndex = null;
function toggleSelect(id, el, rangeIndex) {
  if (rangeIndex != null && lastSelIndex != null) {
    const [a, b] = [lastSelIndex, rangeIndex].sort((x, y) => x - y);
    for (let i = a; i <= b; i++) S.selected.add(S.items[i].id);
  } else if (S.selected.has(id)) S.selected.delete(id);
  else S.selected.add(id);
  if (rangeIndex != null) lastSelIndex = rangeIndex;
  else lastSelIndex = S.items.findIndex((p) => p.id === id);
  document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("sel", S.selected.has(Number(t.dataset.id))));
  renderSelbar();
}
// favorite or unfavorite posts and update their tiles where they sit
async function setFavorite(ids, on) {
  await api("/api/posts/bulk", { method: "POST", body: { ids, favorite: on } });
  for (const id of ids) {
    const it = S.items.find((x) => x.id === id); if (it) it.favorite = on ? 1 : 0;
    document.querySelectorAll(`#grid .tile[data-id="${id}"]`).forEach((t) => t.classList.toggle("fav", on));
  }
  loadSidebar();
}
async function trashSelected() {
  const moved = [...S.selected];
  if (!moved.length || S.filter.trashed) return;
  await api("/api/posts/trash", { method: "POST", body: { ids: moved } });
  toast(`Moved ${moved.length} to trash`, { action: { label: "Undo", fn: async () => { await api("/api/posts/trash", { method: "POST", body: { ids: moved, restore: true } }); loadSidebar(); reload(); } } });
  // take them out of the grid where they are, without jumping to the top
  S.items = S.items.filter((p) => !moved.includes(p.id));
  if (S.total) S.total -= moved.length;
  clearSelection(); relayoutInPlace(moved); renderScope(); loadSidebar();
}
function clearSelection() {
  S.selected.clear(); lastSelIndex = null;
  document.querySelectorAll(".tile.sel").forEach((t) => t.classList.remove("sel"));
  renderSelbar();
}
function renderSelbar() {
  const n = S.selected.size, bar = $("#selbar");
  bar.hidden = !n;
  $("#grid").classList.toggle("selecting", n > 0);   // keep every tile's box visible while picking
  if (!n) return;
  const ids = () => [...S.selected];
  const count = h("span", { id: "selCount" }, `${n} selected`);
  const done = () => { clearSelection(); loadSidebar(); reload(); };
  if (S.filter.trashed) {
    fill(bar, count,
      h("button", { onclick: async () => { await api("/api/posts/trash", { method: "POST", body: { ids: ids(), restore: true } }); toast(`Restored ${n}`); done(); } }, "Restore"),
      h("button", { class: "danger", onclick: () => modal({ title: `Delete ${n} permanently?`, text: "Their files are removed from your computer. This can't be undone.",
        actions: [{ label: "Cancel", fn: (c) => c() }, { label: "Delete forever", danger: true, fn: async (c) => { await api("/api/trash/purge", { method: "POST", body: { ids: ids() } }); c(); done(); } }] }) }, "Delete forever"),
      h("span", { class: "grow" }),
      h("button", { onclick: clearSelection }, "Clear"));
    return;
  }
  const input = h("input", { list: "collectionList", placeholder: "Collection name or #tag" });
  const add = async () => {
    const v = input.value.trim();
    if (!v) return input.focus();
    await api("/api/posts/bulk", { method: "POST", body: v.startsWith("#") ? { ids: ids(), add_tags: [v.slice(1)] } : { ids: ids(), add_collections: [v] } });
    learnSignal("keep", S.items.filter((x) => S.selected.has(x.id)));
    toast(`Added ${n} to <b>${esc(v)}</b>`); done();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); e.stopPropagation(); });
  const boards = pick({ placeholder: "Moodboard…", value: "", cls: "selbar-pick", options: [
    { value: "__new", label: "New moodboard from selection" },
    ...(S.side?.boards || []).map((b) => ({ value: String(b.id), label: b.name, group: "Add to" }))] });
  boards.onchange = async () => {
    const v = boards.value; boards.value = "";
    if (v) learnSignal("keep", S.items.filter((x) => S.selected.has(x.id)));
    if (v === "__new") {
      const name = await askText("New moodboard", { value: S.filter.collection || "Untitled moodboard", label: "Create" });
      if (!name) return;
      const r = await api("/api/boards", { method: "POST", body: { name, post_ids: ids() } });
      clearSelection(); loadSidebar(); Board.open(r.id);
    } else if (v) {
      const r = await api(`/api/boards/${v}/add`, { method: "POST", body: { post_ids: ids() } });
      const name = S.side.boards.find((b) => String(b.id) === v)?.name;
      toast(`Added ${r.added} to <b>${esc(name)}</b>`, { action: { label: "Open", fn: () => Board.open(Number(v)) } });
      clearSelection(); loadSidebar();
    }
  };
  const allFav = ids().every((id) => S.items.find((x) => x.id === id)?.favorite);
  const star = h("button", { class: "selstar" + (allFav ? " on" : ""), title: (allFav ? "Remove from Favorites" : "Add to Favorites") + " (F)", "aria-label": "Favorite",
    onclick: async () => { await setFavorite(ids(), !allFav); toast(allFav ? `Removed ${n} from <b>Favorites</b>` : `Added ${n} to <b>Favorites</b>`); renderSelbar(); } }, "★");
  fill(bar, count, star, input, h("button", { onclick: add }, "Add"),
    S.filter.collection ? h("button", { class: "ghost", onclick: async () => {
      await api("/api/posts/bulk", { method: "POST", body: { ids: ids(), remove_collections: [S.filter.collection] } });
      toast(`Removed ${n} from <b>${esc(S.filter.collection)}</b>`); done(); } }, "Remove from this one") : null,
    boards,
    h("button", { onclick: () => exportDialog({ ids: ids(), name: "Stash selection", count: n }) }, "Export"),
    h("button", { class: "ghost danger", title: "Move to trash (Delete)", onclick: trashSelected }, "Trash"),
    h("span", { class: "grow" }),
    h("button", { onclick: clearSelection }, "Clear"));
}

// ---------------- search that learns ----------------
// what you open, keep and reject after a search teaches it what you meant (stored on this Mac only)
function learnSignal(signal, posts, extra = {}) {
  const q = searchLabel();
  if (!q || !S.aiOn) return;
  const list = (Array.isArray(posts) ? posts : [posts]).filter(Boolean);
  if (!list.length) return;
  if (list.length === 1) {
    const p = list[0];
    api("/api/feedback", { method: "POST", body: { q, signal, post_id: p.id, idx: p.hit?.idx || 0, t: p.hit?.t || 0, ...extra } }).catch(() => {});
  } else api("/api/feedback", { method: "POST", body: { q, signal, post_ids: list.map((p) => p.id), ...extra } }).catch(() => {});
}
function notAMatch(p) {
  learnSignal("notmatch", p);
  const i = S.items.findIndex((x) => x.id === p.id);
  if (i >= 0) { S.items.splice(i, 1); if (S.total) S.total--; relayoutInPlace([p.id]); renderScope(); }
  toast("Got it. Searches like this will leave it out and lean away from shots like it.");
}

// ---------------- viewer ----------------
const V = { video: null };
async function openViewer(i) {
  if (i < 0 || i >= S.items.length) return;
  const wasOpen = !$("#viewer").hidden;
  S.viewerIndex = i; S.inPt = S.outPt = null;
  const hit = S.items[i].hit;
  S.slide = hit ? hit.idx : 0;
  S.seekTo = hit && hit.t >= 0.5 ? hit.t : null;
  $("#viewer").hidden = false; document.body.style.overflow = "hidden";
  const id = S.items[i].id;
  wasOpen ? navReplace() : navCommit();   // stepping through posts updates this spot; opening one is a new step
  S.post = await api(`/api/posts/${id}`);
  if (S.items[S.viewerIndex]?.id !== id) return;
  renderStage(); renderInfo();
  if (i >= S.items.length - 6) loadMore();
}
// closing it yourself (Esc, Close) is like going back; closing it to jump somewhere else isn't
function userCloseViewer() { closeViewer(); navClosed(); }
function closeViewer() {
  $("#viewer").hidden = true; document.body.style.overflow = "";
  if (V.video) V.video.pause();
  fill($("#frame"), ); S.post = null;
}
function currentMedia() { return S.post?.media[S.slide]; }
function renderStage() {
  V.preview = false;
  const m = currentMedia();
  const frame = $("#frame");
  V.video = null;
  if (!m) { fill(frame, h("p", { class: "hint" }, "Media hasn't downloaded yet. It'll appear after the next sync.")); $("#deck").hidden = true; fill($("#slides"), ); return; }
  if (m.kind === "video") {
    const v = h("video", { src: m.src, poster: m.poster_src || m.thumb_src, playsinline: true, loop: true, preload: "auto" });
    v.muted = !V.sound; v.defaultMuted = !V.sound;
    v.addEventListener("click", togglePlay);
    v.addEventListener("timeupdate", updateDeck);
    v.addEventListener("loadedmetadata", updateDeck);
    if (S.seekTo != null) { const t = S.seekTo; S.seekTo = null; v.addEventListener("loadedmetadata", () => { v.currentTime = t; }, { once: true }); }
    v.addEventListener("play", updateDeck); v.addEventListener("pause", updateDeck);
    fill(frame, v); V.video = v;
    v.play().catch(() => {});
    $("#deck").hidden = false;
  } else {
    fill(frame, h("img", { src: m.src, alt: S.post.caption?.slice(0, 120) || "" }));
    $("#deck").hidden = true;
  }
  const slides = S.post.media.length > 1 ? S.post.media.map((mm, j) =>
    h("button", { class: j === S.slide ? "on" : "", onclick: () => { S.slide = j; S.inPt = S.outPt = null; renderStage(); renderInfo(); }, "aria-label": `Item ${j + 1}` },
      h("img", { src: mm.thumb_src, alt: "" }))) : [];
  fill($("#slides"), ...slides);
  updateDeck();
}
function updateDeck() {
  const v = V.video; if (!v) return;
  const d = v.duration || currentMedia()?.duration || 1;
  const pct = (t) => `${(t / d) * 100}%`;
  $("#head").style.left = pct(v.currentTime);
  $("#played").style.width = pct(v.currentTime);
  $("#tc").textContent = fmtTime(v.currentTime);
  $("#play").textContent = v.paused ? "Play" : "Pause";
  $("#mute").textContent = v.muted ? "Unmute" : "Mute";
  if (V.preview && !v.paused) {
    const a = S.inPt ?? 0, b = S.outPt ?? v.duration;
    if (v.currentTime >= b - 0.03 || v.currentTime < a - 0.1) v.currentTime = a;
  }
  const pv = $("#preview");
  pv.disabled = S.inPt == null && S.outPt == null; pv.textContent = V.preview ? "Stop preview" : "Preview";
  pv.classList.toggle("on", !!V.preview);
  const mi = $("#markIn"), mo = $("#markOut"), r = $("#range");
  mi.classList.toggle("on", S.inPt != null); mo.classList.toggle("on", S.outPt != null);
  if (S.inPt != null) mi.style.left = pct(S.inPt);
  if (S.outPt != null) mo.style.left = `calc(${pct(S.outPt)} - 2px)`;
  const a = S.inPt ?? 0, b = S.outPt ?? d;
  r.style.display = S.inPt != null || S.outPt != null ? "block" : "none";
  r.style.left = pct(a); r.style.width = pct(Math.max(0, b - a));
  $("#inTc").textContent = S.inPt != null ? fmtTime(S.inPt) : "–";
  $("#outTc").textContent = S.outPt != null ? fmtTime(S.outPt) : "–";
  const range = $("#clipRange");
  if (range) range.textContent = `${fmtTime(a)} → ${fmtTime(b)} / ${(b - a).toFixed(1)}S`;
}
// videos open muted; once you unmute, sound stays on as you move between posts (until Stash is closed)
function toggleMute() {
  V.sound = !V.sound;
  if (V.video) { V.video.muted = !V.sound; if (V.sound && V.video.paused) V.video.play().catch(() => {}); }
  updateDeck();
}
$("#mute").onclick = toggleMute;
function togglePlay() { const v = V.video; if (!v) return; v.paused ? v.play() : v.pause(); }
function step(dir) { const v = V.video; if (!v) return; v.pause(); v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + dir / 30)); }
function setIn() { if (!V.video) return; S.inPt = V.video.currentTime; if (S.outPt != null && S.outPt <= S.inPt) S.outPt = null; updateDeck(); }
function setOut() { if (!V.video) return; S.outPt = V.video.currentTime; if (S.inPt != null && S.inPt >= S.outPt) S.inPt = null; updateDeck(); }
// the buttons: click to set, click again to clear
function toggleIn() { if (S.inPt != null) { S.inPt = null; V.preview = false; updateDeck(); } else setIn(); }
function toggleOut() { if (S.outPt != null) { S.outPt = null; V.preview = false; updateDeck(); } else setOut(); }
// play just the marked section, on a loop
function togglePreview() {
  const v = V.video; if (!v || (S.inPt == null && S.outPt == null)) return;
  V.preview = !V.preview;
  if (V.preview) { v.currentTime = S.inPt ?? 0; v.play().catch(() => {}); }
  updateDeck();
}
(() => {
  const scrub = $("#scrub");
  const seek = (e) => {
    const v = V.video; if (!v) return;
    const r = scrub.getBoundingClientRect();
    v.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (v.duration || 0);
  };
  // hold still while you drag, then carry on playing from where you let go (if it was playing)
  let resume = false;
  scrub.addEventListener("pointerdown", (e) => { scrub.setPointerCapture(e.pointerId); resume = !!V.video && !V.video.paused; V.video?.pause(); seek(e); scrub.onpointermove = seek; });
  const up = () => { scrub.onpointermove = null; if (resume && V.video) V.video.play().catch(() => {}); resume = false; };
  scrub.addEventListener("pointerup", up); scrub.addEventListener("pointercancel", up);
})();
$("#play").onclick = togglePlay;
$("#stepBack").onclick = () => step(-1);
$("#stepFwd").onclick = () => step(1);
$("#setIn").onclick = toggleIn;
$("#setOut").onclick = toggleOut;
$("#preview").onclick = togglePreview;
(function previewTick() {   // keep the preview tight to the marks (timeupdate alone overshoots)
  const v = V.video;
  if (V.preview && v && !v.paused) {
    const a = S.inPt ?? 0, b = S.outPt ?? v.duration;
    if (v.currentTime >= b - 0.02 || v.currentTime < a - 0.1) v.currentTime = a;
  }
  requestAnimationFrame(previewTick);
})();
$("#grab").onclick = grabFrame;

async function downloadUrl(url) {
  if (DESKTOP) {
    try { savedToast(await api("/api/desktop/save", { method: "POST", body: { url } })); }
    catch (e) { toast(esc(e.message), { error: true }); }
    return;
  }
  const a = h("a", { href: url, download: "" }); document.body.append(a); a.click(); a.remove();
}
async function runTool(label, action, body, btn) {
  const m = currentMedia();
  if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = label; }
  try {
    const r = await api(`/api/media/${m.id}/${action}`, { method: "POST", body });
    downloadUrl(r.url);
    if (!DESKTOP) toast(`Saved <b>${esc(r.name)}</b> (${(r.size / 1048576).toFixed(1)} MB)`);
  } catch (e) { toast(esc(e.message), { error: true }); }
  finally { if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label; } }
}
function grabFrame() { if (V.video) runTool("Grabbing…", "still", { t: V.video.currentTime }, $("#grab")); }

function lumHex(hx) {
  const n = parseInt(hx.slice(1), 16);
  return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
}
function lookLink(k, v, label) {
  return v ? h("button", { class: "link", title: "Show all like this", onclick: () => { closeViewer(); updateFilter({ [k]: v, similar: null, simimg: null }); } }, label || v) : "–";
}
function renderInfo() {
  const p = S.post, m = currentMedia();
  const info = $("#info");
  const save = async (body) => {
    S.post = await api(`/api/posts/${p.id}`, { method: "PATCH", body });
    const item = S.items[S.viewerIndex];
    if (item && item.id === p.id) Object.assign(item, { favorite: S.post.favorite });
    document.querySelectorAll(`#grid .tile[data-id="${p.id}"]`).forEach((t) => t.classList.toggle("fav", !!S.post.favorite));
    renderInfo(); loadSidebar();
  };
  const chipList = (names, kind) => names.length ? h("div", { class: "chips" }, ...names.map((n) =>
    h("span", { class: "chip" + (kind === "tag" ? " tag" : "") }, kind === "tag" ? "#" + n : n,
      h("button", { "aria-label": `Remove ${n}`, onclick: () => save(kind === "tag" ? { remove_tags: [n] } : { remove_collections: [n] }) }, "×")))) : null;
  const adder = (placeholder, kind) => {
    const inp = h("input", { class: "add-input", placeholder, list: kind === "col" ? "collectionList" : null });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && inp.value.trim()) {
        const vals = inp.value.split(",").map((x) => x.trim().replace(/^#/, "")).filter(Boolean);
        save(kind === "tag" ? { add_tags: vals } : { add_collections: vals });
      }
      e.stopPropagation();
    });
    return inp;
  };
  const notes = h("textarea", { rows: 4, placeholder: "Why you saved it, where you'd use it" }, p.notes || "");
  notes.addEventListener("keydown", (e) => e.stopPropagation());
  notes.addEventListener("change", () => save({ notes: notes.value }));

  const cap = h("p", { class: "caption" }, p.caption || "No caption.");
  const capMore = (p.caption || "").length > 360 ? h("button", { class: "more", onclick: () => { cap.classList.toggle("open"); capMore.textContent = cap.classList.contains("open") ? "Show less" : "Show more"; } }, "Show more") : null;

  const isVideo = m?.kind === "video";
  const spec = (k, v) => [h("div", { class: "k" }, k), h("div", { class: "v" }, v)];
  const sections = [];
  if (m) {
    sections.push(h("section", {},
      h("div", { class: "cells" },
        h("button", { class: "primary", onclick: () => downloadUrl(`/api/media/${m.id}/download`) }, isVideo ? "Download video" : "Download image"),
        p.media.length > 1 ? h("button", { onclick: () => downloadUrl(`/api/posts/${p.id}/zip`) }, `All ${p.media.length} as zip`) : null)));
  }
  if (isVideo) {
    const fmt = pick({ value: "640", options: [480, 640, 960, 1280].map((w) => ({ value: String(w), label: w + " wide" })) });
    const fps = pick({ value: "15", options: [12, 15, 24].map((f) => ({ value: String(f), label: f + " fps" })) });
    const range = () => ({ start: S.inPt ?? 0, end: S.outPt ?? (V.video?.duration || m.duration) });
    sections.push(h("section", {},
      h("h4", {}, "Clip", h("span", { class: "tc", id: "clipRange" })),
      h("div", { class: "cells" },
        h("button", { onclick: (e) => runTool("Exporting…", "clip", { ...range(), format: "mp4" }, e.currentTarget) }, "Export MP4"),
        h("button", { onclick: (e) => runTool("Making GIF…", "clip", { ...range(), format: "gif", width: Number(fmt.value), fps: Number(fps.value) }, e.currentTarget) }, "Export GIF")),
      h("div", { class: "opts" }, h("span", { class: "lbl" }, "GIF"), fmt, fps),
      h("p", { class: "hint pad", style: "margin:0;border-top:1px solid var(--rule-soft)" }, "Mark a section with I and O, or export the whole video.")));
    sections.push(h("section", {},
      h("h4", {}, "Stills"),
      h("div", { class: "cells" },
        h("button", { onclick: grabFrame }, "Grab frame"),
        h("button", { onclick: (e) => runTool("Finding shots…", "shots", {}, e.currentTarget) }, "Every shot")),
      h("p", { class: "hint pad", style: "margin:0;border-top:1px solid var(--rule-soft)" }, "Every shot saves one full-resolution PNG per cut. Step frames with , and .")));
  }
  const total = S.total || S.items.length;
  fill(info,
    h("div", { class: "head-row" },
      navPair(),
      h("span", { class: "pos" }, `${pad(S.viewerIndex + 1)} / ${pad(total)}`),
      h("button", { class: "fav-btn" + (p.favorite ? " on" : ""), title: "Favorite (F)", "aria-pressed": p.favorite ? "true" : "false", onclick: () => save({ favorite: !p.favorite }) }, p.favorite ? "★ Favorite" : "☆ Favorite")),
    h("div", { class: "spec" },
      ...spec("Creator", p.username ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); closeViewer(); setFilter({ creator: p.username }, false); } }, "@" + p.username) : "–"),
      ...spec("Saved", fmtDay(p.saved_at)),
      ...spec("Posted", fmtDay(p.posted_at)),
      ...spec("Format", m ? `${m.kind === "video" ? "Video" : "Image"} ${m.width || "?"}×${m.height || "?"}${isVideo ? " " + fmtTime(m.duration) : ""}`.toUpperCase() : "Not downloaded"),
      ...(isVideo ? spec("Quality", (() => {
        const mbps = m.bitrate ? (m.bitrate / 1e6).toFixed(1) + " Mbps" : "";
        const state = m.hq === 1 ? "Best available" : m.hq === -1 ? "Best Instagram has" : "Checking for a sharper copy";
        const btn = h("button", { class: "link", style: "border:0;background:none;padding:0;font:inherit;text-decoration:underline;cursor:pointer;color:inherit",
          title: "Fetch this video from Instagram again and keep whichever copy is sharper",
          onclick: async () => { await api(`/api/posts/${p.id}/redownload`, { method: "POST" }); toast("Re-downloading at the best quality Instagram has. It swaps in when it's done."); } }, "Re-download");
        return h("span", {}, [mbps, state].filter(Boolean).join(" / ").toUpperCase(), " / ", btn);
      })()) : []),
      ...(p.media.length > 1 ? spec("Item", `${pad(S.slide + 1, 2)} / ${pad(p.media.length, 2)}`) : []),
      ...spec("Source", h("a", { href: p.url, onclick: (e) => { e.preventDefault(); openExternal(p.url); } }, "Instagram ↗")),
      ...(m && m.content !== undefined && (m.content || S.aiOn) ? spec("Content", (() => {
        const sel = pick({ plain: true, value: m.content || "", placeholder: "Not tagged yet", title: m.content_fixed ? "Set by you" : "Tagged by AI. Change it if it's wrong.",
          options: CONTENT.filter(([v]) => v !== "notfilm").map(([v, l]) => ({ value: v, label: l + (m.content === v && !m.content_fixed ? " (AI)" : "") })) });
        sel.onchange = async () => { await api(`/api/media/${m.id}/content`, { method: "POST", body: { content: sel.value } }); toast("Label updated"); S.post = await api(`/api/posts/${p.id}`); renderInfo(); };
        return sel;
      })()) : []),
      ...(m && m.tone ? [
        ...spec("Aspect", lookLink("ar", m.ar_label, m.ar_label)),
        ...spec("Look", h("span", {}, lookLink("tone", m.tone, LOOK.tone[1].find((o) => o[0] === m.tone)?.[1]), " / ",
          lookLink("sat", m.sat, LOOK.sat[1].find((o) => o[0] === m.sat)?.[1]), " / ",
          lookLink("temp", m.temp, LOOK.temp[1].find((o) => o[0] === m.temp)?.[1]), " / ",
          lookLink("contrast", m.contrast, (LOOK.contrast[1].find((o) => o[0] === m.contrast)?.[1] || "") + " contrast"))),
      ] : [])),
    m && m.palette ? h("div", { class: "palette" }, ...m.palette.map(([hex, share]) =>
      h("button", { style: `background:${hex};flex:${share} 1 0;color:${lumHex(hex) > 0.55 ? "#000" : "#fff"}`, title: `Find frames featuring ${hex}`,
        onclick: () => { closeViewer(); updateFilter({ color: hex, similar: null, simimg: null }); } }, h("span", {}, hex)))) : null,
    h("div", { class: "cells" },
      h("button", { onclick: () => { const id = p.id; closeViewer(); S.queryImg = null; updateFilter({ similar: String(id), simimg: null }); } }, "Similar shots"),
      m && m.palette ? h("button", { onclick: () => { closeViewer(); updateFilter({ color: m.palette.find(([hx]) => { const l = lumHex(hx); return l > 0.12 && l < 0.92; })?.[0] || m.palette[0][0] }); } }, "Same color") : null),
    cap, capMore,
    ...sections,
    m && (S.aiOn || m.techniques?.length) ? techSection(p, m) : null,
    searchActive() && S.aiOn && S.items[S.viewerIndex]?.id === p.id ? h("div", { class: "cells" },
      h("button", { onclick: () => { const it = S.items[S.viewerIndex]; closeViewer(); notAMatch(it); } }, `Not a match for “${searchLabel()}”`)) : null,
    h("section", {}, h("h4", {}, "Collections"), chipList(p.collections, "col"), adder("Add to collection", "col")),
    h("section", {}, h("h4", {}, "Tags"), chipList(p.tags || [], "tag"), adder("Add tags, comma separated", "tag")),
    h("section", {}, h("h4", {}, "Notes"), notes),
    h("section", { class: "trash-row" }, p.deleted_at
      ? h("button", { onclick: async () => { await api("/api/posts/trash", { method: "POST", body: { ids: [p.id], restore: true } }); toast("Restored"); closeViewer(); loadSidebar(); reload(); } }, "Restore from trash")
      : h("button", { class: "danger", onclick: async () => {
          await api("/api/posts/trash", { method: "POST", body: { ids: [p.id] } });
          toast("Moved to trash", { action: { label: "Undo", fn: async () => { await api("/api/posts/trash", { method: "POST", body: { ids: [p.id], restore: true } }); loadSidebar(); reload(); } } });
          closeViewer(); loadSidebar(); reload(); } }, "Move to trash")),
    h("div", { class: "grow" }),
    h("div", { class: "navbar" },
      h("button", { onclick: () => openViewer(S.viewerIndex - 1), disabled: S.viewerIndex <= 0 }, "Previous"),
      h("button", { onclick: userCloseViewer }, "Close"),
      h("button", { onclick: () => openViewer(S.viewerIndex + 1), disabled: S.viewerIndex >= total - 1 }, "Next")),
  );
  updateDeck();
}

function techSection(p, m) {
  const tags = m.techniques || [];
  const refresh = async () => { S.post = await api(`/api/posts/${p.id}`); renderInfo(); loadSidebar(); };
  const set = async (tag, on) => {
    await api(`/api/media/${m.id}/technique`, { method: "POST", body: { tag, on } });
    toast(on ? "Added. Shots that look like this one will pick it up too." : "Removed. Stash won't tag this one again, and leans away from it for look-alikes.");
    refresh();
  };
  const add = pick({ placeholder: "+ Add technique", value: "", cls: "boxed",
    options: (S.tech?.tags || []).filter((t) => !tags.some((x) => x.tag === t.tag)).map((t) => ({ value: t.tag, label: t.label, group: t.group })) });
  add.setAttribute("aria-label", "Add a technique");
  add.onchange = () => { if (add.value) set(add.value, true); };
  const how = { ai: "Tagged by AI", motion: "Measured from the camera movement", learned: "Learned from shots you tagged", user: "Added by you" };
  return h("section", {}, h("h4", {}, "Technique"),
    h("div", { class: "chips" }, ...tags.map((t) => h("span", { class: "chip tech " + t.source, title: how[t.source] || "" },
      h("button", { class: "link", onclick: () => { closeViewer(); updateFilter({ tech: t.tag }); } }, t.label + (t.source === "user" ? "" : t.source === "learned" ? " ·" : "")),
      h("button", { class: "x", title: "Not this: remove", "aria-label": "Remove " + t.label, onclick: () => set(t.tag, false) }, "×"))),
      tags.length ? null : h("span", { class: "hint" }, S.aiOn ? "Nothing tagged yet." : "")),
    h("div", { class: "opts" }, add));
}

// ---------------- settings / sync drawer ----------------
async function openDrawer(focus) {
  $("#drawer").hidden = false; $("#scrim").hidden = false;
  S.settings = await api("/api/settings");
  renderDrawer(focus);
  pollJobs(true);
}
function closeDrawer() { $("#drawer").hidden = true; $("#scrim").hidden = true; }
$("#closeDrawer").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
$("#syncPill").onclick = () => openDrawer();

function renderDrawer(focus) {
  const s = S.settings, c = S.side?.counts || {};
  const body = $("#drawerBody");
  const saveSettings = async (patch, quiet) => {
    S.settings = { ...S.settings, ...(await api("/api/settings", { method: "PUT", body: patch })) };
    if (!quiet) toast("Settings saved");
  };

  // --- status + run
  const runBtn = h("button", { class: "primary", onclick: () => startJobs(["sync", "backfill"]) }, "Sync now");
  const statusSec = h("section", {},
    h("h3", {}, "Status"),
    h("div", { class: "stat-line" }, `${(c.ready || 0).toLocaleString()} posts ready`, c.pending ? `, ${c.pending.toLocaleString()} waiting for media` : "", c.gone ? `, ${c.gone} no longer available` : ""),
    h("div", { class: "row" }, runBtn,
      h("button", { onclick: () => startJobs(["sync"]) }, "Check for new saves only"),
      c.pending ? h("button", { onclick: () => startJobs(["backfill"]) }, "Fetch waiting media") : null,
      null,
      h("button", { class: "danger", onclick: () => api("/api/jobs", { method: "POST", body: { action: "cancel" } }).then(() => toast("Stopping after the current post")) }, "Stop")),
    c.lowq ? h("p", { class: "hint", style: "margin-top:10px" },
      `${c.lowq.toLocaleString()} video posts are lined up to be re-checked for a sharper copy. Stash does this on its own in the background, taking turns with syncing. Nothing to click.`) : null,
    h("div", { class: "jobs", id: "jobList", style: "margin-top:14px" }));
  const verLine = h("div", { class: "stat-line", style: "margin-top:14px" }, "Stash " + (UPD?.current || ""));
  const verBtn = h("button", { onclick: async () => {
    verBtn.disabled = true; const u = await checkUpdate(true); verBtn.disabled = false;
    if (!u) return toast("Couldn't check for updates", { error: true });
    if (u.error) return toast(esc(u.error), { error: true });
    if (u.available && u.can_install) updateDialog();
    else if (u.available) toast(`Stash ${esc(u.latest)} is out. Download it from the Releases page.`, { action: { label: "Open", fn: () => openExternal(u.url) } });
    else toast(`You're on the latest version (${esc(u.current)})`);
    verLine.textContent = "Stash " + u.current;
  } }, "Check for updates");
  statusSec.append(verLine, h("div", { class: "row" }, verBtn));
  if (!UPD) checkUpdate().then((u) => { if (u) verLine.textContent = "Stash " + u.current; });
  for (const f of s._catching_up || []) statusSec.append(h("p", { class: "hint" },
    `${f.name}: catching up on older saves. ${f.seen ? f.seen.toLocaleString() + " read so far. " : ""}It keeps going in the background from where it left off, even after Stash is closed and reopened.`));
  if (s._signed_in === false) statusSec.append(h("p", { style: "color:var(--danger)" },
    "Instagram isn't seeing you as signed in, so it's handing out smaller, softer videos. Open instagram.com in the browser chosen below, make sure you're logged in, and if your Mac asks about “Chrome Safe Storage”, choose Always Allow."));
  if (!s._gallery_dl) statusSec.append(h("p", { style: "color:var(--danger)" }, "The sync engine is missing from this copy of Stash. Download the latest version and replace the app."));

  // --- import
  const fileInput = h("input", { type: "file", accept: ".zip,.json", multiple: true, hidden: true });
  const drop = h("div", { class: "drop", tabindex: "0", onclick: () => fileInput.click() }, "Drop the export .zip here (all parts if there are several), or click to choose");
  const doImport = async (files) => {
    files = [...files];
    const form = (extra) => { const fd = new FormData(); for (const f of files) fd.append("file", f); for (const [k, v] of Object.entries(extra)) fd.append(k, v); return fd; };
    drop.textContent = "Reading export…";
    let pv;
    try { pv = await api("/api/import", { method: "POST", body: form({ preview: "1" }) }); }
    catch (e) { drop.textContent = e.message; return; }
    drop.textContent = "Drop the export .zip here (all parts if there are several), or click to choose";
    // pick which collections come in: nothing is ticked until you choose
    const boxes = pv.collections.map((c) => { const i = h("input", { type: "checkbox", value: c.name }); return [i, h("label", { class: "check" }, i, `${c.name}  `, h("span", { class: "hint", style: "display:inline" }, pad(c.count)))]; });
    const unc = h("input", { type: "checkbox" });
    const go = h("button", { class: "primary", disabled: true }, "Import");
    const sync_ = () => { go.disabled = !boxes.some(([i]) => i.checked) && !unc.checked; };
    boxes.forEach(([i]) => { i.onchange = sync_; }); unc.onchange = sync_;
    if (!pv.collections.length) {
      return modal({
        title: "No collections found in this export",
        text: pv.collections_file
          ? `Stash found your ${pv.posts_found.toLocaleString()} saves, but couldn't read the collection names from saved_collections.json. Nothing was imported.`
          : `Stash found your ${pv.posts_found.toLocaleString()} saves, but not the file that lists your collections (saved_collections.json). Nothing was imported.`,
        body: h("p", { class: "hint" }, pv.collections_file
          ? "Instagram may have changed its export format again. Send that one file to Claude to get it fixed. It only holds your collection names and post links."
          : "Big exports often arrive as several .zip files (part 1, part 2…). Drop all of them in at once, and Stash will find the collections file in whichever part has it."),
        actions: [{ label: "OK", primary: true, fn: (c) => c() }],
      });
    }
    const close = modal({
      title: "Which collections should come in?",
      text: `This export has ${pv.posts_found.toLocaleString()} saves. Only the collections you tick are added. Nothing else in the export is touched.`,
      body: h("div", { class: "pick-cols" }, ...boxes.map(([, l]) => l),
        pv.uncollected ? h("label", { class: "check", style: "margin-top:10px" }, unc, `Saves that aren't in any collection  `, h("span", { class: "hint", style: "display:inline" }, pad(pv.uncollected))) : null),
      actions: [{ label: "Cancel", fn: (c) => c() }],
    });
    $(".modal .row").append(go);
    go.onclick = async () => {
      const picked = boxes.filter(([i]) => i.checked).map(([i]) => i.value);
      close(); drop.textContent = "Importing…";
      try {
        const r = await api("/api/import", { method: "POST", body: form({ collections: JSON.stringify(picked), uncollected: unc.checked ? "1" : "0" }) });
        drop.textContent = `Added ${r.new_posts.toLocaleString()} saves from ${picked.length ? picked.join(", ") : "your uncollected saves"}` +
          (r.dated ? `. Real save dates applied to ${r.dated.toLocaleString()} posts.` : ".");
        toast(`Imported ${r.new_posts.toLocaleString()} saves. Run Sync now to download their media.`);
        loadSidebar(); reload();
      } catch (e) { drop.textContent = e.message; }
    };
  };
  fileInput.onchange = () => doImport(fileInput.files);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); doImport(e.dataTransfer.files); });
  const importSec = h("section", { id: "sec-import" },
    h("h3", {}, h("span", { class: "step" }, "01"), "Import your backlog"),
    h("p", {}, "Instagram's data export lists every save and which collection it's in. Request it once, drop it here, and sync fills in the photos and videos."),
    h("details", { open: focus === "import" ? true : null },
      h("summary", {}, "How to get the export"),
      h("ol", { class: "steps" },
        h("li", {}, "Instagram app or web: Settings, then Accounts Center"),
        h("li", {}, "Your information and permissions, then Download your information"),
        h("li", {}, "Choose Some of your information and tick Saved"),
        h("li", {}, "Set the format to JSON (not HTML) and media quality to anything"),
        h("li", {}, "Instagram emails you a link, usually within a day"))),
    drop, fileInput);

  // --- account + collections
  const user = h("input", { value: s.instagram_username || "", placeholder: "yourhandle" });
  const browser = pick({ cls: "boxed", value: s.cookies_source || "chrome", options: ["chrome", "safari", "firefox", "brave", "edge", "vivaldi", "zen", "file"].map((b) =>
    ({ value: b, label: b === "file" ? "A cookies.txt file" : b[0].toUpperCase() + b.slice(1) })) });
  const cfile = h("input", { value: s.cookies_file || "", placeholder: "/path/to/cookies.txt" });
  const cfileField = h("label", { class: "field", hidden: s.cookies_source !== "file" }, "cookies.txt path", cfile);
  browser.onchange = () => { cfileField.hidden = browser.value !== "file"; };
  const allSaved = h("input", { type: "checkbox", checked: !!s.include_uncollected });
  const colList = h("div", { class: "col-list" });
  const colRows = [];
  const addColRow = (c = { name: "", url: "" }) => {
    const name = h("input", { value: c.name, placeholder: "Name" });
    const url = h("input", { value: c.url, placeholder: "https://www.instagram.com/you/saved/name/1784…/" });
    url.addEventListener("change", () => {
      const m = url.value.match(/\/saved\/([^/]+)\/\d+/);
      if (m && !name.value) name.value = decodeURIComponent(m[1]).replace(/-/g, " ").replace(/\b\w/g, (x) => x.toUpperCase());
    });
    const row = h("div", { class: "col-row" }, name, url, h("button", { class: "ghost", "aria-label": "Remove collection", onclick: () => { row.remove(); colRows.splice(colRows.indexOf(r), 1); } }, "×"));
    const r = { name, url };
    colRows.push(r); colList.append(row);
  };
  (s.collections || []).forEach(addColRow);
  if (!colRows.length) addColRow();
  const auto = pick({ cls: "boxed", value: String(Number(s.auto_sync_hours) || 0), options: [[0, "Off"], [3, "Every 3 hours"], [6, "Every 6 hours"], [12, "Twice a day"], [24, "Once a day"]].map(([v, l]) =>
    ({ value: String(v), label: l })) });
  const perRun = h("input", { value: s.backfill_per_run, inputmode: "numeric" });
  const d0 = h("input", { value: s.request_delay?.[0] ?? 6, inputmode: "numeric" });
  const d1 = h("input", { value: s.request_delay?.[1] ?? 12, inputmode: "numeric" });
  const syncSec = h("section", { id: "sec-sync" },
    h("h3", {}, h("span", { class: "step" }, "02"), "Connect sync"),
    h("p", {}, "Sync reads your saves using the Instagram login already in your browser. Nothing is posted and no password is stored here. Requests are slow on purpose, so a big first sync takes a while."),
    h("p", { class: "hint" }, "The first time, your Mac asks if Stash may use “Chrome Safe Storage” (or your browser's equivalent). Choose Always Allow, that's how Stash reads your Instagram login. For Safari, turn on Full Disk Access for Stash in System Settings › Privacy & Security."),
    h("div", { class: "two" },
      h("label", { class: "field" }, "Instagram username", user),
      h("label", { class: "field" }, "Browser you're logged in with", browser)),
    cfileField,
    h("label", { class: "check" }, allSaved, "Also bring in saves that aren't in a collection (your whole All posts feed and the rest of the data export). Off = only your collections."),
    h("h3", { class: "sub" }, "Collections to sync"),
    h("p", {}, "Open each collection on instagram.com in your browser and paste its address. Linked collections download in one pass, which is much faster than fetching posts one by one."),
    colList,
    h("button", { onclick: () => addColRow() }, "Add another collection"),
    h("div", { class: "two", style: "margin-top:14px" },
      h("label", { class: "field" }, "Sync automatically", auto),
      h("label", { class: "field" }, "Backlog: old saves to download per sync", perRun)),
    h("details", {},
      h("summary", {}, "Pacing"),
      h("p", {}, "Seconds to wait between requests to Instagram. Going faster than the default raises the chance Instagram asks you to verify your account."),
      h("div", { class: "two" }, h("label", { class: "field" }, "At least", d0), h("label", { class: "field" }, "At most", d1))),
    h("div", { class: "row", style: "margin-top:12px" },
      h("button", { class: "primary", onclick: () => saveSettings({
        instagram_username: user.value.trim().replace(/^@/, ""), cookies_source: browser.value, cookies_file: cfile.value.trim(),
        include_uncollected: allSaved.checked, auto_sync_hours: Number(auto.value), backfill_per_run: Number(perRun.value) || 150,
        request_delay: [Number(d0.value) || 6, Math.max(Number(d1.value) || 12, Number(d0.value) || 6)],
        collections: colRows.map((r) => ({ name: r.name.value.trim(), url: r.url.value.trim() })).filter((r) => r.url),
      }) }, "Save sync settings")));

  const themeNow = getTheme();
  const appearSec = h("section", {}, h("h3", {}, "Appearance"),
    h("div", { class: "seg" }, ...[["auto", "Match my Mac"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) =>
      h("button", { class: themeNow === k ? "on" : "", onclick: (e) => { applyTheme(k); e.currentTarget.parentNode.querySelectorAll("button").forEach((b) => b.classList.remove("on")); e.currentTarget.classList.add("on"); } }, l))));

  const gb = (n) => (n / 1e9 >= 10 ? (n / 1e9).toFixed(0) : (n / 1e9).toFixed(2)) + " GB";
  const storBody = h("div", {}, h("div", { class: "stat-line" }, "Measuring…"));
  const storSec = h("section", { id: "sec-storage" }, h("h3", {}, "Storage"),
    h("p", {}, "The database, previews and video stills always stay on this Mac, so browsing is instant. The originals (the big files) can live in Dropbox or on an external drive instead."),
    storBody);
  const moveTo = async (path, label) => {
    const ok = await askConfirm(`Move originals to ${label}?`, {
      text: "Stash moves them into a “Stash Media” folder there. On the same disk (like Dropbox) this takes moments, and Dropbox uploads in the background. Syncing waits until the move is done. If Stash closes partway, it finishes next time.",
      label: "Move originals" });
    if (!ok) return;
    try {
      await api("/api/storage/move", { method: "POST", body: { path } });
      toast(`Moving originals to <b>${esc(label)}</b>`); pollJobs(true); setTimeout(loadStorage, 1500);
    } catch (e) { toast(esc(e.message), { error: true }); }
  };
  const loadStorage = () => api("/api/storage?sizes=1").then((st) => {
    const spec = (k, v) => [h("div", { class: "k" }, k), h("div", { class: "v" }, v)];
    const where = st.candidates.find((c) => st.media_dir.startsWith(c.path.replace(/\/Stash Media$/, "")))?.label || (st.is_default ? "This Mac" : "Custom folder");
    const others = st.candidates.filter((c) => c.path !== st.media_dir);
    const custom = h("input", { placeholder: "/Volumes/Drive/Stash Media", style: "flex:1;min-width:200px" });
    custom.addEventListener("keydown", (e) => e.stopPropagation());
    fill(storBody,
      h("div", { class: "spec", style: "margin:0 -16px 12px;border-top:1px solid var(--rule-soft)" },
        ...spec("Originals", `${gb(st.originals)} / ${where}`),
        ...spec("Folder", h("span", {}, st.media_dir, " ", DESKTOP ? h("button", { class: "link", style: "border:0;padding:0;text-decoration:underline;font:inherit;text-transform:none", onclick: () => api("/api/desktop/reveal", { method: "POST", body: { what: "originals" } }) }, "Show") : null)),
        ...spec("On this Mac", `${gb(st.local)} / database + previews`)),
      st.available ? null : h("p", { style: "color:var(--danger)" }, "That folder isn't reachable right now. Plug the drive in or check Dropbox is running. You can still browse; opening originals and syncing wait until it's back."),
      st.moving_to ? h("p", { style: "color:var(--signal)" }, `Moving to ${st.moving_to}… progress shows under Status.`) : null,
      others.length || !st.is_default ? h("div", { class: "row", style: "margin-bottom:10px" }, h("span", { class: "lbl", style: "margin-right:4px" }, "Move to"),
        ...others.map((c) => h("button", { onclick: () => moveTo(c.path, c.label) }, c.label)),
        st.is_default ? null : h("button", { onclick: () => moveTo(st.default_path, "this Mac") }, "This Mac")) : null,
      h("div", { class: "row" }, custom, h("button", { onclick: () => custom.value.trim() && moveTo(custom.value.trim(), custom.value.trim()) }, "Move to folder")),
      !st.is_default && /dropbox|google|icloud|onedrive/i.test(where)
        ? h("p", { class: "hint", style: "margin-top:12px" }, "To free up space on this Mac: in Finder, right-click the Stash Media folder and choose Make online-only (Dropbox). Stash fetches an original from the cloud when you open it, which can take a few seconds the first time.")
        : null);
  }).catch((e) => fill(storBody, h("p", { class: "hint" }, esc(e.message))));
  loadStorage();

  const aiBody = h("div", {}, h("div", { class: "stat-line" }, "…"));
  const aiSec = h("section", { id: "sec-ai" }, h("h3", {}, "AI search & tags"),
    h("p", {}, "Search for what's in your saves, not just their captions: “flower”, “red neon at night”, “overhead shot of a table”. Stash looks at every image and about one frame a second of every video, and video results open at the moment that matches. It also tags what each save is (film frame, talking to camera, behind the scenes, gear, text) for the Content filter. Everything runs on this Mac; turning it on downloads a 150 MB model once and scans your library in the background."),
    aiBody);
  let aiTimer;
  const loadAI = () => api("/api/ai").then((a) => {
    clearTimeout(aiTimer);
    S.aiOn = a.enabled && a.installed;
    const line = a.downloading ? `Downloading model… ${Math.round(a.progress * 100)}%`
      : a.enabled && a.installed ? `Scanned ${a.scanned.toLocaleString()} of ${a.total.toLocaleString()} images and videos` + (a.scanned < a.total ? " (keeps going in the background, search improves as it goes)" : ". Search looks inside everything.")
      : "Off";
    const learnLine = h("div", { class: "hint" });
    api("/api/techniques").then((t) => {
      const l = t.learned;
      learnLine.textContent = l.signals || l.tags ? `Learning from you: ${l.signals} picks and passes across ${l.searches} searches, ${l.tags} technique tags you set.` : "Search learns from what you open, keep, and mark “Not a match”.";
    }).catch(() => {});
    fill(aiBody,
      h("div", { class: "stat-line" }, line),
      a.enabled && a.installed ? learnLine : null,
      a.enabled && a.installed ? h("button", { class: "ghost", onclick: async () => {
        if (!(await askConfirm("Reset what search has learned?", { text: "Searches go back to the AI on its own. Technique tags you set by hand stay.", label: "Reset", danger: true }))) return;
        await api("/api/feedback", { method: "POST", body: { signal: "reset" } }); toast("Search learning reset"); loadAI();
      } }, "Reset learning") : null,
      a.error ? h("p", { style: "color:var(--danger)" }, a.error) : null,
      a.enabled && (a.installed || a.downloading)
        ? h("button", { onclick: async () => { await api("/api/ai/toggle", { method: "POST", body: { enabled: false } }); loadAI(); } }, "Turn off")
        : h("button", { class: "primary", onclick: async () => { await api("/api/ai/toggle", { method: "POST", body: { enabled: true } }); toast("Downloading the AI model"); loadAI(); } }, "Turn on AI search"));
    if (!$("#drawer").hidden && (a.downloading || (a.enabled && a.installed && a.tagged < a.total))) aiTimer = setTimeout(loadAI, 3000);
  }).catch(() => fill(aiBody));
  loadAI();

  const libInfo = h("div", { class: "stat-line" }, "Checking library…");
  const libSec = h("section", {}, h("h3", {}, "Library"),
    h("p", {}, "Stash backs up your library's database once a day and keeps the last 7. Your photos and videos stay in the folder below; they aren't copied into backups."),
    libInfo,
    h("div", { class: "row" },
      h("button", { onclick: async (e) => { const b = e.currentTarget; b.disabled = true; try { await api("/api/library/backup", { method: "POST", body: {} }); toast("Backup saved"); loadLib(); } catch (er) { toast(esc(er.message), { error: true }); } b.disabled = false; } }, "Back up now"),
      h("button", { onclick: async (e) => {
        const b = e.currentTarget; b.disabled = true; b.innerHTML = '<span class="spinner"></span>Checking…';
        try {
          const r = await api("/api/library/check", { method: "POST", body: {} });
          toast(r.requeued ? `Checked ${r.checked.toLocaleString()} files. ${r.requeued} posts lost files and were queued to download again.` : `Checked ${r.checked.toLocaleString()} files. All good.`);
          loadSidebar(); reload();
        } catch (er) { toast(esc(er.message), { error: true }); }
        b.disabled = false; b.textContent = "Check files";
      } }, "Check files")));
  const loadLib = () => api("/api/library").then((l) => {
    const last = l.backups[0];
    libInfo.textContent = `${(l.size / 1073741824).toFixed(2)} GB of media. ` + (last ? `Last backup ${new Date(last.mtime * 1000).toLocaleString()}.` : "No backup yet.");
    if (!l.ffmpeg) libInfo.append(h("div", { style: "color:var(--danger);margin-top:6px" }, "ffmpeg wasn't found, so thumbnails, stills and clips won't work."));
  }).catch(() => { libInfo.textContent = ""; });
  loadLib();

  const aboutSec = h("section", {},
    h("h3", {}, "Where your files live"),
    h("p", {}, "Your library (database, previews, backups, exports) is in ", h("code", {}, s._data_dir), ". Where the originals live is set under Storage above."),
    DESKTOP ? h("button", { onclick: () => api("/api/desktop/reveal", { method: "POST", body: { what: "library" } }) }, "Show in Finder") : null,
    h("p", { class: "hint", style: "margin-top:14px" }, `Stash ${s._version || ""}`));

  fill(body, statusSec, importSec, syncSec, storSec, aiSec, appearSec, libSec, aboutSec);
  renderJobs();
  if (focus) setTimeout(() => $(`#sec-${focus}`)?.scrollIntoView({ block: "start" }), 50);
}
async function startJobs(kinds) {
  try {
    await api("/api/jobs", { method: "POST", body: { kinds } });
    toast(kinds.includes("sync") ? "Sync started" : "Fetching waiting media");
    pollJobs(true);
  } catch (e) { toast(esc(e.message), { error: true }); }
}
function renderJobs() {
  const list = $("#jobList");
  if (list) fill(list, ...S.jobs.slice(0, 5).map((j) => h("div", { class: "job" },
    h("span", { class: "k" }, { sync: "Sync", backfill: "Backlog", upgrade: "Upgrade", move: "Storage" }[j.kind] || j.kind),
    h("div", {},
      h("span", { class: "s-" + j.status }, j.message || j.status),
      j.status === "running" && j.total ? h("div", { class: "progress" }, h("div", { style: `width:${Math.round((j.done / j.total) * 100)}%` })) : null,
      j.finished ? h("div", { class: "hint", style: "margin:2px 0 0" }, new Date(j.finished * 1000).toLocaleString()) : null))));
  const running = S.jobs.find((j) => j.status === "running" || j.status === "queued");
  const pill = $("#syncPill");
  pill.classList.toggle("live", !!running);
  pill.classList.toggle("error", !running && S.jobs[0]?.status === "error");
  $("#syncText").textContent = running ? (running.kind === "sync" ? "Syncing" : "Fetching") : "Sync";
}
let pollTimer, wasRunning = false;
async function pollJobs(now) {
  clearTimeout(pollTimer);
  try {
    S.jobs = await api("/api/jobs");
    renderJobs();
    const running = S.jobs.some((j) => j.status === "running" || j.status === "queued");
    if (running || wasRunning) loadSidebar();
    if (wasRunning && !running) { reload(); const last = S.jobs[0]; if (last) toast(esc(last.message), { error: last.status === "error" }); }
    wasRunning = running;
    pollTimer = setTimeout(pollJobs, running || !$("#drawer").hidden ? 2500 : 20000);
  } catch { pollTimer = setTimeout(pollJobs, 20000); }
}

// ---------------- global wiring ----------------
let qTimer;
$("#q").addEventListener("keydown", (e) => {
  const v = $("#q").value.trim();
  if (e.key === "Enter" && v) {   // lock this term in as a chip and start the next one
    e.preventDefault(); clearTimeout(qTimer);
    const l = chipList_(); if (!l.some((x) => x.toLowerCase() === v.toLowerCase())) l.push(v);
    setChips(l); delete S.filter.q; $("#q").value = ""; writeHash(); reload();
  } else if (e.key === "Backspace" && !$("#q").value && chipList_().length) {
    e.preventDefault(); const l = chipList_(); l.pop(); setChips(l); writeHash(); reload();
  }
});
$("#q").addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { S.filter.q = $("#q").value.trim(); if (!S.filter.q) delete S.filter.q; writeHash(); reload(); }, 220);
});
$("#sort").onchange = () => { S.sort = $("#sort").value; S.seed = Math.floor(Math.random() * 1e6); S.seeds = []; shuffleUI(); try { localStorage.setItem("sf-sort", S.sort); } catch {} reload(); navCommit(); };
// Shuffle: deal again, or go back to the one before
function shuffleUI() {
  $("#reshuffle").hidden = S.sort !== "random";
  $("#unshuffle").hidden = S.sort !== "random" || !(S.seeds || []).length;
}
$("#reshuffle").onclick = () => { (S.seeds = S.seeds || []).push(S.seed); S.seed = Math.floor(Math.random() * 1e6); shuffleUI(); reload(); window.scrollTo(0, 0); };
$("#unshuffle").onclick = () => { if (!S.seeds?.length) return; S.seed = S.seeds.pop(); shuffleUI(); reload(); window.scrollTo(0, 0); };
shuffleUI();
$("#density").oninput = () => { try { localStorage.setItem("sf-size", $("#density").value); } catch {} layout(true); };
$("#menuBtn").onclick = () => $("#side").classList.toggle("open");
let resizeT; window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => layout(false), 120); });
new IntersectionObserver((e) => { if (e[0].isIntersecting) loadMore(); }, { rootMargin: "1200px" }).observe($("#sentinel"));

document.addEventListener("keydown", (e) => {
  if (!$("#board").hidden || $("#modalRoot").children.length) return;
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  if (!$("#viewer").hidden) {
    if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
    const k = e.key.toLowerCase();
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const multi = S.post?.media.length > 1;
    const keys = {
      escape: userCloseViewer,
      arrowright: () => { if (S.viewerIndex < S.items.length - 1) openViewer(S.viewerIndex + 1); },
      arrowleft: () => { if (S.viewerIndex > 0) openViewer(S.viewerIndex - 1); },
      " ": togglePlay, i: toggleIn, o: toggleOut, p: togglePreview, g: grabFrame, m: toggleMute,
      ",": () => step(-1), ".": () => step(1),
      f: () => S.post && $(".fav-btn")?.click(),
      arrowdown: () => { if (multi) { S.slide = (S.slide + 1) % S.post.media.length; renderStage(); renderInfo(); } },
      arrowup: () => { if (multi) { S.slide = (S.slide - 1 + S.post.media.length) % S.post.media.length; renderStage(); renderInfo(); } },
    };
    // claim every key we use, even at the first/last post: an unclaimed key makes the Mac play its "can't do that" sound
    if (keys[k]) { e.preventDefault(); keys[k](); }
    return;
  }
  if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.key === "/") { e.preventDefault(); $("#q").focus(); }
  else if (e.key.toLowerCase() === "f" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    // F: favorite what's selected, or the tile under the mouse
    const ids = S.selected.size ? [...S.selected] : S.hoverId ? [S.hoverId] : [];
    if (!ids.length) return;
    e.preventDefault();
    const on = !ids.every((id) => S.items.find((x) => x.id === id)?.favorite);
    setFavorite(ids, on).then(() => {
      if (S.selected.size) renderSelbar();
      toast(on ? `Added ${ids.length > 1 ? ids.length + " " : ""}to <b>Favorites</b>` : `Removed ${ids.length > 1 ? ids.length + " " : ""}from <b>Favorites</b>`);
    });
  }
  else if (e.key === "Escape") { if (!$("#drawer").hidden) closeDrawer(); else clearSelection(); }
  else if ((e.key === "Delete" || e.key === "Backspace") && S.selected.size && $("#drawer").hidden) { e.preventDefault(); trashSelected(); }
});
window.addEventListener("hashchange", () => { readHash(); reload(); renderSidebar(); navCommit(); });

// ---------------- back / forward ----------------
// one trail through everything: library views, searches, posts and moodboards. ⌘[ and ⌘] work too.
const NAV = { stack: [], i: -1, restoring: false };
function navNow() {
  return { filter: { ...S.filter }, sort: S.sort, seed: S.seed,
    post: $("#viewer").hidden ? null : (S.items[S.viewerIndex]?.id ?? S.post?.id ?? null),
    board: typeof Board !== "undefined" && !$("#board").hidden ? Board.current() : null };
}
const navKey = (e) => e ? JSON.stringify([e.filter, e.sort, e.sort === "random" ? e.seed : 0, e.post, e.board]) : "";
function navCommit() {
  if (NAV.restoring) return;
  const now = navNow(), cur = NAV.stack[NAV.i];
  if (cur && navKey(cur) === navKey(now)) return;
  NAV.stack = NAV.stack.slice(0, NAV.i + 1);
  NAV.stack.push({ ...now, y: 0 });
  if (NAV.stack.length > 200) NAV.stack.shift();
  NAV.i = NAV.stack.length - 1;
  navButtons();
}
function navReplace() {
  if (NAV.restoring || !NAV.stack[NAV.i]) return navCommit();
  NAV.stack[NAV.i] = { ...navNow(), y: NAV.stack[NAV.i].y };
  navButtons();
}
// closing a post or moodboard: step back if that's where you came from, so Forward can reopen it
function navClosed() {
  if (NAV.restoring) return;
  const prev = NAV.stack[NAV.i - 1];
  if (prev && navKey(prev) === navKey(navNow())) { NAV.i--; navButtons(); }
  else navReplace();
}
window.navCommit = navCommit; window.navClosed = navClosed;
window.addEventListener("scroll", () => {
  if (window.scrollX) window.scrollTo(0, window.scrollY);   // the app never sits shifted sideways
  if (!NAV.restoring && $("#viewer").hidden && $("#board").hidden && NAV.stack[NAV.i]) NAV.stack[NAV.i].y = window.scrollY;
}, { passive: true });
const until = (fn, ms = 8000) => new Promise((res) => { const t0 = Date.now(); (function tick() { if (fn() || Date.now() - t0 > ms) res(); else setTimeout(tick, 40); })(); });
async function navGo(dir) {
  const j = NAV.i + dir, e = NAV.stack[j];
  if (!e || NAV.restoring) return;
  NAV.restoring = true; NAV.i = j; navButtons();
  try {
    if (e.board) { if (Board.current() !== e.board) { if (!$("#viewer").hidden) closeViewer(); await Board.open(e.board); } return; }
    if (!$("#board").hidden) await Board.close(true);
    const same = navKey({ ...e, post: null, board: null }) === navKey({ ...navNow(), post: null, board: null });
    if (!same) {
      if (!$("#viewer").hidden) closeViewer();
      S.filter = { ...e.filter }; S.sort = e.sort; S.seed = e.seed;
      $("#sort").value = e.sort; shuffleUI();
      $("#q").value = S.filter.q || ""; renderChips(); writeHash(); clearSelection(); renderSidebar(); renderFilters?.();
      reload();
      // load far enough down to land where you were
      await until(() => !S.loading && (!S.more || document.documentElement.scrollHeight > e.y + innerHeight));
    }
    if (e.post) {
      let k = S.items.findIndex((x) => x.id === e.post);
      for (let n = 0; k < 0 && S.more && n < 20; n++) { await until(() => !S.loading); await loadMore(); k = S.items.findIndex((x) => x.id === e.post); }
      if (!$("#viewer").hidden && S.post?.id === e.post) return;
      if (k >= 0) await openViewer(k);
    } else {
      if (!$("#viewer").hidden) closeViewer();
      window.scrollTo(0, e.y);
    }
  } finally { NAV.restoring = false; }
}
function navButtons() {
  document.querySelectorAll(".nav-back").forEach((b) => { b.disabled = NAV.i <= 0; });
  document.querySelectorAll(".nav-fwd").forEach((b) => { b.disabled = NAV.i >= NAV.stack.length - 1; });
}
function navPair(cls = "") {
  return h("div", { class: "navpair " + cls },
    h("button", { class: "nav-back", title: "Back (⌘[)", "aria-label": "Back", disabled: NAV.i <= 0, onclick: () => navGo(-1) }, "←"),
    h("button", { class: "nav-fwd", title: "Forward (⌘])", "aria-label": "Forward", disabled: NAV.i >= NAV.stack.length - 1, onclick: () => navGo(1) }, "→"));
}
$("#menuBtn").after(navPair("cell"));
$("#boardClose").before(navPair("cell"));
document.addEventListener("keydown", (e) => {
  if (!(e.metaKey || e.ctrlKey) || $("#modalRoot").children.length) return;
  if (e.key === "[") { e.preventDefault(); e.stopImmediatePropagation(); navGo(-1); }
  else if (e.key === "]") { e.preventDefault(); e.stopImmediatePropagation(); navGo(1); }
}, true);
window.addEventListener("mouseup", (e) => { if (e.button === 3) navGo(-1); else if (e.button === 4) navGo(1); });

const setView = (v) => { S.view = v; try { localStorage.setItem("sf-view", v); } catch {} layout(true); };
$("#viewIndex").onclick = () => setView("index");
$("#viewFrames").onclick = () => setView("frames");
async function searchByImage(file) {
  if (!file || !file.type.startsWith("image/")) return toast("Drop a JPG or PNG image", { error: true });
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api("/api/search-image", { method: "POST", body: fd });
    if (S.queryImg) URL.revokeObjectURL(S.queryImg);
    S.queryImg = URL.createObjectURL(file);
    closeViewer(); updateFilter({ simimg: r.token, similar: null });
  } catch (e) { toast(esc(e.message), { error: true }); }
}
$("#byImage").onclick = () => $("#imageFile").click();
$("#imageFile").onchange = () => { searchByImage($("#imageFile").files[0]); $("#imageFile").value = ""; };
let dragDepth = 0;
const fileDrag = (e) => [...(e.dataTransfer?.types || [])].includes("Files") && $("#drawer").hidden && $("#board").hidden;
window.addEventListener("dragenter", (e) => { if (!fileDrag(e)) return; dragDepth++; $("#dropzone").hidden = false; });
window.addEventListener("dragleave", (e) => { if (!fileDrag(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#dropzone").hidden = true; } });
window.addEventListener("dragover", (e) => { if (fileDrag(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!fileDrag(e)) return;
  e.preventDefault(); dragDepth = 0; $("#dropzone").hidden = true;
  searchByImage(e.dataTransfer.files[0]);
});
$("#themeBtn").onclick = () => applyTheme({ auto: "light", light: "dark", dark: "auto" }[getTheme()]);
applyTheme(getTheme());

// ---------------- updates ----------------
let UPD = null;
async function checkUpdate(force) {
  try { UPD = await api("/api/update" + (force ? "?force=1" : "")); } catch { return null; }
  const btn = $("#updateBtn");
  btn.hidden = !(UPD.available && UPD.can_install);
  btn.textContent = `Update to ${UPD.latest}`;
  return UPD;
}
function updateDialog() {
  if (!UPD) return;
  const mb = UPD.asset?.size ? ` (about ${Math.round(UPD.asset.size / 1048576)} MB)` : "";
  const changes = (UPD.notes.split(/Changes:/)[1] || "").trim().split("\n")[0].slice(0, 300);
  modal({
    title: `Update to Stash ${UPD.latest}`,
    text: `You have ${UPD.current}. Stash downloads the new version${mb}, swaps it in and reopens. Your library, collections and moodboards stay exactly as they are.`,
    body: h("div", {},
      changes ? h("p", { class: "hint" }, "What's new: " + changes) : null,
      h("p", { class: "hint" }, "If your Mac then asks about “Chrome Safe Storage”, choose Always Allow. That's how sync keeps reading your Instagram sign-in.")),
    actions: [{ label: "Later", fn: (c) => c() }, { label: "Update now", primary: true, fn: (c) => { c(); installUpdate(); } }],
  });
}
async function installUpdate() {
  const t = toast('<span class="spinner"></span>Downloading update', { ms: 1e9 });
  const say = (m) => { t.querySelector("span").innerHTML = m; };
  try { await api("/api/update", { method: "POST" }); } catch (e) { t.remove(); return toast(esc(e.message), { error: true }); }
  const poll = async () => {
    let r; try { r = await api("/api/update"); } catch { say("Restarting Stash…"); return; }   // the app has quit to swap itself
    const s = r.state;
    if (s.status === "error") { t.remove(); return toast("Update failed: " + esc(s.error), { error: true }); }
    if (s.status === "manual") { t.remove(); return modal({ title: "Almost there", text: s.message, actions: [{ label: "OK", primary: true, fn: (c) => c() }] }); }
    say('<span class="spinner"></span>' + esc(s.status === "restarting" ? "Restarting Stash…" : s.status === "installing" ? "Installing update" : s.message || "Downloading update"));
    setTimeout(poll, 800);
  };
  poll();
}
$("#updateBtn").onclick = updateDialog;

// boot
try {
  const so = localStorage.getItem("sf-sort"); if (so) { S.sort = so; $("#sort").value = so; shuffleUI(); }
  const sz = localStorage.getItem("sf-size"), old = localStorage.getItem("sf-density");
  if (sz) $("#density").value = sz; else if (old) $("#density").value = 11 - Number(old);   // flip the old setting
  else $("#density").value = 6;
  const vw = localStorage.getItem("sf-view"); if (vw === "frames" || vw === "index") S.view = vw;
} catch {}
readHash();
navCommit();
if (S.filter.simimg) { delete S.filter.simimg; writeHash(); }
loadSidebar().then(() => { reload(); });
pollJobs();
if (DESKTOP) {
  checkUpdate().then((u) => { if (u?.available && u.can_install) toast(`Stash ${esc(u.latest)} is available`, { action: { label: "Update", fn: updateDialog } }); });
  setInterval(() => checkUpdate(), 3 * 3600 * 1000);
}
