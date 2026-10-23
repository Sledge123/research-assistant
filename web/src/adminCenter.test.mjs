import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";

const settle = async () => { await new Promise((resolve) => setImmediate(resolve)); await Vue.nextTick(); };
const account = (id = "admin-a", extra = {}) => ({ id, email: `${id}@example.invalid`, verified: true,
  is_admin: true, csrf_token: "test-csrf", quotas: {}, credits: { available: 10, frozen: 0, checked_in: false }, ...extra });
const userRow = (id, extra = {}) => ({ id, email: `${id}@example.invalid`, verified: true, is_admin: false,
  is_debug: false, debug_feature: null, created_at: "2026-10-20T01:00:00Z", available: 10, frozen: 2, xrd_jobs: 1, literature_jobs: 2, ...extra });
const detail = (id, extra = {}) => ({ user: userRow(id), credits: { available: 10, frozen: 2, checked_in: false, items: [] }, recent_jobs: [], ...extra });
const overview = { users: { total: 23, verified: 21, admins: 2, regular_total: 17, regular_verified: 15, regular_admins: 1, debug_total: 6 }, jobs: { xrd_total: 41, literature_total: 12, queued: 3, running: 1 }, credits: { available: 109, frozen: 4 }, recent_activity: [] };

function evaluate(source, dependencies) {
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const result = { exports: {} };
  new Function("require", "module", "exports", code)((name) => {
    if (!(name in dependencies)) throw new Error(`测试缺少依赖 ${name}`);
    return dependencies[name];
  }, result, result.exports);
  return result.exports;
}

// 用 Vue 真正渲染组件；仅替代浏览器 DOM、HTTP 与会话存储这三个外部边界。
function fixture(t, options = {}) {
  const originals = { fetch: globalThis.fetch, document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, crypto: globalThis.crypto, sessionStorage: globalThis.sessionStorage };
  if (!globalThis.crypto) Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
  Object.defineProperty(globalThis, "document", { configurable: true, writable: true, value: { activeElement: null } });
  Object.defineProperty(globalThis, "Document", { configurable: true, value: class Document {} });
  Object.defineProperty(globalThis, "ShadowRoot", { configurable: true, value: class ShadowRoot {} });
  const storage = options.storage ?? new Map();
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key),
  } });
  const calls = [], unauthorized = [];
  globalThis.fetch = (url, init = {}) => new Promise((resolve, reject) => {
    const item = { path: String(url).replace("/highscore/api", ""), method: init.method ?? "GET", headers: new Headers(init.headers),
      body: init.body ? JSON.parse(init.body) : null, done: false,
      respond(value, status = 200) { assert.equal(item.done, false); item.done = true; resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } })); },
      fail() { item.done = true; reject(new TypeError("网络中断")); } };
    calls.push(item);
  });
  const node = (tag, text = "") => ({ tag, text, children: [], parent: null, props: {}, value: "", tagName: tag.toUpperCase(), get options() { return this.children.filter(item => item.tag === "option"); }, addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document, getAttribute(key) { return this.props[key]; } });
  const renderer = Vue.createRenderer({
    createElement: (tag) => node(tag), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
    setText: (item, text) => { item.text = text; }, setElementText: (item, text) => { item.text = text; item.children = []; },
    parentNode: (item) => item.parent, nextSibling: (item) => item.parent?.children[item.parent.children.indexOf(item) + 1] ?? null,
    patchProp: (item, key, old, value) => { item.props[key] = value; },
    insert(item, parent, anchor) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(item); else parent.children.splice(index, 0, item); },
    remove(item) { if (item.parent) item.parent.children.splice(item.parent.children.indexOf(item), 1); item.parent = null; },
  });
  const api = evaluate(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), {});
  const source = readFileSync(new URL(`./${options.component ?? "AdminCenter"}.vue`, import.meta.url), "utf8");
  const compiled = compileScript(parse(source).descriptor, { id: "admin-ui-contract", inlineTemplate: true }).content;
  const deps = { vue: Vue, "./api": api, "./Icon.vue": { default: Vue.defineComponent({ render: () => null }) } };
  const analyticsSource = readFileSync(new URL("./AnalyticsPanel.vue", import.meta.url), "utf8");
  deps["./AnalyticsPanel.vue"] = { default: evaluate(compileScript(parse(analyticsSource).descriptor,
    { id: "analytics-ui-contract", inlineTemplate: true }).content, deps).default };
  const component = evaluate(compiled, deps).default;
  const props = Vue.reactive({ user: options.user ?? account(), version: 0 });
  const root = node("root"); let disposed = false;
  const changed = [];
  const app = renderer.createApp({ setup: () => () => Vue.h(component, options.component === "Announcements" ? { version: props.version } : { user: props.user, ...(!options.component || options.component === "AnalyticsPanel" ? { sessionGeneration: props.version } : {}), onUnauthorized: (cause) => unauthorized.push(cause), onAnnouncementsChanged: () => changed.push(true) }, { settings: () => Vue.h("p", "原有运行控制") }) });
  app.mount(root);
  const walk = (item = root) => [item, ...item.children.flatMap((child) => walk(child))];
  const text = (item = root) => item.tag === "#comment" ? "" : item.text + item.children.map((child) => text(child)).join("");
  const find = (predicate) => { const value = walk().find(predicate); assert.ok(value, "未找到目标界面元素"); return value; };
  const click = async (label) => { const element = find((item) => item.tag === "button" && text(item).trim() === label); assert.ok(!element.props.disabled, `${label} 不应被禁用`); element.props.onClick?.({ preventDefault() {} }); await settle(); };
  const input = async (id, value) => { const element = find((item) => item.props.id === id); element.props["onUpdate:modelValue"](value); await settle(); };
  const submit = async (label) => { const form = find((item) => item.tag === "form" && item.props["aria-label"] === label); form.props.onSubmit({ preventDefault() {} }); await settle(); };
  const call = (path, method = "GET") => { const value = calls.find((item) => !item.done && item.path === path && item.method === method); assert.ok(value, `没有待响应请求 ${method} ${path}`); return value; };
  const unmount = () => { if (!disposed) { disposed = true; app.unmount(); } };
  t.after(() => { unmount(); for (const key of Object.keys(originals)) { if (originals[key] === undefined) delete globalThis[key]; else Object.defineProperty(globalThis, key, { configurable: true, writable: true, value: originals[key] }); } });
  return { props, root, calls, unauthorized, changed, storage, text, find, click, input, submit, call, unmount };
}

