// Walk Journal: phone client. Plain JS, no framework.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

const store = {
  get(k, d = null) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { localStorage.setItem(k, JSON.stringify(v)); },
  del(k) { localStorage.removeItem(k); },
};

// No accounts: on a public (cloud) Base Camp, this random id keeps each phone's journal private to it.
// A laptop Base Camp ignores it. It never leaves this site.
const USER = (() => {
  let id = store.get("wj-user");
  if (!id) {
    id = (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/[^A-Za-z0-9]/g, "");
    try { store.set("wj-user", id); } catch {}
  }
  return id;
})();
const withUser = (path) => `${path}${path.includes("?") ? "&" : "?"}u=${encodeURIComponent(USER)}`;

async function api(path, opts = {}) {
  const init = { ...opts, headers: { "X-WJ-User": USER } };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch {
    setOnline(false);
    throw Object.assign(new Error("You're offline. It's saved and will sync later."), { network: true });
  }
  // The tunnel answers 502/530 when Base Camp itself is unreachable: treat like offline.
  if ([502, 504, 530].includes(r.status)) {
    setOnline(false);
    throw Object.assign(new Error("Base Camp is unreachable. It's saved and will sync later."), { network: true });
  }
  setOnline(true);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw Object.assign(new Error(msg), { status: r.status });  // 429 = a daily limit of the public beta
  }
  return r.json();
}

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- views ----------

const views = {};
function show(name, msg) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `v-${name}`));
  document.body.dataset.view = name;
  const tab = { home: "home", suggest: "home", intro: "home", summary: "home", finish: "home", map: "map", journal: "journal",
    settings: "settings", guide: "guide" }[name];
  $$(".tabs a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${tab}`));
  if (name === "loading") $("#loading-msg").textContent = msg || "Thinking about your walk…";
  views[name]?.();
  window.scrollTo(0, 0);
}

function route() {
  const h = location.hash.slice(1) || "home";
  if (!store.get("settings")?.home_lat && h !== "setup") return show("setup");
  if (h === "quest") return resumeQuest();
  if (h.startsWith("snap/")) return snapFromNotification(h.slice(5));
  show(h in views || $(`#v-${h}`) ? h : "home");
}
window.addEventListener("hashchange", route);

// ---------- geolocation ----------

let lastFix = null;
let lastGeoError = null; // 1 = permission denied, "unsupported" = no GPS API, other = timeout/unavailable
const locationDenied = () => lastGeoError === 1 || lastGeoError === "unsupported";
let watchId = null;
function watchGps() {
  if (watchId !== null || !navigator.geolocation) return;
  watchId = navigator.geolocation.watchPosition(
    (p) => (lastFix = { lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy, at: Date.now() }),
    () => {},
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 },
  );
}
function stopGps() {
  if (watchId !== null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
}
// maxAgeMs: how old a position may be. { fresh: true } ignores every cached position and waits for a new reading.
// If GPS fails, this resolves null rather than handing back an old position from somewhere else.
function getFix(maxAgeMs = 15000, { fresh = false } = {}) {
  const recent = (f) => f && Date.now() - f.at < maxAgeMs;
  if (!fresh && recent(lastFix)) return Promise.resolve(lastFix);
  return new Promise((resolve) => {
    if (!navigator.geolocation) { lastGeoError = "unsupported"; return resolve(null); }
    navigator.geolocation.getCurrentPosition(
      (p) => { lastGeoError = null; resolve((lastFix = { lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy, at: Date.now() })); },
      (err) => { lastGeoError = err?.code ?? "error"; resolve(!fresh && recent(lastFix) ? lastFix : null); },
      { enableHighAccuracy: true, maximumAge: fresh ? 0 : maxAgeMs, timeout: fresh ? 20000 : 10000 },
    );
  });
}

// ---------- setup ----------

$("#setup-gps").onclick = async () => {
  $("#setup-msg").textContent = "Finding you…";
  const fix = await getFix(60000);
  if (!fix) return ($("#setup-msg").textContent = "Couldn't get your location. Type a city instead.");
  await saveHome(fix.lat, fix.lng, await cityFor(fix.lat, fix.lng));
};
async function cityFor(lat, lng) {
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&zoom=10&lat=${lat.toFixed(2)}&lon=${lng.toFixed(2)}`);
    const a = (await r.json()).address || {};
    return [a.city || a.town || a.village || a.county, a.state].filter(Boolean).join(", ") || null;
  } catch { return null; }
}
$("#setup-save").onclick = async () => {
  const city = $("#setup-city").value.trim();
  if (!city) return;
  $("#setup-msg").textContent = "Looking it up…";
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(city)}`);
    const [hit] = await r.json();
    if (!hit) throw new Error("not found");
    await saveHome(+hit.lat, +hit.lon, city);
  } catch {
    $("#setup-msg").textContent = "Couldn't find that city.";
  }
};
async function saveHome(lat, lng, city) {
  await saveSettings({ home_lat: lat, home_lng: lng, home_city: city, units: "mi" });
  location.hash = "home";
  route();
}

// Settings live on the phone (works offline) and on Base Camp (survives reinstalling the app).
async function saveSettings(changes) {
  const s = { auto_read: false, weekly_goal: 3, ...store.get("settings", {}), ...changes };
  store.set("settings", s);
  try { await api("/api/settings", { json: s }); } catch {}
  return s;
}
async function loadSettings() {
  try {
    const remote = await api("/api/settings");
    const local = store.get("settings", {});
    if (remote.home_lat != null && local.auto_read === undefined) store.set("settings", { ...remote, ...local, auto_read: remote.auto_read });
  } catch {}
}
const autoRead = () => !!store.get("settings", {}).auto_read;

// ---------- home ----------

views.home = () => {
  showHomeLine();
  const q = store.get("quest");
  $("#resume").hidden = !(q && !q.finished_at);
  if (q) $("#resume-title").textContent = q.title;
};
$("#resume-btn").onclick = () => resumeQuest();

const choice = (name) => document.querySelector(`input[name=${name}]:checked`).value;

