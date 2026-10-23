import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as Vue from "vue";

// 直接转译生产组合函数及其真实 API 包装，仅控制 fetch、定时器和卸载钩子。
function loadSource(name, dependencies, intervals) {
  const source = readFileSync(new URL(name, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", "setInterval", "clearInterval", compiled)(
    (id) => {
      if (!(id in dependencies)) throw new Error(`未配置测试依赖：${id}`);
      return dependencies[id];
    },
    module,
    module.exports,
    (callback, delay) => { const id = {}; intervals.set(id, { callback, delay }); return id; },
    (id) => intervals.delete(id),
  );
  return module.exports;
}

function fixture(t) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const intervals = new Map();
  const cleanup = [];
  globalThis.fetch = (url, options = {}) => new Promise((resolve) => {
    const call = {
      path: String(url).replace("/highscore/api", ""),
      method: options.method ?? "GET",
      responded: false,
      respond(value, status = 200) {
        assert.equal(call.responded, false, `请求不可重复响应：${call.path}`);
        call.responded = true;
        resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
      },
    };
    requests.push(call);
  });
  const api = loadSource("./api.ts", {}, intervals);
  const state = loadSource("./literatureState.ts", {}, intervals);
  const actual = loadSource("./useLiterature.ts", {
    vue: { ...Vue, onBeforeUnmount: (callback) => cleanup.push(callback) },
    "./api": api,
    "./literatureState": state,
  }, intervals);
  const user = Vue.ref({
    id: "race-test-user", email: "race@example.invalid", verified: true, is_admin: false,
    csrf_token: "test-csrf", quotas: {}, credits: { available: 9, frozen: 0, checked_in: false },
  });
  const scope = Vue.effectScope();
  const store = scope.run(() => actual.useLiterature(user, (cause) => { throw cause; }));
  t.after(() => {
    for (const callback of cleanup) callback();
    scope.stop();
    globalThis.fetch = originalFetch;
  });
  return { store, user, requests, intervals };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const credit = (available, checked_in = false, frozen = 0) => ({ available, checked_in, frozen, items: [] });
const service = { enabled: true, worker_online: true, message: "测试服务" };
const job = {
  id: "new-job", kind: "literature", title: "Concurrent experiment", canonical_title: null, doi: null,
  state: "queued", message: "已提交", created_at: "2026-10-19T08:00:00Z", deadline: "2026-10-20T08:00:00Z",
  ready_at: null, download_expires_at: null, download_available: false, file_expired: false,
  credit_status: "frozen", candidates: [],
};

async function ready(h, initialCredits = credit(9), initialJobs = []) {
  for (const call of h.requests.filter((item) => !item.responded)) {
    call.respond(call.path === "/literature/status" ? service : call.path === "/credits" ? initialCredits : { items: initialJobs });
  }
  await settle();
  for (const call of h.requests.filter((item) => !item.responded && item.path === "/credits")) call.respond(initialCredits);
  await settle();
  assert.equal(h.store.canSubmit.value, true);
}

test("提交期间刷新取得的旧列表和旧余额不得覆盖新任务或停止轮询", async (t) => {
  const h = fixture(t);
  await ready(h);
  const submitting = h.store.submit(job.title);
  const post = h.requests.find((call) => call.method === "POST" && call.path === "/literature/jobs");
  assert.ok(post);
  const refreshing = h.store.refresh();
  const oldReads = h.requests.filter((call) => !call.responded && call.method === "GET" && call.path !== "/literature/status");
  for (const call of h.requests.filter((item) => !item.responded && item.path === "/literature/status")) call.respond(service);
  post.respond(job);
  await settle();
  for (const call of oldReads) call.respond(call.path === "/credits" ? credit(9) : { items: [] });
  for (const call of h.requests.filter((item) => !item.responded && item.method === "GET")) {
    call.respond(call.path === "/credits" ? credit(8, false, 1) : { items: [job] });
  }
  await Promise.all([submitting, refreshing]);
  await settle();
  assert.deepEqual(h.store.jobs.value.map((item) => item.id), [job.id]);
  assert.equal(h.store.selectedJob.value?.id, job.id);
  assert.equal(h.store.activeCount.value, 1);
  assert.equal(h.store.balance.value.available, 8);
  assert.equal(h.store.balance.value.frozen, 1);
  const previousReads = h.requests.length;
  for (const interval of h.intervals.values()) if (interval.delay === 5000) interval.callback();
  assert.equal(h.requests.length, previousReads + 1, "处理中任务应继续轮询");
});

test("签到期间读取的旧余额不得撤销已完成签到", async (t) => {
  const h = fixture(t);
  await ready(h);
  const checking = h.store.checkin();
  const post = h.requests.find((call) => call.path === "/credits/checkin");
  assert.ok(post);
  const reading = h.store.loadCredits();
  const oldRead = h.requests.find((call) => !call.responded && call.method === "GET" && call.path === "/credits");
  post.respond(credit(10, true));
  await checking;
  oldRead?.respond(credit(9, false));
  await reading;
  await settle();
  assert.equal(h.store.balance.value.available, 10);
  assert.equal(h.store.balance.value.checked_in, true);
  assert.equal(h.user.value.credits.available, 10);
  assert.equal(h.user.value.credits.checked_in, true);
});

test("外部模拟写操作完成后强制刷新余额，较早在途余额不能覆盖新账本", async (t) => {
  const h = fixture(t); await ready(h);
  const olderReading = h.store.loadCredits();
  const older = h.requests.find((call) => !call.responded && call.path === "/credits");
  const freshReading = h.store.loadCredits(true);
  const fresh = h.requests.filter((call) => !call.responded && call.path === "/credits").at(-1);
  assert.notEqual(older, fresh);
  fresh.respond(credit(8, false, 1)); await freshReading;
  older.respond(credit(9)); await olderReading;
  assert.equal(h.store.balance.value.available, 8);
  assert.equal(h.store.balance.value.frozen, 1);
  assert.equal(h.user.value.credits.available, 8);
});

test("零余额复用最近 200 条之外的全文后仍保持选中并可下载", async (t) => {
  const h = fixture(t);
  await ready(h, credit(0));
  const oldJob = {
    ...job, id: "older-ready-job", state: "succeeded", credit_status: "charged", download_available: true,
    ready_at: new Date().toISOString(), download_expires_at: new Date(Date.now() + 3 * 86400000).toISOString(),
  };
  const recentJobs = Array.from({ length: 200 }, (_, index) => ({ ...job, id: `recent-${index}`, state: "failed", credit_status: "refunded" }));
  const submitting = h.store.submit(oldJob.title);
  h.requests.find((call) => call.method === "POST").respond(oldJob);
  await settle();
  for (const call of h.requests.filter((item) => !item.responded && item.method === "GET")) {
    call.respond(call.path === "/credits" ? credit(0) : { items: recentJobs });
  }
  await settle();
  h.requests.find((call) => !call.responded && call.path === `/literature/jobs/${oldJob.id}`)?.respond(oldJob);
  await submitting;
  assert.equal(h.store.selectedJob.value?.id, oldJob.id);
  assert.equal(h.store.selectedJob.value?.download_available, true);
  assert.ok(h.store.jobs.value.some((item) => item.id === oldJob.id));
  assert.equal(h.store.balance.value.available, 0);
});

for (const transition of ["write", "account"]) {
  test(`缺页详情的迟到响应不能覆盖${transition === "write" ? "后续写操作" : "新账号任务"}`, async (t) => {
    const h = fixture(t);
    const previous = { ...job, id: "previous-job", state: "succeeded", credit_status: "charged" };
    await ready(h, credit(9), [previous]);
    const reading = h.store.loadJobs();
    h.requests.find((call) => !call.responded && call.path === "/literature/jobs").respond({ items: [] });
    await settle();
    const oldDetail = h.requests.find((call) => !call.responded && call.path === `/literature/jobs/${previous.id}`);
    assert.ok(oldDetail);
    let submitting;
    if (transition === "write") {
      submitting = h.store.submit("Next experiment");
      h.requests.find((call) => !call.responded && call.method === "POST").respond(job);
      await settle();
    } else {
      h.user.value = { ...h.user.value, id: "next-user", email: "next@example.invalid" };
    }
    for (const call of h.requests.filter((item) => !item.responded && ["/literature/jobs", "/credits"].includes(item.path)))
      call.respond(call.path === "/credits" ? credit(8, false, 1) : { items: [job] });
    if (submitting) await submitting;
    await settle();
    oldDetail.respond(previous);
    await reading;
    assert.equal(h.store.selectedJob.value?.id, job.id);
    assert.deepEqual(h.store.jobs.value.map((item) => item.id), [job.id]);
    assert.equal(h.store.balance.value.available, 8);
  });
}

for (const status of [404, 503]) {
  test(`缺页详情返回 ${status} 时${status === 404 ? "移除任务并显示真实消息" : "保留已确认任务并显示真实错误"}`, async (t) => {
    const h = fixture(t);
    const previous = { ...job, id: "previous-job", state: "succeeded", credit_status: "charged" };
    await ready(h, credit(9), [previous]);
    const reading = h.store.loadJobs();
    h.requests.find((call) => !call.responded && call.path === "/literature/jobs").respond({ items: [] });
    await settle();
    h.requests.find((call) => !call.responded && call.path === `/literature/jobs/${previous.id}`).respond({ detail: "测试任务暂不可用" }, status);
    await reading;
    assert.equal(h.store.selectedJob.value?.id ?? null, status === 404 ? null : previous.id);
    assert.equal(h.store.jobsError.value, "测试任务暂不可用");
  });
}