test("系统管理中心显示服务器概览并保留运行设置入口", async (t) => {
  const h = fixture(t);
  h.call("/admin/overview").respond(overview); await settle();
  assert.match(h.text(), /系统管理中心/);
  assert.match(h.text(), /23/); assert.match(h.text(), /109/);
  await h.click("运行设置"); assert.match(h.text(), /原有运行控制/);
});

test("概览以正式账号为主，分别显示调试账号与全部账号数量", async (t) => {
  const h = fixture(t); h.call("/admin/overview").respond(overview); await settle();
  const statistics = h.find((item) => item.tag === "article" && item.props["aria-label"] === "账号统计");
  assert.match(h.text(statistics), /正式账号17/);
  assert.match(h.text(statistics), /已验证 15 · 管理员 1/);
  assert.match(h.text(statistics), /调试账号 6 · 全部账号 23/);
});

test("用户默认筛选正式账号，调试账号按功能或历史调试标记且角色保持独立", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  const selected = h.find((item) => item.tag === "button" && h.text(item) === "正式账号");
  assert.equal(selected.props["aria-pressed"], true);
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("regular")], total: 1, next_offset: null }); await settle();
  await h.click("调试账号");
  const debugUsers = [
    userRow("library-debug", { is_debug: true, debug_feature: "research_library" }),
    userRow("literature-debug", { is_debug: true, debug_feature: "literature" }),
    userRow("pymatgen-debug", { is_debug: true, debug_feature: "pymatgen" }),
    userRow("xrd-debug", { is_debug: true, debug_feature: "xrd" }),
    userRow("abtem-debug", { is_debug: true, debug_feature: "abtem" }),
    userRow("historic-debug", { is_debug: true, debug_feature: null, is_admin: true }),
  ];
  h.call("/admin/users?q=&account_type=debug&offset=0&limit=20").respond({ items: debugUsers, total: 6, next_offset: null }); await settle();
  for (const label of ["调试 · 科研资料库", "调试 · 文献求助", "调试 · 材料工具", "调试 · XRD", "调试 · abTEM", "历史调试"]) assert.match(h.text(), new RegExp(label));
  const regularDebug = h.find((item) => item.tag === "article" && h.text(item).includes("library-debug@example.invalid"));
  const adminDebug = h.find((item) => item.tag === "article" && h.text(item).includes("historic-debug@example.invalid"));
  assert.match(h.text(regularDebug), /用户 · 已验证/); assert.match(h.text(adminDebug), /管理员 · 已验证/);
  await h.click("historic-debug@example.invalid");
  h.call("/admin/users/historic-debug").respond(detail("historic-debug", { user: debugUsers[5] })); await settle();
  const selectedDetail = h.find((item) => item.props["aria-label"] === "用户详情");
  assert.match(h.text(selectedDetail), /历史调试/); assert.match(h.text(selectedDetail), /角色管理员/);
});

