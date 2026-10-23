import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";

const descriptor = parse(readFileSync(new URL("./App.vue", import.meta.url), "utf8")).descriptor;
const script = compileScript(descriptor, { id: "xrd-contract-test" });
const component = script.content;
const template = compileTemplate({ source: descriptor.template.content, filename: "App.vue", id: "xrd-contract-test", compilerOptions: { bindingMetadata: script.bindings } }).code;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const candidate = (number, overrides = {}) => ({
  id: `candidate-${number}`, code: `00-000-${String(number).padStart(4, "0")}`,
  phase_name: `测试物相 ${number}`, chemical_formula: "Mn O2", source: "PDF-2",
  content_type: "diffraction", exportable: true, ...overrides,
});
const searchJob = (overrides = {}) => ({
  id: "search-1", kind: "search", state: "succeeded", archive_status: "not_started",
  mail_status: "not_requested", message: "", results: [], candidates: [candidate(1)],
  candidate_total: 1, candidate_next_offset: null, search_version: 2,
  created_at: "2026-10-20T01:00:00Z", download_available: false, ...overrides,
});
const archiveJob = (overrides = {}) => ({
  ...searchJob(), id: "download-1", kind: "archive", state: "queued", candidates: [],
  candidate_total: 0, credits: { reserved: 1, charged: 0, status: "frozen" }, ...overrides,
});

test("真实渲染：索引的数据范围和截断提示在选卡收费前显示，完整库同号字段保持", async (t) => {
  const h = fixture(t, true);
  const index = candidate(1, {source: "索引来源", report_type_label: "索引报告",
    report_scope_detail: "未提供晶胞、空间群和参考文献；峰表可能截断。", possible_truncation: true, peak_count: 199});
  const full = candidate(2, {source: "完整来源", space_group: "Pmmn", report_type_label: "数据库报告",
    report_scope_detail: "包含原记录已提供的字段和峰表；缺失字段标为“未提供”。"});
  h.store.showView("search"); h.store.selectJob(searchJob({candidates:[index, full], candidate_total:2}));
  await Vue.nextTick();
  assert.match(h.text(), /索引报告/);
  assert.match(h.text(), /未提供晶胞、空间群和参考文献/);
  assert.match(h.text(), /峰表可能截断/);
  assert.match(h.text(), /Pmmn/);
  assert.equal(h.requests.some((item) => item.method === "POST"), false);
  h.store.toggleCandidate(index); await Vue.nextTick();
  assert.match(h.text(), /未提供晶胞、空间群和参考文献/);
  assert.equal(h.requests.some((item) => item.method === "POST"), false);
});

test("候选晶胞显示不把零缺失标记当作有效值，也不暴露float32尾数", (t) => {
  const h = fixture(t);
  assert.equal(h.store.candidateCell(candidate(1, {cell_parameters: {a:'0.0', b:null, c:'0', alpha:'0.0'}})), '');
  assert.equal(h.store.candidateCell(candidate(2, {cell_parameters: {a:'11.515999794006348', b:'3.5655999183654785', alpha:'90.0'}})), 'a=11.5160 · b=3.5656 · α=90.0000');
});

function evaluate(source, dependencies) {
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled)((name) => {
    if (!(name in dependencies)) throw new Error(`未配置依赖：${name}`);
    return dependencies[name];
  }, module, module.exports);
  return module.exports;
}

