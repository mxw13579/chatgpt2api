// Synthetic callback/state fixtures only; the main thread runs these in its temporary copy.
// Compile whole TS modules as in image-annotation-preview.cjs, without source extraction,
// require hooks, a browser, real storage, or any network access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { File } = require("node:buffer");
const React = require("react");
const ts = require("typescript");

const compiled = new Map();
function compile(file) {
  if (!compiled.has(file)) {
    compiled.set(file, ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src", file), "utf8"), {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022, esModuleInterop: true,
      },
    }).outputText);
  }
  return compiled.get(file);
}

// Mock only the hooks needed to invoke the real components and their callbacks.
// JSX stays a React element tree; no DOM rendering or fake component implementations run.
function hookFixture() {
  const slots = [];
  let cursor = 0;
  let pending = [];
  let dirty = false;
  let component;
  let props;
  let tree;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  function useMemo(create, deps) {
    const index = cursor++;
    if (!slots[index] || !same(slots[index].deps, deps)) slots[index] = { deps, value: create() };
    return slots[index].value;
  }
  function useEffect(effect, deps) {
    const index = cursor++;
    if (slots[index] && same(slots[index].deps, deps)) return;
    const previous = slots[index];
    const slot = slots[index] = { deps };
    pending.push(() => { previous?.cleanup?.(); slot.cleanup = effect(); });
  }
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) {
        const slot = slots[index] = { value: typeof initial === "function" ? initial() : initial };
        slot.set = (update) => {
          const value = typeof update === "function" ? update(slot.value) : update;
          if (!Object.is(value, slot.value)) { slot.value = value; dirty = true; }
        };
      }
      return [slots[index].value, slots[index].set];
    },
    useRef: (value) => useMemo(() => ({ current: value }), []),
    useMemo,
    useCallback: (callback, deps) => useMemo(() => callback, deps),
    useEffect,
    useLayoutEffect: useEffect,
  };
  function render() {
    dirty = false;
    cursor = 0;
    tree = component(props);
    const effects = pending;
    pending = [];
    effects.forEach((effect) => effect());
  }
  return {
    hooks,
    get tree() { return tree; },
    mount(nextComponent, nextProps) { component = nextComponent; props = nextProps; render(); },
    async flush() {
      for (let i = 0; i < 30; i++) {
        if (dirty) render();
        await new Promise(setImmediate);
        if (!dirty) return;
      }
      assert.fail("fixture state did not settle");
    },
    dispose() { slots.forEach((slot) => slot.cleanup?.()); },
  };
}

function findElement(tree, predicate) {
  if (Array.isArray(tree)) {
    for (const child of tree) { const found = findElement(child, predicate); if (found) return found; }
  } else if (React.isValidElement(tree)) {
    if (predicate(tree)) return tree;
    return findElement(tree.props.children, predicate);
  }
}
function text(tree) {
  if (typeof tree === "string") return tree;
  if (Array.isArray(tree)) return tree.map(text).join("");
  return React.isValidElement(tree) ? text(tree.props.children) : "";
}

const reference = {
  image_id: "images/2099/01/01/00000000000000000000000000000001.png",
  account_id: "synthetic-account", storage: "r2", expires_at: "2099-01-01T01:00:00Z",
};
const oldUrl = `https://old.example.test/${reference.image_id}`;
function conversation() {
  return {
    id: "conversation", title: "synthetic", model: "gpt-image-2",
    createdAt: "2000-01-01T00:00:00Z", updatedAt: "2000-01-01T00:00:00Z",
    turns: [{
      id: "turn", prompt: "synthetic", model: "gpt-image-2", mode: "generate",
      referenceImages: [], maskImages: [], count: 1, size: "1024x1024", ratio: "1:1", tier: "1k", quality: "auto",
      createdAt: "2000-01-01T00:00:00Z", status: "success",
      images: [{ ...reference, id: "result", status: "success", url: oldUrl }],
    }],
  };
}