test("账号筛选变更立即清空列表和详情，回到首屏并丢弃旧列表与详情响应", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("first")], total: 21, next_offset: 20 }); await settle();
  await h.click("下一页");
  h.call("/admin/users?q=&account_type=regular&offset=20&limit=20").respond({ items: [userRow("last")], total: 21, next_offset: null }); await settle();
  await h.click("last@example.invalid"); h.call("/admin/users/last").respond(detail("last", { recent_jobs: [{ id: "old-job", kind: "search", state: "succeeded", title: "旧用户任务" }] })); await settle();
  await h.click("刷新详情"); const oldDetail = h.call("/admin/users/last");
  await h.click("上一页"); const oldPage = h.call("/admin/users?q=&account_type=regular&offset=0&limit=20");
  await h.click("调试账号");
  assert.doesNotMatch(h.text(), /last@example.invalid|旧用户任务/);
  const debugPage = h.call("/admin/users?q=&account_type=debug&offset=0&limit=20");
  await h.click("全部");
  h.call("/admin/users?q=&account_type=all&offset=0&limit=20").respond({ items: [userRow("current")], total: 1, next_offset: null }); await settle();
  oldPage.respond({ items: [userRow("stale-regular")], total: 21, next_offset: 20 });
  debugPage.respond({ items: [userRow("stale-debug", { is_debug: true, debug_feature: "xrd" })], total: 6, next_offset: null });
  oldDetail.respond({ detail: "旧详情无权限" }, 401); await settle();
  assert.match(h.text(), /current@example.invalid/); assert.doesNotMatch(h.text(), /stale-|旧详情无权限|旧用户任务/);
  assert.equal(h.unauthorized.length, 0);
  const previous = h.find((item) => item.tag === "button" && h.text(item) === "上一页"); assert.equal(previous.props.disabled, true);
});

test("查询与翻页保留已应用账号筛选，未提交查询不影响筛选切换", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.click("调试账号"); h.call("/admin/users?q=&account_type=debug&offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.input("admin-user-query", "debug"); await h.submit("查找用户");
  h.call("/admin/users?q=debug&account_type=debug&offset=0&limit=20").respond({ items: [userRow("debug-result", { is_debug: true, debug_feature: "xrd" })], total: 21, next_offset: 20 }); await settle();
  await h.input("admin-user-query", "尚未提交"); await h.click("下一页");
  h.call("/admin/users?q=debug&account_type=debug&offset=20&limit=20").respond({ items: [], total: 21, next_offset: null }); await settle();
  await h.click("全部"); assert.ok(h.call("/admin/users?q=debug&account_type=all&offset=0&limit=20"));
});

test("切换管理员账号使旧用户响应失效，新账号恢复正式账号筛选", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  const oldRegular = h.call("/admin/users?q=&account_type=regular&offset=0&limit=20");
  await h.click("调试账号"); const oldDebug = h.call("/admin/users?q=&account_type=debug&offset=0&limit=20");
  h.props.user = account("admin-b"); await settle(); await h.click("用户信息");
  const newRegular = h.calls.at(-1); assert.equal(newRegular.path, "/admin/users?q=&account_type=regular&offset=0&limit=20");
  newRegular.respond({ items: [userRow("new-account-row")], total: 1, next_offset: null }); await settle();
  oldRegular.respond({ detail: "旧登录失效" }, 401); oldDebug.respond({ items: [userRow("old-account-row")], total: 1, next_offset: null }); await settle();
  assert.match(h.text(), /new-account-row@example.invalid/); assert.doesNotMatch(h.text(), /old-account-row|旧登录失效/); assert.equal(h.unauthorized.length, 0);
});

