import type { AbtemJob, AbtemLimits, AbtemParameters, AbtemSource } from "./api";

export const MAX_CIF_BYTES = 512 * 1024;

export function isAbtemPending(state: string): boolean {
  return state === "queued" || state === "running";
}

export function abtemStateLabel(state: string): string {
  return ({ queued: "排队中", running: "计算中", succeeded: "已完成", failed: "未完成", cancelled: "已取消", timed_out: "已超时" } as Record<string, string>)[state] ?? "状态待确认";
}

export function canDownloadAbtem(job: Pick<AbtemJob, "state" | "download_available" | "file_expired" | "download_expires_at">, now: number): boolean {
  const expires = job.download_expires_at ? Date.parse(job.download_expires_at) : Number.NaN;
  return job.state === "succeeded" && job.download_available && !job.file_expired && Number.isFinite(expires) && expires > now;
}

export function abtemCreditLabel(credits: AbtemJob["credits"]): string {
  if (!credits.reserved) return "";
  if (credits.status === "frozen") return `已冻结 ${credits.reserved} 积分`;
  if (credits.charged > 0) return `成功扣除 ${credits.charged} 积分`;
  if (credits.status === "refunded") return "冻结积分已退回";
  return "积分状态待确认";
}

export function parameterError(parameters: AbtemParameters, limits: AbtemLimits): string {
  const fields = [
    ["energy_kev", "加速电压"], ["thickness_nm", "样品厚度"], ["defocus_nm", "离焦量"],
    ["cs_mm", "球差"], ["aperture_mrad", "孔径半角"],
  ] as const;
  for (const [name, label] of fields) {
    const value = parameters[name];
    const [lower, upper] = limits[name];
    if (typeof value !== "number" || !Number.isFinite(value) || value < lower || value > upper) return `${label}须在 ${lower} 至 ${upper} 范围内。`;
  }
  const axis = parameters.zone_axis;
  const [lower, upper] = limits.zone_axis_component;
  if (axis.length !== 3 || axis.some((value) => !Number.isInteger(value) || value < lower || value > upper) || !axis.some((value) => value !== 0))
    return `晶带轴须为 ${lower} 至 ${upper} 的三个整数，且不能全为 0。`;
  return "";
}

export async function readCifUpload(file: Pick<File, "name" | "size" | "arrayBuffer">, configuredLimit = MAX_CIF_BYTES): Promise<AbtemSource & { type: "upload" }> {
  const nameLength = Array.from(file.name).length;
  if (nameLength < 5 || nameLength > 200 || !file.name.toLowerCase().endsWith(".cif") || file.name !== file.name.trim() || /[/\\:\x00-\x1f]/.test(file.name))
    throw new Error("CIF 文件名须为 5 至 200 个字符、以 .cif 结尾，且不包含文件路径。");
  const limit = Math.min(MAX_CIF_BYTES, configuredLimit);
  if (!file.size || file.size > limit) throw new Error(`请选择不超过 ${Math.floor(limit / 1024)} KiB 的 CIF 文件。`);
  const buffer = await file.arrayBuffer();
  if (!buffer.byteLength || buffer.byteLength > limit || buffer.byteLength !== file.size) throw new Error("CIF 文件读取不完整，请重新选择。");
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return { type: "upload", filename: file.name, cif_base64: btoa(binary) };
}
