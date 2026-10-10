// Walk Journal: phone client. Plain JS, no framework.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

const store = {
  get(k, d = null) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { localStorage.setItem(k, JSON.stringify(v)); },
  del(k) { localStorage.removeItem(k); },
};

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
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
  if (name === "loading") $("#loading-msg").textContent = msg || "Thinking about your walk…";
  views[name]?.();
  window.scrollTo(0, 0);
}

function route() {
  const h = location.hash.slice(1) || "home";
  if (!store.get("settings")?.home_lat && h !== "setup") return show("setup");
  if (h === "quest") return resumeQuest();
  show(h in views || $(`#v-${h}`) ? h : "home");
}
window.addEventListener("hashchange", route);

// ---------- geolocation ----------

let lastFix = null;
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
function getFix(maxAgeMs = 15000) {
  if (lastFix && Date.now() - lastFix.at < maxAgeMs) return Promise.resolve(lastFix);
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (p) => resolve((lastFix = { lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy, at: Date.now() })),
      () => resolve(lastFix),
      { enableHighAccuracy: true, maximumAge: maxAgeMs, timeout: 10000 },
    );
  });
}

// ---------- setup ----------

$("#setup-gps").onclick = async () => {
  $("#setup-msg").textContent = "Finding you…";
  const fix = await getFix(60000);
  if (!fix) return ($("#setup-msg").textContent = "Couldn't get your location. Type a city instead.");
  await saveHome(fix.lat, fix.lng, null);
};
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
  const s = { home_lat: lat, home_lng: lng, home_city: city, units: "mi" };
  store.set("settings", s);
  try { await api("/api/settings", { json: s }); } catch {}
  location.hash = "home";
  route();
}

// ---------- home ----------

views.home = () => {
  const q = store.get("quest");
  $("#resume").hidden = !(q && !q.finished_at);
  if (q) $("#resume-title").textContent = q.title;
};
$("#resume-btn").onclick = () => resumeQuest();
const choice = (name) => document.querySelector(`input[name=${name}]:checked`).value;

$("#quest-here").onclick = async () => {
  show("loading", "Finding you…");
  const fix = await getFix();
  if (!fix) { toast("Need your location for that."); return show("home"); }
  try {
    const place = await api("/api/places/here", { json: { lat: fix.lat, lng: fix.lng } });
    await makeQuest(place.id);
  } catch (e) { toast(e.message); show("home"); }
};

async function makeQuest(placeId) {
  show("loading", "Gemma is writing your quest…");
  try {
    const q = await api("/api/quests", { json: { place_id: placeId, distance_tier: choice("tier"), difficulty: choice("diff"), tz: TZ } });
    saveQuest(q);
    show("intro");
  } catch (e) {
    toast(`Couldn't make a quest: ${e.message}`);
    show("home");
  }
}

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
  $("#intro-place").textContent = quest.place?.name || "";
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
  speak(quest);
  show("away");
  try { saveQuest(await api(`/api/quests/${quest.id}/start`, { json: { started_at: startedAt } })); } catch {}
};

// ---------- voice ----------

function speak(q) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const text = `${q.title}. ${q.spoken_intro} Here's what to find. ${q.tasks.map((t, i) => `${i + 1}. ${t.prompt}.`).join(" ")}`;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.95;
  speechSynthesis.speak(u);
}
$("#away-replay").onclick = () => speak(quest);

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

const STATUS_ICON = { todo: "○", pass: "✓", self: "✋", unsure: "?", pending: "⏳", retake: "↺", fail: "✗", skipped: "→", queued: "⏳" };
const STATUS_TEXT = { todo: "", pass: "verified", self: "self-confirmed", unsure: "couldn't tell", pending: "waiting for Base Camp",
  retake: "retake", fail: "not yet", skipped: "skipped", queued: "saved, will sync" };

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
  };
  upd();
  tickTimer._i = setInterval(upd, 30000);
}