test("切换账号筛选保留待确认积分目标与原请求，不自动提交或丢失未知结果", async (t) => {
  const h = fixture(t); await h.click("积分调整");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("target")], total: 1, next_offset: null }); await settle();
  await h.click("target@example.invalid"); h.call("/admin/users/target").respond(detail("target")); await settle();
  await h.input("admin-credit-amount", "2"); await h.input("admin-credit-reason", "保留待确认调整"); await h.submit("积分调整");
  await h.click("调试账号");
  const confirmation = h.find((item) => item.props["aria-labelledby"] === "admin-credit-confirm-title");
  assert.match(h.text(confirmation), /target@example.invalid/); assert.match(h.text(confirmation), /保留待确认调整/);
  assert.equal(h.calls.some((item) => item.method === "POST"), false);
  const selectedDetail = h.find((item) => item.props["aria-label"] === "用户详情"); assert.doesNotMatch(h.text(selectedDetail), /target@example.invalid/);
  h.call("/admin/users?q=&account_type=debug&offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.click("确认调整"); const sent = h.call("/admin/users/target/credits", "POST"); sent.fail(); await settle();
  await h.click("全部"); assert.ok(h.call("/admin/users?q=&account_type=all&offset=0&limit=20"));
  assert.match(h.text(confirmation), /结果待确认/); assert.match(h.text(confirmation), /保留待确认调整/);
  assert.equal(h.calls.filter((item) => item.method === "POST").length, 1);
  assert.equal([...h.storage.values()].some((value) => value.includes(sent.body.request_key)), true);
});

const visits = (period = "7d", pv = 3) => ({ period, timezone: "Asia/Shanghai", start_date: "2026-10-15", end_date: "2026-10-21",
  summary: { pv, uv: 2, ip: 1, new_visitors: 2 }, series: [{ label: "2026-10-21", pv, uv: 2, ip: 1, new_visitors: 2 }],
  pages: [{ name: "search", label: "XRD 晶相检索", pv, uv: 2, ip: 1 }], browsers: [{ name: "Firefox", pv, uv: 2, ip: 1 }],
  systems: [], devices: [], referrers: [], retention_days: 90, first_collected_at: "2026-10-21T01:00:00Z" });

test("管理中心访问统计按需加载并显示服务器指标、趋势和设备分布", async (t) => {
  const h = fixture(t);
  assert.equal(h.calls.some(item => item.path.includes("analytics")), false);
  await h.click("访问统计"); h.call("/admin/analytics?period=7d").respond(visits()); await settle();
  assert.match(h.text(), /浏览量 PV/); assert.match(h.text(), /访客数 UV/); assert.match(h.text(), /独立 IP/);
  assert.match(h.text(), /XRD 晶相检索/); assert.match(h.text(), /Firefox/); assert.match(h.text(), /访问趋势/);
  assert.match(h.text(), /90 天/); assert.equal(h.calls.some(item => item.method === "POST"), false);
});

test("切换统计时段隔离迟到响应，错误不伪装成零访问且可刷新", async (t) => {
  const h = fixture(t, { component: "AnalyticsPanel" });
  const stale = h.call("/admin/analytics?period=7d");
  await h.click("今日"); h.call("/admin/analytics?period=today").respond(visits("today", 23)); await settle();
  stale.respond({ detail: "旧会话失效" }, 401); await settle();
  assert.equal(h.unauthorized.length, 0); assert.match(h.text(), /23/);
  await h.click("昨日"); h.call("/admin/analytics?period=yesterday").respond({ detail: "访问统计暂时不可用" }, 503); await settle();
  assert.match(h.text(), /访问统计暂时不可用/); assert.doesNotMatch(h.text(), /所选时段暂无访问记录/);
  await h.click("刷新统计"); h.call("/admin/analytics?period=yesterday").respond(visits("yesterday", 0)); await settle();
  assert.match(h.text(), /所选时段暂无访问记录/);
});

