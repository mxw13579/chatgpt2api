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

test("dedicated settings keep all account budgets, cycle, domains and blank secrets", async () => {
  let body;
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async (_url, options) => { body = options.body; return {}; } } });
  const account = {
    ...api.newR2Account(), id: "fixture-account", account_id: "0".repeat(32), bucket: "fixture-images",
    has_api_token: true, api_token: "", billing_cycle_day: 17, billing_cycle_confirmed: true,
    domains: [{ id: "images.example.test", domain: "images.example.test", zone_id: "1".repeat(32), enabled: false }],
  };
  assert.equal(account.enabled, false);
  const settings = { ...api.DEFAULT_IMAGE_STORAGE, mode: "r2", enabled: false, r2_accounts: [account], cors_origins: ["https://app.example.test"] };
  await api.saveImageStorageSettings(settings);
  assert.equal(body.enabled, true);
  assert.deepEqual(body.r2_accounts, [account]);
  assert.equal(body.r2_accounts[0].api_token, "");
  assert.equal(body.r2_accounts[0].capacity_bytes, 8_000_000_000);
  await api.saveImageStorageSettings({ ...settings, mode: "local", enabled: false });
  assert.equal(body.r2_accounts.length, 1);
  assert.equal(body.enabled, false);
});

test("settings notification comparison ignores allocation-only changes but includes existing image access", () => {
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async () => assert.fail("No request expected") } });
  const before = { ...api.DEFAULT_IMAGE_STORAGE, mode: "r2", enabled: true, r2_accounts: [{ ...api.newR2Account(), id: "fixture-account" }] };
  const unchanged = structuredClone(before);
  assert.equal(api.imageStorageSettingsAffectImages(before, unchanged), false);
  const allocationOnly = {
    ...unchanged, retention_minutes: 120, cleanup_interval_seconds: 120, usage_refresh_seconds: 600,
    usage_max_age_seconds: 1200, cache_ttl_seconds: 120, cors_origins: ["https://app.example.test"],
    r2_accounts: unchanged.r2_accounts.map((account) => ({
      ...account, enabled: !account.enabled, name: "renamed fixture", a_budget: 400_000, b_budget: 500_000,
      b_warn: 400_000, capacity_bytes: 2_000_000_000, billing_cycle_day: 12, billing_cycle_confirmed: true,
      rate_limit_requests: 80, restrict_methods: false, api_token: "synthetic-rotation",
    })),
  };
  assert.equal(api.imageStorageSettingsAffectImages(before, allocationOnly), false);
  for (const patch of [
    { mode: "local", enabled: false }, { public_base_url: "https://images.example.test" },
    { webdav_url: "https://dav.example.test" }, { webdav_root_path: "fixture/images" },
    { webdav_username: "fixture-user" }, { webdav_password: "synthetic-password" },
  ]) {
    assert.equal(api.imageStorageSettingsAffectImages(before, { ...before, ...patch }), true);
  }
  assert.deepEqual(before, unchanged);
});

test("all management buttons have real frozen-contract request wrappers", async () => {
  const calls = [];
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async (url, options = {}) => { calls.push({ url, method: options.method || "GET", body: options.body }); return {}; } } });
  const id = "fixture-account";
  const prefix = `/api/image-storage/accounts/${id}`;
  await api.getImageStorageOverview();
  await api.testR2Account(id);
  await api.refreshR2Usage(id);
  await api.getR2Zones(id);
  await api.getR2Domains(id);
  await api.addR2Domain(id, "images.example.test", "1".repeat(32));
  await api.updateR2Domain(id, "images.example.test", { active: true });
  await api.updateR2Domain(id, "images.example.test", { enabled: false });
  await api.deleteR2Domain(id, "images.example.test");
  await api.checkR2Domains(id, "images.example.test");
  await api.applyR2Protection(id, { rate_limit_requests: 120, restrict_methods: true, cors_origins: ["*"] });
  await api.restoreR2Access(id);
  await api.cleanupExpiredImages();
  await api.cleanupLegacyImages();
  await api.resolveImageReferences([{ ...reference, api_token: "synthetic-not-allowed", url: "https://old.example.test/image.png" }]);
  assert.deepEqual(calls.map(({ url, method }) => [method, url]), [
    ["GET", "/api/image-storage"], ["POST", `${prefix}/test`], ["POST", `${prefix}/usage/refresh`],
    ["GET", `${prefix}/zones`], ["GET", `${prefix}/domains`], ["POST", `${prefix}/domains`],
    ["PATCH", `${prefix}/domains/images.example.test`], ["PATCH", `${prefix}/domains/images.example.test`],
    ["DELETE", `${prefix}/domains/images.example.test`], ["POST", `${prefix}/domains/check`],
    ["PUT", `${prefix}/protection`], ["POST", `${prefix}/access/restore`],
    ["POST", "/api/image-storage/cleanup"], ["POST", "/api/image-storage/legacy/cleanup"],
    ["POST", "/api/image-storage/resolve"],
  ]);
  assert.deepEqual(calls[6].body, { active: true });
  assert.deepEqual(calls[7].body, { enabled: false });
  assert.deepEqual(calls[13].body, { confirm: true });
  assert.deepEqual(calls[14].body, { images: [{ image_id: reference.image_id, account_id: reference.account_id }] });
});

test("domain switching refreshes result and reused reference URLs while retaining identity and task metadata", async () => {
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