function renderTasks() {
  $("#task-list").innerHTML = quest.tasks.map((t) => {
    const st = t.local_status || t.status;
    const reason = t.capture?.reason && st !== "pass" ? `<small>${esc(t.capture.reason)}</small>` : "";
    const done = ["pass", "self", "skipped"].includes(st);
    const actions = [];
    if (!done && st !== "queued") {
      actions.push(`<button class="btn small" data-act="${t.kind === "tracker" ? "tracker" : "snap"}" data-id="${t.id}">${t.kind === "tracker" ? "Add screenshot" : t.attempts ? "Retake" : "Snap"}</button>`);
      if (st === "unsure" && t.capture) actions.push(`<button class="btn small ghost" data-act="self" data-cid="${t.capture.id}">It's right</button>`);
      if (quest.skips_left > 0 && quest.difficulty === "hard") actions.push(`<button class="btn small ghost" data-act="skip" data-id="${t.id}">Skip</button>`);
    }
    return `<li class="task s-${st}">
      <span class="ico" title="${STATUS_TEXT[st]}">${STATUS_ICON[st] || "○"}</span>
      <div class="body"><span>${esc(t.prompt)}${t.rare ? ' <span class="tag">rare</span>' : ""}</span>${reason}
        ${STATUS_TEXT[st] ? `<em>${STATUS_TEXT[st]}</em>` : ""}</div>
      <div class="acts">${actions.join("")}</div></li>`;
  }).join("");
}

$("#task-list").onclick = async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const { act, id, cid } = b.dataset;
  if (act === "snap") openCamera(quest.tasks.find((t) => t.id === id));
  if (act === "tracker") pickTracker(id);
  if (act === "self") {
    try { await api(`/api/captures/${cid}/self-confirm`, { method: "POST" }); await refreshQuest(); } catch (err) { toast(err.message); }
  }
  if (act === "skip") {
    try { saveQuest(await api(`/api/quests/${quest.id}/skip`, { json: { task_id: id } })); renderTasks(); } catch (err) { toast(err.message); }
  }
};

async function refreshQuest() {
  try { saveQuest(await api(`/api/quests/${quest.id}`)); } catch {}
  if (document.body.dataset.view === "away") renderTasks();
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
  const job = { questId: quest.id, taskId: task.id, clientId: crypto.randomUUID(), blob, takenAt, fix, source, fileModified };
  setLocalStatus(task.id, "queued");
  toast("Checking your photo…");
  try {
    const cap = await api(`/api/quests/${job.questId}/captures`, { method: "POST", body: captureForm(job) });
    await refreshQuest();
    toast(cap.status === "pass" ? `✓ ${cap.reason || "Verified!"}` : cap.reason || STATUS_TEXT[cap.status]);
  } catch (e) {
    toast(`Upload failed: ${e.message}`);
    setLocalStatus(task.id, null);
  }
}

function setLocalStatus(taskId, st) {
  const t = quest.tasks.find((x) => x.id === taskId);
  if (t) t.local_status = st || undefined;
  store.set("quest", quest);
  renderTasks();
}

// ---------- finish ----------

$("#finish-btn").onclick = () => show("finish");
$("#finish-save").onclick = async () => {
  const screenSeconds = screenTime.seconds();
  quest.local.done = true;
  const body = {
    note: $("#finish-note").value.trim() || null,
    mood: document.querySelector("input[name=mood]:checked")?.value || null,
    screen_seconds: screenSeconds,
    finished_at: Date.now() / 1000,
  };
  try {
    const res = await api(`/api/quests/${quest.id}/finish`, { json: body });
    saveQuest(res);
    stopGps();
    show("summary");
  } catch (e) {
    toast(`Couldn't save: ${e.message}`);
  }
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
  $("#sum-points").textContent = `${q.points} points`;
  $("#sum-badges").innerHTML = (q.new_badges || []).map((b) => `<div class="badge"><span>${b.icon}</span>${esc(b.name)}</div>`).join("");
  $("#sum-tasks").innerHTML = q.tasks.map((t) => `<li class="task s-${t.status}"><span class="ico">${STATUS_ICON[t.status]}</span>
    <div class="body"><span>${esc(t.prompt)}</span>${t.capture?.photo_url ? `<img src="${t.capture.photo_url}" alt="" loading="lazy">` : ""}</div></li>`).join("");
};

// ---------- boot ----------

quest = store.get("quest");
route();