test("统计组件隔离权限撤销、账号及会话切换和卸载后的响应", async (t) => {
  const h = fixture(t, { component: "AnalyticsPanel", user: account("regular", { is_admin: false }) });
  assert.equal(h.calls.length, 0);
  h.props.user = account(); await settle(); const old = h.call("/admin/analytics?period=7d");
  h.props.version++; await settle(); old.respond({ detail: "旧会话失效" }, 401); await settle();
  assert.equal(h.unauthorized.length, 0);
  const current = h.call("/admin/analytics?period=7d"); h.props.user = account("regular", { is_admin: false }); await settle();
  current.respond(visits()); await settle(); assert.doesNotMatch(h.text(), /Firefox/);
  h.props.user = account(); await settle(); const late = h.call("/admin/analytics?period=7d");
  h.unmount(); late.respond({ detail: "过期会话" }, 401); await settle(); assert.equal(h.unauthorized.length, 0);
});

test("普通用户和未验证管理员不请求或显示任何管理数据", async (t) => {
  for (const user of [account("regular", { is_admin: false }), account("unverified", { verified: false })]) {
    const h = fixture(t, { user }); await settle();
    assert.equal(h.calls.length, 0); assert.doesNotMatch(h.text(), /系统管理中心/);
    h.unmount();
  }
});

test("用户查询支持分页，切换用户后迟到详情不得覆盖当前用户", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("first")], total: 21, next_offset: 20 }); await settle();
  await h.click("下一页");
  h.call("/admin/users?q=&account_type=regular&offset=20&limit=20").respond({ items: [userRow("last")], total: 21, next_offset: null }); await settle();
  await h.click("last@example.invalid"); const late = h.call("/admin/users/last");
  await h.click("上一页");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("first")], total: 21, next_offset: 20 }); await settle();
  await h.click("first@example.invalid");
  h.call("/admin/users/first").respond(detail("first", { recent_jobs: [{ id: "job-1", kind: "search", state: "succeeded", created_at: "2026-10-20T01:00:00Z", title: "MnO2 检索" }] })); await settle();
  late.respond(detail("last", { recent_jobs: [{ id: "late", title: "迟到内容", kind: "search", state: "succeeded", created_at: "2026-10-20T01:00:00Z" }] })); await settle();
  assert.match(h.text(), /MnO2 检索/); assert.doesNotMatch(h.text(), /迟到内容/);
  await h.input("admin-user-query", "first@example.invalid"); await h.submit("查找用户");
  assert.ok(h.call("/admin/users?q=first%40example.invalid&account_type=regular&offset=0&limit=20"));
});

test("积分调整先确认目标与原因，断网保留原幂等键和正文，重试后只采用服务端余额", async (t) => {
  const h = fixture(t); await h.click("积分调整");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("target")], total: 1, next_offset: null }); await settle();
  await h.click("target@example.invalid"); h.call("/admin/users/target").respond(detail("target")); await settle();
  await h.input("admin-credit-amount", "5"); await h.input("admin-credit-reason", "补记科研活动积分");
  await h.submit("积分调整");
  assert.equal(h.calls.some((item) => item.method === "POST"), false);
  assert.match(h.text(), /确认调整/); assert.match(h.text(), /target@example.invalid/); assert.match(h.text(), /补记科研活动积分/);
  await h.click("确认调整"); const first = h.call("/admin/users/target/credits", "POST");
  assert.equal(first.headers.get("X-CSRF-Token"), "test-csrf");
  assert.equal(first.body.amount, 5); assert.equal(first.body.reason, "补记科研活动积分");
  first.fail(); await settle(); assert.match(h.text(), /待确认/);
  const persisted = JSON.stringify([...h.storage.values()]); assert.match(persisted, /补记科研活动积分/); assert.doesNotMatch(persisted, /csrf|test-csrf/);
  await h.click("使用原请求重试"); const retry = h.call("/admin/users/target/credits", "POST"); assert.deepEqual(retry.body, first.body);
  retry.respond({ adjustment: { id: "adjustment-1" }, credits: { available: 88, frozen: 2 } }); await settle();
  h.call("/admin/users/target").respond(detail("target", { credits: { available: 88, frozen: 2, checked_in: false, items: [] } })); await settle();
  assert.match(h.text(), /88 \/ 2/); assert.equal([...h.storage.values()].some((value) => value.includes(first.body.request_key)), false);
  assert.equal(h.calls.filter((item) => item.path === "/admin/users/target/credits").length, 2);
});

