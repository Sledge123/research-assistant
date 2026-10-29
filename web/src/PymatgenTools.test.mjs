import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";

const descriptor = parse(readFileSync(new URL("./PymatgenTools.vue", import.meta.url), "utf8")).descriptor;
const script = compileScript(descriptor, { id: "materials-pricing" });
const template = compileTemplate({ source: descriptor.template.content, filename: "PymatgenTools.vue", id: "materials-pricing", compilerOptions: { bindingMetadata: script.bindings } }).code;
const settle = () => new Promise(resolve => setImmediate(resolve));
function evaluate(source, dependencies) {
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)(name => {
    assert.ok(name in dependencies, `未配置依赖 ${name}`); return dependencies[name];
  }, module, module.exports);
  return module.exports;
}
function fixture(t, overrides = {}) {
  const originals = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
  globalThis.document = { activeElement: null };
  globalThis.Document = class Document {}; globalThis.ShadowRoot = class ShadowRoot {};
  const costs = { analyze: 1, convert: 1, xrd: 2, supercell: 1, compare: 1 };
  const requests = [];
  const jobs = overrides.jobs ?? [];
  const dependencies = {
    vue: Vue, "./Icon.vue": { default: { render: () => null } }, "./PymatgenResult.vue": { default: { render: () => null } },
    "./pymatgenSession": { pendingPymatgen: () => null, savePendingPymatgen: () => {} },
    "./api": { API_BASE: "/highscore/api", ApiError: class extends Error {}, requestKey: () => "synthetic-request",
      request: async (route, options) => {
        requests.push({ route, options });
        if (route === "/pymatgen/status") return { enabled: true, worker_online: true, message: "", limits: {}, costs };
        if (route.startsWith("/pymatgen/jobs?")) return { items: jobs, next_offset: null };
        if (route === "/credits") return { available: 10, frozen: 0, checked_in: true };
        return jobs.find(job => route.endsWith(job.id));
      } },
  };
  const component = evaluate(script.content, dependencies).default;
  component.render = evaluate(template, dependencies).render;
  const node = (tag, text = "") => ({ tag, text, children: [], props: {}, parent: null, value: "", tagName: tag.toUpperCase(),
    addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document, getAttribute(key) { return this.props[key]; } });
  const renderer = Vue.createRenderer({
    createElement: tag => node(tag), createText: text => node("text", text), createComment: () => node("comment"),
    setText: (item, text) => { item.text = text; }, setElementText: (item, text) => { item.text = text; item.children = []; },
    parentNode: item => item.parent, nextSibling: item => item.parent?.children[item.parent.children.indexOf(item) + 1] ?? null,
    patchProp: (item, key, previous, value) => { item.props[key] = value; },
    insert: (item, parent, anchor) => { item.parent = parent; const index = parent.children.indexOf(anchor); if (index < 0) parent.children.push(item); else parent.children.splice(index, 0, item); },
    remove: item => { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); },
  });
  const root = node("root"), events = [];
  const app = renderer.createApp(component, { user: { id: "synthetic", verified: true, csrf_token: "synthetic" }, onCreditsChanged: () => events.push("creditsChanged"), ...overrides });
  t.after(() => { app.unmount(); for (const key of Object.keys(originals)) { if (originals[key] === undefined) delete globalThis[key]; else globalThis[key] = originals[key]; } });
  app.mount(root);
  const walk = (item = root) => [item, ...item.children.flatMap(child => walk(child))];
  const text = (item = root) => item.text + item.children.map(text).join("");
  return { root, walk, text, requests, events, costs, button: label => walk().find(item => item.tag === "button" && text(item).includes(label)) };
}

test("材料工具名称及成功费用：切换XRD为2积分，其余每项1积分", async t => {
  const h = fixture(t); await settle(); await Vue.nextTick();
  assert.doesNotMatch(h.text(), /全部免费|开始计算 · 免费/);
  assert.match(h.text(), /开始计算 · 1 积分/);
  const xrd = h.button("XRD衍射峰生成"); assert.ok(xrd);
  xrd.props.onClick(); await Vue.nextTick();
  assert.match(h.text(), /开始计算 · 2 积分/);
  assert.match(h.text(), /失败或取消.*退回/);
  h.button("结构比较").props.onClick(); await Vue.nextTick();
  assert.match(h.text(), /开始计算 · 1 积分/);
  assert.equal(h.requests.some(item => item.options?.method === "POST"), false);
});

test("历史免费任务和新任务费用分别展示，并通知全站余额刷新", async t => {
  const base = { id: "a".repeat(32), operation: "xrd", state: "failed", phase: "failed", progress: 0,
    input_name: "synthetic.cif", created_at: "2026-10-22T05:00:00Z", updated_at: "2026-10-22T05:00:01Z", message: "计算未完成。",
    cancel_requested: false, download_available: false, download_expires_at: null, file_expired: false, summary: null };
  const h = fixture(t, { jobs: [{ ...base, credits: { reserved: 2, charged: 0, status: "refunded" } },
    { ...base, id: "b".repeat(32), credits: { reserved: 0, charged: 0, status: "not_required" } }] });
  await settle(); await Vue.nextTick();
  assert.match(h.text(), /已退回 2 积分/);
  assert.match(h.text(), /历史免费任务/);
  assert.ok(h.events.includes("creditsChanged"));
});
