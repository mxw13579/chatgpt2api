const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "../src/lib/image-cache.ts"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    esModuleInterop: true,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

global.window = { location: { origin: "https://app.test" } };
let objectUrlIndex = 0;
const revokedUrls = [];
URL.createObjectURL = () => `blob:cached-${++objectUrlIndex}`;
URL.revokeObjectURL = (url) => revokedUrls.push(url);

function loadImageCache(records, fetcher) {
  global.fetch = fetcher;
  const storage = {
    getItem: async (key) => records.get(key) ?? null,
    setItem: async (key, value) => {
      records.set(key, value);
      return value;
    },
    removeItem: async (key) => records.delete(key),
    clear: async () => records.clear(),
  };
  const module = { exports: {} };
  const requireModule = (name) => {
    if (name === "@/lib/image-reference") {
      const helper = { exports: {} };
      const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src/lib/image-reference.ts"), "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      }).outputText;
      new Function("require", "module", "exports", code)(requireModule, helper, helper.exports);
      return helper.exports;
    }
    if (name === "localforage") {
      return { __esModule: true, default: { createInstance: () => storage } };
    }
    return require(name);
  };
  new Function("require", "module", "exports", compiled)(requireModule, module, module.exports);
  return module.exports;
}

test("persistent cache serves a server image after a simulated page reload without another request", async () => {
  const records = new Map();
  let fetchCount = 0;
  const fetcher = async () => {
    fetchCount += 1;
    return new Response(new Blob(["image-a"], { type: "image/png" }), { status: 200 });
  };

  const firstPage = loadImageCache(records, fetcher);
  const first = await firstPage.resolveCachedImage("/images/a.png");
  assert.equal(first.fromCache, false);
  assert.equal(fetchCount, 1);
  assert.equal(records.size, 1);

  const reloadedPage = loadImageCache(records, fetcher);
  const second = await reloadedPage.resolveCachedImage("/images/a.png");
  assert.equal(second.fromCache, true);
  assert.equal(fetchCount, 1);
  assert.match(second.src, /^blob:cached-/);
});

test("concurrent image consumers share one server request", async () => {
  const records = new Map();
  let fetchCount = 0;
  const cache = loadImageCache(records, async () => {
    fetchCount += 1;
    await Promise.resolve();
    return new Response(new Blob(["image-b"], { type: "image/png" }), { status: 200 });
  });

  const [thumbnail, lightbox] = await Promise.all([
    cache.resolveCachedImage("/images/b.png"),
    cache.resolveCachedImage("/images/b.png"),
  ]);
  assert.equal(fetchCount, 1);
  assert.equal(thumbnail.src, lightbox.src);
});

test("R2 display uses its source URL and never reads or stores a persistent body", async () => {
  const records = new Map();
  const url = "https://images.example.test/images/2099/01/01/fixture.png";
  records.set(`image-cache:v1:${url}`, { version: 1, source: url, blob: new Blob(["old cache"]), cachedAt: 0 });
  let requests = 0;
  const cache = loadImageCache(records, async () => { requests++; throw new Error("unexpected request"); });
  const reference = { image_id: "images/2099/01/01/fixture.png", account_id: "fixture", expires_at: "2099-01-01T01:00:00Z" };
  const result = await cache.resolveCachedImage(url, reference);
  assert.equal(result.src, url);
  assert.equal(result.blob, null);
  assert.equal(result.fromCache, false);
  assert.equal(cache.getImmediateCachedImageSource(url, reference), url);
  assert.equal(requests, 0);
  assert.equal(records.size, 1);
});

test("expired and paused R2 images neither fetch nor return old cached bodies", async () => {
  const records = new Map();
  let requests = 0;
  const cache = loadImageCache(records, async () => { requests++; throw new Error("unexpected request"); });
  for (const flags of [{ expires_at: "2000-01-01T00:00:00Z" }, { expired: true }, { unavailable: true }, { expires_at: "invalid" }]) {
    const reference = { image_id: "images/fixture.png", ...flags };
    assert.equal(cache.getImmediateCachedImageSource("https://images.example.test/images/fixture.png", reference), null);
    await assert.rejects(cache.resolveCachedImage("https://images.example.test/images/fixture.png", reference));
    await assert.rejects(cache.readImageBlob("https://images.example.test/images/fixture.png", reference));
  }
  assert.equal(requests, 0);
  assert.equal(records.size, 0);
});

test("explicit R2 download is transient and does not populate either image cache", async () => {
  const records = new Map();
  let requests = 0;
  const cache = loadImageCache(records, async (_url, options) => {
    requests++;
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    return new Response(new Blob(["synthetic image"]), { status: 200 });
  });
  const reference = { image_id: "images/fixture.png", expires_at: "2099-01-01T00:00:00Z" };
  const url = "https://images.example.test/images/fixture.png";
  assert.equal((await cache.readImageBlob(url, reference)).size, 15);
  assert.equal(requests, 1);
  assert.equal(records.size, 0);
  assert.equal(cache.getImmediateCachedImageSource(url), null);
});

test("deleting an image removes its persistent entry and revokes its Blob URL", async () => {
  const records = new Map();
  const cache = loadImageCache(
    records,
    async () => new Response(new Blob(["image-c"], { type: "image/png" }), { status: 200 }),
  );
  const loaded = await cache.resolveCachedImage("/images/c.png");
  assert.equal(records.size, 1);

  await cache.deleteCachedImageSources(["/images/c.png"]);
  assert.equal(records.size, 0);
  assert.ok(revokedUrls.includes(loaded.src));
});