function fixture(t, items = [conversation()]) {
  const view = hookFixture();
  const parts = Object.fromEntries([
    "ImageComposer", "ImageDrawingDialog", "ImageResults", "ImageSidebar", "ImageLightbox", "Button",
    "Dialog", "DialogContent", "DialogDescription", "DialogFooter", "DialogHeader", "DialogTitle",
  ].map((name) => [name, function FixtureElement() { return null; }]));
  const records = new Map([["items", structuredClone(items)]]);
  const errors = [];
  const resolves = [];
  const reads = [];
  const edits = [];
  const generations = [];
  let failSave = false;
  let host = "current.example.test";
  let resolver = async (images) => ({ items: images.map((image) => ({
    ...image, url: `https://${host}/${image.image_id}`, expired: false, unavailable: false,
  })) });
  const resolveImageReferences = async (images) => {
    resolves.push(structuredClone(images));
    return resolver(images);
  };
  const storage = {
    getItem: async (key) => structuredClone(records.get(key) || null),
    setItem: async (key, value) => {
      if (failSave) throw new Error("synthetic local quota failure");
      records.set(key, structuredClone(value));
      return value;
    },
    removeItem: async (key) => records.delete(key),
  };
  const modules = new Map();
  const mocks = {
    react: view.hooks,
    sonner: { toast: { error: (message) => errors.push(message), success() {}, info() {} } },
    localforage: { __esModule: true, default: { createInstance: () => storage } },
    "@/lib/image-storage": { IMAGE_STORAGE_CHANGED: "image-storage-updated", resolveImageReferences },
    "@/lib/api": {
      fetchImageTasks: async () => ({ items: [], missing_ids: [] }),
      fetchImageModelCatalog: async () => ({ models: ["gpt-image-2"], default_image_model: "gpt-image-2" }),
      fetchAccounts: async () => { throw new Error("fixture must not load real accounts"); },
      createImageEditTask: async (id, files, prompt, model, size, quality, masks) => {
        edits.push({ id, files, prompt, model, size, quality, masks });
        return { id, status: "success", data: [{ b64_json: "c3ludGhldGlj" }] };
      },
      createImageGenerationTask: async (id) => {
        generations.push(id);
        return { id, status: "success", data: [{ b64_json: "c3ludGhldGlj" }] };
      },
    },
    "@/lib/image-cache": {
      clearCachedImages: async () => {}, deleteCachedImageSources: async () => {},
      readImageBlob: async (url, image) => {
        reads.push({ url, image });
        return new Blob(["synthetic image bytes"], { type: "image/png" });
      },
      downloadImageSource: async () => { throw new Error("fixture does not trigger downloads"); },
    },
    "@/hooks/use-cached-image-source": {
      useCachedImageSource: (source, enabled, image) =>
        enabled && !load("lib/image-reference.ts").isImageUnavailable(image) ? source : "",
    },
    "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    "@/lib/use-auth-guard": { useAuthGuard: () => ({ isCheckingAuth: false, session: { role: "user" } }) },
    "@/app/settings/store": { useSettingsStore: (select) => select({ config: { image_timeout_retry_secs: 30 } }) },
    "@/app/image/components/image-composer": { ImageComposer: parts.ImageComposer },
    "@/app/image/components/image-drawing-dialog": { ImageDrawingDialog: parts.ImageDrawingDialog },
    "@/app/image/components/image-results": { ImageResults: parts.ImageResults },
    "@/app/image/components/image-sidebar": { ImageSidebar: parts.ImageSidebar },
    "@/components/image-lightbox": { ImageLightbox: parts.ImageLightbox },
    "@/components/ui/button": { Button: parts.Button },
    "@/components/ui/dialog": parts,
    "@radix-ui/react-dialog": Object.fromEntries(["Root", "Portal", "Overlay", "Content", "Title", "Close"].map((name) => [name, `dialog-${name}`])),
  };
  function load(file) {
    if (modules.has(file)) return modules.get(file).exports;
    const module = { exports: {} };
    modules.set(file, module);
    new Function("require", "module", "exports", compile(file))((name) => {
      if (name in mocks) return mocks[name];
      if (name === "@/lib/image-reference") return load("lib/image-reference.ts");
      if (name === "@/lib/image-history") return load("lib/image-history.ts");
      if (name === "@/store/image-conversations") return load("store/image-conversations.ts");
      if (name.startsWith("@/")) throw new Error(`Unmocked fixture import: ${name}`);
      return require(name);
    }, module, module.exports);
    return module.exports;
  }
  const previous = Object.fromEntries(["window", "File", "FileReader", "fetch"].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const local = new Map();
  const browserStorage = {
    getItem: (key) => local.get(key) || null,
    setItem: (key, value) => local.set(key, value),
    removeItem: (key) => local.delete(key),
  };
  const events = new EventTarget();
  global.window = {
    localStorage: browserStorage, sessionStorage: browserStorage,
    addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events),
    setTimeout: () => { throw new Error("synthetic tasks must finish without polling"); },
  };
  global.File = File;
  global.FileReader = class {
    readAsDataURL(file) {
      void file.arrayBuffer().then((buffer) => {
        this.result = `data:${file.type};base64,${Buffer.from(buffer).toString("base64")}`;
        this.onload();
      });
    }
  };
  global.fetch = async () => { throw new Error("network forbidden in synthetic fixture"); };
  t.after(() => {
    view.dispose();
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {
    view, load, errors, resolves, reads, edits, generations, records,
    set failSave(value) { failSave = value; },
    set host(value) { host = value; },
    set resolver(value) { resolver = value; },
    props(name) {
      const element = findElement(view.tree, (node) => node.type === parts[name]);
      assert.ok(element, `missing ${name}`);
      return element.props;
    },
    button(label) {
      const element = findElement(view.tree, (node) => node.type === parts.Button && text(node).includes(label));
      assert.ok(element, `missing button ${label}`);
      return element.props;
    },
    async mountPage() {
      const element = load("app/image/page.tsx").default();
      view.mount(element.type, element.props);
      await view.flush();
    },
    async refresh() { this.button("刷新图片链接").onClick(); await view.flush(); },
    async submit(prompt = "synthetic edit") {
      this.props("ImageComposer").onPromptChange(prompt);
      await view.flush();
      await this.props("ImageComposer").onSubmit();
      await view.flush();
    },
  };
}

function result(f) { return f.props("ImageResults").selectedConversation.turns[0].images[0]; }

test("initial resolution survives local history-save failure without a cloud failure", async (t) => {
  const f = fixture(t);
  f.failSave = true;
  await f.mountPage();
  assert.equal(result(f).url, `https://current.example.test/${reference.image_id}`);
  assert.equal(result(f).unavailable, false);
  assert.equal(result(f).expired, false);
  assert.equal(f.props("ImageSidebar").isLoadingHistory, false);
  assert.ok(f.errors.some((message) => message.includes("保存本地图片历史失败")));
  assert.ok(f.errors.every((message) => !/解析失败|停止加载/.test(message)));
  assert.equal(f.records.get("items")[0].turns[0].images[0].url, oldUrl);
});

test("refresh keeps resolved history, draft, annotation and lightbox state when local save fails", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  await f.props("ImageResults").onContinueEdit("conversation", result(f));
  await f.props("ImageResults").onAnnotateImage("conversation", result(f), result(f).url);
  f.props("ImageResults").onOpenLightbox([{ ...result(f), src: result(f).url }], 0);
  await f.view.flush();
  f.errors.length = 0;
  f.failSave = true;
  f.host = "restored.example.test";
  await f.refresh();
  const expected = `https://restored.example.test/${reference.image_id}`;
  assert.equal(result(f).url, expected);
  assert.equal(result(f).unavailable, false);
  assert.equal(f.props("ImageComposer").referenceImages[0].dataUrl, expected);
  assert.equal(f.props("ImageComposer").referenceImages[0].unavailable, false);
  assert.equal(f.props("ImageDrawingDialog").source, expected);
  assert.equal(f.props("ImageDrawingDialog").reference.unavailable, false);
  assert.equal(f.props("ImageLightbox").images[0].src, expected);
  assert.equal(f.props("ImageLightbox").images[0].unavailable, false);
  assert.equal(f.button("刷新图片链接").disabled, false);
  assert.equal(f.errors.length, 1);
  assert.match(f.errors[0], /保存本地图片历史失败/);
});

test("R2 add-reference reads no body; submitting the edit reads it exactly once", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  await f.props("ImageResults").onContinueEdit("conversation", result(f));
  await f.view.flush();
  const draft = f.props("ImageComposer").referenceImages[0];
  assert.equal(draft.image_id, reference.image_id);
  assert.equal(draft.account_id, reference.account_id);
  assert.equal(draft.expires_at, reference.expires_at);
  assert.equal(draft.dataUrl, `https://current.example.test/${reference.image_id}`);
  assert.equal(f.reads.length, 0);
  await f.submit();
  assert.equal(f.reads.length, 1);
  assert.equal(f.edits.length, 1);
  assert.equal(f.generations.length, 0);
  assert.equal(await f.edits[0].files[0].text(), "synthetic image bytes");
  assert.equal(f.edits[0].files[0].type, "image/png");
});

