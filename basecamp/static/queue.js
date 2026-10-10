// Offline-first outbox. Every capture is saved to IndexedDB with its photo, time and GPS the moment
// the shutter fires, then synced to Base Camp in order when the phone is back online.
const Outbox = (() => {
  const DB = "walk-journal";
  const STORE = "outbox";
  let dbp = null;

  function open() {
    if (dbp) return dbp;
    dbp = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbp;
  }

  async function tx(mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const out = fn(t.objectStore(STORE));
      t.oncomplete = () => resolve(out?.result ?? out);
      t.onerror = () => reject(t.error);
    });
  }

  const add = (job) => tx("readwrite", (s) => s.put({ ...job, id: job.id || crypto.randomUUID(), createdAt: Date.now() }));
  const remove = (id) => tx("readwrite", (s) => s.delete(id));
  const all = async () => (await tx("readonly", (s) => s.getAll())).sort((a, b) => a.createdAt - b.createdAt);

  let flushing = null;
  // send(job) -> response JSON; throws {network:true} to stop, or other errors to drop the job.
  async function flush(send, onDone) {
    if (flushing) return flushing;
    flushing = (async () => {
      let sent = 0;
      for (const job of await all()) {
        try {
          const res = await send(job);
          await remove(job.id);
          sent++;
          onDone?.(job, res, null);
        } catch (err) {
          if (err.network) break;  // still offline; keep the rest in order
          await remove(job.id);    // the server rejected it; don't retry forever
          onDone?.(job, null, err);
        }
      }
      return sent;
    })().finally(() => (flushing = null));
    return flushing;
  }

  return { add, all, remove, flush };
})();
