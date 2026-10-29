<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import Icon from "./Icon.vue";
import LiteratureRequest from "./LiteratureRequest.vue";
import LiteratureTasks from "./LiteratureTasks.vue";
import CreditsPanel from "./CreditsPanel.vue";
import LiteratureAdmin from "./LiteratureAdmin.vue";
import AdminCenter from "./AdminCenter.vue";
import Announcements from "./Announcements.vue";
import ResearchLibrary from "./ResearchLibrary.vue";
import AbtemSimulation from "./AbtemSimulation.vue";
import PymatgenTools from "./PymatgenTools.vue";
import { forgetPendingPymatgen } from "./pymatgenSession";
import { trackPage } from "./analytics";
import { useLiterature } from "./useLiterature";
import { useAbtem } from "./useAbtem";
import { abtemCreditLabel, abtemStateLabel, isAbtemPending } from "./abtemState";
import {
  API_BASE,
  ApiError,
  request,
  requestDownload,
  requestKey,
  type Candidate,
  type CandidatePage,
  type Health,
  type Job,
  type LiteratureJob,
  type User,
} from "./api";

type View = "literature" | "search" | "library" | "abtem" | "pymatgen" | "tasks" | "credits" | "account" | "admin";
type SearchMode = "codes" | "formula";
type AuthMode = "login" | "register" | "verify" | "resend-verification";
interface Settings {
  license_confirmed: boolean;
  cloud_validated: boolean;
  public_enabled: boolean;
}
interface AdminStatus {
  settings: Settings;
  worker_online: boolean;
  active_jobs: number;
  queue_length: number;
}

const view = ref<View>("literature");
const user = ref<User | null>(null);
const health = ref<Health | null>(null);
const healthError = ref(false);
const loading = ref(true);
const busy = ref(false);
const authBusy = ref(false);
const logoutPending = ref(false);
const refreshing = ref(false);
const notice = ref("");
const error = ref("");
const authMessage = ref("");
const authError = ref("");
const authMode = ref<AuthMode>("login");
const auth = reactive({ email: "", password: "", token: "" });
const mode = ref<SearchMode>("codes");
const contentType = ref<"diffraction" | "structure">("diffraction");
const query = ref("");
const jobs = ref<Job[]>([]);
const selectedJob = ref<Job | null>(null);
// 历史任务的详情选择与检索中的候选、分页及下载相互独立。
const currentSearchJob = ref<Job | null>(null);
const searchDownloadJobs = ref<Record<string, Job[]>>({});
const searchReports = computed(() => currentSearchJob.value ? searchDownloadJobs.value[currentSearchJob.value.id] ?? [] : []);
const selectedCandidates = ref<string[]>([]);
const candidatePages = ref<Record<number, CandidatePage>>({});
const candidateCache = ref<Record<string, Candidate>>({});
const candidateOffset = ref(0);
const candidateTotal = ref(0);
const candidatePageLoading = ref(false);
const candidatePageError = ref("");
const candidateRetryOffset = ref(0);
const visibleCandidates = computed(() => candidatePages.value[candidateOffset.value]?.items ?? []);
const candidateNextOffset = computed(() => candidatePages.value[candidateOffset.value]?.next_offset ?? null);
const automaticDownloads = new Map<string, { accountId: string; version: number }>();
let candidateGeneration = 0;
let candidateRequestVersion = 0;
let searchFormVersion = 0;
let jobsRequestVersion = 0;
let searchRestoreChecked = false;
const filter = ref("all");
const materialCreditJob = ref<string | null>(null);
const taskKind = ref<"literature" | "xrd" | "abtem" | "pymatgen">("literature");
const confirmMailJob = ref<Job | null>(null);
const confirmMailRequiresUnknown = ref(false);
const mailConfirmationUnknown = computed(
  () =>
    confirmMailRequiresUnknown.value ||
    confirmMailJob.value?.mail_status === "unknown",
);
const mailDialog = ref<HTMLElement | null>(null);
let previousDialogFocus: HTMLElement | null = null;
const adminStatus = ref<AdminStatus | null>(null);
const announcementsVersion = ref(0);
const settings = reactive<Settings>({
  license_confirmed: false,
  cloud_validated: false,
  public_enabled: false,
});
const idempotency = new Map<string, string>();
let jobsTimer: ReturnType<typeof setInterval> | undefined;
let healthTimer: ReturnType<typeof setInterval> | undefined;
let sessionVersion = 0;
let authTransitionVersion = 0;
let appAlive = true;
let adminRequestVersion = 0;
watch(() => user.value?.id, () => {
  forgetPendingPymatgen();
  ++sessionVersion; ++jobsRequestVersion; busy.value = false;
  jobs.value = [];
  selectedJob.value = null; currentSearchJob.value = null; searchDownloadJobs.value = {};
  searchRestoreChecked = false;
  automaticDownloads.clear(); resetCandidates();
  ++adminRequestVersion; adminStatus.value = null;
}, { flush: "sync" });
function sameSession(id: string, version: number) {
  return appAlive && user.value?.id === id && sessionVersion === version;
}
watch(query, () => { ++searchFormVersion; searchRestoreChecked = true; }, { flush: "sync" });
const {
  service: literatureService, serviceError: literatureServiceError,
  jobs: literatureJobs, jobsLoading: literatureJobsLoading, jobsError: literatureJobsError,
  selectedId: selectedLiteratureId, selectedJob: selectedLiterature,
  credits, balance: creditBalance, creditLoading, creditBusy, creditError, creditNotice,
  actionBusy: literatureBusy, actionError: literatureError, actionNotice: literatureNotice,
  now: literatureNow, activeCount: literatureActiveCount, canSubmit: canSubmitLiterature,
  loadService: loadLiteratureService, loadCredits, checkin, submit: createLiterature,
  cancel: cancelLiterature, select: selectLiteratureCandidate, showJob: showLiteratureJob,
  refresh: refreshLiterature,
} = useLiterature(user, handleError);
const {
  service: abtemService, serviceError: abtemServiceError, jobs: abtemJobs, jobsLoading: abtemJobsLoading,
  jobsError: abtemJobsError, jobsNextOffset: abtemJobsNextOffset, selectedJob: selectedAbtem,
  sources: abtemSources, sourcesLoading: abtemSourcesLoading, sourcesError: abtemSourcesError,
  sourcesLoaded: abtemSourcesLoaded, sourcesNextOffset: abtemSourcesNextOffset,
  actionBusy: abtemBusy, actionError: abtemError, actionNotice: abtemNotice, pendingSubmission: abtemPendingSubmission,
  activeCount: abtemActiveCount, canSubmit: canSubmitAbtem, submit: submitAbtem, cancel: cancelAbtem,
  loadJobs: loadAbtemJobs, loadSources: loadAbtemSources, reconcileSubmission: reconcileAbtem,
  showJob: showAbtemJob, refresh: refreshAbtem,
} = useAbtem(user, handleError, () => loadCredits(true));