test("legacy base64 results remain editable without an eager File conversion or download", async (t) => {
  const source = conversation();
  source.turns[0].images = [{ id: "legacy", status: "success", b64_json: Buffer.from("legacy bytes").toString("base64") }];
  const f = fixture(t, [source]);
  await f.mountPage();
  await f.props("ImageResults").onContinueEdit("conversation", result(f));
  await f.view.flush();
  assert.match(f.props("ImageComposer").referenceImages[0].dataUrl, /^data:image\/png;base64,/);
  await f.submit();
  assert.equal(f.edits.length, 1);
  assert.equal(f.generations.length, 0);
  assert.equal(f.reads.length, 0);
  assert.equal(await f.edits[0].files[0].text(), "legacy bytes");
});

test("reusing an R2 turn does not download content until submission", async (t) => {
  const source = conversation();
  source.turns[0].mode = "edit";
  source.turns[0].referenceImages = [{ ...reference, name: "reference.png", type: "image/png", dataUrl: oldUrl }];
  const f = fixture(t, [source]);
  await f.mountPage();
  await f.props("ImageResults").onReuseTurnConfig("conversation", "turn");
  await f.view.flush();
  assert.equal(f.reads.length, 0);
  assert.equal(f.props("ImageComposer").referenceImages.length, 1);
  await f.submit();
  assert.equal(f.reads.length, 1);
  assert.equal(f.edits.length, 1);
});