// Writing a quest takes about a minute on Base Camp. Extra taps used to queue more quests behind it,
// so while one is being written every quest button is ignored (also across a page reload, for 2 minutes).
const WRITING_KEY = "writingSince";
let writing = false;
function isWriting() {
  if (writing) return true;
  try { return Date.now() - (+sessionStorage.getItem(WRITING_KEY) || 0) < 120000; } catch { return false; }
}
async function oneAtATime(fn) {
  if (isWriting()) return toast("Still writing your quest, about 1 minute.");
  writing = true;
  try { sessionStorage.setItem(WRITING_KEY, Date.now()); } catch {}
  document.body.classList.add("writing");
  try { await fn(); } finally {
    writing = false;
    try { sessionStorage.removeItem(WRITING_KEY); } catch {}
    document.body.classList.remove("writing");
  }
}

// No location (permission denied): everyday tasks only, photos say "place not confirmed".
// The saved home only sets the season; it's never used to check photos.
function noLocationQuest() {
  const s = store.get("settings", {});
  toast("Location is off, so this walk uses everyday tasks and photos will say “place not confirmed”.", 7000);
  return makeQuestNow({ mode: "no_location", lat: s.home_lat ?? null, lng: s.home_lng ?? null });
}

$("#walk-here").onclick = () => oneAtATime(async () => {
  show("loading", "Getting a fresh GPS fix for your start…");
  const fix = await getFix(0, { fresh: true });
  if (!fix) {
    if (locationDenied()) return noLocationQuest();
    toast("Couldn't get a fresh GPS fix. Walk from here starts exactly where you are, so check location is on and try again.", 7000);
    return show("home");
  }
  await makeQuestNow({ mode: "from_here", lat: fix.lat, lng: fix.lng });
});

// ---------- update my location ----------
// The saved home (from setup) is used for the Home walks pin, the map's starting view, and as a backup
// when GPS isn't available for "Find me a walk". This replaces it with a fresh GPS reading.

async function updateMyLocation(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Getting fresh GPS…";
  try {
    const fix = await getFix(0, { fresh: true });
    if (!fix) return toast("Couldn't get a GPS fix. Check that location is on for this site, then try again.", 7000);
    const city = await cityFor(fix.lat, fix.lng);
    await saveSettings({ home_lat: fix.lat, home_lng: fix.lng, home_city: city });
    toast(`Location updated${city ? `: ${city}` : ""} (±${Math.round(fix.accuracy)} m)`);
    showHomeLine();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function showHomeLine() {
  const s = store.get("settings", {});
  const where = s.home_city || (s.home_lat != null ? `${(+s.home_lat).toFixed(2)}, ${(+s.home_lng).toFixed(2)}` : "not set");
  $$(".home-line").forEach((el) => (el.textContent = `Saved location: ${where}`));
}
$$(".update-location").forEach((b) => (b.onclick = () => updateMyLocation(b)));

function makeQuest(target) {
  return oneAtATime(() => makeQuestNow(target));
}

async function makeQuestNow(target) {
  show("loading", "Writing your quest, about 1 minute");
  const where = typeof target === "string" ? { mode: "place", place_id: target } : target;
  try {
    const q = await api("/api/quests", { json: { ...where, distance_tier: choice("tier"), difficulty: choice("diff"), tz: TZ } });
    saveQuest(q);
    await prefetchAudio(q);
    show("intro");
  } catch (e) {
    toast(`Couldn't make a quest: ${e.message}`);
    show("home");
  }
}

// ---------- walk suggestions ----------

let suggestions = null;
$("#find-walk").onclick = () => oneAtATime(async () => {
  show("loading", "Finding walks near you, about 1 minute");
  const s = store.get("settings");
  let fix = await getFix(60000);
  if (!fix && locationDenied()) return noLocationQuest();
  fix = fix || { lat: s.home_lat, lng: s.home_lng };
  const qs = new URLSearchParams({ lat: fix.lat, lng: fix.lng, tier: choice("tier"), difficulty: choice("diff"), tz: TZ });
  try {
    suggestions = { ...(await api(`/api/places/suggest?${qs}`)), from: fix };
    if (!suggestions.picks.length) {
      toast("No walks found in that range. Try a longer distance, or Walk from here.");
      return show("home");
    }
    show("suggest");
  } catch (e) {
    toast(e.message, 6000);
    show("home");
  }
});

const PIN = (cls) => L.divIcon({ className: `pin ${cls}`, html: "<span></span>", iconSize: [28, 28], iconAnchor: [14, 28], popupAnchor: [0, -26] });
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const maps = {};
function makeMap(id) {
  if (!window.L) return null;
  if (maps[id]) return maps[id];
  const m = L.map(id, { zoomControl: false, attributionControl: true });
  L.tileLayer(TILE_URL, { maxZoom: 18, attribution: TILE_ATTR }).addTo(m);
  m._layer = L.layerGroup().addTo(m);
  return (maps[id] = m);
}

views.suggest = () => {
  const { picks, others, from } = suggestions;
  $("#suggest-list").innerHTML = picks.map((p, i) => `
    <div class="card place">
      <p class="eyebrow">${i === 0 ? "Best right now" : "Also good"} · ${p.distance_mi} mi</p>
      <h3>${esc(p.name)}</h3>
      <p>${esc(p.reason)}</p>
      <button class="btn primary" data-go="${p.id}">Go here</button>
    </div>`).join("");
  const m = makeMap("suggest-map");
  if (!m) return;
  setTimeout(() => {
    m.invalidateSize();
    m._layer.clearLayers();
    L.circleMarker([from.lat, from.lng], { radius: 7, color: "#4A3B2C", fillColor: "#9CC9E3", fillOpacity: 1 }).addTo(m._layer).bindPopup("You");
    others.forEach((p) => L.marker([p.lat, p.lng], { icon: PIN("faded") }).addTo(m._layer).bindPopup(esc(p.name)));
    picks.forEach((p) => L.marker([p.lat, p.lng], { icon: PIN("pick") }).addTo(m._layer).bindPopup(`<b>${esc(p.name)}</b><br>${esc(p.reason)}`));
    m.fitBounds(L.latLngBounds([[from.lat, from.lng], ...picks.map((p) => [p.lat, p.lng])]).pad(0.25));
  }, 50);
};
$("#suggest-list").onclick = (e) => {
  const id = e.target.closest("[data-go]")?.dataset.go;
  if (id) makeQuest(id);
};

// ---------- places I've walked ----------

views.map = async () => {
  let data;
  try { data = await api("/api/map"); store.set("mapCache", data); } catch { data = store.get("mapCache"); }
  if (!data) return ($("#map-stats").textContent = "Map needs Base Camp. Try again when you're back online.");
  const st = data.stats;
  $("#map-stats").innerHTML = `<div><b>${st.walks}</b> walks</div><div><b>${st.places}</b> places</div><div><b>${fmtDuration(st.outside_seconds)}</b> outside</div>`;
  const m = makeMap("walked-map");
  if (!m) return;
  setTimeout(() => {
    m.invalidateSize();
    m._layer.clearLayers();
    const pts = [];
    data.suggested.forEach((p) => {
      L.marker([p.lat, p.lng], { icon: PIN("faded") }).addTo(m._layer).bindPopup(`${esc(p.name)}<br><small>not walked yet</small>`);
    });
    data.visited.forEach((p) => {
      pts.push([p.lat, p.lng]);
      const icon = L.divIcon({ className: `pin solid${p.id === "home-walks" ? " home" : ""}`, html: `<span>${p.visits.length}</span>`, iconSize: [30, 30], iconAnchor: [15, 30], popupAnchor: [0, -28] });
      const visits = p.visits.map((v) => `<div class="visit"><b>${new Date(v.date * 1000).toLocaleDateString()}</b> ${esc(v.title)}${v.partial ? " <small>(partial walk)</small>" : ""}
        <div class="thumbs">${v.photos.map((u) => `<img src="${u}" alt="">`).join("")}</div></div>`).join("");
      L.marker([p.lat, p.lng], { icon }).addTo(m._layer).bindPopup(`<b>${esc(p.name)}</b> · ${p.visits.length} visit${p.visits.length > 1 ? "s" : ""}${visits}`, { maxWidth: 260 });
    });
    const s = store.get("settings");
    if (pts.length) m.fitBounds(L.latLngBounds(pts).pad(0.3), { maxZoom: 15 });
    else m.setView([s.home_lat, s.home_lng], 13);
  }, 50);
};

// ---------- quest state ----------

let quest = null;
function saveQuest(q) {
  const prev = store.get("quest");
  if (prev && prev.id === q.id) q.local = prev.local;
  q.local = q.local || { screenMs: 0 };
  quest = q;
  store.set("quest", q);
}
function resumeQuest() {
  quest = store.get("quest");
  if (!quest) return show("home");
  if (quest.finished_at) return show("summary");
  show(quest.local.started ? "away" : "intro");
}

views.intro = () => {
  $("#intro-place").textContent = quest.mode === "from_here"
    ? `Walk from here · within ${fmtDistance(quest.radius_m)}` : quest.place?.name || "";
  $("#intro-title").textContent = quest.title;
  $("#intro-text").textContent = quest.spoken_intro;
  const mins = quest.deadline_minutes;
  $("#intro-meta").textContent = [`${quest.tasks.length} finds`, `${mins} min`, quest.weather, quest.sunset && `sunset ${quest.sunset}`]
    .filter(Boolean).join(" · ");
  $("#intro-tasks").innerHTML = quest.tasks.map((t) => `<li>${esc(t.prompt)}${t.rare ? ' <span class="tag">rare</span>' : ""}</li>`).join("");
};

$("#intro-start").onclick = async () => {
  const startedAt = Date.now() / 1000;
  quest.local.started = startedAt;
  quest.started_at = quest.started_at || startedAt;
  quest.deadline_at = quest.deadline_at || startedAt + quest.deadline_minutes * 60;
  store.set("quest", quest);
  screenTime.start();
  if (autoRead()) speak(quest);
  show("away");
  showQuestNotification();
  await Outbox.add({ kind: "start", questId: quest.id, body: { started_at: startedAt } });
  sync();
};

// ---------- voice ----------
// ElevenLabs mp3 from Base Camp, cached on the phone before leaving; browser speech as a fallback.

const AUDIO_CACHE = "wj-audio-v1";
async function prefetchAudio(q) {
  const url = `/api/quests/${q.id}/audio`;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    if (await cache.match(url)) return true;
    const r = await fetch(url, { headers: { "X-WJ-User": USER } });
    if (!r.ok) return false;
    await cache.put(url, r);
    return true;
  } catch { return false; }
}

async function speak(q) {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  const audio = $("#voice");
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const hit = await cache.match(`/api/quests/${q.id}/audio`);
    if (hit) {
      if (audio._url) URL.revokeObjectURL(audio._url);
      audio._url = URL.createObjectURL(await hit.blob());
      audio.src = audio._url;
      await audio.play();
      return;
    }
  } catch {}
  speakBrowser(q);
}