test("积分拒绝零、小数、越界与空原因，不向服务器提交", async (t) => {
  const h = fixture(t); await h.click("积分调整");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("target")], total: 1, next_offset: null }); await settle();
  await h.click("target@example.invalid"); h.call("/admin/users/target").respond(detail("target")); await settle();
  await h.input("admin-credit-reason", "原因");
  for (const value of ["0", "1.5", "1000001", "-1000001", "1e2"]) {
    await h.input("admin-credit-amount", value); await h.submit("积分调整"); assert.match(h.text(), /非零整数/);
  }
  await h.input("admin-credit-amount", "-5"); await h.input("admin-credit-reason", "  "); await h.submit("积分调整"); assert.match(h.text(), /填写/);
  assert.equal(h.calls.some((item) => item.method === "POST"), false);
});

test("待确认积分请求在同会话恢复原正文，账户切换隔离，迟到401及卸载响应不能影响新账号", async (t) => {
  const h = fixture(t); await h.click("积分调整");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("target")], total: 1, next_offset: null }); await settle();
  await h.click("target@example.invalid"); h.call("/admin/users/target").respond(detail("target")); await settle();
  await h.input("admin-credit-amount", "-2"); await h.input("admin-credit-reason", "修正重复赠分"); await h.submit("积分调整"); await h.click("确认调整");
  const sent = h.call("/admin/users/target/credits", "POST"), snapshot = JSON.parse(JSON.stringify(sent.body));
  sent.fail(); await settle(); const storage = h.storage; h.unmount();
  const restored = fixture(t, { storage }); assert.match(restored.text(), /修正重复赠分/);
  await restored.click("使用原请求重试"); const late = restored.call("/admin/users/target/credits", "POST"); assert.deepEqual(late.body, snapshot);
  restored.props.user = account("admin-b"); await settle(); assert.doesNotMatch(restored.text(), /修正重复赠分/);
  late.respond({ detail: "登录已失效" }, 401); await settle(); assert.equal(restored.unauthorized.length, 0);
  const pendingOverview = restored.calls.findLast((item) => !item.done && item.path === "/admin/overview");
  restored.unmount(); pendingOverview.respond({ detail: "登录已失效" }, 401); await settle(); assert.equal(restored.unauthorized.length, 0);
});

test("公告新建只保存草稿，发布须确认；旧版本409保留编辑并要求重新加载", async (t) => {
  const h = fixture(t); await h.click("网站公告");
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.click("新建公告"); await h.input("admin-announcement-title", "例行维护"); await h.input("admin-announcement-body", "<img src=x onerror=alert(1)>\n维护结束后恢复。");
  await h.submit("公告编辑"); const create = h.call("/admin/announcements", "POST");
  assert.equal("status" in create.body, false); assert.equal(create.body.title, "例行维护");
  const announcement = { id: "notice-1", title: create.body.title, body: create.body.body, status: "draft", revision: 1, created_at: "2026-10-20T01:00:00Z", updated_at: "2026-10-20T01:00:00Z", published_at: null };
  create.respond(announcement); await settle();
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [announcement], total: 1, next_offset: null }); await settle();
  assert.equal(h.calls.some((item) => item.method === "PATCH"), false);
  await h.click("发布公告"); assert.equal(h.calls.some((item) => item.method === "PATCH"), false);
  await h.click("确认发布"); const publish = h.call("/admin/announcements/notice-1", "PATCH"); assert.equal(publish.body.revision, 1); assert.equal(publish.body.status, "published");
  publish.respond({ ...announcement, status: "published", revision: 2, published_at: "2026-10-20T02:00:00Z" }); await settle();
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [{ ...announcement, status: "published", revision: 2 }], total: 1, next_offset: null }); await settle();
  await h.input("admin-announcement-body", "保留我的编辑"); await h.submit("公告编辑");
  h.call("/admin/announcements/notice-1", "PATCH").respond({ detail: "公告版本已变化，请刷新。" }, 409); await settle();
  assert.match(h.text(), /重新加载/);
  assert.equal(h.find((item) => item.props.id === "admin-announcement-body").value, "保留我的编辑");
  const form = h.find((item) => item.tag === "form" && item.props["aria-label"] === "公告编辑");
  assert.ok(form.children.flatMap((item) => [item, ...item.children]).some((item) => item.props.type === "submit" && item.props.disabled));
  assert.equal(h.root.children.some((item) => item.tag === "img"), false);
  const changes = h.changed.length;
  await h.click("重新加载此公告（放弃当前编辑）");
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [{ ...announcement, body: "其他管理员的新内容", status: "published", revision: 4 }], total: 1, next_offset: null }); await settle();
  assert.equal(h.find((item) => item.props.id === "admin-announcement-body").value, "其他管理员的新内容");
  assert.equal(h.changed.length, changes + 1, "重新核对未知提交后应刷新公开公告");
  await h.click("撤回公告"); await h.click("确认撤回");
  const withdraw = h.call("/admin/announcements/notice-1", "PATCH"); assert.equal(withdraw.body.revision, 4); assert.equal(withdraw.body.status, "draft");
  withdraw.respond({ ...announcement, status: "draft", revision: 5 }); await settle(); assert.equal(h.changed.length, changes + 2);
});

