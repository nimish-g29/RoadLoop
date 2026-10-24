// Offline-first upload queue in IndexedDB. Every detection is saved locally first, then sent.
// client_id makes uploads idempotent on the server, so retries can never double-count.
import { api } from "./api.js";

const DB = "roadloop", STORE = "uploads";

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "meta.client_id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx(mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const result = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(result?.result ?? result);
    t.onerror = () => reject(t.error);
  });
}

// Fallback for browsers with IndexedDB disabled (private mode): memory only.
const memory = new Map();
let useMemory = false;

export const queue = {
  async add(item) {
    item.createdAt ??= Date.now();
    item.attempts ??= 0;
    if (!useMemory) {
      try {
        await tx("readwrite", (s) => s.put(item));
        return;
      } catch {
        useMemory = true;
      }
    }
    memory.set(item.meta.client_id, item);
  },
  async all() {
    if (useMemory) return [...memory.values()];
    try {
      return await tx("readonly", (s) => s.getAll());
    } catch {
      useMemory = true;
      return [...memory.values()];
    }
  },
  async remove(id) {
    if (useMemory) return memory.delete(id);
    return tx("readwrite", (s) => s.delete(id));
  },
};

export class Uploader extends EventTarget {
  constructor() {
    super();
    this.busy = false;
    this.pending = 0;
    window.addEventListener("online", () => this.flush());
    setInterval(() => this.flush(), 15000);
  }

  async add(meta, blob) {
    await queue.add({ meta, blob });
    await this.flush();
  }

  async refreshCount() {
    this.pending = (await queue.all()).length;
    this.dispatchEvent(new CustomEvent("count", { detail: this.pending }));
    return this.pending;
  }

  async flush() {
    if (this.busy) return;
    this.busy = true;
    try {
      const items = (await queue.all()).sort((a, b) => a.createdAt - b.createdAt);
      for (const it of items) {
        try {
          const res = await api.uploadDetection(it.meta, it.blob);
          await queue.remove(it.meta.client_id);
          this.dispatchEvent(new CustomEvent("uploaded", { detail: { meta: it.meta, res } }));
        } catch (e) {
          if (e.permanent) {
            await queue.remove(it.meta.client_id); // e.g. rider unknown after a DB reset
            this.dispatchEvent(new CustomEvent("rejected", { detail: { meta: it.meta, error: e } }));
          } else {
            break; // offline or server busy: keep order, try again later
          }
        }
      }
    } finally {
      this.busy = false;
      await this.refreshCount();
    }
  }
}
