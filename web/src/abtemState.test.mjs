import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const compiled = ts.transpileModule(readFileSync(new URL("./abtemState.ts", import.meta.url), "utf8"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const module = { exports: {} }; new Function("module", "exports", compiled)(module, module.exports);
const { parameterError, canDownloadAbtem, readCifUpload, MAX_CIF_BYTES, abtemCreditLabel } = module.exports;
const parameters = { energy_kev: 200, zone_axis: [0, 0, 1], thickness_nm: 2, defocus_nm: 10, cs_mm: 1, aperture_mrad: 20 };
const limits = { energy_kev: [80, 300], zone_axis_component: [-3, 3], thickness_nm: [0.5, 10], defocus_nm: [-200, 200], cs_mm: [0, 3], aperture_mrad: [5, 50] };

test("文件读取前限制 512KiB，遵守服务端更小限额并保留原始字节", async () => {
  let reads = 0;
  await assert.rejects(() => readCifUpload({ name: "large.cif", size: MAX_CIF_BYTES + 1, arrayBuffer: async () => { ++reads; return new ArrayBuffer(1); } }), /512 KiB/);
  assert.equal(reads, 0);
  const bytes = new Uint8Array([100, 97, 116, 97, 95, 83, 105, 10, 255]);
  const file = { name: "硅.cif", size: bytes.length, arrayBuffer: async () => bytes.buffer };
  const source = await readCifUpload(file);
  assert.equal(source.type, "upload"); assert.equal(source.filename, "硅.cif");
  assert.deepEqual(Buffer.from(source.cif_base64, "base64"), Buffer.from(bytes));
  await assert.rejects(() => readCifUpload(file, 8), /CIF 文件/);
  await assert.rejects(() => readCifUpload({ ...file, size: 8 }), /读取不完整/);
});

test("文件名按服务端契约在读取前校验，并给出中文提示", async () => {
  let reads = 0;
  for (const name of [".cif", "structure.txt", "../silicon.cif", `${"a".repeat(197)}.cif`])
    await assert.rejects(() => readCifUpload({ name, size: 8, arrayBuffer: async () => { ++reads; return new ArrayBuffer(8); } }), /CIF 文件名/);
  assert.equal(reads, 0);
});

test("数值范围取自 status，非有限数/全零或非整数晶带轴必须拒绝", () => {
  assert.equal(parameterError(parameters, limits), "");
  for (const axis of [[0, 0, 0], [0.5, 0, 1], [4, 0, 1]]) assert.match(parameterError({ ...parameters, zone_axis: axis }, limits), /晶带轴/);
  for (const value of [Number.NaN, Number.POSITIVE_INFINITY, 400, "200"]) assert.match(parameterError({ ...parameters, energy_kev: value }, limits), /加速电压/);
  assert.match(parameterError(parameters, { ...limits, thickness_nm: [3, 8] }), /样品厚度/);
});

test("仅成功、可下载且未到期的本人结果可展示；失败不冒称已退款", () => {
  const now = Date.now(), available = { state: "succeeded", download_available: true, file_expired: false, download_expires_at: new Date(now + 1000).toISOString() };
  assert.equal(canDownloadAbtem(available, now), true);
  for (const override of [{ state: "running" }, { file_expired: true }, { download_available: false }, { download_expires_at: null }, { download_expires_at: new Date(now).toISOString() }]) assert.equal(canDownloadAbtem({ ...available, ...override }, now), false);
  assert.equal(abtemCreditLabel({ reserved: 1, charged: 0, status: "frozen" }), "已冻结 1 积分");
  assert.equal(abtemCreditLabel({ reserved: 0, charged: 0, status: "refunded" }), "");
  assert.equal(abtemCreditLabel({ reserved: 1, charged: 0, status: "refunded" }), "冻结积分已退回");
});