test("概览可分页核对管理操作记录，纯文本显示原因和操作人", async (t) => {
  const h = fixture(t); h.call("/admin/overview").respond(overview); await settle();
  await h.click("查看操作记录");
  h.call("/admin/activity?offset=0&limit=20").respond({ items: [{ id: "1", action: "admin_credit_adjustment", actor_email: "admin-a@example.invalid", target_email: "target@example.invalid", amount: 3, reason: "<b>补记</b>", created_at: "2026-10-20T01:00:00Z" }], total: 21, next_offset: 20 }); await settle();
  assert.match(h.text(), /admin-a@example.invalid/); assert.match(h.text(), /<b>补记<\/b>/);
  await h.click("下一页动态"); assert.ok(h.call("/admin/activity?offset=20&limit=20"));
});

test("可用积分不足的确定拒绝允许改正后重新确认，不改变冻结余额", async (t) => {
  const h = fixture(t); await h.click("积分调整");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("target")], total: 1, next_offset: null }); await settle();
  await h.click("target@example.invalid"); h.call("/admin/users/target").respond(detail("target")); await settle();
  await h.input("admin-credit-amount", "-20"); await h.input("admin-credit-reason", "误录修正"); await h.submit("积分调整"); await h.click("确认调整");
  h.call("/admin/users/target/credits", "POST").respond({ detail: "可用积分不足，不能扣除冻结积分，请刷新余额后重试。" }, 409); await settle();
  await h.click("修改后重新确认"); await h.input("admin-credit-amount", "-1"); await h.submit("积分调整");
  assert.match(h.text(), /-1 分/); assert.match(h.text(), /10 \/ 2/);
});