function fixture(t, rendered = false) {
  const originals = { fetch: globalThis.fetch, document: globalThis.document, crypto: globalThis.crypto,
    Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot,
    create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  const requests = [], downloads = [], cleanup = [];
  if (originals.crypto === undefined) Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
  globalThis.fetch = (url, options = {}) => new Promise((resolve) => {
    const call = {
      path: String(url).replace("/highscore/api", ""), method: options.method ?? "GET",
      body: options.body ? JSON.parse(options.body) : null, options, responded: false,
      respond(value, status = 200) {
        assert.equal(call.responded, false, `不能重复响应 ${call.path}`); call.responded = true;
        resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
      },
      respondBlob() {
        assert.equal(call.responded, false); call.responded = true;
        resolve(new Response(new Blob(["verified ZIP bytes"]), { status: 200, headers: { "Content-Type": "application/zip" } }));
      },
    };
    requests.push(call);
  });
  globalThis.document = {
    createElement() { return { href: "", download: "", click() { downloads.push({ href: this.href, name: this.download }); }, remove() {} }; },
    body: { appendChild() {} }, activeElement: null,
  };
  if (rendered) {
    globalThis.Document = class Document {};
    globalThis.ShadowRoot = class ShadowRoot {};
  }
  URL.createObjectURL = () => "blob:synthetic-xrd-download";
  URL.revokeObjectURL = () => {};
  const api = evaluate(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), {});
  const empty = Vue.ref([]), none = Vue.ref(null), falseRef = Vue.ref(false), message = Vue.ref("");
  const literature = { service: Vue.ref({ enabled: true, worker_online: true }), serviceError: falseRef,
    jobs: empty, jobsLoading: falseRef, jobsError: message, selectedId: none, selectedJob: none,
    credits: Vue.ref({ available: 10, frozen: 0, checked_in: false, items: [] }),
    balance: Vue.ref({ available: 10, frozen: 0, checked_in: false }), creditLoading: falseRef,
    creditBusy: falseRef, creditError: message, creditNotice: message, actionBusy: falseRef,
    actionError: message, actionNotice: message, now: Vue.ref(Date.now()), activeCount: Vue.ref(0), canSubmit: Vue.ref(true) };
  for (const key of ["loadService", "loadCredits", "checkin", "submit", "cancel", "select", "showJob", "refresh"]) literature[key] = async () => {};
  const deps = { vue: { ...Vue, onMounted() {}, onBeforeUnmount(callback) { cleanup.push(callback); } },
    "./api": api, "./analytics": { trackPage: async () => {} }, "./useLiterature": { useLiterature: () => literature } };
  deps["./pymatgenSession"] = evaluate(readFileSync(new URL("./pymatgenSession.ts", import.meta.url), "utf8"), {});
  const abtem = { service: none, serviceError: message, jobs: empty, jobsLoading: falseRef, jobsError: message, jobsNextOffset: none, selectedJob: none,
    sources: empty, sourcesLoading: falseRef, sourcesError: message, sourcesLoaded: falseRef, sourcesNextOffset: none,
    actionBusy: falseRef, actionError: message, actionNotice: message, pendingSubmission: none, activeCount: Vue.ref(0), canSubmit: falseRef };
  const abtemOpened = [];
  for (const key of ["submit", "cancel", "loadJobs", "loadSources", "reconcileSubmission", "refresh"]) abtem[key] = async () => {};
  abtem.showJob = async (id) => abtemOpened.push(id);
  deps["./useAbtem"] = { useAbtem: () => abtem };
  deps["./abtemState"] = evaluate(readFileSync(new URL("./abtemState.ts", import.meta.url), "utf8"), {});
  for (const name of ["Icon", "LiteratureRequest", "LiteratureTasks", "CreditsPanel", "LiteratureAdmin", "AdminCenter", "Announcements", "AbtemSimulation", "ResearchLibrary", "PymatgenTools"]) deps[`./${name}.vue`] = { default: Vue.defineComponent({ render: () => null }) };
  deps["./AdminCenter.vue"] = { default: Vue.defineComponent({
    props: ["user", "sessionGeneration"],
    emits: ["unauthorized", "announcementsChanged", "runtime"],
    setup(_props, { slots }) { return () => slots.settings?.() ?? null; },
  }) };
  const actual = evaluate(component, deps).default;
  const scope = Vue.effectScope();
  const store = scope.run(() => actual.setup({}, { expose() {} }));
  const account = { id: "xrd-ui-user", email: "xrd-ui@example.invalid", verified: true, is_admin: false,
    csrf_token: "synthetic-csrf", quotas: { search_daily_limit: 100, search_used: 0, archive_daily_limit: 20, archive_used: 0 },
    credits: { available: 10, frozen: 0, checked_in: false } };
  store.user.value = account;
  store.health.value = { status: "ok", service_enabled: true, worker_online: true, gates: {} };
  const node = (tag, text = "") => ({ tag, text, children: [], parent: null, props: {}, value: "", tagName: tag.toUpperCase(), addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document, getAttribute(key) { return this.props[key]; } });
  const root = node("root"); let app;
  if (rendered) {
    const renderer = Vue.createRenderer({
      createElement: (tag) => node(tag), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
      setText: (item, text) => { item.text = text; }, setElementText: (item, text) => { item.text = text; item.children = []; },
      parentNode: (item) => item.parent, nextSibling: (item) => item.parent?.children[item.parent.children.indexOf(item) + 1] ?? null,
      patchProp: (item, key, old, value) => { item.props[key] = value; },
      insert(item, parent, anchor) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(item); else parent.children.splice(index, 0, item); },
      remove(item) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = null; },
    });
    actual.render = evaluate(template, { vue: Vue }).render;
    app = renderer.createApp({ ...actual, setup: () => store }); app.mount(root);
  }
  t.after(() => {
    app?.unmount();
    for (const callback of cleanup) callback(); scope.stop();
    globalThis.fetch = originals.fetch;
    if (originals.document === undefined) delete globalThis.document; else globalThis.document = originals.document;
    if (originals.crypto === undefined) delete globalThis.crypto;
    for (const key of ["Document", "ShadowRoot"]) { if (originals[key] === undefined) delete globalThis[key]; else globalThis[key] = originals[key]; }
    URL.createObjectURL = originals.create; URL.revokeObjectURL = originals.revoke;
  });
  async function reads(jobs) {
    for (let round = 0; round < 4; round++) {
      await settle();
      for (const call of requests.filter((item) => !item.responded)) {
        if (call.path === "/jobs") call.respond({ items: jobs });
        else if (call.path === "/auth/me") call.respond(account);
      }
    }
  }
  const walk = (item = root) => [item, ...item.children.flatMap((child) => walk(child))];
  const text = (item = root) => item.tag === "#comment" ? "" : item.text + item.children.map((child) => text(child)).join("");
  const find = (predicate) => { const item = walk().find(predicate); assert.ok(item, "未找到检索页面目标元素"); return item; };
  const click = async (label) => { const item = find((item) => item.tag === "button" && text(item).trim() === label); assert.ok(!item.props.disabled, `${label} 不应禁用`); item.props.onClick?.({ preventDefault() {} }); await settle(); await Vue.nextTick(); };
  return { store, requests, downloads, account, reads, walk, text, find, click, abtemOpened };
}

