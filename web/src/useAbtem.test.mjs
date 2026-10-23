import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import test from "node:test";
import ts from "typescript";
import * as Vue from "vue";

function load(name, dependencies, intervals = new Map()) {
  const source = readFileSync(new URL(name, import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", "setInterval", "clearInterval", code)((name) => {
    if (!(name in dependencies)) throw new Error(`未配置依赖：${name}`);
    return dependencies[name];
  }, module, module.exports, (callback, delay) => { const key = {}; intervals.set(key, { callback, delay }); return key; }, (key) => intervals.delete(key));
  return module.exports;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const account = { id: "abtem-user", email: "simulation@example.invalid", verified: true, is_admin: false, csrf_token: "test-csrf", quotas: {}, credits: { available: 8, frozen: 0, checked_in: false } };
const parameters = { energy_kev: 200, zone_axis: [0, 0, 1], thickness_nm: 2, defocus_nm: 10, cs_mm: 1, aperture_mrad: 20 };
const limits = { max_cif_bytes: 524288, energy_kev: [80, 300], zone_axis_component: [-3, 3], thickness_nm: [0.5, 10], defocus_nm: [-200, 200], cs_mm: [0, 3], aperture_mrad: [5, 50], retention_days: 3 };
const service = { enabled: true, worker_online: true, message: "", defaults: parameters, limits };
const source = { type: "upload", filename: "silicon.cif", cif_base64: "ZGF0YV9TaQo=" };
const job = { id: "abtem-1", kind: "abtem", state: "queued", phase: "queued", message: "等待计算", created_at: "2026-10-21T02:00:00Z", updated_at: "2026-10-21T02:00:00Z", progress: 0, parameters, input_name: "silicon.cif", source_type: "upload", credits: { reserved: 1, charged: 0, status: "frozen" }, download_available: false, download_expires_at: null, file_expired: false, cancel_requested: false };

function fixture(t, { initialUser = account, storage = new Map() } = {}) {
  const original = { fetch: globalThis.fetch, crypto: globalThis.crypto, sessionStorage: globalThis.sessionStorage };
  if (!globalThis.crypto) Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
  Object.defineProperty(globalThis, "sessionStorage", { value: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) }, configurable: true });
  const requests = [], intervals = new Map(), cleanup = [], refreshed = [], unauthorized = [];
  globalThis.fetch = (url, options = {}) => new Promise((resolve, reject) => {
    const call = { path: String(url).replace("/highscore/api", ""), method: options.method ?? "GET", body: options.body ? JSON.parse(options.body) : null, options, responded: false,
      respond(value, status = 200) { assert.equal(call.responded, false); call.responded = true; resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } })); },
      disconnect() { assert.equal(call.responded, false); call.responded = true; reject(new Error("network")); },
    };
    requests.push(call);
  });
  const api = load("./api.ts", {}), state = load("./abtemState.ts", {});
  const actual = load("./useAbtem.ts", { vue: { ...Vue, onBeforeUnmount: (callback) => cleanup.push(callback) }, "./api": api, "./abtemState": state }, intervals);
  const user = Vue.ref(initialUser ? { ...initialUser } : null), scope = Vue.effectScope();
  const store = scope.run(() => actual.useAbtem(user, (cause) => unauthorized.push(cause), async () => refreshed.push(true)));
  t.after(() => {
    for (const callback of cleanup) callback(); scope.stop(); globalThis.fetch = original.fetch;
    for (const key of ["crypto", "sessionStorage"]) {
      if (original[key] === undefined) delete globalThis[key]; else Object.defineProperty(globalThis, key, { value: original[key], configurable: true });
    }
  });
  return { store, user, requests, intervals, refreshed, unauthorized, storage };
}
async function ready(h, items = []) {
  h.requests.find((call) => call.path === "/abtem/status" && !call.responded)?.respond(service);
  h.requests.find((call) => call.path === "/abtem/jobs?offset=0&limit=50" && !call.responded)?.respond({ items, next_offset: null });
  await settle();
}
function outstanding(h, path) { return h.requests.find((call) => !call.responded && call.path === path); }

