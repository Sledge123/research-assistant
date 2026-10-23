import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("页签、说明、首页品牌和登录名称统一为科研助手，技术 API 路径保持", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const app = readFileSync(new URL("./App.vue", import.meta.url), "utf8");
  assert.match(html, /<title>科研助手/);
  assert.match(html, /content="科研助手/);
  assert.match(app, /aria-label="科研助手首页"/);
  assert.match(app, /登录科研助手/);
  assert.doesNotMatch(app, /晶谱|JINGPU/);
  assert.match(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), /API_BASE = "\/highscore\/api"/);
});

test("系统管理中心仅对已验证管理员展示，原运行页面通过设置插槽保留", () => {
  const app = readFileSync(new URL("./App.vue", import.meta.url), "utf8");
  assert.match(app, /<AdminCenter[\s>]/);
  assert.match(app, /<template #settings>/);
  assert.match(app, /<Announcements[\s>]/);
  assert.match(app, /系统管理中心/);
  assert.doesNotMatch(app, />\s*运行管理\s*</);
  assert.match(app, /user\?\.is_admin && user\.verified/);
});