function speakBrowser(q) {
  if (!("speechSynthesis" in window)) return;
  const text = `${q.title}. ${q.spoken_intro} Here's what to find. ${q.tasks.map((t, i) => `${i + 1}. ${t.prompt}.`).join(" ")} Enjoy the walk.`;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.95;
  speechSynthesis.speak(u);
}
$("#away-replay").onclick = () => speak(quest);
$("#intro-read").onclick = () => speak(quest);

// ---------- screen time (Page Visibility API) ----------

const screenTime = {
  since: null,
  start() {
    if (document.visibilityState === "visible") this.since = Date.now();
  },
  flush() {
    if (!quest?.local?.started || quest.local.done) return;
    if (this.since) {
      quest.local.screenMs += Date.now() - this.since;
      this.since = document.visibilityState === "visible" ? Date.now() : null;
      store.set("quest", quest);
    }
  },
  seconds() {
    this.flush();
    return Math.round((quest?.local?.screenMs || 0) / 1000);
  },
};
document.addEventListener("visibilitychange", () => {
  if (!quest?.local?.started || quest.local.done) return;
  if (document.visibilityState === "visible") screenTime.since = Date.now();
  else screenTime.flush(), (screenTime.since = null);
});
window.addEventListener("pagehide", () => screenTime.flush());

// ---------- phone away / tasks ----------

const STATUS_ICON = { todo: "○", pass: "✓", self: "✋", unconfirmed: "◐", unsure: "?", pending: "⏳", retake: "↺", fail: "✗", skipped: "→", queued: "⏳" };
const STATUS_TEXT = { todo: "", pass: "verified", self: "self-confirmed", unconfirmed: "time/place not confirmed", unsure: "couldn't tell",
  pending: "waiting for Base Camp", retake: "retake", fail: "not yet", skipped: "skipped", queued: "saved, will sync" };

views.away = () => {
  watchGps();
  renderTasks();
  tickTimer();
};