test("模拟积分流水与任务中心打开同一个模拟任务，不进入 XRD 或文献接口", async (t) => {
  const h = fixture(t);
  await h.store.openCreditJob("simulation-ledger-job", "abtem");
  assert.equal(h.store.view.value, "abtem");
  assert.deepEqual(h.abtemOpened, ["simulation-ledger-job"]);
  await h.store.openAbtemTask("simulation-history-job");
  assert.deepEqual(h.abtemOpened, ["simulation-ledger-job", "simulation-history-job"]);
  assert.equal(h.requests.length, 0);
});

test("XRD 只保留卡片编号和化学式两个检索方式", (t) => {
  const h = fixture(t);
  assert.deepEqual(h.store.modes.map((item) => item.id), ["codes", "formula"]);
});

test("真实渲染：XRD取消每日额度，0和旧正数限额均只展示全站共用积分", async (t) => {
  for (const limit of [0, 10]) await t.test(`旧检索限额=${limit}`, async (child) => {
    const h = fixture(child, true);
    h.store.user.value = { ...h.account,
      quotas: { search_daily_limit: limit, search_used: 129, archive_daily_limit: 20, archive_used: 99 },
      credits: { available: 999, frozen: 999, checked_in: false } };
    h.store.creditBalance.value = { available: 7, frozen: 2, checked_in: false };
    h.store.showView("search"); await Vue.nextTick();
    const workbench = h.text().replace(/\s+/g, "");
    assert.match(workbench, /免费检索不限次数/);
    assert.match(workbench, /可用积分7/);
    assert.match(workbench, /冻结积分2/);
    assert.doesNotMatch(workbench, /今日可检索|今日可归档|129\/|99\/|\/0次|\/10次|\/20张|NaN|Infinity/);
    h.store.creditBalance.value = { available: 6, frozen: 3, checked_in: false }; await Vue.nextTick();
    assert.match(h.text().replace(/\s+/g, ""), /可用积分6冻结积分3/);
    h.store.showView("account"); await Vue.nextTick();
    const account = h.text().replace(/\s+/g, "");
    assert.match(account, /可用积分6冻结积分3/);
    assert.match(account, /XRD检索免费/);
    assert.match(account, /成功归档每张1积分/);
    assert.doesNotMatch(account, /今日文字检索|今日卡片归档|每日.*配额|配额.*重置|129\/|99\/|\/10次|\/20张|NaN|Infinity/);
  });
});

test("旧每日检索和归档额度耗尽仍允许免费检索及按积分选卡", async (t) => {
  const h = fixture(t, true);
  h.store.user.value = { ...h.account, quotas: { search_daily_limit: 10, search_used: 129, archive_daily_limit: 20, archive_used: 99 } };
  h.store.mode.value = "formula"; h.store.query.value = "mno2";
  h.store.showView("search"); await Vue.nextTick();
  const query = h.find((item) => item.tag === "button" && String(item.props.class).includes("search-submit"));
  assert.equal(query.props.disabled, false);
  assert.equal(h.store.canSubmit.value, true);
  const search = searchJob(); h.store.selectJob(search); h.store.toggleCandidate(search.candidates[0]);
  await Vue.nextTick();
  assert.equal(h.store.canArchiveSelection.value, true);
  const download = h.find((item) => item.tag === "button" && h.text(item).includes("下载所选"));
  assert.equal(download.props.disabled, false);
});

test("真实渲染：管理员取消每日额度入口，旧响应字段不进入可编辑设置", async (t) => {
  const h = fixture(t, true), administrator = { ...h.account, is_admin: true };
  h.store.user.value = administrator;
  const flags = { license_confirmed: true, cloud_validated: true, public_enabled: true };
  const status = { settings: { ...flags, search_daily_limit: 10, archive_daily_limit: 20 }, worker_online: true, active_jobs: 0, queue_length: 0 };
  const loading = h.store.loadAdmin();
  h.requests.find((item) => item.path === "/admin/status").respond(status);
  await loading;
  assert.deepEqual({ ...h.store.settings }, flags);
  h.store.showView("admin"); await Vue.nextTick();
  assert.equal(h.walk().filter((item) => item.tag === "input" && item.props.type === "number").length, 0);
  assert.doesNotMatch(h.text(), /每账号每日|每日上限|每日配额|0\s*表示不限次数/);
});