test("未登录或未验证时不读取模拟接口；验证完成才加载默认参数与本人任务", async (t) => {
  const h = fixture(t, { initialUser: null });
  assert.equal(h.requests.length, 0);
  h.user.value = { ...account, verified: false };
  assert.equal(h.requests.length, 0);
  h.user.value = { ...account };
  await ready(h);
  assert.equal(h.store.canSubmit.value, true);
  assert.deepEqual(h.store.service.value.defaults, parameters);
  assert.ok(h.requests.every((call) => call.options.credentials === "same-origin"));
});

test("模拟服务已开放而执行器暂不可用时，仍可按服务端契约提交排队", async (t) => {
  const h = fixture(t); await ready(h);
  const reading = h.store.loadService();
  outstanding(h, "/abtem/status").respond({ ...service, worker_online: false, message: "模拟服务暂时不可用，任务将排队等待。" }); await reading;
  assert.equal(h.store.canSubmit.value, true);
  const submitting = h.store.submit(source, parameters);
  outstanding(h, "/abtem/jobs").respond(job); await settle();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [job], next_offset: null }); await submitting;
  assert.equal(h.store.selectedJob.value.state, "queued");
});

test("提交断线保留完整快照，核对重放同一幂等请求，轮询绝不重交", async (t) => {
  const h = fixture(t); await ready(h);
  const input = { ...parameters, zone_axis: [1, 0, 0] };
  const submitting = h.store.submit(source, input);
  const first = outstanding(h, "/abtem/jobs");
  assert.ok(first.body.request_key); assert.equal(first.options.headers.get("X-CSRF-Token"), "test-csrf");
  input.zone_axis[0] = 3;
  first.disconnect(); await submitting;
  assert.deepEqual(h.store.pendingSubmission.value.parameters.zone_axis, [1, 0, 0]);
  assert.equal(h.store.canSubmit.value, false);
  assert.ok(h.storage.has("highscore.abtem.pending.abtem-user"));
  for (const interval of h.intervals.values()) interval.callback();
  assert.equal(h.requests.filter((call) => call.method === "POST").length, 1);
  outstanding(h, "/abtem/status")?.respond(service);
  const reconciling = h.store.reconcileSubmission();
  const second = outstanding(h, "/abtem/jobs");
  assert.deepEqual(second.body, first.body);
  second.respond(job); await settle();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [job], next_offset: null });
  await reconciling;
  assert.equal(h.store.pendingSubmission.value, null); assert.equal(h.storage.size, 0);
  assert.equal(h.store.selectedJob.value.id, job.id); assert.equal(h.store.activeCount.value, 1);
  assert.ok(h.refreshed.length > 0);
});

test("明确输入拒绝不会留下待核对请求；5xx 保留，刷新页面能核对原请求", async (t) => {
  const h = fixture(t); await ready(h);
  let submitting = h.store.submit(source, parameters);
  outstanding(h, "/abtem/jobs").respond({ detail: "结构暂不支持" }, 422); await submitting;
  assert.equal(h.store.pendingSubmission.value, null); assert.equal(h.store.actionError.value, "结构暂不支持");
  submitting = h.store.submit(source, parameters);
  const failed = outstanding(h, "/abtem/jobs"); failed.respond({ detail: "暂时不可用" }, 503); await submitting;
  const saved = JSON.parse(h.storage.get("highscore.abtem.pending.abtem-user"));
  assert.equal(saved.request_key, failed.body.request_key);
  const next = fixture(t, { storage: h.storage }); await ready(next);
  assert.deepEqual(next.store.pendingSubmission.value, saved);
  assert.equal(next.store.canSubmit.value, false);
});

test("提交响应缺失任务结构时保留原请求供核对，不能把不完整数据写进任务列表", async (t) => {
  const h = fixture(t); await ready(h);
  const submitting = h.store.submit(source, parameters);
  outstanding(h, "/abtem/jobs").respond(null); await submitting;
  assert.deepEqual(h.store.jobs.value, []);
  assert.ok(h.store.pendingSubmission.value?.request_key);
  assert.match(h.store.actionError.value, /未读取到完整模拟任务/);
});