function tickTimer() {
  clearInterval(tickTimer._i);
  const upd = () => {
    if (!quest?.deadline_at) return;
    const left = Math.round((quest.deadline_at * 1000 - Date.now()) / 60000);
    $("#away-timer").textContent = left > 0 ? `${left} min left` : "Time's up. Finish when you're ready.";
    $("#away-dist").textContent = awayLine();
  };
  upd();
  tickTimer._i = setInterval(upd, 15000);
}

function metersBetween(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function fmtDistance(m) {
  return m >= 1609 ? `${+(m / 1609.344).toFixed(1)} mi` : `${Math.round(m)} m`;
}

function awayLine() {
  if (quest?.mode !== "from_here" || !quest.start || !lastFix) return "";
  const d = metersBetween(quest.start, lastFix);
  const goal = quest.away_goal_m;
  const nudge = goal && !quest.away_met ? (d >= goal ? " · far enough, snap one here!" : ` · get ${goal} m away for one photo`) : "";
  return `You're ${fmtDistance(d)} from your start${nudge}`;
}

function awayRequirement() {
  if (quest.mode !== "from_here" || !quest.away_goal_m) return "";
  const met = quest.away_met;
  return `<li class="task s-${met ? "pass" : "todo"}"><span class="ico">${met ? "✓" : "🚶"}</span>
    <div class="body"><span>Take one photo at least ${quest.away_goal_m} m from where you started</span>
    <em>${met ? "done" : `farthest verified photo so far: ${quest.farthest_m || 0} m`}</em></div></li>`;
}

const SWAPPABLE = ["todo", "fail", "retake", "unsure"];

function swapsLine() {
  if (quest.swaps_left == null || quest.finished_at) return "";
  return `<li class="swaps muted">🔄 Swaps left: ${quest.swaps_left}</li>`;
}

function progressLine() {
  const p = quest.progress;
  if (!p || !p.total) return "";
  const msg = p.done >= p.needed ? `${p.done} of ${p.total} done · walk complete 🌿` : `${p.done} of ${p.total} done · ${p.needed} completes the walk`;
  return `<li class="swaps muted">✓ ${msg}</li>`;
}

function renderTasks() {
  $("#task-list").innerHTML = progressLine() + swapsLine() + awayRequirement() + quest.tasks.map((t) => {
    const st = t.local_status || t.status;
    const label = (!t.local_status && t.capture?.label) || STATUS_TEXT[st];
    const reason = t.capture?.reason && st !== "pass" ? `<small>${esc(t.capture.reason)}</small>` : "";
    const done = ["pass", "self", "skipped"].includes(st);
    const actions = [];
    if (!done && st !== "queued") {
      if (t.kind === "tracker") actions.push(`<button class="btn small" data-act="tracker" data-id="${t.id}">Add screenshot</button>`);
      else if (st === "unconfirmed") // already ◐: another gallery pick can't do better, a live photo can
        actions.push(`<button class="btn small" data-act="snap" data-id="${t.id}">Take photo for a full ✓</button>`);
      else actions.push(`<button class="btn small" data-act="snap" data-id="${t.id}">Take photo</button>`,
        `<button class="btn small ghost" data-act="gallery" data-id="${t.id}">Choose from gallery</button>`);
      if (st === "unsure" && t.capture) actions.push(`<button class="btn small ghost" data-act="self" data-cid="${t.capture.id}">It's right</button>`);
      if (quest.skips_left > 0 && quest.difficulty === "hard") actions.push(`<button class="btn small ghost" data-act="skip" data-id="${t.id}">Skip</button>`);
      if (t.kind === "photo" && quest.swaps_left > 0 && SWAPPABLE.includes(st))
        actions.push(`<button class="btn small ghost" data-act="swap" data-id="${t.id}">Swap this task</button>`);
    }
    return `<li class="task s-${st}">
      <span class="ico" title="${esc(label)}">${STATUS_ICON[st] || "○"}</span>
      <div class="body"><span>${esc(t.prompt)}${t.rare ? ' <span class="tag">rare</span>' : ""}</span>${reason}
        ${label ? `<em>${esc(label)}</em>` : ""}</div>
      <div class="acts">${actions.join("")}</div></li>`;
  }).join("");
}

$("#task-list").onclick = async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const { act, id, cid } = b.dataset;
  if (act === "snap") openCamera(quest.tasks.find((t) => t.id === id));
  if (act === "tracker") pickTracker(id);
  if (act === "gallery") pickGallery(id);
  if (act === "self") {
    try { await api(`/api/captures/${cid}/self-confirm`, { method: "POST" }); await refreshQuest(); } catch (err) { toast(err.message); }
  }
  if (act === "swap") return swapTask(id, b);
  if (act === "skip") {
    try { saveQuest(await api(`/api/quests/${quest.id}/skip`, { json: { task_id: id } })); renderTasks(); } catch (err) { toast(err.message); }
  }
};

async function swapTask(taskId, button) {
  if (!online) return toast("Swapping needs Base Camp. Try again when you're back online.");
  button.disabled = true;
  button.textContent = "Swapping…";
  toast("Gemma is finding a different task…", 60000);
  try {
    const res = await api(`/api/quests/${quest.id}/swap`, { json: { task_id: taskId } });
    saveQuest(res);
    renderTasks();
    showQuestNotification();
    toast(`New task: ${res.swap.with}`);
    // The cached read-aloud audio still has the old task in it.
    try { await (await caches.open(AUDIO_CACHE)).delete(`/api/quests/${quest.id}/audio`); } catch {}
    prefetchAudio(quest);
  } catch (e) {
    toast(e.message);
    renderTasks();
  }
}

async function refreshQuest() {
  try { saveQuest(await api(`/api/quests/${quest.id}`)); } catch {}
  if (document.body.dataset.view === "away") renderTasks();
  showQuestNotification();
}

// ---------- camera ----------

let camTask = null;
let camStream = null;
async function openCamera(task) {
  camTask = task;
  $("#cam-task").textContent = task.prompt;
  watchGps();
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 } }, audio: false });
    $("#cam-video").srcObject = camStream;
    $("#camera").hidden = false;
    updateGpsLabel();
  } catch {
    // No live camera (e.g. permissions or plain http): use the OS camera via a capture input.
    $("#cam-fallback").value = "";
    $("#cam-fallback").click();
  }
}
function updateGpsLabel() {
  if ($("#camera").hidden) return;
  $("#cam-gps").textContent = lastFix ? `GPS ±${Math.round(lastFix.accuracy)} m` : "Getting GPS…";
  setTimeout(updateGpsLabel, 2000);
}
function closeCamera() {
  camStream?.getTracks().forEach((t) => t.stop());
  camStream = null;
  $("#camera").hidden = true;
}
$("#cam-cancel").onclick = closeCamera;
$("#cam-shutter").onclick = async () => {
  const video = $("#cam-video");
  const takenAt = Date.now();
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const fix = await getFix();
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.9));
  closeCamera();
  submitCapture({ task: camTask, blob, takenAt, fix, source: "camera" });
};
$("#cam-fallback").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const takenAt = Date.now();
  const fix = await getFix();
  submitCapture({ task: camTask, blob: file, takenAt, fix, source: "input", fileModified: file.lastModified });
};

