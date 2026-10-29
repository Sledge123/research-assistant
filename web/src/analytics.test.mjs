import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import test from "node:test";
import ts from "typescript";

function fixture(t, extra = {}) {
  const originals = Object.fromEntries(["fetch", "navigator", "document", "window", "crypto"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const calls = [];
  for (const [key, value] of Object.entries({ crypto: webcrypto, navigator: { doNotTrack: "0", ...extra },
    document: { referrer: "https://example.org/paper?email=secret&verify=secret#password" },
    window: { location: { hostname: "site.example.test", href: "https://site.example.test/?verify=private" } },
    fetch: async (url, init) => { calls.push({ url, init }); return new Response(null, { status: 204 }); } })) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  t.after(() => { for (const [key, descriptor] of Object.entries(originals)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  } });
  const code = ts.transpileModule(readFileSync(new URL("./analytics.ts", import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)(() => ({ API_BASE: "/highscore/api" }), module, module.exports);
  return { ...module.exports, calls };
}

test("页面上报只发送固定业务标识与外部域名，使用现有同源API和独立Cookie", async (t) => {
  const h = fixture(t); await h.trackPage("search");
  assert.equal(h.calls.length, 1);
  const { url, init } = h.calls[0]; assert.equal(url, "/highscore/api/analytics/pageview");
  assert.equal(init.credentials, "same-origin"); assert.equal(init.keepalive, true);
  const body = JSON.parse(init.body); assert.deepEqual(Object.keys(body).sort(), ["event_id", "page", "referrer"]);
  assert.equal(body.page, "search"); assert.equal(body.referrer, "example.org"); assert.match(body.event_id, /^[0-9a-f]{32}$/);
  assert.doesNotMatch(init.body, /secret|verify|password|email/);
});

test("隐私选项与非页面状态不产生访问上报", async (t) => {
  for (const options of [{ doNotTrack: "1" }, { globalPrivacyControl: true }]) {
    const h = fixture(t, options); await h.trackPage("search"); assert.equal(h.calls.length, 0);
  }
  const h = fixture(t); await h.trackPage("?verify=secret"); assert.equal(h.calls.length, 0);
});

test("页面上报顺序等待访客Cookie，网络失败静默完成且不自动重试", async (t) => {
  const h = fixture(t); let release; const calls = [];
  globalThis.fetch = (url, init) => { calls.push(JSON.parse(init.body).page); return calls.length === 1
    ? new Promise(resolve => { release = resolve; }) : Promise.reject(new Error("offline")); };
  const first = h.trackPage("literature"), second = h.trackPage("search");
  await new Promise(resolve => setImmediate(resolve)); assert.deepEqual(calls, ["literature"]);
  release(new Response(null, { status: 204 })); await Promise.all([first, second]);
  assert.deepEqual(calls, ["literature", "search"]);
});

test("站内来源与非法来源只保留空值，App仅在首次进入及实际页面切换时采集", async (t) => {
  const h = fixture(t);
  document.referrer = "https://site.example.test/?verify=secret"; await h.trackPage("account");
  assert.equal(JSON.parse(h.calls[0].init.body).referrer, "");
  document.referrer = "javascript:alert(1)"; await h.trackPage("library");
  assert.equal(JSON.parse(h.calls[1].init.body).referrer, "");
  const app = readFileSync(new URL("./App.vue", import.meta.url), "utf8");
  assert.match(app, /watch\(view,/); assert.match(app, /if \(!token\) void trackPage\(view.value\)/);
  assert.doesNotMatch(app, /trackPage\((?:url|token|user|query|auth)/);
});
