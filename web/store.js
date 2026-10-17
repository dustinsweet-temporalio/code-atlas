// IndexedDB store for conversations: the system of record for chat transcripts.
// One object store, one record per conversation (keyed by its id, which is also the
// server's session id). No build step: loaded before app.js, exposes window.ChatStore.
window.ChatStore = (() => {
  const DB = "code-atlas", STORE = "conversations", VERSION = 1;
  let dbp = null;

  const open = () => dbp ||= new Promise((resolve, reject) => {
    if (!window.indexedDB) { reject(new Error("IndexedDB is not available")); return; }
    const req = indexedDB.open(DB, VERSION);
    req.onupgradeneeded = () => {
      const s = req.result.createObjectStore(STORE, { keyPath: "id" });
      s.createIndex("updated", "updated");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("IndexedDB is blocked by another tab"));
  });

  // Runs fn(store) in one transaction and resolves, once it commits, with the result of
  // the request fn returns (a request's .result is only readable after its success event).
  const tx = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const req = fn(t.objectStore(STORE));
      let result;
      if (req) req.onsuccess = () => { result = req.result; };
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error("IndexedDB transaction aborted"));
    });
  };

  return {
    available: async () => { try { await open(); return true; } catch { return false; } },
    loadAll: async () => ((await tx("readonly", (s) => s.getAll())) || [])
      .sort((a, b) => (b.updated || 0) - (a.updated || 0)),
    put: (conv) => { conv.updated = Date.now(); return tx("readwrite", (s) => s.put(conv)); },
    putAll: (convs) => tx("readwrite", (s) => { convs.forEach((c) => s.put(c)); }),
    remove: (id) => tx("readwrite", (s) => s.delete(id)),
    // Ask the browser not to evict this site's storage under pressure (best effort).
    persist: async () => { try { return navigator.storage?.persist ? await navigator.storage.persist() : false; } catch { return false; } },
  };
})();