test("管理员保存运行设置仅提交三个公开标志，兼容旧缓存但不再提交额度", async (t) => {
  const h = fixture(t, true), administrator = { ...h.account, is_admin: true };
  h.store.user.value = administrator;
  const flags = { license_confirmed: true, cloud_validated: true, public_enabled: false };
  Object.assign(h.store.settings, flags, { search_daily_limit: 10, archive_daily_limit: 20 });
  const status = { settings: { ...h.store.settings }, worker_online: true, active_jobs: 0, queue_length: 0 };
  h.store.adminStatus.value = status;
  const saving = h.store.saveSettings();
  const patch = h.requests.find((item) => item.path === "/admin/settings" && item.method === "PATCH");
  assert.deepEqual(patch.body, flags);
  patch.respond({}); await settle();
  for (const call of h.requests.filter((item) => !item.responded)) {
    if (call.path === "/admin/status") call.respond(status);
    else if (call.path === "/health") call.respond(h.store.health.value);
    else if (call.path === "/auth/me") call.respond(administrator);
  }
  await saving;
  assert.equal(h.store.notice.value, "运行设置已保存。");
});

test("编号在衍射和结构中均只提交免费候选检索，不直接归档或邮件", async (t) => {
  for (const content of ["diffraction", "structure"]) await t.test(content, async (child) => {
    const h = fixture(child); h.store.contentType.value = content;
    h.store.query.value = content === "structure" ? "ICSD-34807" : "00-046-1045";
    const pending = h.store.submitQuery();
    const call = h.requests.find((item) => item.method === "POST");
    assert.equal(call.path, "/jobs/search");
    assert.deepEqual({ mode: call.body.mode, query: call.body.query, content: call.body.content, search_version: call.body.search_version },
                     { mode: "codes", query: h.store.query.value, content, search_version: 2 });
    assert.equal("send_mail" in call.body, false);
    call.respond(searchJob()); await h.reads([searchJob()]); await pending;
    assert.equal(h.requests.some((item) => item.path === "/jobs/archive"), false);
  });
});

test("化学式 MnO2 原输入提交 v2 免费搜索，不改写源公式", async (t) => {
  const h = fixture(t); h.store.mode.value = "formula"; h.store.query.value = "MnO2";
  const pending = h.store.submitQuery(); const call = h.requests.find((item) => item.method === "POST");
  assert.equal(call.path, "/jobs/search");
  assert.equal(call.body.query, "MnO2"); assert.equal(call.body.search_version, 2);
  assert.equal(call.body.content, "diffraction");
  call.respond(searchJob()); await h.reads([searchJob()]); await pending;
  assert.equal(h.store.selectedJob.value.candidates[0].chemical_formula, "Mn O2");
});

test("未选择候选不能创建归档；unsupported 保留说明并禁选", async (t) => {
  const h = fixture(t); h.store.selectJob(searchJob());
  await h.store.archiveSelection(); assert.equal(h.requests.length, 0);
  h.store.toggleCandidate(candidate(2, { exportable: false, support_message: "原占有率尚未核验，暂不可下载" }));
  assert.deepEqual(h.store.selectedCandidates.value, []);
  assert.match(h.store.error.value, /暂不可下载/);
});

test("全量候选分页、跨页选择与轮询刷新保持，阻止同号不同来源误选", async (t) => {
  const h = fixture(t), first = Array.from({ length: 50 }, (_, i) => candidate(i + 1));
  const job = searchJob({ candidates: first, candidate_total: 101, candidate_next_offset: 50 });
  h.store.selectJob(job); h.store.jobs.value = [job]; h.store.toggleCandidate(first[0]);
  const pending = h.store.loadCandidatePage(50);
  const call = h.requests.find((item) => item.path === "/jobs/search-1/candidates?offset=50&limit=50");
  assert.ok(call);
  const second = [candidate(51, { code: first[0].code, source: "PDF4索引" }), ...Array.from({ length: 49 }, (_, i) => candidate(i + 52))];
  call.respond({ items: second, total: 101, next_offset: 100 }); await pending;
  h.store.toggleCandidate(second[0]);
  assert.deepEqual(h.store.selectedCandidates.value, [first[0].id]);
  assert.match(h.store.error.value, /同号|同一编号/);
  h.store.toggleCandidate(second[1]);
  const reading = h.store.loadJobs(); await h.reads([job]); await reading;
  assert.deepEqual(h.store.selectedCandidates.value, [first[0].id, second[1].id]);
  assert.deepEqual(h.store.visibleCandidates.value.map((item) => item.id), second.map((item) => item.id));
  assert.equal(h.store.candidateTotal.value, 101);
  const last = h.store.loadCandidatePage(100);
  h.requests.find((item) => !item.responded && item.path.includes("offset=100")).respond({ items: [candidate(101)], total: 101, next_offset: null });
  await last;
  assert.deepEqual(h.store.visibleCandidates.value.map((item) => item.id), ["candidate-101"]);
  await h.store.loadCandidatePage(0);
  assert.deepEqual(h.store.selectedCandidates.value, [first[0].id, second[1].id]);
  h.store.toggleCandidate(first[0]); h.store.toggleCandidate(second[0]);
  assert.deepEqual(h.store.selectedCandidates.value, [second[1].id, second[0].id]);
});