test("a draft R2 ID still resolves after its source history result is deleted", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  await f.props("ImageResults").onContinueEdit("conversation", result(f));
  f.props("ImageResults").onDeleteResults("conversation", "turn");
  await f.view.flush();
  f.button("确认删除").onClick();
  await f.view.flush();
  assert.equal(result(f).image_id, undefined);
  f.resolves.length = 0;
  f.host = "draft-only.example.test";
  await f.refresh();
  assert.deepEqual(f.resolves.flat().map((image) => image.image_id), [reference.image_id]);
  assert.equal(f.props("ImageComposer").referenceImages[0].dataUrl, `https://draft-only.example.test/${reference.image_id}`);
  assert.equal(f.props("ImageComposer").referenceImages[0].unavailable, false);
  assert.equal(f.reads.length, 0);
});

test("a failed refresh stops requested URLs but does not invalidate newer unrequested active images", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  let rejectRequest;
  const newer = { ...reference, image_id: "images/2099/01/01/00000000000000000000000000000002.png", id: "newer", status: "success" };
  const newerUrl = `https://newer.example.test/${newer.image_id}`;
  f.resolver = (images) => images[0].image_id === reference.image_id
    ? new Promise((_resolve, reject) => { rejectRequest = reject; })
    : Promise.resolve({ items: [{ ...newer, url: newerUrl, unavailable: false }] });
  f.button("刷新图片链接").onClick();
  await f.view.flush();
  await f.props("ImageResults").onContinueEdit("conversation", newer);
  await f.props("ImageResults").onAnnotateImage("conversation", newer, newerUrl);
  f.props("ImageResults").onOpenLightbox([{ ...newer, src: newerUrl }], 0);
  await f.view.flush();
  rejectRequest(new Error("synthetic resolve failure"));
  await f.view.flush();
  assert.equal(result(f).url, "");
  assert.equal(result(f).unavailable, true);
  assert.equal(f.props("ImageComposer").referenceImages[0].dataUrl, newerUrl);
  assert.equal(f.props("ImageComposer").referenceImages[0].unavailable, false);
  assert.equal(f.props("ImageDrawingDialog").source, newerUrl);
  assert.equal(f.props("ImageLightbox").images[0].src, newerUrl);
  assert.equal(f.props("ImageLightbox").images[0].unavailable, undefined);
  assert.match(f.errors.at(-1), /刷新 R2 图片地址失败/);
});

