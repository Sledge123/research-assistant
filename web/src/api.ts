export const API_BASE = "/highscore/api";

export interface Candidate {
  id: string;
  code: string;
  phase_name: string;
  chemical_formula: string | null;
  source: string;
  mineral_name?: string | null;
  space_group?: string | null;
  cell_parameters?: Partial<Record<"a" | "b" | "c" | "alpha" | "beta" | "gamma", string | number | null>> | null;
  peak_count?: number | null;
  site_count?: number | null;
  content_type?: "diffraction" | "structure";
  exportable?: boolean;
  support_message?: string | null;
  report_type_label?: string;
  report_scope_detail?: string;
  possible_truncation?: boolean;
}

export interface CandidatePage {
  items: Candidate[];
  total: number;
  next_offset: number | null;
}

export interface Job {
  id: string;
  kind: string;
  state: string;
  archive_status: string;
  mail_status: string;
  message: string;
  results: unknown[] | Record<string, unknown> | null;
  candidates: Candidate[];
  search_version?: number;
  candidate_total?: number;
  candidate_next_offset?: number | null;
  search_query?: string;
  search_mode?: "codes" | "formula";
  content_type?: "diffraction" | "structure";
  search_job_id?: string;
  created_at: string;
  download_available: boolean;
  archive_type?: string;
  archive_label?: string;
  progress?: number;
  credits?: { reserved: number; charged: number; status: string };
}

export interface User {
  id: string;
  email: string;
  verified: boolean;
  is_admin: boolean;
  csrf_token: string;
  quotas: Record<string, number>;
  credits: CreditBalance;
}

export interface CreditBalance {
  available: number;
  frozen: number;
  checked_in: boolean;
}

export interface CreditEntry {
  id: string;
  kind: string;
  label: string;
  amount: number;
  frozen_amount: number;
  created_at: string;
  job_id: string | null;
  job_kind?: "xrd" | "literature" | "abtem" | "pymatgen" | null;
  reason?: string;
  operator_email?: string;
}

export interface CreditSummary extends CreditBalance {
  items: CreditEntry[];
}

export interface LiteratureServiceStatus {
  enabled: boolean;
  worker_online: boolean;
  message: string;
}

export interface AbtemParameters {
  energy_kev: number;
  zone_axis: [number, number, number];
  thickness_nm: number;
  defocus_nm: number;
  cs_mm: number;
  aperture_mrad: number;
}

export interface AbtemLimits {
  max_cif_bytes: number;
  energy_kev: [number, number];
  zone_axis_component: [number, number];
  thickness_nm: [number, number];
  defocus_nm: [number, number];
  cs_mm: [number, number];
  aperture_mrad: [number, number];
  retention_days: number;
}

export interface AbtemServiceStatus extends LiteratureServiceStatus {
  defaults: AbtemParameters;
  limits: AbtemLimits;
}

export interface AbtemArchiveSource {
  job_id: string;
  code: string;
  phase_name: string;
  source: string;
}

export type AbtemSource = { type: "upload"; filename: string; cif_base64: string }
  | { type: "archive"; job_id: string; code: string };

export interface AbtemSubmission {
  request_key: string;
  source: AbtemSource;
  parameters: AbtemParameters;
}

export interface AbtemJob {
  id: string;
  kind: "abtem";
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "timed_out";
  phase: string;
  message: string;
  created_at: string;
  updated_at: string;
  progress: number;
  parameters: AbtemParameters;
  input_name: string;
  source_type: "upload" | "archive";
  credits: { reserved: number; charged: number; status: string };
  download_available: boolean;
  download_expires_at: string | null;
  file_expired: boolean;
  cancel_requested: boolean;
  result_summary?: { actual_thickness_nm?: number; atom_count?: number; grid?: number[]; elapsed_seconds?: number };
}

export interface AbtemPage<T> {
  items: T[];
  next_offset: number | null;
}

export interface LiteratureCandidate {
  id: string;
  title: string;
  doi: string | null;
  journal: string | null;
  year: string | number | null;
}

