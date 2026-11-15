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
test("ordinary settings saves omit the entire stale R2 config without mutating the form", async () => {
  let request;
  const api = load("lib/api.ts", { "@/lib/request": { httpRequest: async (url, options) => { request = { url, ...options }; return {}; } } });
  const config = { proxy: "", image_retention_days: 12, image_storage: { r2_accounts: [{ id: "stale" }] } };
  await api.updateSettingsConfig(config);
  assert.equal(request.url, "/api/settings");
  assert.equal(request.method, "POST");
  assert.equal("image_storage" in request.body, false);
  assert.equal(request.body.image_retention_days, 12);
  assert.deepEqual(config.image_storage, { r2_accounts: [{ id: "stale" }] });
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
  const settings = { ...api.DEFAULT_IMAGE_STORAGE, r2_accounts: [account] };
  await api.saveImageStorageSettings(settings);
  assert.deepEqual(body, settings);
  assert.deepEqual(Object.keys(body).sort(), ["cache_ttl_seconds", "cleanup_interval_seconds", "r2_accounts", "retention_minutes"]);
});

test("settings notification comparison ignores allocation-only changes but includes image access", () => {
  const api = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async () => assert.fail("No request expected") } });
  const before = { ...api.DEFAULT_IMAGE_STORAGE, r2_accounts: [{ ...api.newR2Account(), id: "fixture-account" }] };
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
  await api.resolveImageReferences([{ ...reference, url: "https://old.example.test/image.png" }]);
  await api.provisionR2Account("fixture-token", "img.example.test");
  assert.deepEqual(calls.pop(), { url: "/api/image-storage/accounts/provision", method: "POST", body: { api_token: "fixture-token", domain: "img.example.test" } });
  assert.deepEqual(calls.map(({ url, method }) => [method, url]), [
    ["GET", "/api/image-storage"], ["POST", `/api/image-storage/accounts/${id}/test`],
    ["POST", "/api/image-storage/cleanup"], ["POST", "/api/image-storage/resolve"],
  ]);
  assert.deepEqual(calls[3].body, { images: [{ image_id: reference.image_id, account_id: reference.account_id }] });
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
