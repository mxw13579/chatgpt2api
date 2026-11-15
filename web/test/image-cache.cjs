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

function loadImageCache(fetcher) {
  global.fetch = fetcher;
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
    return require(name);
  };
  new Function("require", "module", "exports", compiled)(requireModule, module, module.exports);
  return module.exports;
}

test("expired and paused R2 images are never fetched", async () => {
  let requests = 0;
  const cache = loadImageCache(async () => { requests++; throw new Error("unexpected request"); });
  for (const flags of [{ expires_at: "2000-01-01T00:00:00Z" }, { expired: true }, { unavailable: true }, { expires_at: "invalid" }]) {
    const reference = { image_id: "images/fixture.png", ...flags };
    await assert.rejects(cache.readImageBlob("https://images.example.test/images/fixture.png", reference));
  }
  assert.equal(requests, 0);
});

test("explicit R2 download bypasses credentials, HTTP cache and redirects", async () => {
  let requests = 0;
  const cache = loadImageCache(async (_url, options) => {
    requests++;
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    return new Response(new Blob(["synthetic image"]), { status: 200 });
  });
  const reference = { image_id: "images/fixture.png", expires_at: "2099-01-01T00:00:00Z" };
  assert.equal((await cache.readImageBlob("https://images.example.test/images/fixture.png", reference)).size, 15);
  assert.equal(requests, 1);
});
