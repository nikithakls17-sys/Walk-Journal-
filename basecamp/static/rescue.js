// Walk Journal rescue: send offline items stranded on an expired link to the current Base Camp.
// Run it on the OLD link's page (via a bookmarklet). It reads this phone's offline queue for that link,
// sends quest starts, photos (with the time and GPS saved at the shutter) and finishes, in the order they
// happened, and shows what it did. Nothing is deleted from the phone.
(async () => {
  const BASE = new URL(document.currentScript ? document.currentScript.src : location.href).origin;
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;inset:12px;z-index:99999;overflow:auto;background:#FBF5E6;color:#4A3B2C;" +
    "font:16px/1.5 system-ui,sans-serif;padding:18px;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,.35)";
  document.body.appendChild(box);
  const say = (html) => { const p = document.createElement("p"); p.innerHTML = html; p.style.margin = "6px 0"; box.appendChild(p); };
  say("<b>🌿 Walk Journal rescue</b>");
  say(`Sending to: ${BASE}`);
  if (BASE === location.origin) {
    say("⚠️ Open the <b>old</b> link first, then tap the bookmark. (This is already the new link.)");
    return;
  }

  let db;
  try {
    db = await new Promise((resolve, reject) => {
      const r = indexedDB.open("walk-journal");
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  } catch (e) {
    return say(`Couldn't open this phone's saved items: ${e}`);
  }
  if (!db.objectStoreNames.contains("outbox")) return say("No saved items on this link. Nothing to rescue.");
  const jobs = await new Promise((resolve, reject) => {
    const req = db.transaction("outbox").objectStore("outbox").getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
  jobs.sort((a, b) => a.createdAt - b.createdAt);
  const photos = jobs.filter((j) => j.kind === "capture").length;
  say(`Found <b>${jobs.length}</b> saved item(s): ${photos} photo(s).`);
  if (!jobs.length) return say("Nothing to rescue on this link.");

  let ok = 0, failed = 0;
  for (const j of jobs) {
    try {
      if (j.kind === "capture") {
        const fd = new FormData();
        fd.append("task_id", j.taskId);
        fd.append("client_id", j.clientId);  // same id as the original, so a photo is never stored twice
        fd.append("taken_at", j.takenAt);
        if (j.fix) { fd.append("lat", j.fix.lat); fd.append("lng", j.fix.lng); fd.append("accuracy", j.fix.accuracy); }
        fd.append("source", j.source || "camera");
        if (j.fileModified) fd.append("file_modified", j.fileModified);
        fd.append("photo", j.blob, "photo.jpg");
        await fetch(`${BASE}/api/quests/${j.questId}/captures`, { method: "POST", mode: "no-cors", body: fd });
        say(`📸 photo sent (taken ${new Date(j.takenAt).toLocaleTimeString()})`);
      } else {
        const fd = new FormData();
        fd.append("job", JSON.stringify({ kind: j.kind, questId: j.questId, body: j.body || {} }));
        await fetch(`${BASE}/api/rescue/job`, { method: "POST", mode: "no-cors", body: fd });
        say(`✓ quest ${j.kind} sent`);
      }
      ok++;
    } catch (e) {
      failed++;
      say(`✗ couldn't send a ${j.kind}: ${e.message || e}`);
    }
  }
  say(`<b>Done.</b> Sent ${ok} item(s)${failed ? `, ${failed} couldn't be sent (try again in a minute)` : ""}.`);
  say("Nothing was deleted from this phone. Photos are checked on Base Camp now; open the new link's Journal in a few minutes.");
  const close = document.createElement("button");
  close.textContent = "Close";
  close.style.cssText = "margin-top:10px;padding:10px 18px;border-radius:12px;border:0;background:#7BA05B;color:#fff;font-weight:700";
  close.onclick = () => box.remove();
  box.appendChild(close);
})();