const modes: {
  id: SearchMode;
  label: string;
  example: string;
  hint: string;
}[] = [
  {
    id: "codes",
    label: "卡片编号",
    example: "例如：00-046-1045",
    hint: "多个编号用空格或逗号分隔，每次最多 20 个，先查看匹配候选。",
  },
  {
    id: "formula",
    label: "化学式",
    example: "例如：MnO2、SiO2",
    hint: "如 MnO2、SiO2；按元素比例匹配，保留原始化学式。",
  },
];
const currentMode = computed(() =>
  modes.find((item) => item.id === mode.value)!,
);
const queryExample = computed(() => contentType.value === "structure" && mode.value === "codes" ? "例如：ICSD-12345" : currentMode.value.example);
const queryHint = computed(() => contentType.value === "structure" && mode.value === "codes" ? "输入 ICSD 结构编号，多个编号用空格或逗号分隔，每次最多 20 个，先查看匹配候选。" : currentMode.value.hint);
function changeContent(value: "diffraction" | "structure") {
  if (contentType.value === value) return;
  contentType.value = value;
  query.value = "";
  ++searchFormVersion;
  searchRestoreChecked = true;
  currentSearchJob.value = null; resetCandidates();
  clearMessages();
}
const activeJobs = computed(() =>
  jobs.value.filter((job) => ["queued", "running"].includes(job.state)),
);
const hasActiveJob = computed(() => activeJobs.value.length > 0);
const allActiveCount = computed(() => activeJobs.value.length + literatureActiveCount.value + abtemActiveCount.value);
const visibleJobs = computed(() =>
  jobs.value.filter(
    (job) =>
      filter.value === "all" ||
      (filter.value === "active"
        ? ["queued", "running"].includes(job.state)
        : ["succeeded", "failed", "needs_attention", "cancelled"].includes(
            job.state,
          )),
  ),
);
const xrdRestricted = computed(() => health.value?.database_admin_only === true && !user.value?.is_admin);
const canSubmit = computed(
  () =>
    !!user.value?.verified &&
    health.value?.service_enabled === true &&
    !xrdRestricted.value &&
    !healthError.value &&
    !busy.value &&
    !hasActiveJob.value,
);
const canArchiveSelection = computed(
  () =>
    canSubmit.value &&
    currentSearchJob.value?.kind === "search" && currentSearchJob.value.state === "succeeded" &&
    selectedCandidates.value.length > 0 &&
    selectedCandidates.value.length <= 20,
);
const statusText = computed(() =>
  healthError.value
    ? "服务连接中断"
    : !health.value
      ? "正在连接服务"
      : !health.value.service_enabled
        ? "服务筹备中"
        : xrdRestricted.value
          ? "暂未开放"
          : health.value.worker_online
            ? health.value.database_admin_only ? "检索服务可用" : "检索服务可用"
            : "查询服务暂不可用",
);
const showingXrd = computed(() => view.value === "search" || (view.value === "tasks" && taskKind.value === "xrd"));
const showingAbtem = computed(() => view.value === "abtem" || view.value === "tasks" && taskKind.value === "abtem");
const headerStatusText = computed(() => showingAbtem.value ? !user.value ? "登录后使用电镜模拟" : !user.value.verified ? "验证邮箱后使用电镜模拟" : abtemServiceError.value ? "模拟服务连接中断" : !abtemService.value ? "正在连接模拟服务" : !abtemService.value.enabled ? "电镜模拟暂未开放" : "计算暂缓，可提交排队" : showingXrd.value ? `XRD · ${statusText.value}` : literatureServiceError.value ? "文献服务连接中断" : !literatureService.value ? "正在连接文献服务" : !literatureService.value.enabled ? "文献服务暂未开放" : literatureService.value.worker_online ? "文献求助服务可用" : "文献服务暂时不可用");
const headerOnline = computed(() => showingAbtem.value ? !abtemServiceError.value && abtemService.value?.enabled && abtemService.value.worker_online : showingXrd.value ? !xrdRestricted.value && health.value?.service_enabled && health.value.worker_online : !literatureServiceError.value && literatureService.value?.enabled && literatureService.value.worker_online);
function clearMessages() {
  error.value = "";
  notice.value = "";
}
function keyFor(body: unknown, purpose = "job") {
  const signature = `${purpose}:${JSON.stringify(body)}`;
  if (!idempotency.has(signature)) idempotency.set(signature, requestKey());
  return { signature, key: idempotency.get(signature)! };
}
function handleError(cause: unknown) {
  error.value =
    cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  if (cause instanceof ApiError && cause.status === 401) {
    user.value = null;
    jobs.value = [];
    selectedJob.value = null;
    adminStatus.value = null;
    confirmMailJob.value = null;
    idempotency.clear();
    view.value = "account";
  }
}
function showView(next: View) {
  if (next === "admin" && (!user.value?.is_admin || !user.value.verified)) return;
  view.value = next;
  clearMessages();
  if (next === "credits" && user.value) void loadCredits();
  if (next === "abtem" && user.value) void refreshAbtem();
}
function openLiteratureTasks() {
  taskKind.value = "literature";
  showView("tasks");
}
async function submitLiterature(title: string) {
  clearMessages();
  const result = await createLiterature(title);
  if (result) openLiteratureTasks();
  else if (literatureError.value) error.value = literatureError.value;
}
async function renewLiterature(job: LiteratureJob) {
  await submitLiterature(job.canonical_title || job.title);
}
async function openAbtemTask(id: string) {
  showView("abtem");
  await showAbtemJob(id);
}
async function openCreditJob(id: string, kind?: "xrd" | "literature" | "abtem" | "pymatgen" | null) {
  if (kind === "pymatgen") { materialCreditJob.value = id; taskKind.value = "pymatgen"; showView("tasks"); return; }
  if (kind === "abtem") { await openAbtemTask(id); return; }
  if (kind === "xrd") {
    const version = sessionVersion;
    const accountId = user.value?.id;
    try {
      const job = await request<Job>(`/jobs/${encodeURIComponent(id)}`);
      if (version !== sessionVersion || accountId !== user.value?.id) return;
      selectHistoryJob(job);
      showView("tasks");
    } catch (cause) {
      if (version !== sessionVersion || accountId !== user.value?.id) return;
      handleError(cause);
    }
    return;
  }
  openLiteratureTasks();
  await showLiteratureJob(id);
}
function changeMode(next: SearchMode) {
  if (mode.value === next) return;
  mode.value = next;
  query.value = "";
  ++searchFormVersion;
  searchRestoreChecked = true;
  currentSearchJob.value = null; resetCandidates();
  clearMessages();
}
function stateLabel(state: string) {
  return (
    (
      {
        queued: "排队中",
        running: "处理中",
        succeeded: "已完成",
        failed: "未完成",
        needs_attention: "待处理",
        cancelled: "已取消",
      } as Record<string, string>
    )[state] ?? "状态待确认"
  );
}
function archiveLabel(state?: string) {
  return (
    (
      {
        not_started: "未开始",
        pending: "待归档",
        queued: "待归档",
        running: "归档中",
        complete: "归档已完成",
        completed: "归档已完成",
        succeeded: "归档已完成",
        verified: "归档已完成",
        success: "报告已生成",
        partial: "部分报告已生成",
        failed: "归档未完成",
        needs_attention: "归档待处理",
        cancelled: "已取消",
        not_applicable: "无需归档",
        none: "未开始",
      } as Record<string, string>
    )[state ?? ""] ?? (state ? "状态待确认" : "未开始")
  );
}
function mailLabel(state?: string) {
  return (
    (
      {
        not_requested: "未选择发送",
        not_started: "未发送",
        pending: "等待发送",
        queued: "等待发送",
        running: "正在提交",
        sending: "正在提交",
        submitted: "已提交",
        accepted: "已提交",
        sent: "已提交",
        smtp_accepted: "已提交",
        failed: "发送失败",
        unknown: "状态待确认",
        offline_validated: "仅离线验证",
        skipped: "未发送",
        not_applicable: "无需发送",
        none: "未发送",
      } as Record<string, string>
    )[state ?? ""] ?? (state ? "状态待确认" : "未发送")
  );
}
function creditLabel(job: Job) {
  const credit = job.credits;
  if (!credit?.reserved) return "";
  if (credit.status === "frozen") return `冻结 ${credit.reserved} 积分`;
  const refunded = credit.reserved - credit.charged;
  return `已扣 ${credit.charged} 积分` + (refunded > 0 ? `，已退回 ${refunded} 积分` : "");
}
function archiveTypeLabel(job: Job) {
  if (job.archive_label) return job.archive_label.startsWith("数据库报告") ? "数据库报告" : job.archive_label;
  if (Array.isArray(job.results)) {
    const labels = job.results.flatMap(item => {
      const label = item && typeof item === "object" && "archive_label" in item ? item.archive_label : null;
      return typeof label === "string" ? [label.startsWith("数据库报告") ? "数据库报告" : label] : [];
    });
    if (labels.length) return [...new Set(labels)].join("、");
  }
  if (
    job.results &&
    !Array.isArray(job.results) &&
    typeof job.results.archive_label === "string"
  )
    return job.results.archive_label.startsWith("数据库报告") ? "数据库报告" : job.results.archive_label;
  return job.kind === "archive" || job.download_available
    ? "数据库报告"
    : "";
}
function cifCodes(job: Job): string[] {
  if (!job.download_available || !Array.isArray(job.results)) return [];
  return job.results.flatMap(item => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    return record.status === "success" && typeof record.code === "string" && /^ICSD-[0-9]{6}$/.test(record.code) ? [record.code] : [];
  });
}
function archiveItems(
  job: Job,
): { code: string; status: string; message: string }[] {
  if (!Array.isArray(job.results)) return [];
  return job.results
    .filter((item) => item && typeof item === "object")
    .flatMap((item) => {
      const record = item as Record<string, unknown>;
      const code = record.actual_code ?? record.code;
      if (typeof code !== "string") return [];
      return [
        {
          code,
          status:
            typeof record.status === "string"
              ? record.status
              : job.archive_status === "success"
                ? "success"
                : "unknown",
          message: typeof record.message === "string" ? record.message : "",
        },
      ];
    });
}
function cardStatusLabel(status: string) {
  return (
    (
      {
        success: "已完成",
        skipped: "已完成",
        not_found: "未发现该卡片信息",
        failed: "处理失败",
        not_started: "未处理",
      } as Record<string, string>
    )[status] ?? "待确认"
  );
}
function canRequestMail(job: Job) {
  return (
    job.kind === "archive" &&
    job.download_available &&
    (["failed", "unknown"].includes(job.mail_status) ||
      job.archive_status === "partial")
  );
}
function initialPartialMail(job: Job) {
  return (
    job.archive_status === "partial" &&
    ["not_requested", "not_started", "pending"].includes(job.mail_status)
  );
}
function trapDialogFocus(event: KeyboardEvent) {
  if (event.key === "Escape" && !busy.value) {
    confirmMailJob.value = null;
    return;
  }
  if (event.key !== "Tab" || !mailDialog.value) return;
  const elements = [
    ...mailDialog.value.querySelectorAll<HTMLButtonElement>(
      "button:not(:disabled)",
    ),
  ];
  const first = elements[0];
  const last = elements[elements.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
function kindLabel(kind: string) {
  return (
    (
      {
        search: "文字检索",
        archive: "卡片归档",
        mail_retry: "邮件重新投递",
      } as Record<string, string>
    )[kind] ?? "处理任务"
  );
}
function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Asia/Shanghai",
        hour12: false,
      }).format(date);
}
function taskShortId(id: string) {
  return id.slice(0, 8).toUpperCase();
}

