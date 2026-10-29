import assert from "node:assert/strict";
import test from "node:test";
import { canDownloadLiterature, canSubmitLiterature, findAvailableLiterature, isLiteratureExpired, isLiteraturePending, literatureStateLabel } from "./literatureState.ts";

const expires = "2026-10-22T08:00:00Z";
const file = { state: "succeeded", download_available: true, file_expired: false, download_expires_at: expires };

test("PDF 到期边界立即停止下载，并支持服务端提前标记过期", () => {
  assert.equal(canDownloadLiterature(file, Date.parse(expires) - 1), true);
  assert.equal(canDownloadLiterature(file, Date.parse(expires)), false);
  assert.equal(isLiteratureExpired(file, Date.parse(expires)), true);
  assert.equal(canDownloadLiterature({ ...file, file_expired: true }, Date.parse(expires) - 1), false);
  assert.equal(canDownloadLiterature({ ...file, download_available: false }, Date.parse(expires) - 1), false);
  assert.equal(canDownloadLiterature({ ...file, download_expires_at: null }, Date.parse(expires) - 1), false);
});

test("过期标记和旧 ready 阶段不能覆盖失败或取消状态", () => {
  for (const state of ["failed", "cancelled", "needs_attention", "running"]) {
    const job = { ...file, state, stage: "ready", file_expired: true };
    assert.equal(isLiteratureExpired(job, Date.parse(expires)), false, state);
    assert.equal(canDownloadLiterature(job, Date.parse(expires) - 1), false, state);
  }
});

test("所有未结束文献状态持续刷新并可取消，结束状态不视为等待", () => {
  for (const state of ["queued", "running", "waiting", "needs_selection", "needs_attention"])
    assert.equal(isLiteraturePending(state), true, state);
  for (const state of ["succeeded", "failed", "cancelled"])
    assert.equal(isLiteraturePending(state), false, state);
});

test("处理中的文献显示服务端阶段，失败取消和待人工处理保留真实业务状态", () => {
  for (const [stage, expected] of [
    ["fetching", "正在获取正文"], ["awaiting_fulltext", "等待全文"], ["renewing", "正在重新获取"],
    ["verifying", "正在核验"], ["retrying", "稍后重试"],
  ]) assert.equal(literatureStateLabel("running", stage), expected);
  for (const [state, expected] of [["failed", "未获取"], ["cancelled", "已取消"], ["needs_attention", "待处理"], ["needs_selection", "请选择文献"]])
    for (const stage of ["fetching", "renewing", "ready"]) assert.equal(literatureStateLabel(state, stage), expected);
  assert.equal(literatureStateLabel("running"), "处理中", "旧任务省略阶段时维持原文案");
  assert.equal(literatureStateLabel("waiting"), "等待全文");
  assert.equal(literatureStateLabel("waiting", "ready"), "等待全文", "未完成任务不能靠阶段宣称可下载");
});

test("提交依赖本人邮箱、文献开放状态和操作锁，余额交由服务端判断", () => {
  const user = { verified: true };
  const service = { enabled: true, worker_online: false };
  for (const credits of [{ available: 1 }, { available: 0 }, null])
    assert.equal(canSubmitLiterature({ ...user, credits }, service, false), true);
  assert.equal(canSubmitLiterature(user, { enabled: false }, false), false);
  assert.equal(canSubmitLiterature({ verified: false }, service, false), false);
  assert.equal(canSubmitLiterature(null, service, false), false);
  assert.equal(canSubmitLiterature(user, service, true), false);
});

test("有效全文被最近 200 条任务挤出时，零余额仍可提交服务端免费复用", () => {
  const recentJobs = Array.from({ length: 200 }, (_, index) => ({
    ...file, id: `recent-${index}`, title: `Recent experiment ${index}`, canonical_title: null,
  }));
  const user = { verified: true, credits: { available: 0, frozen: 0, checked_in: false } };
  assert.equal(findAvailableLiterature(recentJobs, "Earlier valid experiment", Date.parse(expires) - 1), null);
  assert.equal(canSubmitLiterature(user, { enabled: true }, false), true);
});

test("本人有效同标题全文可直接复用，过期记录或相似标题不复用", () => {
  const job = { ...file, id: "own-pdf", title: " Original Title ", canonical_title: "Canonical title" };
  assert.equal(findAvailableLiterature([job], " canonical   TITLE ", Date.parse(expires) - 1)?.id, "own-pdf");
  assert.equal(findAvailableLiterature([job], "Original Title", Date.parse(expires) - 1)?.id, "own-pdf");
  assert.equal(findAvailableLiterature([job], "Canonical title", Date.parse(expires)), null);
  assert.equal(findAvailableLiterature([job], "Canonical title extra", Date.parse(expires) - 1), null);
});

test("免费复用采用服务端的 NFKC、Unicode casefold 与字母数字规则", () => {
  for (const [stored, entered] of [
    ["Alpha-beta experiment", "Alpha beta experiment"],
    ["Ａｌｐｈａ experiment ①", "Alpha experiment 1"],
    ["Straße experiment", "STRASSE experiment"],
    ["Μέθοδος Σ experiment", "ΜΈΘΟΔΟΣ σ experiment"],
    ["研究：电池-2026", "研究 电池2026"],
    ["ᾶ experiment", "α experiment"],
  ]) {
    const job = { ...file, id: "own-pdf", title: stored, canonical_title: null };
    assert.equal(findAvailableLiterature([job], entered, Date.parse(expires) - 1)?.id, "own-pdf", `${stored} / ${entered}`);
    assert.equal(findAvailableLiterature([job], entered, Date.parse(expires)), null, "过期记录不得复用");
  }
  const distinct = { ...file, id: "different-paper", title: "café ı experiment", canonical_title: null };
  assert.equal(findAvailableLiterature([distinct], "cafe i experiment", Date.parse(expires) - 1), null);
});