let galleryTaskId = null;
function pickGallery(id) {
  galleryTaskId = id;
  $("#gallery-input").value = "";
  $("#gallery-input").click();
}
$("#gallery-input").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const task = quest.tasks.find((t) => t.id === galleryTaskId);
  if ((task.local_status || task.status) === "unconfirmed") return openCamera(task);
  const fix = quest.mode === "no_location" ? null : await getFix(15000);
  // Sent with its EXIF intact; Base Camp reads the time + GPS, then strips them before saving.
  submitCapture({ task, blob: file, takenAt: Date.now(), fix, source: "gallery" });
};

let trackerTaskId = null;
function pickTracker(id) {
  trackerTaskId = id;
  $("#tracker-input").value = "";
  $("#tracker-input").click();
}
$("#tracker-input").onchange = (e) => {
  const file = e.target.files[0];
  if (!file) return;
  submitCapture({ task: quest.tasks.find((t) => t.id === trackerTaskId), blob: file, takenAt: Date.now(), fix: null, source: "tracker" });
};

// ---------- capture upload ----------

function captureForm(job) {
  const fd = new FormData();
  fd.append("task_id", job.taskId);
  fd.append("client_id", job.clientId);
  fd.append("taken_at", job.takenAt);
  if (job.fix) {
    fd.append("lat", job.fix.lat);
    fd.append("lng", job.fix.lng);
    fd.append("accuracy", job.fix.accuracy);
  }
  fd.append("source", job.source);
  if (job.fileModified) fd.append("file_modified", job.fileModified);
  fd.append("photo", job.blob, "photo.jpg");
  return fd;
}

async function submitCapture({ task, blob, takenAt, fix, source, fileModified }) {
  // Saved to IndexedDB first, so a photo taken with no signal is never lost.
  const job = { kind: "capture", questId: quest.id, taskId: task.id, clientId: crypto.randomUUID(), blob, takenAt, fix, source, fileModified };
  await Outbox.add(job);
  setLocalStatus(task.id, "queued");
  toast(navigator.onLine ? "Checking your photo…" : "Saved. It'll be checked when you're back online.");
  sync();
}

// ---------- sync ----------

async function sendJob(job) {
  if (job.kind === "start") return api(`/api/quests/${job.questId}/start`, { json: job.body });
  if (job.kind === "capture") return api(`/api/quests/${job.questId}/captures`, { method: "POST", body: captureForm(job) });
  if (job.kind === "finish") return api(`/api/quests/${job.questId}/finish`, { json: job.body });
  throw new Error(`unknown job ${job.kind}`);
}

async function sync() {
  const sent = await Outbox.flush(sendJob, (job, res, err) => {
    if (job.questId !== quest?.id) return;
    if (job.kind === "capture") {
      setLocalStatus(job.taskId, null);
      if (err) toast(err.status === 429 ? err.message : `A photo couldn't be saved: ${err.message}`, err.status === 429 ? 9000 : 3500);
      else if (document.visibilityState === "visible")
        toast(res.status === "pass" ? `✓ ${res.label === "verified (gallery)" ? "Verified (gallery). " : ""}${res.reason || "Verified!"}`
          : res.reason || STATUS_TEXT[res.status], res.status === "unconfirmed" ? 8000 : 3500);
    }
    if (job.kind === "finish" && res) {
      saveQuest(res);
      if (document.body.dataset.view === "summary") views.summary();
    }
  });
  if (sent) {
    try { await api("/api/captures/retry-pending", { method: "POST" }); } catch {}
    if (quest && !quest.local?.done) await refreshQuest();
  }
  updateNet();
}

let online = navigator.onLine;
function setOnline(v) {
  if (online === v) return;
  online = v;
  updateNet();
}
async function updateNet() {
  const n = (await Outbox.all()).length;
  const el = $("#net");
  el.hidden = online && !n;
  el.textContent = !online ? `offline${n ? ` · ${n} saved` : ""}` : `syncing ${n}…`;
}
window.addEventListener("online", () => { online = true; sync(); });
window.addEventListener("offline", () => setOnline(false));
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && sync());
setInterval(sync, 30000);

function setLocalStatus(taskId, st) {
  const t = quest.tasks.find((x) => x.id === taskId);
  if (t) t.local_status = st || undefined;
  store.set("quest", quest);
  renderTasks();
}

// ---------- finish ----------

$("#finish-btn").onclick = () => show("finish");
views.finish = () => {
  const p = quest.progress;
  $("#finish-partial").textContent = p && !p.completed && p.total
    ? `You've done ${p.done} of ${p.total}. Saving now keeps your photos and notes as a partial walk in your journal and on your map. A walk completes at ${p.needed} of ${p.total}, whenever you're ready.`
    : "";
};

// Gentle: progress toward a weekly goal the walker picks. Never a broken streak, never guilt.
function weekLine(w) {
  if (!w) return "";
  const row = w.weeks_in_a_row >= 2 ? ` ${w.weeks_in_a_row} good weeks in a row.` : "";
  if (w.met) return `🌿 Weekly goal reached: ${w.this_week} of ${w.goal} walks this week. Lovely.${row}`;
  if (w.this_week) return `🌿 ${w.this_week} of ${w.goal} walks this week.${row}`;
  return `🌿 Your week, your pace. Goal: ${w.goal} walks.`;
}
$("#finish-save").onclick = async () => {
  const screenSeconds = screenTime.seconds();
  quest.local.done = true;
  const body = {
    note: $("#finish-note").value.trim() || null,
    mood: document.querySelector("input[name=mood]:checked")?.value || null,
    screen_seconds: screenSeconds,
    finished_at: Date.now() / 1000,
  };
  // Show the result right away; the server copy (points, badges) replaces it when it syncs.
  Object.assign(quest, { finished_at: body.finished_at, screen_seconds: screenSeconds, note: body.note, mood: body.mood,
    outside_seconds: body.finished_at - (quest.started_at || quest.local.started), new_badges: [], points: null });
  store.set("quest", quest);
  stopGps();
  closeQuestNotification();
  await Outbox.add({ kind: "finish", questId: quest.id, body });
  show("summary");
  sync();
};