async function loadHealth() {
  try {
    health.value = await request<Health>("/health");
    healthError.value = false;
  } catch {
    healthError.value = true;
  }
}
async function loadMe(required = false): Promise<User | null> {
  if (authBusy.value && !required) return null;
  const version = sessionVersion;
  const transition = authTransitionVersion;
  try {
    const value = await request<User>("/auth/me");
    if (version !== sessionVersion || transition !== authTransitionVersion) return null;
    if (!value || typeof value.id !== "string" || !value.id || typeof value.email !== "string" || !value.email || typeof value.csrf_token !== "string" || !value.csrf_token)
      throw new ApiError(0, "未读取到有效登录账号，请重新登录。");
    user.value = value;
    return value;
  } catch (cause) {
    if (version !== sessionVersion || transition !== authTransitionVersion) return null;
    if (required) throw cause;
    if (!(cause instanceof ApiError && cause.status === 401))
      handleError(cause);
    return null;
  }
}
function resetCandidates() {
  ++candidateGeneration; ++candidateRequestVersion;
  selectedCandidates.value = [];
  candidatePages.value = {}; candidateCache.value = {};
  candidateOffset.value = 0; candidateTotal.value = 0;
  candidateRetryOffset.value = 0;
  candidatePageLoading.value = false; candidatePageError.value = "";
}
function seedCandidates(job: Job) {
  if (job.kind !== "search") return;
  const items = job.candidates ?? [];
  candidateTotal.value = job.candidate_total ?? items.length;
  candidatePages.value[0] = { items, total: candidateTotal.value, next_offset: job.candidate_next_offset ?? null };
  for (const candidate of items) candidateCache.value[candidate.id] = candidate;
}
async function loadCandidatePage(offset: number) {
  const job = currentSearchJob.value;
  if (!user.value || job?.kind !== "search" || job.state !== "succeeded" || candidatePageLoading.value) return;
  if (!Number.isSafeInteger(offset) || offset < 0) return;
  candidatePageError.value = "";
  if (candidatePages.value[offset]) { candidateOffset.value = offset; return; }
  const { id } = user.value; const version = sessionVersion;
  const generation = candidateGeneration; const pageVersion = ++candidateRequestVersion;
  candidatePageLoading.value = true;
  const current = () => sameSession(id, version) && currentSearchJob.value?.id === job.id && candidateGeneration === generation && candidateRequestVersion === pageVersion;
  try {
    const page = await request<CandidatePage>(`/jobs/${encodeURIComponent(job.id)}/candidates?offset=${offset}&limit=50`);
    if (!current()) return;
    if (!Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < offset + page.items.length
        || (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset || page.next_offset >= page.total)))
      throw new Error("候选列表未完整返回，请重试。");
    candidatePages.value[offset] = page; candidateOffset.value = offset; candidateTotal.value = page.total;
    for (const candidate of page.items) candidateCache.value[candidate.id] = candidate;
  } catch (cause) {
    if (!current()) return;
    candidatePageError.value = cause instanceof Error ? cause.message : "候选加载未完成，请重试。";
    candidateRetryOffset.value = offset;
    if (cause instanceof ApiError && cause.status === 401) handleError(cause);
  } finally {
    if (current()) candidatePageLoading.value = false;
  }
}
function candidateDisabledReason(candidate: Candidate) {
  if (candidate.exportable === false) return candidate.support_message || "当前记录暂不可下载。";
  const conflict = selectedCandidates.value.some((id) => id !== candidate.id && candidateCache.value[id]?.code === candidate.code);
  return conflict ? "同号已选其他来源，请先取消原选择。" : "";
}
function candidateCell(candidate: Candidate) {
  const values = candidate.cell_parameters;
  if (!values) return "";
  return ["a", "b", "c", "alpha", "beta", "gamma"].flatMap((key) => {
    const value = values[key as keyof typeof values];
    const label = ({ alpha: "α", beta: "β", gamma: "γ" } as Record<string, string>)[key] ?? key;
    const number = Number(value);
    return value === null || value === undefined || value === "" || !Number.isFinite(number) || number <= 0
      ? [] : [`${label}=${number.toFixed(4)}`];
  }).join(" · ");
}
function rememberSearchReport(searchId: string, job: Job) {
  searchDownloadJobs.value[searchId] = [job, ...(searchDownloadJobs.value[searchId] ?? []).filter((item) => item.id !== job.id)]
    .sort((first, second) => second.created_at.localeCompare(first.created_at) || first.id.localeCompare(second.id));
}
async function downloadReadyJobs(items: Job[]) {
  for (const [jobId, intent] of automaticDownloads) {
    if (!sameSession(intent.accountId, intent.version)) { automaticDownloads.delete(jobId); continue; }
    const job = items.find((item) => item.id === jobId);
    if (!job) continue;
    if (job.download_available) {
      // 先消费本次点击意图，后续轮询、失败重试和历史打开均不重复自动下载。
      automaticDownloads.delete(jobId);
      try {
        const blob = await requestDownload(jobId);
        if (!sameSession(intent.accountId, intent.version)) continue;
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url; link.download = "XRD-报告.zip";
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (cause) {
        if (sameSession(intent.accountId, intent.version)) {
          error.value = "报告已生成，自动下载未完成，请点击下载报告。";
          if (cause instanceof ApiError && cause.status === 401) handleError(cause);
        }
      }
    } else if (!["queued", "running"].includes(job.state)) automaticDownloads.delete(jobId);
  }
}
async function loadJobs() {
  if (!user.value) return;
  const id = user.value.id;
  const version = sessionVersion;
  const readVersion = ++jobsRequestVersion;
  try {
    const response = await request<{ items: Job[] }>("/jobs");
    if (!sameSession(id, version) || readVersion !== jobsRequestVersion) return;
    const creditChanged = JSON.stringify(jobs.value.map(job => [job.id, job.credits])) !== JSON.stringify(response.items.map(job => [job.id, job.credits]));
    jobs.value = response.items;
    for (const job of response.items) {
      if (job.kind !== "archive" || !job.search_job_id) continue;
      rememberSearchReport(job.search_job_id, job);
    }
    if (!searchRestoreChecked) {
      searchRestoreChecked = true;
      const previous = response.items.find((job) => job.kind === "search" && job.search_version === 2);
      if (!currentSearchJob.value && previous) selectJob(previous);
    }
    if (selectedJob.value) {
      selectedJob.value =
        response.items.find((job) => job.id === selectedJob.value?.id) ??
        selectedJob.value;
    }
    if (currentSearchJob.value) {
      currentSearchJob.value = response.items.find((job) => job.id === currentSearchJob.value?.id) ?? currentSearchJob.value;
      seedCandidates(currentSearchJob.value);
    }
    for (const searchId of Object.keys(searchDownloadJobs.value))
      searchDownloadJobs.value[searchId] = searchDownloadJobs.value[searchId]!.map((job) => response.items.find((item) => item.id === job.id) ?? job);
    if (creditChanged) await loadCredits();
    if (!sameSession(id, version) || readVersion !== jobsRequestVersion) return;
    await downloadReadyJobs(response.items);
  } catch (cause) {
    if (!sameSession(id, version) || readVersion !== jobsRequestVersion) return;
    handleError(cause);
  }
}
async function refresh() {
  refreshing.value = true;
  try {
    await Promise.all([loadHealth(), loadJobs(), loadMe(), refreshLiterature(), refreshAbtem()]);
  } finally {
    refreshing.value = false;
  }
}
async function submitQuery() {
  clearMessages();
  if (!user.value) {
    view.value = "account";
    notice.value = "登录并验证邮箱后，即可提交检索任务。";
    return;
  }
  if (!canSubmit.value || !query.value.trim()) return;
  searchRestoreChecked = true;
  const baseBody = { mode: mode.value, query: query.value.trim(), content: contentType.value, search_version: 2 };
  const formVersion = searchFormVersion;
  if (
    mode.value === "codes" &&
    query.value
      .trim()
      .split(/[\s,，;；]+/)
      .filter(Boolean).length > 20
  ) {
    error.value = "每次最多查询 20 个编号，请减少后重试。";
    return;
  }
  const keyed = keyFor(baseBody);
  const { id, csrf_token } = user.value;
  const version = sessionVersion;
  busy.value = true;
  try {
    const job = await request<Job>(
      "/jobs/search",
      {
        method: "POST",
        body: JSON.stringify({ ...baseBody, request_key: keyed.key }),
      },
      csrf_token,
    );
    if (!sameSession(id, version)) return;
    idempotency.delete(keyed.signature);
    ++jobsRequestVersion;
    jobs.value = [job, ...jobs.value.filter((item) => item.id !== job.id)];
    if (formVersion === searchFormVersion) selectJob(job);
    await Promise.all([loadJobs(), loadMe()]);
    if (!sameSession(id, version)) return;
    if (currentSearchJob.value?.id === job.id) notice.value = "检索已提交，结果返回后可在本页选择并下载。";
  } catch (cause) {
    if (sameSession(id, version)) handleError(cause);
  } finally {
    if (sameSession(id, version)) busy.value = false;
  }
}
function selectJob(job: Job) {
  selectHistoryJob(job);
  if (job.kind !== "search") return;
  if (currentSearchJob.value?.id !== job.id) { ++searchFormVersion; resetCandidates(); }
  currentSearchJob.value = job;
  if (job.search_mode === "codes" || job.search_mode === "formula") mode.value = job.search_mode;
  if (job.content_type === "diffraction" || job.content_type === "structure") contentType.value = job.content_type;
  if (typeof job.search_query === "string") query.value = job.search_query;
  seedCandidates(job);
}
function selectHistoryJob(job: Job) {
  taskKind.value = "xrd";
  selectedJob.value = job;
}
function toggleCandidate(candidate: Candidate) {
  candidateCache.value[candidate.id] = candidate;
  if (selectedCandidates.value.includes(candidate.id))
    selectedCandidates.value = selectedCandidates.value.filter(
      (id) => id !== candidate.id,
    );
  else if (candidateDisabledReason(candidate)) error.value = candidateDisabledReason(candidate);
  else if (selectedCandidates.value.length < 20) {
    selectedCandidates.value.push(candidate.id);
    error.value = "";
  }
  else error.value = "单批最多选择 20 张卡片。";
}
async function archiveSelection() {
  if (!user.value || !currentSearchJob.value || !canArchiveSelection.value) return;
  clearMessages();
  busy.value = true;
  const body = {
    search_job_id: currentSearchJob.value.id,
    candidate_ids: [...selectedCandidates.value],
    send_mail: false,
  };
  const keyed = keyFor(body);
  const { id, csrf_token } = user.value;
  const version = sessionVersion;
  const generation = candidateGeneration;
  const sameSearch = () => currentSearchJob.value?.id === body.search_job_id && candidateGeneration === generation;
  try {
    const job = await request<Job>(
      "/jobs/archive",
      {
        method: "POST",
        body: JSON.stringify({ ...body, request_key: keyed.key }),
      },
      csrf_token,
    );
    if (!sameSession(id, version)) return;
    idempotency.delete(keyed.signature);
    ++jobsRequestVersion;
    jobs.value = [job, ...jobs.value.filter((item) => item.id !== job.id)];
    rememberSearchReport(body.search_job_id, job);
    automaticDownloads.set(job.id, { accountId: id, version });
    if (sameSearch()) selectedCandidates.value = selectedCandidates.value.filter((candidateId) => !body.candidate_ids.includes(candidateId));
    await Promise.all([loadJobs(), loadMe()]);
    if (!sameSession(id, version)) return;
    const updated = searchDownloadJobs.value[body.search_job_id]?.find((item) => item.id === job.id) ?? job;
    if (sameSearch()) notice.value = updated.download_available ? "报告已生成，可在本页下载报告或 CIF。"
      : "正在生成所选报告，完成后自动下载；本页保留下载入口。";
    await downloadReadyJobs([updated]);
  } catch (cause) {
    if (sameSession(id, version) && (sameSearch() || cause instanceof ApiError && cause.status === 401)) handleError(cause);
  } finally {
    if (sameSession(id, version)) busy.value = false;
  }
}
async function jobAction(
  job: Job,
  action: "cancel" | "retry" | "mail-retry",
  confirmUnknown = false,
) {
  if (!user.value || busy.value) return;
  clearMessages();
  busy.value = true;
  const body = {
    ...(action === "mail-retry" ? { confirm_unknown: confirmUnknown } : {}),
  };
  const keyed = keyFor(body, `${job.id}/${action}`);
  const { id, csrf_token } = user.value;
  const version = sessionVersion;
  try {
    const updated = await request<Job>(
      `/jobs/${encodeURIComponent(job.id)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({ ...body, request_key: keyed.key }),
      },
      csrf_token,
    );
    if (!sameSession(id, version)) return;
    idempotency.delete(keyed.signature);
    selectedJob.value = updated;
    confirmMailJob.value = null;
    await Promise.all([loadJobs(), loadMe()]);
    if (!sameSession(id, version)) return;
    notice.value =
      action === "cancel"
        ? "取消请求已提交；正在执行的步骤会在安全位置停止。"
        : action === "mail-retry"
          ? "已创建独立邮件投递记录。"
          : "重试任务已提交。";
  } catch (cause) {
    if (!sameSession(id, version)) return;
    if (
      action === "mail-retry" &&
      cause instanceof ApiError &&
      cause.status === 409 &&
      (cause.code === "mail_delivery_unknown" ||
        cause.message.includes("状态待确认"))
    ) {
      await loadJobs();
      if (!sameSession(id, version)) return;
      confirmMailRequiresUnknown.value = true;
      confirmMailJob.value =
        jobs.value.find((item) => item.id === job.id) ?? job;
      error.value = cause.message;
    } else handleError(cause);
  } finally {
    if (sameSession(id, version)) busy.value = false;
  }
}
function openMailConfirmation(job: Job) {
  clearMessages();
  confirmMailRequiresUnknown.value = job.mail_status === "unknown";
  confirmMailJob.value = job;
}
function changeAuthMode(next: AuthMode) {
  if (authBusy.value) return;
  authMode.value = next;
  authError.value = "";
  authMessage.value = "";
}
async function submitAuth() {
  if (authBusy.value) return;
  const submittedMode = authMode.value;
  const credentials = { email: auth.email.trim(), password: auth.password, token: auth.token.trim() };
  const version = sessionVersion;
  const initialUserId = user.value?.id ?? null;
  const transition = ++authTransitionVersion;
  const current = () => transition === authTransitionVersion && version === sessionVersion && (user.value?.id ?? null) === initialUserId;
  authError.value = "";
  authMessage.value = "";
  authBusy.value = true;
  try {
    if (submittedMode === "verify") {
      await request("/auth/verify", {
        method: "POST",
        body: JSON.stringify({ token: credentials.token }),
      });
      if (!current()) return;
      authMessage.value = "邮箱验证成功，请使用邮箱和密码登录。";
      authMode.value = "login";
      auth.token = "";
    } else {
      const result = await request<{ message?: string }>(
        `/auth/${submittedMode}`,
        {
          method: "POST",
          body: JSON.stringify({
            email: credentials.email,
            password: credentials.password,
          }),
        },
      );
      if (!current()) return;
      if (["register", "resend-verification"].includes(submittedMode))
        authMessage.value =
          result?.message ??
          "若邮箱与凭据满足条件，验证邮件将发送到您的邮箱，请按邮件提示完成验证。";
      else {
        const authenticatedUser = await loadMe(true);
        if (!authenticatedUser || user.value?.id !== authenticatedUser.id || transition !== authTransitionVersion) return;
        const authenticatedVersion = sessionVersion;
        await loadJobs();
        if (!sameSession(authenticatedUser.id, authenticatedVersion) || transition !== authTransitionVersion) return;
        view.value = "literature";

      }
      auth.password = "";
    }
  } catch (cause) {
    if (!current()) return;
    authError.value =
      cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  } finally {
    if (transition === authTransitionVersion) authBusy.value = false;
  }
}
async function logout() {
  if (!user.value || authBusy.value) return;
  const { id, csrf_token } = user.value;
  const version = sessionVersion;
  const transition = ++authTransitionVersion;
  authBusy.value = true;
  logoutPending.value = true;
  try {
    await request("/auth/logout", { method: "POST" }, csrf_token);
    if (transition !== authTransitionVersion || !sameSession(id, version)) return;
    user.value = null;
    jobs.value = [];
    selectedJob.value = null;
    adminStatus.value = null;
    confirmMailJob.value = null;
    selectedCandidates.value = [];
    view.value = "literature";
    idempotency.clear();
  } catch (cause) {
    if (transition === authTransitionVersion && sameSession(id, version)) handleError(cause);
  } finally {
    if (transition === authTransitionVersion) {
      authBusy.value = false;
      logoutPending.value = false;
    }
  }
}
async function loadAdmin() {
  if (!appAlive || !user.value?.is_admin || !user.value.verified) return;
  const id = user.value.id;
  const version = sessionVersion;
  const requestVersion = ++adminRequestVersion;
  const current = () => appAlive && sameSession(id, version) && requestVersion === adminRequestVersion && user.value?.is_admin && user.value.verified;
  try {
    const value = await request<AdminStatus>("/admin/status");
    if (!current()) return;
    adminStatus.value = value;
    const { license_confirmed, cloud_validated, public_enabled } = value.settings;
    Object.assign(settings, { license_confirmed, cloud_validated, public_enabled });
  } catch (cause) {
    if (!current()) return;
    handleError(cause);
  }
}
async function saveSettings() {
  if (!appAlive || !user.value?.is_admin || !user.value.verified || !adminStatus.value || busy.value) return;
  const { id, csrf_token } = user.value;
  const version = sessionVersion;
  const current = () => appAlive && sameSession(id, version) && user.value?.is_admin && user.value.verified;
  busy.value = true;
  clearMessages();
  try {
    const { license_confirmed, cloud_validated, public_enabled } = settings;
    await request(
      "/admin/settings",
      { method: "PATCH", body: JSON.stringify({ license_confirmed, cloud_validated, public_enabled }) },
      csrf_token,
    );
    if (!current()) return;
    await Promise.all([loadAdmin(), loadHealth(), loadMe()]);
    if (!current()) return;
    notice.value = "运行设置已保存。";
  } catch (cause) {
    if (current()) handleError(cause);
  } finally {
    if (current()) busy.value = false;
  }
}

watch(confirmMailJob, async (value) => {
  if (value) {
    previousDialogFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    await nextTick();
    mailDialog.value?.querySelector<HTMLButtonElement>("button")?.focus();
  } else previousDialogFocus?.focus();
});

let analyticsReady = false;
watch(view, (next) => { if (analyticsReady) void trackPage(next); });
onMounted(async () => {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("verify");
  if (token) {
    url.searchParams.delete("verify");
    history.replaceState({}, "", url.pathname + url.search + url.hash);
  }
  analyticsReady = true;
  if (!token) void trackPage(view.value);
  await Promise.all([loadHealth(), loadMe()]);
  await loadJobs();
  loading.value = false;
  if (token) {
    auth.token = token;
    authMode.value = "verify";
    view.value = "account";
    await submitAuth();
  }
  jobsTimer = setInterval(() => {
    if (user.value && hasActiveJob.value && !busy.value) void loadJobs();
  }, 5000);
  healthTimer = setInterval(() => {
    void loadHealth();
  }, 30000);
});
onBeforeUnmount(() => {
  analyticsReady = false;
  appAlive = false; ++adminRequestVersion;
  clearInterval(jobsTimer);
  clearInterval(healthTimer);
});
</script>

<template>
  <div class="app-shell">
    <header class="site-header">
      <div class="header-inner">
        <button class="brand" aria-label="科研助手首页" @click="showView('literature')">
          <svg
            class="brand-mark"
            width="38"
            height="38"
            viewBox="0 0 40 40"
            fill="none"
            aria-hidden="true"
          >
            <path d="m20 3 15 9v17l-15 9-15-9V12L20 3Z" fill="currentColor" />
            <path
              d="m20 10 9 5v10l-9 5-9-5V15l9-5Zm0 0v20m-9-15 18 10m0-10L11 25"
              stroke="white"
              stroke-width="1.8"
              stroke-linejoin="round"
            />
          </svg>
          <span class="brand-name"
            >科研助手</span
          >
        </button>
        <nav class="main-nav" aria-label="主导航">
          <button :class="{ active: view === 'literature' }" @click="showView('literature')">文献求助</button>
          <button
            :class="{ active: view === 'search' }"
            @click="showView('search')"
          >
            XRD 晶相
          </button>
          <button :class="{ active: view === 'library' }" @click="showView('library')">资料库</button>
          <button :class="{ active: view === 'abtem' }" @click="showView('abtem')">电镜模拟</button>
          <button :class="{ active: view === 'pymatgen' }" @click="showView('pymatgen')">材料工具</button>
          <button
            :class="{ active: view === 'tasks' }"
            @click="showView('tasks')"
          >
            任务中心<span v-if="allActiveCount" class="nav-count">{{
              allActiveCount
            }}</span>
          </button>
          <button :class="{ active: view === 'credits' }" @click="showView('credits')">积分</button>
          <button
            v-if="user?.is_admin && user.verified"
            :class="{ active: view === 'admin' }"
            @click="showView('admin')"
          >
            系统管理中心
          </button>
        </nav>
        <div class="header-right">
          <span v-if="!loading && !headerOnline && view !== 'pymatgen' && !(view === 'tasks' && taskKind === 'pymatgen')" class="header-status"
            ><i
              :class="{
                online: headerOnline,
              }"
            ></i
            >{{ headerStatusText }}</span
          >
          <button v-if="user" class="header-credit" aria-label="查看我的积分和签到" @click="showView('credits')"><span>积分</span><strong>{{ creditBalance?.available ?? '—' }}</strong></button>
          <button class="account-button" :aria-label="user ? '我的账号' : '登录或注册'" @click="showView('account')">
            <Icon name="user" :size="17" /><span>{{
              user ? "我的账号" : "登录 / 注册"
            }}</span>
          </button>
        </div>
      </div>
    </header>

    <main>
      <Announcements :version="announcementsVersion" />
      <div
        v-if="error || notice"
        class="page-notice"
        :class="error ? 'notice-error' : 'notice-success'"
        role="status"
      >
        <Icon :name="error ? 'info' : 'check'" /><span>{{
          error || notice
        }}</span
        ><button
          class="icon-button"
          aria-label="关闭提示"
          @click="clearMessages"
        >
          <Icon name="close" :size="17" />
        </button>
      </div>

      <LiteratureRequest
        v-if="view === 'literature'"
        :user="user" :balance="creditBalance" :service="literatureService" :service-error="literatureServiceError"
        :can-submit="canSubmitLiterature" :busy="literatureBusy" :checked-in-busy="creditBusy" :credit-error="creditError" :credit-notice="creditNotice"
        :jobs="literatureJobs" :now="literatureNow"
        @submit="submitLiterature" @reuse="openCreditJob" @login="showView('account')" @tasks="openLiteratureTasks" @credits="showView('credits')" @checkin="checkin" @xrd="showView('search')"
      />
      <AbtemSimulation
        v-else-if="view === 'abtem'" :user="user" :service="abtemService" :service-error="abtemServiceError"
        :jobs="abtemJobs" :selected="selectedAbtem" :jobs-loading="abtemJobsLoading" :jobs-error="abtemJobsError" :jobs-next-offset="abtemJobsNextOffset"
        :sources="abtemSources" :sources-loading="abtemSourcesLoading" :sources-error="abtemSourcesError" :sources-loaded="abtemSourcesLoaded" :sources-next-offset="abtemSourcesNextOffset"
        :busy="abtemBusy" :can-submit="canSubmitAbtem && !creditBusy && !literatureBusy" :action-error="abtemError" :notice="abtemNotice" :pending-submission="!!abtemPendingSubmission" :now="literatureNow"
        @login="showView('account')" @submit="submitAbtem" @cancel="cancelAbtem" @select-job="showAbtemJob" @refresh="refreshAbtem" @sources="loadAbtemSources" @more-jobs="loadAbtemJobs(true)" @reconcile="reconcileAbtem" @xrd="showView('search'); changeContent('structure')"
      />
      <PymatgenTools v-else-if="view === 'pymatgen'" :user="user" :balance="creditBalance" @credits-changed="loadCredits" @login="showView('account')" @auth-error="handleError" />
      <ResearchLibrary v-else-if="view === 'library'" :user="user" @login="showView('account')" />
      <template v-else-if="view === 'search'">
        <section class="content-wrap service-heading"><h1>XRD 晶相检索</h1><p>按卡片编号或化学式查询，选择匹配项后下载报告。</p></section>

        <div class="content-wrap search-layout">
          <section class="search-panel panel">
            <div class="section-heading">
              <div class="title-with-icon">
                <span class="section-icon pink"><Icon name="search" /></span>
                <h2>{{ contentType === "structure" ? "查询晶体结构" : "查询衍射卡片" }}</h2>
              </div>
              <button class="text-button" @click="showView('literature')">文献求助<Icon name="arrow" :size="15" /></button>
            </div>
            <div class="content-types" role="group" aria-label="查询内容">
              <button type="button" :aria-pressed="contentType === 'diffraction'" :class="{ active: contentType === 'diffraction' }" @click="changeContent('diffraction')">衍射卡片</button>
              <button type="button" :aria-pressed="contentType === 'structure'" :class="{ active: contentType === 'structure' }" @click="changeContent('structure')">晶体结构（CIF）</button>
            </div>
            <div class="search-tabs" role="tablist" aria-label="检索类型">
              <button
                v-for="item in modes"
                :key="item.id"
                :class="{ active: mode === item.id }"
                role="tab"
                :aria-selected="mode === item.id"
                @click="changeMode(item.id)"
              >
                {{ item.label }}
              </button>
            </div>
            <form @submit.prevent="submitQuery">
              <div class="search-input-row">
                <Icon name="search" :size="21" /><input
                  v-model="query"
                  aria-label="检索内容"
                  :placeholder="queryExample"
                  maxlength="2000"
                /><button
                  class="button primary search-submit"
                  type="submit"
                  :disabled="
                    busy || (user !== null && (!canSubmit || !query.trim()))
                  "
                >
                  <span>{{
                    busy
                      ? "正在提交"
                      : "查询"
                  }}</span
                  ><Icon v-if="!busy" name="arrow" :size="17" /><span
                    v-else
                    class="spinner"
                  ></span>
                </button>
              </div>
              <p class="input-hint">{{ queryHint }}</p>
              <p class="input-hint">查询免费；选中后每项成功报告 1 积分，未成功部分退回。</p>
            </form>
            <div v-if="xrdRestricted" class="inline-note"><Icon name="shield" :size="17" /><span>查询服务暂未开放。</span></div>
            <div
              v-else-if="!loading && !health?.service_enabled"
              class="inline-note"
            >
              <Icon name="info" :size="17" /><span>{{
                healthError
                  ? "暂时无法连接后台服务，请稍后刷新。"
                  : "查询服务暂未开放。"
              }}</span>
            </div>
            <div v-else-if="!health?.worker_online" class="inline-note">
              <Icon name="info" :size="17" /><span
                >查询服务暂不可用，已提交任务将继续保留。</span
              >
            </div>
            <div v-else-if="hasActiveJob" class="inline-note">
              <Icon name="history" :size="17" /><span
                >您有一个正在处理的任务，完成后可继续提交。</span
              >
            </div>
          </section>

          <aside class="side-panel panel">
            <div class="section-heading">
              <h2>我的工作台</h2>
              <span class="mini-dot"></span>
            </div>
            <template v-if="user"
              ><p class="workbench-email">{{ user.email }}</p>
              <div class="quota-row">
                <span>免费检索</span><strong>不限次数</strong>
              </div>
              <div class="quota-row">
                <span>可用积分</span><strong>{{ creditBalance?.available ?? '—' }}</strong>
              </div>
              <div class="quota-row">
                <span>冻结积分</span><strong>{{ creditBalance?.frozen ?? '—' }}</strong>
              </div>
              <button
                class="button light full-width"
                @click="showView('tasks')"
              >
                查看我的任务<Icon name="arrow" :size="16" /></button></template
            ><template v-else
              ><div class="workbench-avatar">
                <Icon name="user" :size="27" />
              </div>
              <p class="side-lead">登录后查询与下载</p>
              <p class="small-muted center">
                验证邮箱后开通个人工作台<br />查看任务、下载归档、接收邮件
              </p>
              <button
                class="button light full-width"
                @click="showView('account')"
              >
                登录 / 注册<Icon name="arrow" :size="16" /></button
            ></template>
          </aside>

          <section v-if="currentSearchJob" class="panel result-panel" aria-label="检索结果与下载">
            <div class="section-heading">
              <div class="title-with-icon">
                <span class="section-icon blue"><Icon name="grid" /></span>
                <h2>
                  检索结果
                </h2>
                <span class="status-pill" :class="currentSearchJob.state">{{
                  stateLabel(currentSearchJob.state)
                }}</span>
              </div>
              <button class="text-button" :disabled="refreshing" @click="refresh">刷新结果<Icon name="refresh" :size="15" /></button>
            </div>
            <p v-if="currentSearchJob.search_query" class="search-query-summary">{{ currentSearchJob.content_type === 'structure' ? '晶体结构' : '衍射卡片' }} · {{ currentSearchJob.search_mode === 'formula' ? '化学式' : '卡片编号' }}：{{ currentSearchJob.search_query }}</p>
            <p v-if="currentSearchJob.message && currentSearchJob.state !== 'succeeded'" class="job-message" role="status">
              {{ currentSearchJob.message }}
            </p>
            <template
              v-if="
                currentSearchJob.state === 'succeeded'
              "
              ><template v-if="candidateTotal > 0"
                ><div class="results-toolbar">
                  <span
                    >找到
                    <strong>{{ candidateTotal }}</strong>
                    条匹配</span
                  ><span>已选 {{ selectedCandidates.length }} / 20</span>
                </div>
                <div v-if="selectedCandidates.length" class="selected-candidates" aria-label="已选匹配项">
                  <button v-for="id in selectedCandidates" :key="id" type="button" @click="toggleCandidate(candidateCache[id]!)"
                    :aria-label="`取消选择 ${candidateCache[id]?.code}，${candidateCache[id]?.source}`">
                    {{ candidateCache[id]?.code }} · {{ candidateCache[id]?.source }} <span aria-hidden="true">×</span>
                  </button>
                </div>
                <div class="candidate-grid">
                  <label
                    v-for="candidate in visibleCandidates"
                    :key="candidate.id"
                    class="candidate-card"
                    :class="{
                      selected: selectedCandidates.includes(candidate.id),
                      unavailable: !!candidateDisabledReason(candidate),
                    }"
                    ><input
                      type="checkbox"
                      :checked="selectedCandidates.includes(candidate.id)"
                      :disabled="!!candidateDisabledReason(candidate)"
                      @change="toggleCandidate(candidate)"
                    /><span class="candidate-icon"
                      ><Icon name="file" :size="23" /></span
                    ><span class="candidate-code">{{ candidate.code }}</span
                    ><strong>{{
                      candidate.phase_name || "未提供物相名称"
                    }}</strong
                    ><span class="candidate-formula">{{
                      candidate.chemical_formula || "未提供化学式"
                    }}</span><span v-if="candidate.mineral_name" class="candidate-detail">矿物：{{ candidate.mineral_name }}</span>
                    <span v-if="candidate.space_group" class="candidate-detail">空间群：{{ candidate.space_group }}</span>
                    <span v-if="candidateCell(candidate)" class="candidate-detail candidate-cell">{{ candidateCell(candidate) }}</span>
                    <span v-if="candidate.site_count != null" class="candidate-detail">原子位点：{{ candidate.site_count }}</span>
                    <span v-else-if="candidate.content_type !== 'structure' && candidate.peak_count != null" class="candidate-detail">峰数：{{ candidate.peak_count }}</span>
                    <span v-if="candidate.report_type_label" class="candidate-detail">{{ candidate.report_type_label }}</span>
                    <span v-if="candidate.report_scope_detail" class="candidate-detail">{{ candidate.report_scope_detail }}</span>
                    <span v-if="candidateDisabledReason(candidate)" class="candidate-unavailable">{{ candidateDisabledReason(candidate) }}</span>
                    <span class="candidate-source">{{
                      candidate.source || "来源未提供"
                    }}</span></label
                  >
                </div>
                <div class="candidate-pagination" aria-label="候选分页">
                  <span>第 {{ candidateOffset + 1 }}–{{ candidateOffset + visibleCandidates.length }} 条 / 共 {{ candidateTotal }} 条</span>
                  <div><button type="button" class="button light" :disabled="candidateOffset === 0 || candidatePageLoading" @click="loadCandidatePage(Math.max(0, candidateOffset - 50))">上一页</button>
                    <button type="button" class="button light" :disabled="candidateNextOffset === null || candidatePageLoading" @click="candidateNextOffset !== null && loadCandidatePage(candidateNextOffset)">{{ candidatePageLoading ? "加载中" : "下一页" }}</button></div>
                </div>
                <p v-if="candidatePageError" class="candidate-page-error" role="alert">{{ candidatePageError }} <button type="button" class="text-button" :disabled="candidatePageLoading" @click="loadCandidatePage(candidateRetryOffset)">重试</button></p>
                <div class="selection-bar">
                  <span>每次最多下载 20 项，同一编号仅选一个来源。</span
                  ><button
                    class="button primary"
                    :disabled="!canArchiveSelection"
                    @click="archiveSelection"
                  >
                    下载所选 {{ selectedCandidates.length || "" }} 项<Icon
                      name="download"
                      :size="16"
                    />
                  </button></div
              ></template>
              <div v-else class="empty-state compact">
                <Icon name="search" :size="35" />
                <h3>未发现该卡片信息</h3>
              </div></template
            >
            <div v-else-if="['queued', 'running'].includes(currentSearchJob.state)" class="search-progress" role="status"><span class="spinner"></span><span>{{ currentSearchJob.state === 'queued' ? '查询已排队，结果将在本页显示。' : '正在查询，结果将在本页显示。' }}</span></div>
            <div v-for="report in searchReports" :key="report.id" class="search-report-result" aria-live="polite" aria-label="所选报告与下载">
              <div class="results-toolbar"><strong>所选报告</strong><span>{{ stateLabel(report.state) }}</span></div>
              <p v-if="archiveTypeLabel(report)" class="report-label">{{ archiveTypeLabel(report) }}</p>
              <p v-if="report.message" class="job-message">{{ report.message }}</p>
              <p v-if="report.credits?.reserved" class="report-label">{{ creditLabel(report) }}</p>
              <div v-if="archiveItems(report).length" class="card-result-list" aria-label="逐卡归档结果"><div v-for="(item, index) in archiveItems(report)" :key="`${item.code}-${index}`" class="card-result-row"><span>{{ item.code }}<small v-if="item.message">{{ item.message }}</small></span><span :class="['success', 'skipped'].includes(item.status) ? 'success-text' : 'muted'">{{ cardStatusLabel(item.status) }}</span></div></div>
              <p v-if="report.download_available" class="input-hint">报告已生成，可重复下载，不另扣积分。</p>
              <div class="detail-actions"><a v-if="report.download_available" class="button primary" :href="`${API_BASE}/jobs/${encodeURIComponent(report.id)}/download`"><Icon name="download" :size="17" />下载报告</a>
                <a v-for="code in cifCodes(report)" :key="code" class="button light" :href="`${API_BASE}/jobs/${encodeURIComponent(report.id)}/cif-download?code=${encodeURIComponent(code)}`"><Icon name="download" :size="17" />{{ cifCodes(report).length === 1 ? "下载 CIF" : `${code} CIF` }}</a></div>
            </div>
          </section>

        </div>
      </template>

      <section v-else-if="view === 'tasks'" class="content-wrap inner-page">
        <div class="page-heading">
          <div>
            <span class="tiny-kicker">我的工作台</span>
            <h1>任务中心<span class="heading-dot">.</span></h1>
            <p>关闭页面后，已提交的任务仍会继续处理。</p>
          </div>
          <button
            class="button neutral"
            :disabled="refreshing"
            @click="refresh"
          >
            <Icon name="refresh" :size="17" />{{
              refreshing ? "刷新中" : "刷新进度"
            }}
          </button>
        </div>
        <div class="task-kind-tabs" aria-label="任务类型"><button :class="{ active: taskKind === 'literature' }" :aria-pressed="taskKind === 'literature'" @click="taskKind = 'literature'"><Icon name="file" :size="18" />文献求助<span>{{ literatureJobs.length }}</span></button><button :class="{ active: taskKind === 'xrd' }" :aria-pressed="taskKind === 'xrd'" @click="taskKind = 'xrd'"><Icon name="grid" :size="18" />XRD 晶相<span>{{ jobs.length }}</span></button><button :class="{ active: taskKind === 'abtem' }" :aria-pressed="taskKind === 'abtem'" @click="taskKind = 'abtem'"><Icon name="grid" :size="18" />电镜模拟<span>{{ abtemJobs.length }}</span></button><button :class="{ active: taskKind === 'pymatgen' }" :aria-pressed="taskKind === 'pymatgen'" @click="taskKind = 'pymatgen'"><Icon name="grid" :size="18" />材料工具</button></div>
        <div v-if="taskKind === 'xrd' && xrdRestricted" class="module-banner"><Icon name="shield" :size="18" /><span>查询服务暂未开放。</span></div>
        <div v-if="!user" class="panel empty-state">
          <div class="empty-icon"><Icon name="history" :size="35" /></div>
          <h2>登录后，查看你的每一步</h2>
          <p>文献求助、XRD 报告与电镜模拟记录保存在这里。</p>
          <button class="button primary" @click="showView('account')">
            登录 / 注册<Icon name="arrow" :size="16" />
          </button>
        </div>
        <template v-else
          ><LiteratureTasks v-if="taskKind === 'literature'"
            :jobs="literatureJobs" :selected="selectedLiterature" :busy="literatureBusy || creditBusy" :loading="literatureJobsLoading" :error="literatureJobsError" :action-error="literatureError" :notice="literatureNotice" :now="literatureNow" :can-submit="canSubmitLiterature"
            @select-job="selectedLiteratureId = $event" @cancel="cancelLiterature" @candidate="selectLiteratureCandidate" @renew="renewLiterature" @request="showView('literature')" @refresh="refreshLiterature"
          /><PymatgenTools v-else-if="taskKind === 'pymatgen'" :key="materialCreditJob ?? 'history'" :user="user" :balance="creditBalance" :initial-job-id="materialCreditJob" @credits-changed="loadCredits" history-only @login="showView('account')" @start="showView('pymatgen')" @auth-error="handleError" /><section v-else-if="taskKind === 'abtem'" class="panel abtem-history"><div class="section-heading"><h2>电镜模拟任务</h2><button class="text-button" @click="showView('abtem')">开始模拟<Icon name="arrow" :size="15" /></button></div><p v-if="abtemJobsError" class="module-message error-text" role="alert">{{ abtemJobsError }}</p><p v-if="!abtemJobs.length" class="abtem-note">{{ abtemJobsLoading ? '正在读取任务…' : '尚无模拟记录。' }}</p><button v-for="job in abtemJobs" :key="job.id" class="abtem-history-row" @click="openAbtemTask(job.id)"><span><strong>{{ job.input_name }}</strong><small>{{ formatDate(job.created_at) }} · {{ abtemCreditLabel(job.credits) }}</small></span><span class="status-pill" :class="job.state">{{ job.cancel_requested && isAbtemPending(job.state) ? '取消处理中' : abtemStateLabel(job.state) }}</span><Icon name="arrow" :size="16" /></button><button v-if="abtemJobsNextOffset !== null" class="text-button abtem-more" :disabled="abtemJobsLoading" @click="loadAbtemJobs(true)">加载更早任务</button></section><div v-else class="task-layout">
            <section class="panel tasks-list">
              <div class="list-tabs" role="tablist" aria-label="任务筛选">
                <button
                  v-for="item in [
                    { id: 'all', label: '全部任务' },
                    { id: 'active', label: '处理中' },
                    { id: 'done', label: '已结束' },
                  ]"
                  :key="item.id"
                  :class="{ active: filter === item.id }"
                  role="tab"
                  :aria-selected="filter === item.id"
                  @click="filter = item.id"
                >
                  {{ item.label }}</button
                ><span>{{ visibleJobs.length }} 个任务</span>
              </div>
              <div v-if="!visibleJobs.length" class="empty-state compact">
                <Icon name="file" :size="36" />
                <h3>这里还没有任务</h3>
                <p>从一次检索开始，探索你的第一张卡片。</p>
                <button class="text-button" @click="showView('search')">
                  去检索<Icon name="arrow" :size="15" />
                </button>
              </div>
              <button
                v-for="job in visibleJobs"
                :key="job.id"
                class="task-row"
                :class="{ selected: selectedJob?.id === job.id }"
                @click="selectHistoryJob(job)"
              >
                <span class="task-row-icon"
                  ><Icon
                    :name="
                      job.kind === 'search'
                        ? 'search'
                        : job.kind === 'mail_retry'
                          ? 'mail'
                          : 'file'
                    " /></span
                ><span class="task-row-copy"
                  ><strong
                    >{{ kindLabel(job.kind)
                    }}<span class="job-id"
                      >#{{ taskShortId(job.id) }}</span
                    ></strong
                  ><span>{{ formatDate(job.created_at) }}</span></span
                ><span class="status-pill" :class="job.state">{{
                  stateLabel(job.state)
                }}</span>
              </button>
            </section>
            <section class="panel task-detail">
              <template v-if="selectedJob"
                ><div class="section-heading">
                  <h2>任务详情</h2>
                  <span class="status-pill" :class="selectedJob.state">{{
                    stateLabel(selectedJob.state)
                  }}</span>
                </div>
                <p class="detail-id">#{{ selectedJob.id }}</p>
                <p v-if="selectedJob.credits?.reserved" class="report-label">{{ creditLabel(selectedJob) }}</p>
            <p v-if="archiveTypeLabel(selectedJob)" class="report-label">
                  {{ archiveTypeLabel(selectedJob) }}
                </p>
                <div class="detail-row">
                  <span>任务类型</span
                  ><strong>{{ kindLabel(selectedJob.kind) }}</strong>
                </div>
                <div class="detail-row">
                  <span>创建时间</span
                  ><strong>{{ formatDate(selectedJob.created_at) }}</strong>
                </div>
                <div v-if="selectedJob.kind === 'archive'" class="detail-status">
                  <span class="section-icon blue"><Icon name="file" /></span>
                  <div>
                    <small>报告归档</small
                    ><strong>{{
                      archiveLabel(selectedJob.archive_status)
                    }}</strong>
                  </div>
                </div>
                <div v-if="selectedJob.mail_status !== 'not_requested'" class="detail-status">
                  <span class="section-icon pink"><Icon name="mail" /></span>
                  <div>
                    <small>邮件投递</small
                    ><strong>{{ mailLabel(selectedJob.mail_status) }}</strong>
                  </div>
                </div>
                <div v-if="selectedJob.message" class="detail-message">
                  {{ selectedJob.message }}
                </div>
                <div
                  v-if="archiveItems(selectedJob).length"
                  class="card-result-list"
                  aria-label="逐卡归档结果"
                >
                  <div
                    v-for="(item, index) in archiveItems(selectedJob)"
                    :key="`${item.code}-${index}`"
                    class="card-result-row"
                  >
                    <span
                      >{{ item.code
                      }}<small v-if="item.message">{{
                        item.message
                      }}</small></span
                    >
                    <span
                      :class="
                        ['success', 'skipped'].includes(item.status)
                          ? 'success-text'
                          : 'muted'
                      "
                      >{{ cardStatusLabel(item.status) }}</span
                    >
                  </div>
                </div>
                <div
                  v-if="selectedJob.mail_status === 'unknown'"
                  class="inline-note warning"
                >
                  <Icon name="info" :size="18" /><span
                    >请先检查收件箱，再决定是否重新发送。</span
                  >
                </div>
                <div class="detail-actions">
                  <a
                    v-if="selectedJob.download_available"
                    class="button primary"
                    :href="`${API_BASE}/jobs/${encodeURIComponent(selectedJob.id)}/download`"
                    ><Icon name="download" :size="17" />下载报告</a
                  ><a v-for="code in cifCodes(selectedJob)" :key="code" class="button light" :href="`${API_BASE}/jobs/${encodeURIComponent(selectedJob.id)}/cif-download?code=${encodeURIComponent(code)}`"><Icon name="download" :size="17" />{{ cifCodes(selectedJob).length === 1 ? "下载 CIF" : `${code} CIF` }}</a><button
                    v-if="
                      selectedJob.kind === 'search' &&
                      selectedJob.state === 'succeeded'
                    "
                    class="button light"
                    @click="selectJob(selectedJob); showView('search')"
                  >
                    查看并选择卡片<Icon name="arrow" :size="16" /></button
                  ><button
                    v-if="['queued', 'running'].includes(selectedJob.state)"
                    class="button neutral"
                    :disabled="busy"
                    @click="jobAction(selectedJob, 'cancel')"
                  >
                    取消任务</button
                  ><button
                    v-if="
                      ['failed', 'cancelled'].includes(selectedJob.state) &&
                      selectedJob.mail_status !== 'unknown'
                    "
                    class="button neutral"
                    :disabled="busy || hasActiveJob || !health?.service_enabled || xrdRestricted"
                    @click="jobAction(selectedJob, 'retry')"
                  >
                    <Icon name="refresh" :size="16" />重试任务</button
                  ><button
                    v-if="canRequestMail(selectedJob)"
                    class="button neutral"
                    :disabled="busy || hasActiveJob || !health?.service_enabled || xrdRestricted"
                    @click="openMailConfirmation(selectedJob)"
                  >
                    <Icon name="mail" :size="16" />{{
                      initialPartialMail(selectedJob)
                        ? "发送已完成报告"
                        : "重新发送邮件"
                    }}
                  </button>
                </div></template
              >
              <div v-else class="empty-state compact">
                <Icon name="file" :size="35" />
                <h3>查看任务详情</h3>
                <p>选择任务查看结果与下载。</p>
              </div>
            </section>
          </div></template
        >
      </section>

      <section v-else-if="view === 'credits'" class="content-wrap inner-page"><CreditsPanel :user="user" :balance="creditBalance" :summary="credits" :loading="creditLoading" :busy="creditBusy" :action-busy="literatureBusy || abtemBusy" :error="creditError" :notice="creditNotice" @login="showView('account')" @checkin="checkin" @refresh="loadCredits" @job="openCreditJob" @request="showView('literature')" /></section>

      <section
        v-else-if="view === 'account'"
        class="content-wrap inner-page account-page"
      >
        <div class="page-heading">
          <div>
            <span class="tiny-kicker">账号与邮箱</span>
            <h1>我的账号<span class="heading-dot">.</span></h1>
            <p>管理账号与验证邮箱。</p>
          </div>
        </div>
        <div class="account-grid">
          <section class="panel account-form">
            <template v-if="user"
              ><div class="profile-avatar"><Icon name="user" :size="34" /></div>
              <h2>{{ user.email }}</h2>
              <span class="verified-label"
                ><Icon name="check" :size="14" />{{
                  user.verified ? "邮箱已验证" : "邮箱待验证"
                }}</span
              >
              <div class="account-credit-summary"><div><span>可用积分</span><strong>{{ creditBalance?.available ?? '—' }}</strong></div><div><span>冻结积分</span><strong>{{ creditBalance?.frozen ?? '—' }}</strong></div><button class="text-button" @click="showView('credits')">签到与流水<Icon name="arrow" :size="15" /></button></div>
              <div class="profile-quota">
                <div>
                  <span>XRD 检索</span><strong>免费</strong>
                </div>
                <div>
                  <span>成功归档</span><strong>每张 1 积分</strong>
                </div>
              </div>
              <p class="small-muted">
                XRD 与文献共用积分，报告邮件仅发送至以上验证邮箱。文献 PDF 在站内下载。
              </p>
              <button class="button neutral full-width" :disabled="authBusy" @click="logout">
                <Icon name="logout" :size="17" />{{ logoutPending ? '正在退出…' : '退出登录' }}
              </button></template
            ><template v-else
              ><div class="auth-tabs">
                <button
                  :class="{ active: authMode === 'login' }"
                  :disabled="authBusy"
                  @click="changeAuthMode('login')"
                >
                  欢迎回来</button
                ><button
                  :class="{ active: authMode === 'register' }"
                  :disabled="authBusy"
                  @click="changeAuthMode('register')"
                >
                  注册账号
                </button>
              </div>
              <h2>
                {{
                  authMode === "verify"
                    ? "验证邮箱"
                    : authMode === "login"
                      ? "登录科研助手"
                      : authMode === "resend-verification"
                        ? "重新发送验证邮件"
                        : "开启你的科研工作台"
                }}
              </h2>
              <p class="small-muted">
                {{
                  authMode === "register"
                    ? "注册赠 10 积分，验证邮箱后使用。"
                    : authMode === "verify"
                      ? "粘贴验证邮件中的令牌，完成邮箱验证。"
                      : authMode === "resend-verification"
                        ? "请输入注册时使用的邮箱和密码。"
                        : "使用已验证的邮箱登录。"
                }}
              </p>
              <form class="auth-fields" @submit.prevent="submitAuth">
                <template v-if="authMode !== 'verify'"
                  ><label
                    >邮箱地址<input
                      v-model="auth.email"
                      type="email"
                      placeholder="name@example.com"
                      autocomplete="email"
                      maxlength="254"
                      :disabled="authBusy"
                      required /></label
                  ><label
                    >密码<input
                      v-model="auth.password"
                      type="password"
                      :autocomplete="
                        authMode === 'register'
                          ? 'new-password'
                          : 'current-password'
                      "
                      :minlength="authMode === 'register' ? 12 : 1"
                      maxlength="128"
                      :disabled="authBusy"
                      :placeholder="
                        authMode === 'register'
                          ? '至少 12 个字符'
                          : '请输入密码'
                      "
                      required /></label></template
                ><label v-else
                  >验证令牌<input
                    v-model="auth.token"
                    autocomplete="off"
                    :disabled="authBusy"
                    required
                /></label>
                <div
                  v-if="authError"
                  class="auth-alert error-text"
                  role="alert"
                >
                  {{ authError }}
                </div>
                <div
                  v-if="authMessage"
                  class="auth-alert success-text"
                  role="status"
                >
                  {{ authMessage }}
                </div>
                <button
                  class="button primary full-width"
                  :disabled="authBusy"
                  type="submit"
                >
                  {{
                    authBusy
                      ? "处理中…"
                      : authMode === "login"
                        ? "登 录"
                        : authMode === "register"
                          ? "注册并发送验证邮件"
                          : authMode === "resend-verification"
                            ? "重新发送验证邮件"
                            : "验证邮箱"
                  }}<Icon v-if="!authBusy" name="arrow" :size="17" />
                </button>
              </form>
              <button
                v-if="authMode !== 'verify'"
                class="text-button manual-verify"
                :disabled="authBusy"
                @click="changeAuthMode('verify')"
              >
                已有验证邮件？手动验证邮箱
              </button>
              <button
                v-if="authMode !== 'resend-verification'"
                class="text-button manual-verify"
                :disabled="authBusy"
                @click="changeAuthMode('resend-verification')"
              >
                验证邮件未收到或已过期？重新发送
              </button></template
            >
          </section>

        </div>
      </section>

      <section
        v-else-if="view === 'admin' && user?.is_admin && user.verified"
        class="content-wrap inner-page"
      >
        <AdminCenter :key="`${user.id}:${sessionVersion}`" :user="user" :session-generation="sessionVersion" @unauthorized="handleError" @announcements-changed="announcementsVersion++" @runtime="loadAdmin">
        <template #settings>
        <div class="section-heading admin-section-heading">
          <div><h2>运行设置</h2><p class="small-muted">分别管理文献求助与 XRD 数据库服务。</p></div>
          <button class="button neutral" @click="loadAdmin">
            <Icon name="refresh" :size="17" />刷新状态
          </button>
        </div>
        <LiteratureAdmin :user="user" @unauthorized="handleError" @saved="loadLiteratureService" />
        <div class="section-heading admin-section-heading"><div><h2>XRD 数据库服务</h2><p class="small-muted">完成数据库许可与云端验证后开放。</p></div></div>
        <div class="admin-metrics">
          <article class="panel">
            <span>数据库执行器连接</span
            ><strong
              :class="adminStatus?.worker_online ? 'success-text' : 'muted'"
              >{{ adminStatus?.worker_online ? "在线" : "离线" }}</strong
            >
          </article>
          <article class="panel">
            <span>正在执行的任务</span
            ><strong>{{ adminStatus?.active_jobs ?? "—" }}</strong>
          </article>
          <article class="panel">
            <span>排队中的任务</span
            ><strong>{{ adminStatus?.queue_length ?? "—" }}</strong>
          </article>
        </div>
        <form class="panel admin-settings" @submit.prevent="saveSettings">
          <div class="section-heading">
            <h2>服务开放条件</h2>
            <span class="soft-label">管理员设置</span>
          </div>
          <label class="setting-toggle"
            ><span
              ><strong>数据库使用与分发许可已确认</strong
              ><small
                >已核对数据库云端使用、公众查询与报告分发许可。</small
              ></span
            ><input
              v-model="settings.license_confirmed"
              type="checkbox" /></label
          ><label class="setting-toggle"
            ><span
              ><strong>Linux 数据库服务已验收</strong
              ><small
                >已实测只读检索、数据库报告、邮件投递与服务重启恢复。</small
              ></span
            ><input v-model="settings.cloud_validated" type="checkbox" /></label
          ><label class="setting-toggle"
            ><span
              ><strong>开放公众检索</strong
              ><small>以上两项同时满足后，才能开放实际检索服务。</small></span
            ><input v-model="settings.public_enabled" type="checkbox"
          /></label>
          <button
            class="button primary"
            :disabled="busy || !adminStatus"
            type="submit"
          >
            {{ busy ? "保存中…" : "保存运行设置"
            }}<Icon name="check" :size="17" />
          </button>
        </form>
        </template>
        </AdminCenter>
      </section>
    </main>

    <footer class="site-footer">
      <div class="footer-inner">
        <div class="footer-brand">
          科研助手
        </div>
        <div class="footer-meta">
          <span>文献求助 · XRD 晶相检索</span>
        </div>
      </div>
    </footer>

    <div
      v-if="confirmMailJob"
      class="modal-backdrop"
      @click.self="confirmMailJob = null"
    >
      <section
        ref="mailDialog"
        class="modal panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mail-dialog-title"
        @keydown="trapDialogFocus"
      >
        <div class="section-heading">
          <span class="section-icon pink"><Icon name="mail" /></span
          ><button
            class="icon-button"
            aria-label="关闭确认"
            @click="confirmMailJob = null"
          >
            <Icon name="close" />
          </button>
        </div>
        <h2 id="mail-dialog-title">
          {{
            initialPartialMail(confirmMailJob)
              ? "发送已完成的部分报告"
              : "确认重新投递邮件"
          }}
        </h2>
        <p v-if="confirmMailJob.archive_status === 'partial'">
          本批仍有未完成卡片。邮件仅包含当前通过重新核验的报告，未完成卡片不会作为附件。
        </p>
        <p v-if="mailConfirmationUnknown">
          此前邮件的状态待确认，可能已发送成功。重新投递可能导致收到重复邮件。
        </p>
        <p v-else>将使用重新核验成功的归档，创建新的邮件投递记录。</p>
        <p v-if="error" class="auth-alert error-text" role="alert">
          {{ error }}
        </p>
        <div class="modal-recipient">
          收件邮箱<strong>{{ user?.email }}</strong>
        </div>
        <div class="modal-actions">
          <button
            class="button neutral"
            :disabled="busy"
            @click="confirmMailJob = null"
          >
            取消</button
          ><button
            class="button primary"
            :disabled="busy"
            @click="
              jobAction(confirmMailJob, 'mail-retry', mailConfirmationUnknown)
            "
          >
            {{
              busy
                ? "提交中…"
                : initialPartialMail(confirmMailJob) && !mailConfirmationUnknown
                  ? "确认发送已完成报告"
                  : "确认重新发送"
            }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