test("写操作前的迟到列表不能移除已确认任务；已完成后的新操作使用新请求编号", async (t) => {
  const h = fixture(t); await ready(h);
  const oldRead = h.store.loadJobs(), oldCall = outstanding(h, "/abtem/jobs?offset=0&limit=50");
  const submitting = h.store.submit(source, parameters), first = outstanding(h, "/abtem/jobs");
  const complete = { ...job, state: "succeeded", credits: { reserved: 1, charged: 1, status: "charged" } };
  first.respond(complete); await settle();
  oldCall.respond({ items: [], next_offset: null });
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [complete], next_offset: null });
  await Promise.all([oldRead, submitting]);
  assert.equal(h.store.selectedJob.value.id, job.id); assert.equal(h.store.canSubmit.value, true);
  const next = h.store.submit(source, parameters), second = outstanding(h, "/abtem/jobs");
  assert.notEqual(first.body.request_key, second.body.request_key);
  second.respond({ detail: "测试结束" }, 422); await next;
});

test("运行中取消继续读取状态，确认停止退款前不能提交新计算", async (t) => {
  const running = { ...job, state: "running", progress: 25 };
  const h = fixture(t); await ready(h, [running]);
  const cancelling = h.store.cancel(running);
  const requested = { ...running, cancel_requested: true };
  outstanding(h, `/abtem/jobs/${job.id}/cancel`).respond(requested); await settle();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [requested], next_offset: null });
  await cancelling;
  assert.equal(h.store.activeCount.value, 1); assert.equal(h.store.canSubmit.value, false);
  const posts = h.requests.filter((call) => call.method === "POST").length;
  await h.store.cancel(requested);
  assert.equal(h.requests.filter((call) => call.method === "POST").length, posts);
  for (const interval of h.intervals.values()) if (interval.delay === 4000) interval.callback();
  const cancelled = { ...requested, state: "cancelled", credits: { reserved: 1, charged: 0, status: "refunded" } };
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [cancelled], next_offset: null }); await settle();
  assert.equal(h.store.activeCount.value, 0); assert.equal(h.store.canSubmit.value, true);
  assert.equal(h.store.selectedJob.value.credits.status, "refunded");
});

test("结构分页按 next_offset 追加且去重，切换账号后迟到分页不回写", async (t) => {
  const h = fixture(t); await ready(h);
  const a = { job_id: "structure-1", code: "ICSD-000001", phase_name: "硅", source: "ICSD" };
  const b = { ...a, code: "ICSD-000002" };
  let reading = h.store.loadSources();
  outstanding(h, "/abtem/sources?offset=0&limit=50").respond({ items: [a], next_offset: 50 }); await reading;
  reading = h.store.loadSources(true);
  outstanding(h, "/abtem/sources?offset=50&limit=50").respond({ items: [a, b], next_offset: 100 }); await reading;
  assert.deepEqual(h.store.sources.value.map((item) => item.code), [a.code, b.code]);
  reading = h.store.loadSources(true); const old = outstanding(h, "/abtem/sources?offset=100&limit=50");
  h.user.value = { ...account, id: "other-account", csrf_token: "other-csrf" };
  old.respond({ items: [{ ...a, code: "old-account-record" }], next_offset: null }); await reading; await ready(h);
  assert.deepEqual(h.store.sources.value, []); assert.equal(h.store.sourcesNextOffset.value, null);
});

test("任务分页支持历史详情，较早详情的迟到响应不能覆盖后选中的任务", async (t) => {
  const h = fixture(t); await ready(h);
  const first = h.store.showJob("old-1"), old = outstanding(h, "/abtem/jobs/old-1");
  const second = h.store.showJob("old-2");
  outstanding(h, "/abtem/jobs/old-2").respond({ ...job, id: "old-2", state: "failed" }); await second;
  old.respond({ ...job, id: "old-1" }); await first;
  assert.equal(h.store.selectedJob.value.id, "old-2");
  const reading = h.store.loadJobs();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [job], next_offset: 50 }); await settle();
  outstanding(h, "/abtem/jobs/old-2").respond({ ...job, id: "old-2", state: "failed" }); await reading;
  assert.equal(h.store.selectedJob.value.id, "old-2"); assert.equal(h.store.jobsNextOffset.value, 50);
  const more = h.store.loadJobs(true);
  outstanding(h, "/abtem/jobs?offset=50&limit=50").respond({ items: [{ ...job, id: "older" }], next_offset: null }); await more;
  assert.deepEqual(h.store.jobs.value.map((item) => item.id), [job.id, "older", "old-2"]);
  const polling = h.store.loadJobs();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [job], next_offset: 50 }); await settle();
  outstanding(h, "/abtem/jobs/old-2").respond({ ...job, id: "old-2", state: "failed" }); await polling;
  assert.ok(h.store.jobs.value.some((item) => item.id === "older"));
  assert.equal(h.store.jobsNextOffset.value, null);
});