test("network failure stops a requested draft URL and later resolution restores it", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  await f.props("ImageResults").onContinueEdit("conversation", result(f));
  await f.view.flush();
  f.resolver = async () => { throw new Error("synthetic resolve failure"); };
  await f.refresh();
  assert.equal(f.props("ImageComposer").referenceImages[0].dataUrl, "");
  assert.equal(f.props("ImageComposer").referenceImages[0].unavailable, true);
  f.resolver = async () => ({ items: [{ ...reference, url: oldUrl }] });
  await f.refresh();
  assert.equal(f.props("ImageComposer").referenceImages[0].dataUrl, oldUrl);
  assert.equal(f.props("ImageComposer").referenceImages[0].unavailable, false);
});

test("lightbox uses fresh availability flags rather than stale parent flags", async (t) => {
  const f = fixture(t, []);
  f.resolver = async () => ({ items: [{ ...reference, url: `https://current.example.test/${reference.image_id}` }] });
  const Lightbox = f.load("components/image-lightbox.tsx").ImageLightbox;
  f.view.mount(Lightbox, {
    images: [{ ...reference, id: "preview", src: oldUrl, expired: true, unavailable: true }],
    currentIndex: 0, open: true, onOpenChange() {}, onIndexChange() {},
  });
  await f.view.flush();
  const image = findElement(f.view.tree, (node) => node.type === "img");
  assert.ok(image);
  assert.equal(image.props.src, `https://current.example.test/${reference.image_id}`);
  const download = findElement(f.view.tree, (node) => node.props["aria-label"] === "下载图片");
  assert.equal(download.props.disabled, false);
});

test("lightbox still refuses an expired date even if availability flags are false", async (t) => {
  const f = fixture(t, []);
  const Lightbox = f.load("components/image-lightbox.tsx").ImageLightbox;
  f.view.mount(Lightbox, {
    images: [{ ...reference, id: "expired", src: oldUrl, expires_at: "2000-01-01T00:00:00Z", expired: false, unavailable: false }],
    currentIndex: 0, open: true, onOpenChange() {}, onIndexChange() {},
  });
  await f.view.flush();
  assert.equal(findElement(f.view.tree, (node) => node.type === "img"), undefined);
  assert.equal(f.resolves.length, 0);
  assert.match(text(f.view.tree), /图片已过期/);
});

test("local upload, sketch and reference removal still submit an edit without remote reads", async (t) => {
  const f = fixture(t, []);
  await f.mountPage();
  await f.props("ImageComposer").onReferenceImageChange([new File(["local upload"], "local.png", { type: "image/png" })]);
  f.props("ImageComposer").onOpenSketch();
  await f.view.flush();
  await f.props("ImageDrawingDialog").onApply(new File(["sketch"], "sketch.png", { type: "image/png" }));
  await f.view.flush();
  assert.equal(f.props("ImageComposer").referenceImages.length, 2);
  f.props("ImageComposer").onRemoveReferenceImage(0);
  await f.view.flush();
  assert.equal(f.props("ImageComposer").referenceImages.length, 1);
  assert.equal(f.props("ImageComposer").maskImages.length, 0);
  await f.submit();
  assert.equal(f.reads.length, 0);
  assert.equal(f.edits.length, 1);
  assert.equal(await f.edits[0].files[0].text(), "sketch");
});

test("R2 annotation keeps its metadata and local mask, downloading the source once on submit", async (t) => {
  const f = fixture(t);
  await f.mountPage();
  await f.props("ImageResults").onAnnotateImage("conversation", result(f), result(f).url);
  await f.view.flush();
  await f.props("ImageDrawingDialog").onApply(new File(["synthetic mask"], "mask.png", { type: "image/png" }));
  await f.view.flush();
  assert.equal(f.reads.length, 0);
  assert.equal(f.props("ImageComposer").referenceImages[0].image_id, reference.image_id);
  assert.match(f.props("ImageComposer").maskImages[0].dataUrl, /^data:image\/png;base64,/);
  await f.submit();
  assert.equal(f.reads.length, 1);
  assert.equal(f.edits.length, 1);
  assert.equal(await f.edits[0].masks[0].text(), "synthetic mask");
});
