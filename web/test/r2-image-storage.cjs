// Pure synthetic fixtures only. Main thread may run with the existing web dependencies.
// No real configuration, browser, HTTP service or Cloudflare access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

function load(file, mocks = {}) {
  const source = fs.readFileSync(path.join(__dirname, "../src", file), "utf8");
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)((name) => {
    if (name in mocks) return mocks[name];
    if (name === "@/lib/image-reference") return load("lib/image-reference.ts");
    throw new Error(`Unmocked import: ${name}`);
  }, module, module.exports);
  return module.exports;
}

const reference = {
  image_id: "images/2099/01/01/00000000000000000000000000000001.png",
  account_id: "fixture-account", expires_at: "2099-01-01T01:00:00Z", storage: "r2",
};
function conversation() {
  return {
    id: "conversation-fixture", title: "synthetic", model: "gpt-image-2",
    createdAt: "2099-01-01T00:00:00Z", updatedAt: "2099-01-01T00:00:00Z",
    turns: [{
      id: "turn-fixture", prompt: "synthetic", model: "gpt-image-2", mode: "edit",
      referenceImages: [{ ...reference, name: "reference.png", type: "image/png", dataUrl: "https://old.example.test/images/fixture.png" }],
      maskImages: [], count: 1, size: "1024x1024", ratio: "1:1", tier: "1k", quality: "auto", status: "success",
      createdAt: "2099-01-01T00:00:00Z",
      images: [{ ...reference, id: "result-fixture", taskId: "task-fixture", status: "success", url: "https://old.example.test/images/fixture.png", b64_json: "must-not-persist" }],
    }],
  };
}

test("ordinary settings saves omit the entire stale R2 config without mutating the form", async () => {
  let request;
  const api = load("lib/api.ts", { "@/lib/request": { httpRequest: async (url, options) => { request = { url, ...options }; return {}; } } });
  const config = { proxy: "", image_retention_days: 12, image_storage: { mode: "r2", r2_accounts: [{ id: "stale" }] } };
  await api.updateSettingsConfig(config);
  assert.equal(request.url, "/api/settings");
  assert.equal(request.method, "POST");
  assert.equal("image_storage" in request.body, false);
  assert.equal(request.body.image_retention_days, 12);
  assert.equal(config.image_storage.mode, "r2");
});

for (const [fill, expected] of [
  [0, "00000000-0000-4000-8000-000000000000"],
  [255, "ffffffff-ffff-4fff-bfff-ffffffffffff"],
]) {
  test(`HTTP account creation needs no randomUUID and preserves UUID v4 bits (${fill})`, (t) => {
    const previous = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    let calls = 0;
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: {
      getRandomValues(bytes) {
        calls += 1;
        assert.ok(bytes instanceof Uint8Array);
        assert.equal(bytes.length, 16);
        return bytes.fill(fill);
      },
    } });
    t.after(() => Object.defineProperty(globalThis, "crypto", previous));
    assert.equal(crypto.randomUUID, undefined);
    const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: () => assert.fail("Draft creation must not request the server") } });
    const account = api.newR2Account();
    assert.equal(account.id, expected);
    assert.equal(account.enabled, false);
    assert.equal(calls, 1);
  });
}

test("new account drafts receive distinct canonical UUID v4 identifiers", () => {
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: () => assert.fail("No request expected") } });
  const first = api.newR2Account();
  const second = api.newR2Account();
  for (const { id } of [first, second]) {
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  }
  assert.notEqual(first.id, second.id);
});

test("dedicated settings keep account fields and blank secrets", async () => {
  let body;
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async (_url, options) => { body = options.body; return {}; } } });
  const account = {
    ...api.newR2Account(), id: "fixture-account", account_id: "0".repeat(32), bucket: "fixture-images",
    has_secret_access_key: true, secret_access_key: "", public_base_url: "https://images.example.test",
  };
  assert.equal(account.enabled, false);
  assert.equal(account.monthly_upload_limit, 900_000);
  const settings = { ...api.DEFAULT_IMAGE_STORAGE, mode: "r2", enabled: false, r2_accounts: [account] };
  await api.saveImageStorageSettings(settings);
  assert.equal(body.enabled, true);
  assert.deepEqual(body.r2_accounts, [account]);
  await api.saveImageStorageSettings({ ...settings, mode: "local", enabled: false });
  assert.equal(body.r2_accounts.length, 1);
  assert.equal(body.enabled, false);
});

test("settings notification comparison ignores allocation-only changes but includes image access", () => {
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async () => assert.fail("No request expected") } });
  const before = { ...api.DEFAULT_IMAGE_STORAGE, mode: "r2", enabled: true, r2_accounts: [{ ...api.newR2Account(), id: "fixture-account" }] };
  const unchanged = structuredClone(before);
  assert.equal(api.imageStorageSettingsAffectImages(before, unchanged), false);
  const allocationOnly = {
    ...unchanged, retention_minutes: 120, cleanup_interval_seconds: 120, cache_ttl_seconds: 120,
    r2_accounts: unchanged.r2_accounts.map((account) => ({
      ...account, enabled: !account.enabled, name: "renamed fixture", monthly_upload_limit: 10, secret_access_key: "synthetic-rotation",
    })),
  };
  assert.equal(api.imageStorageSettingsAffectImages(before, allocationOnly), false);
  for (const patch of [
    { mode: "local", enabled: false }, { public_base_url: "https://images.example.test" },
    { webdav_url: "https://dav.example.test" }, { webdav_root_path: "fixture/images" },
    { webdav_username: "fixture-user" }, { webdav_password: "synthetic-password" },
    { r2_accounts: [{ ...before.r2_accounts[0], public_base_url: "https://new.example.test" }] },
    { r2_accounts: [] },
  ]) {
    assert.equal(api.imageStorageSettingsAffectImages(before, { ...before, ...patch }), true);
  }
  assert.deepEqual(before, unchanged);
});

