import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";

function evaluate(source, dependencies = {}) {
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)((name) => { if (!(name in dependencies)) throw new Error(`未配置依赖：${name}`); return dependencies[name]; }, module, module.exports);
  return module.exports;
}
const state = evaluate(readFileSync(new URL("./abtemState.ts", import.meta.url), "utf8"));
const descriptor = parse(readFileSync(new URL("./AbtemSimulation.vue", import.meta.url), "utf8")).descriptor;
const script = compileScript(descriptor, { id: "abtem-render" });
const template = compileTemplate({ source: descriptor.template.content, filename: "AbtemSimulation.vue", id: "abtem-render", compilerOptions: { bindingMetadata: script.bindings } });
const defaults = { energy_kev: 120, zone_axis: [1, 1, 0], thickness_nm: 3, defocus_nm: -20, cs_mm: 0.5, aperture_mrad: 25 };
const service = { enabled: true, worker_online: true, message: "", defaults, limits: { max_cif_bytes: 524288, energy_kev: [80, 300], zone_axis_component: [-3, 3], thickness_nm: [0.5, 10], defocus_nm: [-200, 200], cs_mm: [0, 3], aperture_mrad: [5, 50], retention_days: 3 } };
const user = { id: "render-user", verified: true, csrf_token: "render-csrf" };
const now = Date.now();
const job = { id: "sim/result 1", input_name: "硅.cif", parameters: defaults, state: "succeeded", message: "模拟完成", credits: { reserved: 1, charged: 1, status: "charged" }, download_available: true, file_expired: false, download_expires_at: new Date(now + 3600000).toISOString(), created_at: new Date(now).toISOString(), progress: 100, cancel_requested: false };

function fixture(t, overrides = {}) {
  const originals = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, fetch: globalThis.fetch, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  const downloads = [], requests = [];
  globalThis.document = { createElement: () => ({ href: "", download: "", click() { downloads.push({ href: this.href, filename: this.download }); }, remove() {} }), body: { appendChild() {} } }; globalThis.Document = class Document {}; globalThis.ShadowRoot = class ShadowRoot {};
  URL.createObjectURL = () => "blob:abtem-test"; URL.revokeObjectURL = () => {};
  globalThis.fetch = (url, options) => new Promise((resolve) => requests.push({ url, options, respond: (body, init = {}) => resolve(new Response(body, init)) }));
  const events = [], props = Vue.reactive({ user, service, serviceError: "", jobs: [job], selected: job, jobsLoading: false, jobsError: "", jobsNextOffset: null,
    sources: [], sourcesLoading: false, sourcesError: "", sourcesLoaded: false, sourcesNextOffset: null, busy: false, canSubmit: true, actionError: "", notice: "", pendingSubmission: false, now, ...overrides });
  const component = evaluate(script.content, { vue: Vue, "./Icon.vue": { default: Vue.defineComponent({ render: () => null }) }, "./api": { API_BASE: "/highscore/api" }, "./abtemState": state, "./literatureState": { formatLiteratureDate: (value) => value ?? "—" } }).default;
  component.render = evaluate(template.code, { vue: Vue }).render;
  const node = (tag, text = "") => ({ tag, text, children: [], parent: null, props: {}, value: "", tagName: tag.toUpperCase(), addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document, getAttribute(key) { return this.props[key]; } });
  const root = node("root");
  const renderer = Vue.createRenderer({
    createElement: (tag) => node(tag), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
    setText: (item, text) => { item.text = text; }, setElementText: (item, text) => { item.text = text; item.children = []; },
    parentNode: (item) => item.parent, nextSibling: (item) => item.parent?.children[item.parent.children.indexOf(item) + 1] ?? null,
    patchProp: (item, key, old, value) => { item.props[key] = value; },
    insert(item, parent, anchor) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(item); else parent.children.splice(index, 0, item); },
    remove(item) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = null; },
  });
  const handlers = {};
  for (const name of ["Login", "Submit", "Cancel", "SelectJob", "Refresh", "Sources", "MoreJobs", "Reconcile", "Xrd"]) handlers[`on${name}`] = (...args) => events.push([name, ...args]);
  const app = renderer.createApp({ setup: () => () => Vue.h(component, { ...props, ...handlers }) }); app.mount(root);
  t.after(() => { app.unmount(); for (const key of ["document", "Document", "ShadowRoot", "fetch"]) { if (originals[key] === undefined) delete globalThis[key]; else globalThis[key] = originals[key]; } URL.createObjectURL = originals.create; URL.revokeObjectURL = originals.revoke; });
  const walk = (item = root) => [item, ...item.children.flatMap((child) => walk(child))];
  const text = (item) => item.text + item.children.map(text).join("");
  const button = (label) => walk().find((item) => item.tag === "button" && text(item).includes(label));
  return { props, events, walk, text, root, button, downloads, requests };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("实际渲染：结果科学图与下载只使用本人鉴权接口，到期即撤下预览与下载", async (t) => {
  const h = fixture(t);
  assert.deepEqual(h.walk().filter((item) => item.tag === "img").map((item) => item.props.src), ["/highscore/api/abtem/jobs/sim%2Fresult%201/image/saed", "/highscore/api/abtem/jobs/sim%2Fresult%201/image/hrtem"]);
  assert.equal(h.button("下载完整结果").props.disabled, false);
  h.props.now = now + 4 * 86400000; await Vue.nextTick();
  assert.equal(h.walk().filter((item) => item.tag === "img").length, 0);
  assert.equal(h.walk().filter((item) => item.tag === "a").length, 0);
  assert.equal(h.button("下载完整结果"), undefined);
  assert.match(h.text(h.root), /结果已到期/);
});

