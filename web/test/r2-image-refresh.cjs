// Synthetic component callbacks only; main thread runs this with existing web dependencies.
// Compile complete TS modules, as in r2-image-storage.cjs. No source extraction, DOM,
// real auth, configuration, network requests or D-owned implementation dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

const elements = new Proxy({}, { get: (_target, name) => name });
const jsx = (type, props) => ({ type, props: props || {} });
function load(file, mocks) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)((name) => {
    if (name in mocks) return mocks[name];
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
    if (name === "lucide-react" || name.startsWith("@/components/")) return elements;
    throw new Error(`Unmocked import: ${name}`);
  }, module, module.exports);
  return module.exports;
}

// Minimal hook mock to invoke the real callbacks and effects, without adding a renderer dependency.
function hooks() {
  let current;
  const cell = () => current.cells[current.cursor++] ||= {};
  const same = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const react = {
    useState(initial) {
      const state = cell();
      if (!("value" in state)) state.value = typeof initial === "function" ? initial() : initial;
      return [state.value, (next) => { state.value = typeof next === "function" ? next(state.value) : next; }];
    },
    useMemo(create, deps) {
      const state = cell();
      if (!same(state.deps, deps)) { state.value = create(); state.deps = deps; }
      return state.value;
    },
    useCallback(callback, deps) { return react.useMemo(() => callback, deps); },
    useRef(value) { return react.useMemo(() => ({ current: value }), []); },
    useEffect(effect, deps) {
      const state = cell();
      if (!same(state.deps, deps)) {
        state.deps = deps;
        current.effects.push(() => { state.cleanup?.(); state.cleanup = effect(); });
      }
    },
  };
  return {
    react,
    mount(Component, props = {}) {
      const view = {
        cells: [], cursor: 0, effects: [], tree: null,
        render() {
          current = view; view.cursor = 0; view.effects = [];
          view.tree = Component(props);
          for (const effect of view.effects) effect();
          return view.tree;
        },
        dispose() { for (const state of view.cells) state.cleanup?.(); },
      };
      view.render();
      return view;
    },
  };
}
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object") return [];
  return [node, ...nodes(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (node === null || node === undefined || typeof node === "boolean") return "";
  return typeof node === "object" ? text(node.props?.children) : String(node);
}
function find(view, predicate) {
  const result = nodes(view.tree).find(predicate);
  assert.ok(result, "Expected rendered fixture element");
  return result;
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function act(view, callback) {
  callback(); view.render();
  await settle(); view.render();
}
async function click(view, label) {
  const button = find(view, (node) => node.type === "Button" && text(node) === label && !node.props.disabled);
  await act(view, () => button.props.onClick());
}
function browser(t) {
  const previous = { window: globalThis.window, fetch: globalThis.fetch };
  const window = new EventTarget();
  const writes = [];
  window.confirm = () => true;
  window.localStorage = { setItem: (...args) => writes.push(args) };
  globalThis.window = window;
  globalThis.fetch = async () => { throw new Error("Network forbidden in synthetic refresh fixture"); };
  t.after(() => {
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    globalThis.fetch = previous.fetch;
  });
  return { window, writes };
}
const quietToast = { toast: { success() {}, error() {} } };

async function storageCard(t) {
  const host = browser(t);
  const renderer = hooks();
  const library = load("lib/image-storage.ts", { "@/lib/request": { httpRequest: async () => assert.fail("Unexpected HTTP request") } });
  const domains = ["current", "spare", "disabled"].map((name) => ({
    id: `${name}.example.test`, domain: `${name}.example.test`, zone_id: "fixture-zone", enabled: name !== "disabled",
  }));
  let overview = {
    settings: { ...library.DEFAULT_IMAGE_STORAGE, mode: "both", enabled: true, r2_accounts: [{
      ...library.newR2Account(), id: "fixture-account", name: "Fixture", enabled: true,
      active_domain_id: domains[0].id, domains,
    }] },
    accounts: [{ id: "fixture-account", status: "eligible", usage: { managed_disabled: true }, domains: domains.map((item) => ({ ...item, protected: true })) }],
  };
  const failures = new Map();
  const calls = [];
  let notices = 0;
  host.window.addEventListener(library.IMAGE_STORAGE_CHANGED, () => { notices += 1; });
  const action = (name, result = { result: { ok: true, complete: true, removed: 1, uploaded: 1, skipped: 0, failed: 0 } }) => async (...args) => {
    calls.push(name);
    if (failures.get(name) === "reject") throw new Error("Synthetic action failure");
    if (failures.get(name) === "result") return { result: { ok: false, error: "Synthetic partial mutation" } };
    return typeof result === "function" ? result(...args) : structuredClone(result);
  };
  const api = { ...library,
    getImageStorageOverview: action("overview", () => structuredClone(overview)),
    saveImageStorageSettings: async (settings) => {
      calls.push("save");
      // Simulate a committed mutation whose response may subsequently fail.
      overview = { ...overview, settings: structuredClone(settings) };
      if (failures.has("save")) throw new Error("Synthetic save response failure");
      return structuredClone(overview);
    },
    getR2Domains: action("domains", { items: overview.accounts[0].domains, managed_enabled: false }),
    getR2Zones: action("zones", { items: [{ id: "fixture-zone", name: "example.test" }] }),
    checkR2Domains: action("checks", { items: [] }),
  };
  for (const name of ["testR2Account", "refreshR2Usage", "applyR2Protection", "restoreR2Access", "cleanupExpiredImages", "cleanupLegacyImages", "addR2Domain", "updateR2Domain", "deleteR2Domain"]) api[name] = action(name);
  const component = load("app/settings/components/image-storage-card.tsx", {
    react: renderer.react, sonner: quietToast, "@/lib/image-storage": api,
    "@/lib/api": { testImageStorageConnection: action("webdavTest"), syncImageStorage: action("webdavSync") },
    "../store": { useSettingsStore: { setState() {} } },
  });
  const view = renderer.mount(component.ImageStorageCard);
  await settle(); view.render();
  const domainNode = find(view, (node) => node.type?.name === "DomainManager");
  const domainView = renderer.mount(domainNode.type, domainNode.props);
  domainView.render();
  return { ...host, view, domainView, failures, calls, notices: () => notices };
}

test("read bindings/zones/checks and connection tests never broadcast, including errors", async (t) => {
  const fixture = await storageCard(t);
  assert.equal(fixture.notices(), 0, "initial admin overview is read-only");
  for (const [view, label, action] of [
    [fixture.view, "连接/权限测试", "testR2Account"], [fixture.view, "测试 WebDAV", "webdavTest"],
    [fixture.domainView, "读取绑定 / 证书状态", "domains"], [fixture.domainView, "读取可用 Zone", "zones"],
    [fixture.domainView, "服务器立即检查", "checks"], [fixture.domainView, "服务器检查", "checks"],
  ]) {
    await click(view, label);
    fixture.failures.set(action, "reject");
    await click(view, label);
    fixture.failures.delete(action);
    assert.equal(fixture.notices(), 0, label);
  }
  // Binding an initially disabled spare does not alter existing image links either.
  await act(fixture.domainView, () => {
    find(fixture.domainView, (node) => node.props["aria-label"] === "新增域名").props.onChange({ target: { value: "new.example.test" } });
    find(fixture.domainView, (node) => node.type === "Select").props.onValueChange("fixture-zone");
  });
  await click(fixture.domainView, "绑定（初始停用）");
  assert.equal(fixture.notices(), 0);
  fixture.failures.set("overview", "reject");
  await click(fixture.view, "重新读取");
  assert.equal(fixture.notices(), 0, "failed admin refresh is not an image mutation");
  assert.deepEqual(fixture.writes, []);
  fixture.domainView.dispose(); fixture.view.dispose();
});

test("image-affecting actions notify on success and partial failure, even if status reload also fails", async (t) => {
  const fixture = await storageCard(t);
  for (const [view, label, action] of [
    [fixture.view, "刷新用量", "refreshR2Usage"], [fixture.view, "刷新全部用量", "refreshR2Usage"],
    [fixture.view, "应用防护与 CORS", "applyR2Protection"], [fixture.view, "恢复公开访问", "restoreR2Access"],
    [fixture.view, "立即清理到期 / 预占对象", "cleanupExpiredImages"],
    [fixture.view, "显式清理旧本地图 / 任务图片内容", "cleanupLegacyImages"], [fixture.view, "全量同步", "webdavSync"],
    [fixture.domainView, "设为当前", "updateR2Domain"], [fixture.domainView, "停用", "updateR2Domain"],
    [fixture.domainView, "启用", "updateR2Domain"], [fixture.domainView, "解绑", "deleteR2Domain"],
  ]) {
    const before = fixture.notices();
    await click(view, label);
    assert.equal(fixture.notices(), before + 1, label);
    fixture.failures.set(action, action === "applyR2Protection" ? "result" : "reject");
    fixture.failures.set("overview", "reject");
    await click(view, label);
    assert.equal(fixture.notices(), before + 2, `${label}: failed mutation still notifies`);
    fixture.failures.clear();
  }
  const before = fixture.notices();
  fixture.failures.set("domains", "reject");
  await click(fixture.domainView, "设为当前");
  assert.equal(fixture.notices(), before + 1, "successful switch with failed follow-up binding read still notifies");
  assert.equal(fixture.writes.length, fixture.notices());
  for (const [key, value] of fixture.writes) {
    assert.equal(key, "image-storage-updated");
    assert.match(value, /^\d+$/, "cross-tab payload is only a timestamp");
  }
  fixture.domainView.dispose(); fixture.view.dispose();
});

test("settings saves notify relevant attempted changes, not budgets/retention or failed preflight reads", async (t) => {
  const fixture = await storageCard(t);
  const submit = async () => {
    const form = find(fixture.view, (node) => node.type === "form");
    form.props.ref.current = { reportValidity: () => true };
    await act(fixture.view, () => form.props.onSubmit({ preventDefault() {} }));
  };
  await act(fixture.view, () => find(fixture.view, (node) => node.props.label === "R2 新图片保留（分钟）").props.onChange(120));
  await submit();
  assert.equal(fixture.notices(), 0);
  await act(fixture.view, () => find(fixture.view, (node) => node.props.label === "B 类保护 / 周期").props.onChange(9_000_000));
  fixture.failures.set("save", "reject");
  await submit();
  assert.equal(fixture.notices(), 0, "even a failed allocation-only save needs no image refresh");
  fixture.failures.clear();
  const changeMode = (mode) => act(fixture.view, () => find(fixture.view, (node) => node.type === "Select" && ["both", "local", "r2", "webdav"].includes(node.props.value)).props.onValueChange(mode));
  await changeMode("local"); await submit();
  assert.equal(fixture.notices(), 1);
  await changeMode("r2");
  fixture.failures.set("save", "reject");
  await submit();
  assert.equal(fixture.notices(), 2, "response failure may follow a committed mode change");
  fixture.failures.clear();
  await changeMode("both");
  const saves = fixture.calls.filter((name) => name === "save").length;
  fixture.failures.set("overview", "reject");
  await submit();
  assert.equal(fixture.calls.filter((name) => name === "save").length, saves);
  assert.equal(fixture.notices(), 2, "failed preflight did not attempt a write");
  fixture.domainView.dispose(); fixture.view.dispose();
});

for (const [pageFile, pageSize] of [["image-manager", 12], ["logs", 10]]) {
  test(`${pageFile}: automatic and manual refresh preserve pagination; query/filters reset; shrinking data clamps`, async (t) => {
    const host = browser(t);
    const renderer = hooks();
    let count = pageSize * 3 - 1;
    let loads = 0;
    const rows = () => Array.from({ length: count }, (_, index) => ({
      id: `fixture-${index}`, rel: `fixture-${index}.png`, url: `https://images.example.test/fixture-${index}.png`,
      name: `fixture-${index}.png`, size: 100, tags: [], created_at: "2099-01-01", time: "2099-01-01", type: "call", detail: {},
    }));
    const fetchRows = async () => { loads += 1; return { items: rows() }; };
    const component = load(`app/${pageFile}/page.tsx`, {
      react: renderer.react, sonner: quietToast,
      "@/lib/api": { fetchManagedImages: fetchRows, fetchSystemLogs: fetchRows, fetchImageTags: async () => ({ tags: [] }), fetchImageStorage: async () => ({ image_count: count, image_size_mb: 1 }) },
      "@/lib/image-storage": { IMAGE_STORAGE_CHANGED: "image-storage-updated", getImageStorageOverview: async () => ({ settings: { mode: "r2", r2_accounts: [] } }), resolveImageReferences: async () => assert.fail("Pagination fixtures have no R2 references") },
      "@/lib/image-reference": { imageReference: (image) => image, isImageExpired: () => false, isImageUnavailable: () => false, isR2Image: () => false, r2ReferenceFromUrl: () => undefined },
      "@/lib/image-cache": {}, "@/hooks/use-image-expiry": { useImageExpiry: () => 0 },
      "@/lib/use-auth-guard": { useAuthGuard: () => ({ isCheckingAuth: false, session: { role: "admin" } }) },
    });
    // The guard's child is the real content component; no production-only test exports.
    const view = renderer.mount(component.default().type);
    await settle(); view.render();
    const pageIs = (page, pages) => assert.ok(text(view.tree).includes(`第 ${page} / ${pages} 页`));
    const nextPage = () => act(view, () => find(view, (node) => node.type === "Button" && nodes(node).some((child) => child.type === "ChevronRight")).props.onClick());
    await nextPage(); await nextPage(); pageIs(3, 3);
    for (const event of [new Event("focus"), new Event("image-storage-updated"), Object.assign(new Event("storage"), { key: "image-storage-updated" })]) {
      const before = loads;
      await act(view, () => host.window.dispatchEvent(event));
      assert.equal(loads, before + 1);
      pageIs(3, 3);
    }
    const before = loads;
    await act(view, () => host.window.dispatchEvent(Object.assign(new Event("storage"), { key: "unrelated-fixture" })));
    assert.equal(loads, before);
    await act(view, () => find(view, (node) => node.type === "Button" && node.props.variant === "ghost" && text(node) === "刷新").props.onClick());
    assert.equal(loads, before + 1); pageIs(3, 3);
    count = pageSize + 1;
    await act(view, () => host.window.dispatchEvent(new Event("image-storage-updated")));
    pageIs(2, 2);
    count = pageSize * 3 - 1;
    await click(view, "查询"); pageIs(1, 3);
    await nextPage(); pageIs(2, 3);
    await act(view, () => find(view, (node) => node.type === "DateRangeFilter").props.onChange("2099-01-01", "2099-01-02"));
    pageIs(1, 3);
    view.dispose();
    const afterDispose = loads;
    host.window.dispatchEvent(new Event("focus"));
    await settle();
    assert.equal(loads, afterDispose, "automatic refresh listeners are cleaned up");
  });
}