test("换账号及同账号新会话均清空旧状态，旧提交/状态/任务响应不能写入新账号", async (t) => {
  const h = fixture(t); await ready(h);
  const submitting = h.store.submit(source, parameters), oldPost = outstanding(h, "/abtem/jobs");
  h.user.value = { ...account, id: "second-user", csrf_token: "next-csrf" };
  await ready(h);
  oldPost.respond(job); await submitting;
  assert.deepEqual(h.store.jobs.value, []); assert.equal(h.store.pendingSubmission.value, null); assert.equal(h.storage.size, 1);
  const oldRead = h.store.loadJobs(), call = outstanding(h, "/abtem/jobs?offset=0&limit=50");
  h.user.value = { ...h.user.value, csrf_token: "renewed-session" };
  h.requests.filter((item) => !item.responded && item !== call).forEach((item) => item.respond(item.path === "/abtem/status" ? service : { items: [], next_offset: null }));
  await settle();
  call.respond({ items: [job], next_offset: null }); await oldRead;
  assert.deepEqual(h.store.jobs.value, []); assert.equal(h.store.actionBusy.value, false);
  h.user.value = { ...account, csrf_token: "original-account-new-session" }; await ready(h);
  assert.equal(h.store.pendingSubmission.value.request_key, oldPost.body.request_key);
  assert.equal(h.store.canSubmit.value, false);
});

test("核对返回401后原请求跨退出与重新登录保留，重登使用新会话和原请求编号", async (t) => {
  const h = fixture(t); await ready(h);
  const submitting = h.store.submit(source, parameters), original = outstanding(h, "/abtem/jobs");
  original.disconnect(); await submitting;
  const checking = h.store.reconcileSubmission();
  outstanding(h, "/abtem/jobs").respond({ detail: "登录状态已失效，请重新登录。" }, 401); await checking;
  assert.equal(h.unauthorized.length, 1);
  h.user.value = null;
  assert.equal(h.store.pendingSubmission.value, null);
  assert.ok(h.storage.has("highscore.abtem.pending.abtem-user"));
  h.user.value = { ...account, csrf_token: "fresh-csrf" }; await ready(h);
  const replaying = h.store.reconcileSubmission(), replay = outstanding(h, "/abtem/jobs");
  assert.deepEqual(replay.body, original.body);
  assert.equal(replay.options.headers.get("X-CSRF-Token"), "fresh-csrf");
  replay.respond(job); await settle();
  outstanding(h, "/abtem/jobs?offset=0&limit=50").respond({ items: [job], next_offset: null }); await replaying;
  assert.equal(h.store.pendingSubmission.value, null);
  assert.equal(h.storage.size, 0);
});

test("核对原键后服务端明确拒绝无效输入时释放表单，允许修正CIF继续提交", async (t) => {
  const h = fixture(t); await ready(h);
  const submitting = h.store.submit(source, parameters);
  outstanding(h, "/abtem/jobs").disconnect(); await submitting;
  const checking = h.store.reconcileSubmission();
  outstanding(h, "/abtem/jobs").respond({ detail: "文件不是有效的CIF结构文件。" }, 422); await checking;
  assert.equal(h.store.pendingSubmission.value, null);
  assert.equal(h.storage.size, 0); assert.equal(h.store.canSubmit.value, true);
  assert.match(h.store.actionError.value, /有效的CIF/);
});

test("本人接口 401 调用统一退出处理，账号切换后的迟到 401 不影响新会话", async (t) => {
  const h = fixture(t); await ready(h);
  const reading = h.store.loadSources(), old = outstanding(h, "/abtem/sources?offset=0&limit=50");
  h.user.value = { ...account, id: "next-user" }; await ready(h);
  old.respond({ detail: "登录已失效" }, 401); await reading;
  assert.equal(h.unauthorized.length, 0);
  const current = h.store.loadSources(); outstanding(h, "/abtem/sources?offset=0&limit=50").respond({ detail: "登录已失效" }, 401); await current;
  assert.equal(h.unauthorized.length, 1);
});