test("公开公告为空不留模块，正文只显示纯文本，分页及版本刷新不混入旧列表", async (t) => {
  const h = fixture(t, { component: "Announcements" });
  h.call("/announcements?offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle(); assert.equal(h.text(), "");
  h.props.version++; await settle();
  const item = { id: "public-1", title: "<b>网站公告</b>", body: "<script>alert(1)</script>\n第二行", revision: 1, published_at: "2026-10-20T01:00:00Z" };
  h.call("/announcements?offset=0&limit=20").respond({ items: [item], total: 21, next_offset: 20 }); await settle();
  assert.match(h.text(), /<script>alert\(1\)<\/script>/); assert.ok(h.find((element) => element.tag === "details"));
  await h.click("更多公告"); const late = h.call("/announcements?offset=20&limit=20");
  h.props.version++; await settle(); h.call("/announcements?offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  late.respond({ items: [{ ...item, id: "old", title: "已撤回的旧列表" }], total: 21, next_offset: null }); await settle();
  assert.equal(h.text(), ""); assert.equal(h.calls.some((call) => call.path.startsWith("/admin/")), false);
});

test("原文献运行设置保持管理员和会话保护，卸载后的迟到401不外传", async (t) => {
  const h = fixture(t, { component: "LiteratureAdmin", user: account("regular", { is_admin: false }) });
  assert.equal(h.calls.length, 0);
  h.props.user = account("operator"); await settle(); const late = h.call("/admin/literature");
  h.unmount(); late.respond({ detail: "登录失效" }, 401); await settle(); assert.equal(h.unauthorized.length, 0);
});

test("公告创建断网后同会话恢复同键正文，切账号后的迟到成功不显示旧公告", async (t) => {
  const h = fixture(t); await h.click("网站公告");
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.click("新建公告"); await h.input("admin-announcement-title", "待确认公告"); await h.input("admin-announcement-body", "保留原正文\n不自动发布"); await h.submit("公告编辑");
  const first = h.call("/admin/announcements", "POST"); first.fail(); await settle(); assert.match(h.text(), /结果待确认/);
  const storage = h.storage; h.unmount(); const restored = fixture(t, { storage });
  await restored.click("重试原公告请求"); const retry = restored.call("/admin/announcements", "POST"); assert.deepEqual(retry.body, first.body);
  restored.props.user = account("admin-b"); await settle();
  retry.respond({ id: "late-ann", title: "待确认公告", body: "保留原正文", status: "draft", revision: 1 }); await settle();
  assert.doesNotMatch(restored.text(), /待确认公告/); assert.equal(restored.changed.length, 0);
});

test("编辑尚未提交的用户查询时，翻页仍使用当前已确认的筛选条件", async (t) => {
  const h = fixture(t); await h.click("用户信息");
  h.call("/admin/users?q=&account_type=regular&offset=0&limit=20").respond({ items: [userRow("first")], total: 21, next_offset: 20 }); await settle();
  await h.input("admin-user-query", "尚未查找"); await h.click("下一页");
  assert.ok(h.call("/admin/users?q=&account_type=regular&offset=20&limit=20"));
});

test("同一管理员重新登录后的新会话不接受旧会话迟到的401", async (t) => {
  const h = fixture(t); const late = h.call("/admin/overview");
  h.props.version++; await settle();
  late.respond({ detail: "旧会话失效" }, 401); await settle();
  assert.equal(h.unauthorized.length, 0);
});

test("同账号auth/me刷新对象保留公告草稿和409冲突编辑，不重新加载管理数据", async (t) => {
  const h = fixture(t); await h.click("网站公告");
  const existing = { id: "existing", title: "原公告", body: "旧正文", status: "draft", revision: 1, created_at: "2026-10-20T01:00:00Z", updated_at: "2026-10-20T01:00:00Z", published_at: null };
  h.call("/admin/announcements?offset=0&limit=20").respond({ items: [existing], total: 1, next_offset: null }); await settle();
  await h.click("新建公告"); await h.input("admin-announcement-title", "未保存草稿"); await h.input("admin-announcement-body", "我的未提交正文");
  const requests = h.calls.length; h.props.user = { ...account(), csrf_token: "refreshed-csrf" }; await settle();
  assert.equal(h.find((item) => item.props.id === "admin-announcement-body").value, "我的未提交正文"); assert.equal(h.calls.length, requests);
  await h.click("原公告"); await h.input("admin-announcement-body", "409之后必须保留"); await h.submit("公告编辑");
  h.call("/admin/announcements/existing", "PATCH").respond({ detail: "版本已变化" }, 409); await settle();
  const afterConflict = h.calls.length; h.props.user = { ...account() }; await settle();
  assert.equal(h.find((item) => item.props.id === "admin-announcement-body").value, "409之后必须保留");
  assert.match(h.text(), /当前编辑已保留/); assert.equal(h.calls.length, afterConflict);
});

test("新公告明确422未提交后保留正文并允许修正，只有重新提交才生成新键", async (t) => {
  const h = fixture(t); await h.click("网站公告"); h.call("/admin/announcements?offset=0&limit=20").respond({ items: [], total: 0, next_offset: null }); await settle();
  await h.click("新建公告"); await h.input("admin-announcement-title", "要修改的草稿"); await h.input("admin-announcement-body", "正文不可丢失"); await h.submit("公告编辑");
  const rejected = h.call("/admin/announcements", "POST"); rejected.respond({ detail: "标题校验未通过，请修正。" }, 422); await settle();
  const body = h.find((item) => item.props.id === "admin-announcement-body"); assert.equal(body.value, "正文不可丢失"); assert.ok(!body.props.disabled);
  assert.doesNotMatch(h.text(), /公告请求结果待确认/); assert.equal([...h.storage.values()].some((item) => item.includes(rejected.body.request_key)), false);
  await h.input("admin-announcement-title", "已修正的标题"); await h.submit("公告编辑");
  const retry = h.call("/admin/announcements", "POST"); assert.equal(retry.body.title, "已修正的标题"); assert.equal(retry.body.body, "正文不可丢失"); assert.notEqual(retry.body.request_key, rejected.body.request_key);
});
