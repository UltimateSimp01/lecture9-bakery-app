import { loadPyodide } from "./runtime/pyodide.mjs";

const base = new URL("./", import.meta.url);
const sessionKey = base.pathname;
const files = [
  "app.py", "bridge.py", "existing_orders.csv",
  ...["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    .map(day => `InstagramOrders_${day}.html`),
];
const imagePath = "/bakery/preview.sqlite3";
const stale = "The dashboard has changed. Review the latest orders before trying again.";
let runtime;

async function startRuntime() {
  self.postMessage({ progress: "Loading the Python order desk…" });
  const py = await loadPyodide({ indexURL: new URL("./runtime/", base).href });
  self.postMessage({ progress: "Loading the course orders and messages…" });
  py.FS.mkdirTree("/bakery");
  await Promise.all(files.map(async name => {
    const response = await fetch(new URL(name, base));
    if (!response.ok) throw new Error(`Could not load ${name} (HTTP ${response.status}).`);
    py.FS.writeFile(`/bakery/${name}`, new Uint8Array(await response.arrayBuffer()));
  }));
  const dispatch = py.runPython("import sys\nsys.path.insert(0, '/bakery')\nfrom bridge import dispatch\ndispatch");
  return { py, dispatch };
}

async function getRuntime() {
  if (!runtime) {
    runtime = startRuntime().catch(error => {
      runtime = undefined;
      throw new Error(`The preview could not start. Check your connection and reload the page. ${error.message}`);
    });
  }
  return runtime;
}

function openStorage() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("mgoc50-bakery-preview", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("sessions");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("Browser storage is unavailable. Allow site storage and reload the page."));
    request.onblocked = () => reject(new Error("Close other preview tabs and reload this page to open browser storage."));
  });
}

// The read and conditional write run inside one IndexedDB transaction across all tabs.
function accessSession(db, update) {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("sessions", update ? "readwrite" : "readonly");
    const store = transaction.objectStore("sessions");
    const request = store.get(sessionKey);
    let result;
    request.onsuccess = () => {
      try {
        result = update ? update(store, request.result) : request.result;
      } catch (error) {
        reject(error);
        transaction.abort();
      }
    };
    transaction.oncomplete = () => resolve(result);
    transaction.onabort = () => reject(new Error(
      "The preview could not access browser storage. Check available space and reload the page."
    ));
  });
}

function compareAndSave(db, expected, next) {
  return accessSession(db, (store, previous) => {
    if (expected === null ? previous !== undefined : previous?.version !== expected) return false;
    store.put(next, sessionKey);
    return true;
  });
}

function run(engine, action, payload = null) {
  const result = JSON.parse(engine.dispatch(action, JSON.stringify(payload)));
  if (!result.ok) throw new Error(result.error);
  return result.value;
}

function snapshot(engine, version) {
  // FS.readFile returns a separate byte array after the Python connection has closed.
  return { version, bytes: engine.py.FS.readFile(imagePath) };
}

async function loadSession(db, engine) {
  let saved = await accessSession(db);
  if (saved === undefined) {
    const initial = run(engine, "seed");
    const fresh = snapshot(engine, initial.version);
    saved = await compareAndSave(db, null, fresh) ? fresh : await accessSession(db);
  }
  if (!Number.isInteger(saved?.version) || !(saved.bytes instanceof Uint8Array) || !saved.bytes.length) {
    throw new Error("The saved preview session could not be read.");
  }
  // Every request starts from the durable image, including the first request after a failed save.
  engine.py.FS.writeFile(imagePath, saved.bytes);
  return saved;
}

async function handle(action, payload) {
  if (action !== "state" && action !== "apply") throw new Error("Unknown order desk operation.");
  const engine = await getRuntime();
  const db = await openStorage();
  try {
    const saved = await loadSession(db, engine);
    if (action === "state") {
      const current = run(engine, "state");
      if (current.version !== saved.version) throw new Error("The saved preview version could not be verified.");
      return current;
    }
    if (payload?.version !== saved.version) throw new Error(stale);
    const result = run(engine, "apply", payload);
    const next = snapshot(engine, result.state.version);
    if (!await compareAndSave(db, saved.version, next)) throw new Error(stale);
    return result;
  } finally {
    db.close();
  }
}

// One worker owns one scratch file. Different tabs coordinate through the durable version check.
let queue = Promise.resolve();
self.addEventListener("message", ({ data }) => {
  queue = queue.then(async () => {
    try {
      const value = await handle(data.action, data.payload);
      self.postMessage({ id: data.id, ok: true, value });
    } catch (error) {
      self.postMessage({ id: data.id, ok: false, error: error.message || "The preview could not complete this action." });
    }
  });
});