test("分页错误保留当前候选和选择，旧查询迟到页不能覆盖新查询", async (t) => {
  const h = fixture(t); const original = searchJob({ candidate_total: 51, candidate_next_offset: 50 });
  h.store.selectJob(original); h.store.toggleCandidate(original.candidates[0]);
  const loading = h.store.loadCandidatePage(50); const call = h.requests.find((item) => item.path.includes("/candidates?"));
  call.respond({ detail: "分页暂不可用，请重试" }, 503); await loading;
  assert.deepEqual(h.store.visibleCandidates.value.map((item) => item.id), ["candidate-1"]);
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-1"]);
  const oldPage = h.store.loadCandidatePage(50), late = h.requests.find((item) => !item.responded);
  h.store.selectJob(searchJob({ id: "search-2", candidates: [candidate(8)] }));
  late.respond({ items: [candidate(51)], total: 51, next_offset: null }); await oldPage;
  assert.deepEqual(h.store.visibleCandidates.value.map((item) => item.id), ["candidate-8"]);
});

test("主动下载所选固定不邮件，归档成功后自动下载一次，历史打开不触发", async (t) => {
  const h = fixture(t), search = searchJob(); h.store.selectJob(search);
  h.store.toggleCandidate(search.candidates[0]);
  const submitting = h.store.archiveSelection(), post = h.requests.find((item) => item.method === "POST");
  assert.equal(post.path, "/jobs/archive"); assert.equal(post.body.send_mail, false);
  assert.equal(post.body.search_job_id, search.id); assert.deepEqual(post.body.candidate_ids, ["candidate-1"]);
  const queued = archiveJob(); post.respond(queued); await h.reads([queued, search]); await submitting;
  const done = archiveJob({ state: "succeeded", archive_status: "success", download_available: true,
    credits: { reserved: 1, charged: 1, status: "charged" } });
  const polling = h.store.loadJobs(); await h.reads([done, search]);
  const download = h.requests.find((item) => item.path === "/jobs/download-1/download");
  assert.ok(download); download.respondBlob(); await polling; await settle();
  assert.equal(h.downloads.length, 1);
  const again = h.store.loadJobs(); await h.reads([done, search]); await again;
  h.store.selectJob(done); const history = h.store.loadJobs(); await h.reads([done]); await history;
  assert.equal(h.requests.filter((item) => item.path === "/jobs/download-1/download").length, 1);
  assert.equal(h.downloads.length, 1);
});

test("归档提交失败保留选择；主动 ZIP 下载失败仍保留手动下载且不循环重下", async (t) => {
  const h = fixture(t), search = searchJob(); h.store.selectJob(search); h.store.toggleCandidate(search.candidates[0]);
  const failed = h.store.archiveSelection(); h.requests.find((item) => item.method === "POST").respond({ detail: "积分不足，请减少选择" }, 402); await failed;
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-1"]); assert.equal(h.store.selectedJob.value.id, search.id);
  const submitting = h.store.archiveSelection(), post = h.requests.find((item) => item.method === "POST" && !item.responded);
  const queued = archiveJob(); post.respond(queued); await h.reads([queued, search]); await submitting;
  const done = archiveJob({ state: "succeeded", archive_status: "success", download_available: true });
  const polling = h.store.loadJobs(); await h.reads([done, search]);
  const download = h.requests.find((item) => item.path === "/jobs/download-1/download");
  assert.ok(download); download.respond({ detail: "下载暂不可用" }, 503); await polling;
  const current = h.store.jobs.value.find((item) => item.id === done.id);
  assert.equal(current.download_available, true);
  const again = h.store.loadJobs(); await h.reads([done, search]); await again;
  assert.equal(h.requests.filter((item) => item.path === "/jobs/download-1/download").length, 1);
});