test("实际渲染：参数默认值来自 status，archive 与 upload 只提交选中的一种来源", async (t) => {
  const archived = { job_id: "structure-job", code: "ICSD-000001", phase_name: "硅", source: "ICSD" };
  const h = fixture(t, { selected: null, jobs: [], sources: [archived], sourcesLoaded: true });
  h.button("我的结构 CIF").props.onClick(); await Vue.nextTick();
  assert.ok(h.events.some((event) => event[0] === "Sources" && event[1] === false), "已经读过结构列表后重新进入，仍须读取新归档");
  const radio = h.walk().find((item) => item.tag === "input" && item.props.type === "radio");
  radio.props["onUpdate:modelValue"](JSON.stringify([archived.job_id, archived.code])); await Vue.nextTick();
  h.walk().find((item) => item.tag === "form").props.onSubmit({ preventDefault() {} });
  const submitted = h.events.find((event) => event[0] === "Submit");
  assert.deepEqual(submitted[1], { type: "archive", job_id: archived.job_id, code: archived.code });
  assert.deepEqual(submitted[2], defaults);
  assert.equal("cif_base64" in submitted[1], false);
  assert.doesNotMatch(h.text(h.root), /服务可用/);
  assert.ok(h.walk().filter((item) => item.tag === "input" && item.props.type === "number" && item.props.id).every((item) => item.props.step === "any"), "物理参数遵守服务端连续范围，不额外限制小数步长");
});

test("实际渲染：结果下载使用同源鉴权GET，下载成功后可以免费再次下载", async (t) => {
  const h = fixture(t);
  const downloading = h.button("下载完整结果").props.onClick(); await Vue.nextTick();
  assert.equal(h.requests[0].url, "/highscore/api/abtem/jobs/sim%2Fresult%201/download");
  assert.equal(h.requests[0].options.credentials, "same-origin");
  h.requests[0].respond("PK-original-simulation", { headers: { "Content-Type": "application/zip" } }); await downloading; await Vue.nextTick();
  assert.equal(h.downloads.length, 1); assert.equal(h.button("下载完整结果").props.disabled, false);
  const repeated = h.button("下载完整结果").props.onClick();
  h.requests[1].respond("PK-original-simulation", { headers: { "Content-Type": "application/zip" } }); await repeated;
  assert.equal(h.downloads.length, 2);
  assert.ok(h.requests.every((item) => !item.options.method || item.options.method === "GET"));
});

for (const status of [401, 410]) test(`实际渲染：下载返回${status}时保留单页并显示服务端中文错误`, async (t) => {
  const h = fixture(t);
  const downloading = h.button("下载完整结果").props.onClick();
  const message = status === 401 ? "登录状态已失效，请重新登录。" : "模拟结果尚未完成或已过期。";
  h.requests[0].respond(JSON.stringify({ detail: message }), { status, headers: { "Content-Type": "application/json" } }); await downloading; await Vue.nextTick();
  assert.equal(h.downloads.length, 0); assert.match(h.text(h.root), new RegExp(message));
  assert.ok(h.events.some((event) => event[0] === "Refresh"), "通过只读刷新核对任务及当前登录状态");
  assert.equal(h.walk().filter((item) => item.tag === "a").length, 0);
});

test("实际渲染：下载响应在账号切换后返回时不能保存旧账号文件或错误", async (t) => {
  const h = fixture(t);
  const downloading = h.button("下载完整结果").props.onClick();
  h.props.user = { ...user, id: "different-user", csrf_token: "different-session" }; await Vue.nextTick();
  h.requests[0].respond("PK-private-old-account", { headers: { "Content-Type": "application/zip" } }); await downloading; await Vue.nextTick();
  assert.equal(h.downloads.length, 0);
  assert.doesNotMatch(h.text(h.root), /下载未完成/);
});

test("实际渲染：退出账号时清空已选上传，迟到文件读取不能恢复旧账号文件", async (t) => {
  const h = fixture(t, { selected: null, jobs: [] });
  let resolve;
  const file = { name: "old-account.cif", size: 8, arrayBuffer: () => new Promise((callback) => { resolve = callback; }) };
  const reading = h.walk().find((item) => item.tag === "input" && item.props.type === "file").props.onChange({ target: { files: [file] } });
  h.props.user = { ...user, id: "next-user", csrf_token: "next-session" }; await Vue.nextTick();
  resolve(new TextEncoder().encode("data_Si\n").buffer); await reading; await settle();
  assert.doesNotMatch(h.text(h.root), /old-account/);
  assert.equal(h.button("开始模拟").props.disabled, true);
  assert.equal(h.events.filter((event) => event[0] === "Submit").length, 0);
});

test("实际渲染：取消处理中继续显示冻结状态并禁用重复取消，核对原请求有独立入口", (t) => {
  const h = fixture(t, { selected: { ...job, state: "running", cancel_requested: true, credits: { reserved: 1, charged: 0, status: "frozen" }, progress: 30 }, pendingSubmission: true, canSubmit: false });
  assert.match(h.text(h.root), /取消处理中/); assert.match(h.text(h.root), /已冻结 1 积分/);
  assert.equal(h.button("正在取消").props.disabled, true);
  h.button("核对提交结果").props.onClick(); assert.equal(h.events.at(-1)[0], "Reconcile");
});