function fmtDuration(sec) {
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} minutes`;
}

views.summary = () => {
  const q = quest;
  const outside = q.outside_seconds ?? (q.finished_at - q.started_at);
  $("#sum-title").textContent = q.title;
  $("#sum-screen").innerHTML = `<b>${Math.round(q.screen_seconds || 0)} seconds</b> on screen.<br><b>${fmtDuration(outside)}</b> outside.`;
  const p = q.progress;
  let line;
  if (q.points == null) line = "Saved on your phone. Points and badges appear when it syncs.";
  else if (q.partial) {
    const far = q.mode === "from_here" && !q.away_met ? ` (and one photo ${q.away_goal_m} m from your start)` : "";
    line = `Partial walk saved to your journal and map 🌿 ${p ? `${p.done} of ${p.total} done; ${p.needed} completes a walk${far}.` : ""} No points this time, and that's okay.`;
  } else line = `${q.points} points`;
  $("#sum-points").textContent = [line, weekLine(q.week)].filter(Boolean).join(" · ");
  $("#sum-badges").innerHTML = (q.new_badges || []).map((b) => `<div class="badge"><span>${b.icon}</span>${esc(b.name)}</div>`).join("");
  const skies = q.tasks.filter((t) => t.category === "sky" && ["pass", "self"].includes(t.status) && t.capture);
  $("#sum-share").innerHTML = skies.length && online && CONFIG.gallery !== false ? `<div class="share">
      <p><b>Share this sky to the Sky Gallery?</b><br><small class="muted">Only the photo, your city, the date and time. Gemma checks it has no people first.</small></p>
      <label>City<input id="share-city" value="${esc(store.get("settings")?.home_city || "")}" placeholder="Warrensburg, MO"></label>
      <button class="btn" id="share-btn" data-cid="${skies[0].capture.id}">Share my sky</button></div>` : "";
  $("#sum-tasks").innerHTML = q.tasks.map((t) => `<li class="task s-${t.local_status || t.status}"><span class="ico">${STATUS_ICON[t.local_status || t.status]}</span>
    <div class="body"><span>${esc(t.prompt)}</span>${t.capture?.photo_url ? `<img src="${t.capture.photo_url}" alt="" loading="lazy">` : ""}</div></li>`).join("");
};

$("#sum-share").onclick = async (e) => {
  const b = e.target.closest("#share-btn");
  if (!b) return;
  b.disabled = true;
  b.textContent = "Checking…";
  try {
    const r = await api(`/api/captures/${b.dataset.cid}/share`, { json: { city: $("#share-city").value } });
    $("#sum-share").innerHTML = `<p>☁️ Shared! <a href="${r.gallery}" target="_blank" rel="noopener">See the Sky Gallery</a></p>`;
  } catch (err) {
    toast(err.message, 6000);
    b.disabled = false;
    b.textContent = "Share my sky";
  }
};

// ---------- journal ----------

views.journal = async () => {
  let data, badges;
  try {
    [data, badges] = await Promise.all([api("/api/journal"), api("/api/badges")]);
    store.set("journalCache", { data, badges });
  } catch {
    ({ data, badges } = store.get("journalCache", {}));
  }
  if (!data) return ($("#j-entries").innerHTML = `<p class="muted">Your journal lives on Base Camp. Open it when you're back online.</p>`);
  $("#j-streak").textContent = weekLine(data.week);
  $("#j-badges").innerHTML = badges.badges.map((b) => `<div class="badge ${b.earned_at ? "" : "locked"}" title="${esc(b.how)}">
    <span>${b.icon}</span>${esc(b.name)}</div>`).join("");
  if (!data.entries.length) return ($("#j-entries").innerHTML = `<p class="muted center">No walks yet. Your first page is waiting outside.</p>`);
  $("#j-entries").innerHTML = data.entries.map(diaryPage).join("");
};

// ---------- diary pages ----------
// One walk = one page: cream paper, a season seal, polaroid prints held by washi tape, the note in a
// diary hand, and at most one tiny critter in a corner. Static: no animation anywhere.

const svg = (body, cls = "") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const MOOD_ICONS = {
  calm: svg('<circle cx="12" cy="12" r="9"/><path d="M8.5 10.5q1.5 1 3 0M12.5 10.5q1.5 1 3 0M9 15q3 2 6 0"/>'),
  happy: svg('<circle cx="12" cy="12" r="9"/><circle cx="9" cy="10" r=".6" fill="currentColor"/><circle cx="15" cy="10" r=".6" fill="currentColor"/><path d="M8.5 14q3.5 3.5 7 0"/>'),
  energized: svg('<path d="M13 3 6 13h5l-1 8 7-10h-5z"/>'),
  tired: svg('<circle cx="11" cy="13" r="8"/><path d="M7.5 12h2.5M12.5 12h2.5M9.5 16q1.5-1 3 0M17 3h3l-3 3h3"/>'),
};
const CRITTERS = [
  // a sleepy little sun
  svg('<circle cx="12" cy="13" r="5"/><path d="M12 4v2M12 20v2M3 13h2M19 13h2M5.6 6.6l1.4 1.4M17 18l1.4 1.4M5.6 19.4 7 18M17 8l1.4-1.4"/><path d="M9.6 12.6q1 .8 2 0M12.4 12.6q1 .8 2 0M11 15.2h2"/>', "critter"),
  // a small snail with a face
  svg('<path d="M3 18h14q3 0 3-3v-1"/><path d="M20 14v-4M18 14v-3"/><circle cx="20" cy="9.5" r=".5" fill="currentColor"/><circle cx="18" cy="10.5" r=".5" fill="currentColor"/><circle cx="10" cy="13" r="5"/><path d="M10 13a2 2 0 1 0 2-2"/>', "critter"),
  // a sprout saying hi
  svg('<path d="M12 21v-8"/><path d="M12 13q-6 0-6-6 6 0 6 6zM12 11q0-5 5-5 0 5-5 5z"/><circle cx="10" cy="17" r=".5" fill="currentColor"/><circle cx="14" cy="17" r=".5" fill="currentColor"/>', "critter"),
];
const TAPES = ["wheat", "sage", "sky", "rose"];