export interface LiteratureJob {
  id: string;
  kind: "literature";
  title: string;
  canonical_title: string | null;
  doi: string | null;
  state: "queued" | "running" | "waiting" | "needs_selection" | "needs_attention" | "succeeded" | "failed" | "cancelled";
  stage?: "" | "fetching" | "awaiting_fulltext" | "renewing" | "verifying" | "ready" | "retrying";
  last_checked_at?: string | null;
  next_check_at?: string | null;
  message: string;
  created_at: string;
  deadline: string;
  ready_at: string | null;
  download_expires_at: string | null;
  download_available: boolean;
  file_expired: boolean;
  credit_status: "frozen" | "charged" | "refunded";
  candidates: LiteratureCandidate[];
}

export interface LiteratureAdminStatus extends LiteratureServiceStatus {
  queue_length: number;
  active_jobs: number;
  needs_attention: number;
  accounts_ready: boolean;
}

export interface Health {
  status: string;
  service_enabled: boolean;
  worker_online: boolean;
  database_admin_only?: boolean;
  gates: Record<string, boolean>;
}

export interface AdminOverview {
  users: { total: number; verified: number; admins: number; regular_total: number; regular_verified: number; regular_admins: number; debug_total: number };
  jobs: { xrd_total: number; literature_total: number; queued: number; running: number };
  credits: { available: number; frozen: number };
  recent_activity: AdminActivity[];
}

export interface AdminActivity {
  id: string | number;
  action: string;
  actor_email?: string | null;
  target_email?: string | null;
  amount?: number | null;
  reason?: string | null;
  title?: string | null;
  created_at: string;
}

export type AdminAccountType = "regular" | "debug" | "all";

export interface AdminUser {
  id: string;
  email: string;
  verified: boolean;
  is_admin: boolean;
  is_debug: boolean;
  debug_feature: "research_library" | "literature" | "pymatgen" | "xrd" | "abtem" | null;
  created_at: string;
  available: number;
  frozen: number;
  xrd_jobs: number;
  literature_jobs: number;
}

export interface AdminRecentJob {
  id: string;
  kind: string;
  state: string;
  created_at: string;
  title?: string;
  codes?: string[];
}

export interface AdminUserDetail {
  user: AdminUser;
  credits: CreditSummary;
  recent_jobs: AdminRecentJob[];
}

export interface Announcement {
  id: string;
  title: string;
  body: string;
  status: "draft" | "published";
  revision: number;
  created_at: string;
  updated_at: string;
  published_at: string | null;
}

export interface ResearchLibraryItem {
  id: string;
  title: string;
  description: string;
  category: string;
  license: string;
  source_url: string;
  filename: string;
  extension: string;
  size_bytes: number;
  created_at: number;
  updated_at: number;
  published_at: number | null;
  revision: number;
  available: boolean;
  status: "draft" | "published" | "archived" | "deleting";
  sha256?: string;
}

export interface ResearchLibraryStorage {
  used_bytes: number;
  quota_bytes: number;
  available_bytes: number;
  max_file_bytes: number;
  server_free_bytes: number;
  minimum_free_bytes: number;
}

export interface ResearchLibraryPage {
  items: ResearchLibraryItem[];
  total: number;
  categories: string[];
  next_offset: number | null;
  storage?: ResearchLibraryStorage;
}

export type PublicAnnouncement = Pick<Announcement, "id" | "title" | "body" | "published_at" | "revision">;

export interface AdminPage<T> {
  items: T[];
  total: number;
  next_offset: number | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
  csrfToken?: string,
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "暂时无法连接服务，请检查网络后重试。");
  }
  const body = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  if (!response.ok) {
    const detail = body?.detail;
    let message = "请求未完成，请稍后重试。";
    let code: string | undefined;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail))
      message = detail
        .map((item: { msg?: string }) => item.msg ?? "输入无效")
        .join("；");
    else if (detail && typeof detail === "object") {
      const structured = detail as { message?: unknown; cause?: unknown };
      if (typeof structured.message === "string") message = structured.message;
      if (typeof structured.cause === "string") code = structured.cause;
    }
    throw new ApiError(response.status, message, code);
  }
  return body as T;
}

export function requestKey(): string {
  return crypto.randomUUID();
}

export async function requestDownload(jobId: string): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}/download`, { credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "报告已生成，自动下载未完成，请使用下载报告链接重试。");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new ApiError(response.status, typeof body?.detail === "string" ? body.detail : "报告下载未完成，请使用下载报告链接重试。");
  }
  return response.blob();
}