test("较早任务列表不能覆盖主动搜索后的新任务并停止轮询", async (t) => {
  const h = fixture(t); const oldReading = h.store.loadJobs(), old = h.requests.find((item) => item.path === "/jobs");
  h.store.query.value = "00-046-1045"; const submitting = h.store.submitQuery();
  const post = h.requests.find((item) => item.method === "POST");
  const fresh = searchJob({ state: "queued", candidates: [], candidate_total: 0 }); post.respond(fresh);
  await settle();
  for (const call of h.requests.filter((item) => !item.responded && item !== old)) {
    if (call.path === "/jobs") call.respond({ items: [fresh] }); else if (call.path === "/auth/me") call.respond(h.account);
  }
  await submitting; old.respond({ items: [] }); await oldReading;
  assert.equal(h.store.jobs.value.some((job) => job.id === fresh.id), true);
  assert.equal(h.store.hasActiveJob.value, true);
});

test("页面重新打开历史成功任务时只保留手动下载，不自动下载", async (t) => {
  const h = fixture(t, true); const done = archiveJob({ state: "succeeded", archive_status: "success", download_available: true });
  h.store.selectHistoryJob(done); h.store.showView("tasks"); const reading = h.store.loadJobs(); await h.reads([done]); await reading; await Vue.nextTick();
  assert.ok(h.walk().some((item) => item.tag === "a" && item.props.href === "/highscore/api/jobs/download-1/download"));
  assert.equal(h.requests.some((item) => item.path.endsWith("/download")), false);
  assert.equal(h.downloads.length, 0);
});

test("退出发生在自动下载请求中时不保存文件，也不保留旧账号候选和任务", async (t) => {
  const h = fixture(t), search = searchJob(); h.store.selectJob(search); h.store.toggleCandidate(search.candidates[0]);
  const submitting = h.store.archiveSelection(), post = h.requests.find((item) => item.method === "POST");
  const queued = archiveJob(); post.respond(queued); await h.reads([queued, search]); await submitting;
  const done = archiveJob({ state: "succeeded", archive_status: "success", download_available: true });
  const polling = h.store.loadJobs(); await h.reads([done, search]);
  const download = h.requests.find((item) => item.path === "/jobs/download-1/download"); assert.ok(download);
  h.store.user.value = null; download.respondBlob(); await polling;
  assert.equal(h.downloads.length, 0);
  assert.equal(h.store.selectedJob.value, null);
  assert.deepEqual(h.store.selectedCandidates.value, []);
  assert.deepEqual(h.store.jobs.value, []);
});

test("编号查询和下载选择各自限制20项，不提前发归档请求", async (t) => {
  const h = fixture(t);
  h.store.query.value = Array.from({ length: 21 }, (_, i) => candidate(i + 1).code).join(" ");
  await h.store.submitQuery(); assert.equal(h.requests.length, 0); assert.match(h.store.error.value, /20/);
  h.store.selectJob(searchJob());
  for (let i = 1; i <= 21; i++) h.store.toggleCandidate(candidate(i));
  assert.equal(h.store.selectedCandidates.value.length, 20);
  assert.match(h.store.error.value, /20/);
  assert.equal(h.requests.length, 0);
});

test("积分记录跳转的迟到401不得清退新账号或重新登录后的会话", async (t) => {
  for (const transition of ["切换账号", "同账号重新登录"]) await t.test(transition, async (child) => {
    const h = fixture(child);
    const pending = h.store.openCreditJob("old-account-job", "xrd");
    const call = h.requests.find((item) => item.path === "/jobs/old-account-job");
    assert.ok(call);
    h.store.user.value = null;
    const current = { ...h.account, id: transition === "切换账号" ? "current-account" : h.account.id, csrf_token: "new-session-csrf" };
    h.store.user.value = current;
    const currentJob = searchJob({ id: "current-account-search" });
    h.store.jobs.value = [currentJob]; h.store.selectJob(currentJob);
    h.store.toggleCandidate(currentJob.candidates[0]);
    h.store.view.value = "credits"; h.store.error.value = "当前会话的提示";
    call.respond({ detail: "旧会话已失效" }, 401); await pending;
    assert.equal(h.store.user.value?.id, current.id);
    assert.equal(h.store.user.value?.csrf_token, "new-session-csrf");
    assert.equal(h.store.view.value, "credits");
    assert.equal(h.store.error.value, "当前会话的提示");
    assert.equal(h.store.selectedJob.value?.id, currentJob.id);
    assert.deepEqual(h.store.selectedCandidates.value, ["candidate-1"]);
    assert.deepEqual(h.store.jobs.value.map((job) => job.id), [currentJob.id]);
  });
});

test("积分记录跳转的当前会话401仍正常清退失效登录", async (t) => {
  const h = fixture(t); const pending = h.store.openCreditJob("expired-job", "xrd");
  h.requests.find((item) => item.path === "/jobs/expired-job").respond({ detail: "请重新登录" }, 401);
  await pending;
  assert.equal(h.store.user.value, null);
  assert.equal(h.store.view.value, "account");
  assert.equal(h.store.error.value, "请重新登录");
});