function hashOf(s) {
  let h = 0;
  for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

function diaryPage(q) {
  const h = hashOf(q.id);
  const when = new Date(q.finished_at * 1000);
  const date = when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const photos = q.tasks.filter((t) => t.capture?.photo_url && t.kind === "photo");
  const mood = q.mood && MOOD_ICONS[q.mood] ? `<span class="mood" title="${esc(q.mood)}">${MOOD_ICONS[q.mood]}<span>${esc(q.mood)}</span></span>` : "";
  const prints = photos.map((t, i) => {
    const tilt = ((h >>> i) & 1 ? 1 : -1) * (1.2 + ((h >>> (i + 3)) % 3) * 0.6);
    const label = t.capture?.label && t.status !== "pass" ? t.capture.label : "";
    return `<figure class="print" style="--tilt:${tilt.toFixed(1)}deg">
      <span class="tape tape-${TAPES[(h + i) % TAPES.length]}"></span>
      <img src="${t.capture.photo_url}" alt="${esc(t.prompt)}" loading="lazy">
      <figcaption>${esc(t.prompt)}${label ? `<small>${esc(label)}</small>` : ""}</figcaption></figure>`;
  }).join("");
  const foot = [q.partial ? "" : `${q.points || 0} points`, `${fmtDuration(q.outside_seconds)} outside`,
    `${Math.round(q.screen_seconds || 0)} s on screen`].filter(Boolean).join(" · ");
  // About one page in three gets a critter, never more than one. ">>>" keeps the hash unsigned (">>" went
  // negative on big hashes and picked a critter that didn't exist, printing "undefined").
  const critter = h % 3 === 0 ? CRITTERS[(h >>> 4) % CRITTERS.length] || "" : "";
  return `<article class="diary-page${q.partial ? " is-partial" : ""}">
    <header class="page-head">
      <div>
        <p class="page-date">${esc(date)}</p>
        <h3 class="page-title">${esc(q.title)}</h3>
        <p class="page-place">${esc(q.place?.name || "")}${mood}</p>
      </div>
      <div class="stamps">
        ${q.season ? `<span class="seal">${esc(q.season)}</span>` : ""}
        ${q.partial ? `<span class="stamp">partial walk</span>` : ""}
      </div>
    </header>
    ${q.note ? `<p class="diary-note">${esc(q.note)}</p>` : ""}
    ${prints ? `<div class="prints">${prints}</div>` : ""}
    ${q.badges.length ? `<p class="page-badges">${q.badges.map((b) => `<span>${b.icon} ${esc(b.name)}</span>`).join("")}</p>` : ""}
    <footer class="page-foot">${foot}</footer>
    ${critter}
  </article>`;
}

// ---------- settings + notifications ----------
// Web Push from Base Camp for reminders; the live quest notification is shown by the phone itself and
// refreshed by Base Camp after each photo is checked. Same tag, so it replaces itself. Text only.

const QUEST_TAG = "wj-quest";
const DONE = ["pass", "self", "unconfirmed", "skipped"];
const settings = () => store.get("settings", {});

views.settings = () => {
  const s = settings();
  $("#auto-read").checked = !!s.auto_read;
  showHomeLine();
  $("#n-daily").checked = !!s.notify_daily;
  $("#n-daily-time").value = s.daily_time || "17:00";
  $("#n-journal").checked = !!s.notify_journal;
  $("#n-journal-time").value = s.journal_time || "20:00";
  $("#n-quest").checked = !!s.notify_quest;
  $("#weekly-goal").value = String(s.weekly_goal || 3);
  $("#notif-status").textContent = notifSupport().message;
};
$("#auto-read").onchange = (e) => saveSettings({ auto_read: e.target.checked });

// ---------- your data + feedback ----------

$("#export-btn").href = withUser("/api/export");
$("#delete-btn").onclick = () => { $("#delete-confirm").hidden = false; $("#delete-btn").hidden = true; };
$("#delete-no").onclick = () => { $("#delete-confirm").hidden = true; $("#delete-btn").hidden = false; };
$("#delete-yes").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/my-data", { method: "DELETE" });
    for (const k of ["quest", "journalCache", "mapCache", "settings", "suggestions", WRITING_KEY]) store.del(k);
    for (const job of await Outbox.all()) await Outbox.remove(job.id);
    try { await caches.delete(AUDIO_CACHE); } catch {}
    toast(`Deleted: ${r.deleted_walks} walk${r.deleted_walks === 1 ? "" : "s"} and ${r.deleted_photos} photo${r.deleted_photos === 1 ? "" : "s"}. Your journal is empty now.`, 6000);
    $("#delete-confirm").hidden = true;
    $("#delete-btn").hidden = false;
    quest = null;
    location.hash = "#setup";
  } catch (err) {
    toast(err.message, 6000);
  } finally {
    e.target.disabled = false;
  }
};
$("#fb-send").onclick = async (e) => {
  const message = $("#fb-message").value.trim();
  const rating = $("#fb-rating").value ? +$("#fb-rating").value : null;
  if (!message && rating == null) return ($("#fb-status").textContent = "Write a few words or pick a rating first.");
  e.target.disabled = true;
  try {
    const r = await api("/api/feedback", { json: { message, rating } });
    $("#fb-status").textContent = r.message;
    $("#fb-message").value = "";
    $("#fb-rating").value = "";
  } catch (err) {
    $("#fb-status").textContent = err.network ? "You're offline. Try again when you're back online." : err.message;
  } finally {
    e.target.disabled = false;
  }
};

// ---------- mode: laptop (local) or public beta (cloud) ----------
// Which privacy note to show and what's switched on comes from Base Camp; the last answer is kept for offline.