test("storage management request wrappers", async () => {
  const calls = [];
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async (url, options = {}) => { calls.push({ url, method: options.method || "GET", body: options.body }); return {}; } } });
  const id = "fixture-account";
  await api.getImageStorageOverview();
  await api.testR2Account(id);
  await api.cleanupExpiredImages();
  await api.cleanupLegacyImages();
  await api.resolveImageReferences([{ ...reference, url: "https://old.example.test/image.png" }]);
  assert.deepEqual(calls.map(({ url, method }) => [method, url]), [
    ["GET", "/api/image-storage"], ["POST", `/api/image-storage/accounts/${id}/test`],
    ["POST", "/api/image-storage/cleanup"], ["POST", "/api/image-storage/legacy/cleanup"],
    ["POST", "/api/image-storage/resolve"],
  ]);
  assert.deepEqual(calls[3].body, { confirm: true });
  assert.deepEqual(calls[4].body, { images: [{ image_id: reference.image_id, account_id: reference.account_id }] });
});

test("reference refresh updates result and reused reference URLs while retaining identity and task metadata", async () => {
  const api = load("lib/image-history.ts", { "@/lib/image-storage": { resolveImageReferences: async (images) => ({ items: images.map((image) => ({ ...image, url: "https://new.example.test/images/fixture.png", expires_at: reference.expires_at })) }) } });
  const source = conversation();
  const [result] = await api.refreshConversationReferences([source]);
  assert.equal(result.updatedAt, source.updatedAt);
  assert.equal(result.turns[0].images[0].url, "https://new.example.test/images/fixture.png");
  assert.equal(result.turns[0].images[0].image_id, reference.image_id);
  assert.equal(result.turns[0].images[0].taskId, "task-fixture");
  assert.equal(result.turns[0].images[0].expires_at, reference.expires_at);
  assert.equal(result.turns[0].images[0].b64_json, undefined);
  assert.equal(result.turns[0].referenceImages[0].dataUrl, "https://new.example.test/images/fixture.png");
  assert.equal(source.turns[0].images[0].url, "https://old.example.test/images/fixture.png");
  const [unavailable] = api.applyResolvedReferences([source], []);
  assert.equal(unavailable.turns[0].images[0].url, "");
  assert.equal(unavailable.turns[0].images[0].unavailable, true);
  const [restored] = api.applyResolvedReferences([unavailable], [{ ...reference, url: "https://new.example.test/images/fixture.png" }]);
  assert.equal(restored.turns[0].images[0].unavailable, false);
});

test("browser conversation reload retains R2 references but strips their bodies; legacy base64 survives", async () => {
  const records = new Map();
  const storage = {
    getItem: async (key) => structuredClone(records.get(key) || null),
    setItem: async (key, value) => records.set(key, structuredClone(value)),
    removeItem: async (key) => records.delete(key),
  };
  const mocks = { localforage: { __esModule: true, default: { createInstance: () => storage } } };
  const firstPage = load("store/image-conversations.ts", mocks);
  const source = conversation();
  source.turns[0].images.push({ id: "legacy", status: "success", b64_json: "c3ludGhldGlj" });
  source.turns[0].referenceImages[0].dataUrl = "data:image/png;base64,must-not-persist";
  await firstPage.saveImageConversation(source);
  const reloaded = load("store/image-conversations.ts", mocks);
  const [restored] = await reloaded.listImageConversations();
  assert.equal(restored.turns[0].images[0].image_id, reference.image_id);
  assert.equal(restored.turns[0].images[0].expires_at, reference.expires_at);
  assert.equal(restored.turns[0].images[0].b64_json, undefined);
  assert.equal(restored.turns[0].referenceImages[0].dataUrl, "");
  assert.equal(restored.turns[0].referenceImages[0].image_id, reference.image_id);
  assert.equal(restored.turns[0].images[1].b64_json, "c3ludGhldGlj");
});

test("R2 expiry boundary and unknown/malformed expiry do not mean permanent access", () => {
  const helpers = load("lib/image-reference.ts");
  const deadline = Date.parse(reference.expires_at);
  assert.equal(helpers.isImageExpired(reference, deadline - 1), false);
  assert.equal(helpers.isImageExpired(reference, deadline), true);
  assert.equal(helpers.isImageExpired({ expires_at: "invalid" }), true);
  assert.equal(helpers.isImageUnavailable({ unavailable: true }), true);
  assert.equal(helpers.isImageUnavailable({ storage: "local" }), false);
  const inferred = helpers.r2ReferenceFromUrl(`https://old.example.test/${reference.image_id}`);
  assert.equal(inferred.image_id, reference.image_id);
  assert.equal(helpers.r2ReferenceFromUrl("https://legacy.example.test/images/legacy.png"), undefined);
});
