// Walk Journal service worker: the app shell works with no signal at the park.
const SHELL = "wj-shell-v7";
const RUNTIME = "wj-runtime-v1";
const SHELL_FILES = [
  "/", "/index.html", "/app.js", "/queue.js", "/style.css", "/manifest.json",
  "/icons/icon-192.png", "/icons/icon-512.png", "/fonts/nunito-latin-wght-normal.woff2",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
];
const MAX_TILES = 300;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  const keep = [SHELL, RUNTIME, "wj-audio-v1"];
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => !keep.includes(k)).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // API calls go to Base Camp; the page handles failures (IndexedDB queue, local caches).
  if (url.origin === location.origin && url.pathname.startsWith("/api/")) return;

  // Photos and map tiles: cache-first, filled as you browse.
  if (url.pathname.startsWith("/photos/") || url.hostname.endsWith("tile.openstreetmap.org")) {
    e.respondWith(cacheFirst(req, RUNTIME, url.hostname.endsWith("tile.openstreetmap.org")));
    return;
  }

  // App shell: serve from cache, refresh in the background.
  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => {
      const net = fetch(req).then((r) => {
        if (r.ok) caches.open(SHELL).then((c) => c.put(req, r.clone()));
        return r;
      }).catch(() => hit || caches.match("/index.html"));
      return hit || net;
    }),
  );
});

// ---------- notifications (text only: never photos or locations) ----------

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { d = { title: "Walk Journal", body: e.data?.text() || "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Walk Journal", {
    body: d.body || "", tag: d.tag, icon: d.icon || "/icons/icon-192.png", renotify: !!d.renotify, silent: !!d.silent,
    actions: d.actions || [], data: { url: d.url || "/", action_urls: d.action_urls || {} },
  }));
});

self.addEventListener("notificationclick", (e) => {
  const data = e.notification.data || {};
  const url = (e.action && data.action_urls?.[e.action]) || data.url || "/";
  e.notification.close();
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((w) => new URL(w.url).origin === location.origin);
    if (win) {
      await win.focus();
      win.postMessage({ type: "open", url });
    } else {
      await self.clients.openWindow(url);
    }
  })());
});

async function cacheFirst(req, name, isTile) {
  const cache = await caches.open(name);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const r = await fetch(req);
    if (r.ok || r.type === "opaque") {
      await cache.put(req, r.clone());
      if (isTile) trim(cache);
    }
    return r;
  } catch {
    return new Response("", { status: 504 });
  }
}

async function trim(cache) {
  const keys = (await cache.keys()).filter((k) => k.url.includes("tile.openstreetmap.org"));
  for (const k of keys.slice(0, Math.max(0, keys.length - MAX_TILES))) await cache.delete(k);
}