let CONFIG = store.get("config", {});
function applyConfig() {
  const cloud = !!CONFIG.cloud;
  document.body.dataset.mode = cloud ? "cloud" : "local";
  $$(".cloud-only").forEach((el) => (el.hidden = !cloud));
  $$(".local-only").forEach((el) => (el.hidden = cloud));
  if (CONFIG.privacy) $$(".privacy-note").forEach((el) => (el.textContent = CONFIG.privacy));
  if (CONFIG.backend === "google")
    $$(".checked-by").forEach((el) => (el.textContent = "Gemma looks at the photo. Photos are checked by Google’s hosted Gemma."));
  $("#notif-card").hidden = CONFIG.push === false && cloud;  // reminders are a laptop feature for now
}
async function loadConfig() {
  try {
    CONFIG = await api("/api/config");
    store.set("config", CONFIG);
  } catch {}
  applyConfig();
}
function showWelcome() {
  if (store.get("welcomed")) return;
  $("#welcome").hidden = false;
  $("#welcome-go").onclick = () => {
    store.set("welcomed", Date.now());
    $("#welcome").hidden = true;
  };
}
$("#weekly-goal").onchange = (e) => saveSettings({ weekly_goal: +e.target.value });

function notifSupport() {
  if (!("Notification" in window) || !("serviceWorker" in navigator) || !("PushManager" in window))
    return { ok: false, message: "This browser can't show notifications here. On iPhone, add Walk Journal to your Home Screen first." };
  if (Notification.permission === "denied")
    return { ok: false, message: "Notifications are blocked for this site. Allow them in your browser settings to turn these on." };
  return { ok: true, message: Notification.permission === "granted" ? "Notifications are allowed." : "" };
}

function b64ToBytes(b64) {
  const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

// Ask permission and subscribe this device to Base Camp's push. Returns false if it can't.
async function enableNotifications() {
  const support = notifSupport();
  if (!support.ok) { $("#notif-status").textContent = support.message; return false; }
  if ((await Notification.requestPermission()) !== "granted") {
    $("#notif-status").textContent = "Notifications weren't allowed, so these stay off.";
    return false;
  }
  try {
    const { key } = await api("/api/push/key");
    const reg = await navigator.serviceWorker.ready;
    const sub = (await reg.pushManager.getSubscription())
      || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) }));
    await api("/api/push/subscribe", { json: sub.toJSON() });
    $("#notif-status").textContent = "Notifications are on for this device.";
    return true;
  } catch (e) {
    $("#notif-status").textContent = `Couldn't turn on reminders: ${e.message}`;
    return false;
  }
}

async function disableNotifications() {
  try {
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", { json: { endpoint: sub.endpoint } }).catch(() => {});
      await sub.unsubscribe();
    }
  } catch {}
}

async function onNotifToggle(e) {
  const changes = {
    notify_daily: $("#n-daily").checked, daily_time: $("#n-daily-time").value || "17:00",
    notify_journal: $("#n-journal").checked, journal_time: $("#n-journal-time").value || "20:00",
    notify_quest: $("#n-quest").checked, tz: TZ,
  };
  const anyOn = changes.notify_daily || changes.notify_journal || changes.notify_quest;
  if (e.target.type === "checkbox" && e.target.checked && !(await enableNotifications())) {
    e.target.checked = false;
    changes[{ "n-daily": "notify_daily", "n-journal": "notify_journal", "n-quest": "notify_quest" }[e.target.id]] = false;
  } else if (!anyOn) {
    await disableNotifications();
  }
  await saveSettings(changes);
  if (!changes.notify_quest) closeQuestNotification();
}
["#n-daily", "#n-daily-time", "#n-journal", "#n-journal-time", "#n-quest"].forEach((s) => ($(s).onchange = onNotifToggle));

// Same shape Base Camp sends (push.quest_payload): no photos, no place, no coordinates.
function questNotification(q) {
  const done = q.tasks.filter((t) => DONE.includes(t.local_status || t.status));
  const next = q.tasks.find((t) => !DONE.includes(t.local_status || t.status));
  const body = q.tasks.map((t) => `${DONE.includes(t.local_status || t.status) ? "✓" : "○"} ${t.prompt}`).join("\n");
  if (!next) return { title: `${q.title}: all ${q.tasks.length} done!`, body: `Tap to finish and write in your journal.\n${body}`, actions: [], url: "/#quest", action_urls: {} };
  const photo = next.kind === "photo";
  return {
    title: `${q.title}: ${done.length} of ${q.tasks.length} done`, body,
    actions: photo ? [{ action: "snap", title: "Take photo" }] : [], url: "/#quest",
    action_urls: photo ? { snap: `/#snap/${next.id}` } : {},
  };
}

async function showQuestNotification() {
  if (!quest || quest.local?.done || !quest.local?.started || !settings().notify_quest) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = questNotification(quest);
    const reg = await navigator.serviceWorker.ready;
    await reg.showNotification(n.title, { body: n.body, tag: QUEST_TAG, renotify: false, silent: true, icon: "/icons/icon-192.png",
      actions: n.actions, data: { url: n.url, action_urls: n.action_urls } });
  } catch {}
}

async function closeQuestNotification() {
  try {
    const reg = await navigator.serviceWorker.ready;
    (await reg.getNotifications({ tag: QUEST_TAG })).forEach((n) => n.close());
  } catch {}
}

function snapFromNotification(taskId) {
  quest = store.get("quest");
  const task = quest?.tasks.find((t) => t.id === taskId);
  if (!task || quest.finished_at) return show("home");
  history.replaceState(null, "", "#quest");
  show("away");
  openCamera(task);
}

// The service worker forwards notification taps here.
navigator.serviceWorker?.addEventListener("message", (e) => {
  if (e.data?.type === "open" && e.data.url) {
    const hash = new URL(e.data.url, location.origin).hash;
    if (location.hash === hash) route(); else location.hash = hash;
  }
});

// ---------- swaying grass ----------

function buildGrass() {
  const W = 1200, H = 90, NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  let seed = 7;
  const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
  for (let layer = 3; layer >= 1; layer--) {
    for (let x = -10; x < W + 10; x += 7 + rnd() * 9) {
      const h = H * (0.35 + rnd() * 0.6) * (layer === 3 ? 0.8 : 1);
      const w = 5 + rnd() * 5;
      const lean = (rnd() - 0.5) * 18;
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", `M${x},${H} Q${x + w / 2 + lean / 2},${H - h / 2} ${x + w / 2 + lean},${H - h} Q${x + w / 2 + lean / 3},${H - h / 2} ${x + w},${H} Z`);
      p.setAttribute("class", `blade g${layer}`);
      p.style.animationDelay = `${-(rnd() * 8).toFixed(2)}s`;
      svg.appendChild(p);
    }
  }
  $(".grass").appendChild(svg);
}

// ---------- boot ----------

buildGrass();
applyConfig();
loadConfig().then(showWelcome);
loadSettings();
quest = store.get("quest");
route();
sync();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