test("真实渲染：MnO2 查询、处理中、候选及下载始终留在检索页面", async (t) => {
  const h = fixture(t, true); h.store.showView("search"); await Vue.nextTick();
  await h.click("化学式");
  h.find((item) => item.tag === "input" && item.props["aria-label"] === "检索内容").props["onUpdate:modelValue"]("MnO2");
  await Vue.nextTick();
  h.find((item) => item.tag === "form").props.onSubmit({ preventDefault() {} }); await settle();
  const post = h.requests.find((item) => item.path === "/jobs/search");
  assert.equal(post.body.query, "MnO2"); assert.equal(post.body.mode, "formula");
  const queued = searchJob({ state: "queued", candidates: [], candidate_total: 0 });
  post.respond(queued); await h.reads([queued]); await Vue.nextTick();
  assert.equal(h.store.view.value, "search"); assert.match(h.text(), /排队中/); assert.doesNotMatch(h.text(), /未发现该卡片信息/);
  const done = searchJob({ candidates: [candidate(1, { phase_name: "二氧化锰", chemical_formula: "Mn O2" })] });
  const polling = h.store.loadJobs(); await h.reads([done]); await polling; await Vue.nextTick();
  assert.match(h.text(), /二氧化锰/); assert.match(h.text(), /Mn O2/);
  h.find((item) => item.tag === "input" && item.props.type === "checkbox").props.onChange(); await Vue.nextTick();
  await h.click("下载所选 1 项");
  const archive = h.requests.find((item) => item.path === "/jobs/archive"); assert.ok(archive);
  archive.respond(archiveJob()); await h.reads([archiveJob(), done]); await Vue.nextTick();
  assert.equal(h.store.view.value, "search"); assert.match(h.text(), /二氧化锰/); assert.match(h.text(), /所选报告/);
});

test("真实渲染：任务中心选择其他任务后返回检索页，当前公式和候选不能被替换", async (t) => {
  const h = fixture(t, true); h.store.showView("search"); h.store.mode.value = "formula"; h.store.query.value = "MnO2";
  h.store.selectJob(searchJob({ candidates: [candidate(1, { phase_name: "当前二氧化锰" })] }));
  h.store.toggleCandidate(candidate(1)); await Vue.nextTick();
  h.store.showView("tasks"); h.store.selectHistoryJob(archiveJob({ id: "old-archive", state: "succeeded", download_available: true }));
  h.store.showView("search"); await Vue.nextTick();
  assert.equal(h.store.query.value, "MnO2");
  assert.match(h.text(), /当前二氧化锰/);
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-1"]);
  assert.doesNotMatch(h.text(), /old-archive/);
});

test("真实渲染：同次检索的多个下载批次都保留在检索页，重复下载沿用同一任务链接", async (t) => {
  const h = fixture(t, true), search = searchJob({ candidates: [candidate(1), candidate(2)], candidate_total: 2 });
  h.store.showView("search"); h.store.selectJob(search); await Vue.nextTick();
  const completed = [];
  for (const number of [1, 2]) {
    h.store.toggleCandidate(candidate(number)); const submitting = h.store.archiveSelection();
    const done = archiveJob({ id: `download-${number}`, state: "succeeded", download_available: true, archive_status: "success", results: [{ code: candidate(number).code, status: "success" }] });
    h.requests.find((item) => item.path === "/jobs/archive" && !item.responded).respond(done);
    completed.push(done); await h.reads([...completed, search]);
    h.requests.find((item) => item.path === `/jobs/download-${number}/download` && !item.responded).respondBlob();
    await submitting; await Vue.nextTick();
  }
  const links = h.walk().filter((item) => item.tag === "a").map((item) => item.props.href);
  assert.ok(links.includes("/highscore/api/jobs/download-1/download"), "第一批的免费下载链接不能被下一批覆盖");
  assert.ok(links.includes("/highscore/api/jobs/download-2/download"));
  assert.equal(h.store.view.value, "search");
});

test("真实渲染：重新加载恢复真实检索条件及所属报告，不自动重新下载或扣分", async (t) => {
  const h = fixture(t, true);
  const previous = searchJob({ search_query: "mno2", search_mode: "formula", content_type: "diffraction" });
  const owned = archiveJob({ id: "owned-report", state: "succeeded", archive_status: "success", download_available: true, search_job_id: previous.id });
  const other = archiveJob({ id: "other-query-report", state: "succeeded", download_available: true, search_job_id: "other-search" });
  const reading = h.store.loadJobs(); await h.reads([owned, other, previous]); await reading;
  h.store.showView("search"); await Vue.nextTick();
  assert.equal(h.store.query.value, "mno2"); assert.equal(h.store.mode.value, "formula");
  assert.match(h.text(), /化学式：mno2/);
  assert.ok(h.walk().some((item) => item.tag === "a" && item.props.href === "/highscore/api/jobs/owned-report/download"));
  assert.equal(h.walk().some((item) => item.tag === "a" && item.props.href === "/highscore/api/jobs/other-query-report/download"), false);
  assert.equal(h.requests.some((item) => item.path.endsWith("/download") || item.method === "POST"), false);
  assert.equal(h.downloads.length, 0);
});

