import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";
import { renderToString } from "vue/server-renderer";

function evaluate(source, dependencies = {}) {
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled)((name) => {
    if (!(name in dependencies)) throw new Error(`未配置依赖：${name}`);
    return dependencies[name];
  }, module, module.exports);
  return module.exports;
}

const state = evaluate(readFileSync(new URL("./literatureState.ts", import.meta.url), "utf8"));
const script = compileScript(parse(readFileSync(new URL("./LiteratureTasks.vue", import.meta.url), "utf8")).descriptor,
  { id: "literature-stages", inlineTemplate: true });
const component = evaluate(script.content, { vue: Vue, "./api": { API_BASE: "/highscore/api" },
  "./Icon.vue": { default: Vue.defineComponent({ render: () => null }) }, "./literatureState": state }).default;
const now = Date.parse("2026-10-21T08:00:00Z");
const task = { id: "paper-task", kind: "literature", title: "合成论文任务", canonical_title: null, doi: null,
  state: "running", stage: "fetching", message: "正在获取正文", created_at: "2026-10-21T07:00:00Z",
  deadline: "2026-10-22T07:00:00Z", ready_at: null, download_expires_at: null, download_available: false,
  file_expired: false, credit_status: "frozen", candidates: [], last_checked_at: "2026-10-21T07:58:00Z", next_check_at: null };

async function render(job) {
  return renderToString(Vue.createSSRApp(component, { jobs: [job], selected: job, busy: false, loading: false,
    error: "", actionError: "", notice: "", now, canSubmit: true }));
}

test("真实组件渲染文献阶段及检查时间，不将处理中任务显示为可下载", async () => {
  const html = await render({ ...task, state: "waiting", stage: "renewing", message: "正在重新获取", next_check_at: "2026-10-21T08:01:00Z" });
  assert.match(html, /status-pill[^>]*>正在重新获取<\/span>/);
  assert.match(html, /最近检查/);
  assert.match(html, /2026\/10\/21 15:58/);
  assert.match(html, /下次检查/);
  assert.match(html, /2026\/10\/21 16:01/);
  assert.doesNotMatch(html, /href="[^"]*\/download"/);
});

test("已核验全文只有实际可下载才标记可下载，到期或文件不可用立即隐藏下载", async () => {
  const available = { ...task, state: "succeeded", stage: "ready", message: "可下载", credit_status: "charged",
    ready_at: "2026-10-21T07:59:00Z", download_expires_at: "2026-10-24T07:59:00Z", download_available: true };
  const html = await render(available);
  assert.match(html, /status-pill[^>]*>可下载<\/span>/);
  assert.match(html, /href="\/highscore\/api\/literature\/jobs\/paper-task\/download"/);
  const missing = await render({ ...available, download_available: false, message: "文件校验失败" });
  assert.match(missing, /status-pill[^>]*>文件不可用<\/span>/);
  assert.doesNotMatch(missing, /href="[^"]*\/download"/);
  const expired = await render({ ...available, download_expires_at: new Date(now).toISOString() });
  assert.match(expired, /status-pill[^>]*>全文已过期<\/span>/);
  assert.doesNotMatch(expired, /href="[^"]*\/download"/);
});

test("失败和取消不被旧阶段或过期标记覆盖，旧任务省略新字段仍可正常显示", async () => {
  for (const [state, label] of [["cancelled", "已取消"], ["failed", "未获取"], ["needs_attention", "待处理"]]) {
    const html = await render({ ...task, state, stage: "ready", file_expired: true, message: label });
    assert.match(html, new RegExp(`status-pill[^>]*>${label}</span>`));
    assert.doesNotMatch(html, /href="[^"]*\/download"/);
    assert.doesNotMatch(html, /全文已过期/);
  }
  const { stage, last_checked_at, next_check_at, ...legacy } = task;
  const html = await render(legacy);
  assert.match(html, /status-pill[^>]*>处理中<\/span>/);
  assert.doesNotMatch(html, /最近检查|下次检查|undefined/);
});