test("真实渲染：任务中心只在显式查看候选时切换检索上下文，恢复结构类型和原输入", async (t) => {
  const h = fixture(t, true), current = searchJob({ search_query: "MnO2", search_mode: "formula", content_type: "diffraction" });
  const historical = searchJob({ id: "old-structure", search_query: "ICSD-12345", search_mode: "codes", content_type: "structure", candidates: [candidate(9, { code: "ICSD-012345", phase_name: "历史结构", content_type: "structure", site_count: 3 })] });
  h.store.selectJob(current); h.store.toggleCandidate(current.candidates[0]);
  h.store.jobs.value = [historical, current]; h.store.showView("tasks"); await Vue.nextTick();
  const row = h.find((item) => item.tag === "button" && String(item.props.class).includes("task-row") && h.text(item).includes("OLD-STRU"));
  row.props.onClick(); await Vue.nextTick();
  assert.equal(h.store.currentSearchJob.value.id, current.id);
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-1"]);
  await h.click("查看并选择卡片");
  assert.equal(h.store.view.value, "search"); assert.equal(h.store.query.value, "ICSD-12345");
  assert.equal(h.store.mode.value, "codes"); assert.equal(h.store.contentType.value, "structure");
  assert.match(h.text(), /历史结构/); assert.deepEqual(h.store.selectedCandidates.value, []);
});

test("旧查询的归档迟到响应不能清空新查询选择、提示或混入新查询下载区", async (t) => {
  const h = fixture(t, true), original = searchJob(); h.store.showView("search"); h.store.selectJob(original); h.store.toggleCandidate(original.candidates[0]);
  const submitting = h.store.archiveSelection(), post = h.requests.find((item) => item.path === "/jobs/archive");
  const next = searchJob({ id: "new-search", search_query: "ZnO", search_mode: "formula", content_type: "diffraction", candidates: [candidate(8, { phase_name: "当前氧化锌", chemical_formula: "Zn O" })] });
  h.store.selectJob(next); h.store.toggleCandidate(next.candidates[0]); h.store.notice.value = "当前查询提示";
  post.respond(archiveJob()); await h.reads([archiveJob(), next, original]); await submitting; await Vue.nextTick();
  assert.equal(h.store.currentSearchJob.value.id, next.id); assert.equal(h.store.notice.value, "当前查询提示");
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-8"]);
  assert.match(h.text(), /当前氧化锌/); assert.doesNotMatch(h.text(), /所选报告/);
  assert.equal(h.store.searchDownloadJobs.value[original.id][0].id, "download-1");
  h.store.selectJob(original); await Vue.nextTick(); assert.match(h.text(), /所选报告/);
});

test("检索提交期间编辑输入，迟到任务不得覆盖新输入或恢复旧候选", async (t) => {
  const h = fixture(t); h.store.mode.value = "formula"; h.store.query.value = "MnO2";
  const submitting = h.store.submitQuery(); h.store.query.value = "ZnO";
  const original = searchJob({ search_query: "MnO2", search_mode: "formula", content_type: "diffraction" });
  h.requests.find((item) => item.path === "/jobs/search").respond(original); await h.reads([original]); await submitting;
  assert.equal(h.store.query.value, "ZnO"); assert.equal(h.store.currentSearchJob.value, null);
  assert.deepEqual(h.store.visibleCandidates.value, []);
});

test("分页迟到401与归档迟到响应不得影响新账号的检索和下载批次", async (t) => {
  const h = fixture(t), original = searchJob({ candidate_total: 51, candidate_next_offset: 50 });
  h.store.selectJob(original); const page = h.store.loadCandidatePage(50);
  const latePage = h.requests.find((item) => item.path.includes("/candidates?"));
  h.store.toggleCandidate(original.candidates[0]); const archiving = h.store.archiveSelection();
  const lateArchive = h.requests.find((item) => item.path === "/jobs/archive");
  h.store.user.value = null; h.store.user.value = { ...h.account, id: "next-account" };
  const next = searchJob({ id: "new-account-search", candidates: [candidate(7)] }); h.store.selectJob(next); h.store.toggleCandidate(next.candidates[0]);
  h.store.view.value = "search"; h.store.error.value = "当前账号提示";
  latePage.respond({ detail: "旧会话失效" }, 401); lateArchive.respond(archiveJob()); await page; await archiving;
  assert.equal(h.store.user.value.id, "next-account"); assert.equal(h.store.currentSearchJob.value.id, next.id);
  assert.deepEqual(h.store.selectedCandidates.value, ["candidate-7"]); assert.deepEqual(h.store.searchReports.value, []);
  assert.equal(h.store.error.value, "当前账号提示"); assert.equal(h.store.view.value, "search");
});
