import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { ApiError, request, requestKey, type CreditSummary, type LiteratureJob, type LiteratureServiceStatus, type User } from "./api";
import { canSubmitLiterature, isLiteraturePending } from "./literatureState";

export function useLiterature(user: Ref<User | null>, onUnauthorized: (cause: unknown) => void) {
  const service = ref<LiteratureServiceStatus | null>(null);
  const serviceError = ref("");
  const jobs = ref<LiteratureJob[]>([]);
  const jobsLoading = ref(false);
  const jobsError = ref("");
  const selectedId = ref("");
  const credits = ref<CreditSummary | null>(null);
  const creditLoading = ref(false);
  const creditBusy = ref(false);
  const creditError = ref("");
  const creditNotice = ref("");
  const actionBusy = ref(false);
  const actionError = ref("");
  const actionNotice = ref("");
  const now = ref(Date.now());
  const balance = computed(() => credits.value ?? user.value?.credits ?? null);
  const selectedJob = computed(() => jobs.value.find((job) => job.id === selectedId.value) ?? null);
  const activeCount = computed(() => jobs.value.filter((job) => isLiteraturePending(job.state)).length);
  const canSubmit = computed(() => !serviceError.value && canSubmitLiterature(user.value, service.value, actionBusy.value || creditBusy.value));
  const keys = new Map<string, string>();
  let sessionVersion = 0;
  let jobsTimer: ReturnType<typeof setInterval> | undefined;
  let creditTimer: ReturnType<typeof setInterval> | undefined;
  let serviceRequest = 0;
  let jobsRequest = 0;
  let creditRequest = 0;

  const current = (version: number, id: string) => sessionVersion === version && user.value?.id === id;
  function message(cause: unknown) {
    return cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  }
  function fail(cause: unknown, target: Ref<string>, version: number, id: string) {
    if (!current(version, id)) return;
    target.value = message(cause);
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized(cause);
  }
  function applyCredits(value: CreditSummary) {
    credits.value = value;
    if (user.value) user.value = { ...user.value, credits: { available: value.available, frozen: value.frozen, checked_in: value.checked_in } };
  }
  function invalidateJobReads() {
    ++jobsRequest;
    jobsLoading.value = false;
  }
  function invalidateCreditReads() {
    ++creditRequest;
    creditLoading.value = false;
  }
  async function loadService() {
    const sequence = ++serviceRequest;
    try {
      const result = await request<LiteratureServiceStatus>("/literature/status");
      if (sequence !== serviceRequest) return;
      service.value = result;
      serviceError.value = "";
    } catch (cause) {
      if (sequence === serviceRequest) serviceError.value = message(cause);
    }
  }
  async function loadJobs(afterWrite = false) {
    if (!user.value || jobsLoading.value || ((actionBusy.value || creditBusy.value) && afterWrite !== true)) return;
    const id = user.value.id;
    const version = sessionVersion;
    const sequence = ++jobsRequest;
    jobsLoading.value = true;
    try {
      const result = await request<{ items: LiteratureJob[] }>("/literature/jobs");
      if (!current(version, id) || sequence !== jobsRequest) return;
      const items = [...result.items];
      const selection = selectedId.value;
      let missingMessage = "";
      // 列表只含最近任务；当前已确认任务须通过详情核查，不能因分页范围消失。
      if (selection && !items.some((job) => job.id === selection)) {
        try {
          const selected = await request<LiteratureJob>(`/literature/jobs/${encodeURIComponent(selection)}`);
          if (!current(version, id) || sequence !== jobsRequest || selectedId.value !== selection) return;
          items.push(selected);
        } catch (cause) {
          if (!current(version, id) || sequence !== jobsRequest || selectedId.value !== selection) return;
          if (cause instanceof ApiError && cause.status === 404) missingMessage = message(cause);
          else throw cause;
        }
      }
      const creditsChanged = JSON.stringify(jobs.value.map((job) => [job.id, job.credit_status])) !== JSON.stringify(items.map((job) => [job.id, job.credit_status]));
      jobs.value = items;
      jobsError.value = missingMessage;
      if (!selectedId.value || !items.some((job) => job.id === selectedId.value)) selectedId.value = items[0]?.id ?? "";
      if (creditsChanged && !actionBusy.value) void loadCredits();
    } catch (cause) {
      if (sequence === jobsRequest) fail(cause, jobsError, version, id);
    } finally {
      if (current(version, id) && sequence === jobsRequest) jobsLoading.value = false;
    }
  }
  async function loadCredits(afterWrite = false) {
    if (!user.value || ((creditLoading.value || actionBusy.value || creditBusy.value) && afterWrite !== true)) return;
    // 外部模拟任务也使用同一账本；写操作之后必须读取新的余额，作废先前仍在途的读取。
    if (afterWrite === true) invalidateCreditReads();
    const id = user.value.id;
    const version = sessionVersion;
    const sequence = ++creditRequest;
    creditLoading.value = true;
    try {
      const result = await request<CreditSummary>("/credits");
      if (!current(version, id) || sequence !== creditRequest) return;
      applyCredits(result);
      creditError.value = "";
    } catch (cause) {
      if (sequence === creditRequest) fail(cause, creditError, version, id);
    } finally {
      if (current(version, id) && sequence === creditRequest) creditLoading.value = false;
    }
  }
  async function checkin() {
    if (!user.value || creditBusy.value || actionBusy.value || balance.value?.checked_in) return;
    const { id, csrf_token } = user.value;
    const version = sessionVersion;
    creditBusy.value = true;
    creditError.value = "";
    creditNotice.value = "";
    // 使先前的余额读取失效，防止较旧的响应覆盖本次签到结果。
    invalidateCreditReads();
    try {
      const result = await request<CreditSummary>("/credits/checkin", { method: "POST", body: "{}" }, csrf_token);
      if (!current(version, id)) return;
      invalidateCreditReads();
      applyCredits(result);
      creditNotice.value = "今日签到已完成，积分余额已更新。";
    } catch (cause) {
      fail(cause, creditError, version, id);
    } finally {
      if (current(version, id)) creditBusy.value = false;
    }
  }
  async function mutate(path: string, body: Record<string, unknown>, success: string): Promise<LiteratureJob | null> {
    if (!user.value || actionBusy.value || creditBusy.value) return null;
    const { id, csrf_token } = user.value;
    const version = sessionVersion;
    actionBusy.value = true;
    actionError.value = "";
    actionNotice.value = "";
    // 作废操作前发出的列表请求，避免旧列表使刚提交的任务消失。
    invalidateJobReads();
    invalidateCreditReads();
    try {
      const result = await request<LiteratureJob>(path, { method: "POST", body: JSON.stringify(body) }, csrf_token);
      if (!current(version, id)) return null;
      // 写响应建立新的读取边界，操作期间的普通刷新不能覆盖确认结果。
      invalidateJobReads();
      invalidateCreditReads();
      jobs.value = [result, ...jobs.value.filter((job) => job.id !== result.id)];
      selectedId.value = result.id;
      actionNotice.value = success;
      await Promise.all([loadJobs(true), loadCredits(true)]);
      return current(version, id) ? result : null;
    } catch (cause) {
      fail(cause, actionError, version, id);
      return null;
    } finally {
      if (current(version, id)) actionBusy.value = false;
    }
  }
  async function submit(title: string) {
    if (!canSubmit.value || !title.trim()) return null;
    const normalized = title.trim();
    if (!keys.has(normalized)) keys.set(normalized, requestKey());
    const result = await mutate("/literature/jobs", { title: normalized, request_key: keys.get(normalized) }, "文献任务已提交，可在这里查看候选、等待进度与下载结果。");
    if (result) keys.delete(normalized);
    return result;
  }
  async function cancel(job: LiteratureJob) {
    if (!isLiteraturePending(job.state)) return null;
    return mutate(`/literature/jobs/${encodeURIComponent(job.id)}/cancel`, {}, "取消请求已处理，请查看任务与积分状态。");
  }
  async function select(job: LiteratureJob, candidateId: string) {
    return mutate(`/literature/jobs/${encodeURIComponent(job.id)}/select`, { candidate_id: candidateId }, "已选择该文献，继续获取全文。");
  }
  async function showJob(jobId: string) {
    if (actionBusy.value || creditBusy.value) return;
    selectedId.value = jobId;
    if (!user.value || jobs.value.some((job) => job.id === jobId)) return;
    const id = user.value.id;
    const version = sessionVersion;
    const sequence = jobsRequest;
    try {
      const result = await request<LiteratureJob>(`/literature/jobs/${encodeURIComponent(jobId)}`);
      if (current(version, id) && sequence === jobsRequest) {
        jobs.value = [result, ...jobs.value.filter((job) => job.id !== result.id)];
        selectedId.value = result.id;
      }
    } catch (cause) { if (sequence === jobsRequest) fail(cause, actionError, version, id); }
  }
  async function refresh() {
    await Promise.all([loadService(), loadJobs(), loadCredits()]);
  }

  watch(() => user.value?.id, (id) => {
    ++sessionVersion;
    ++jobsRequest;
    ++creditRequest;
    clearInterval(jobsTimer);
    clearInterval(creditTimer);
    jobs.value = [];
    selectedId.value = "";
    credits.value = null;
    jobsError.value = "";
    creditError.value = "";
    creditNotice.value = "";
    actionError.value = "";
    actionNotice.value = "";
    actionBusy.value = false;
    creditBusy.value = false;
    jobsLoading.value = false;
    creditLoading.value = false;
    keys.clear();
    if (!id) return;
    void Promise.all([loadJobs(), loadCredits()]);
    jobsTimer = setInterval(() => {
      if (activeCount.value && !actionBusy.value) void loadJobs();
    }, 5000);
    creditTimer = setInterval(() => {
      if (!creditBusy.value && !actionBusy.value) void loadCredits();
    }, 30000);
  }, { immediate: true, flush: "sync" });

  void loadService();
  const serviceTimer = setInterval(() => { void loadService(); }, 30000);
  const clockTimer = setInterval(() => { now.value = Date.now(); }, 1000);
  onBeforeUnmount(() => {
    ++sessionVersion;
    ++serviceRequest;
    clearInterval(jobsTimer);
    clearInterval(creditTimer);
    clearInterval(serviceTimer);
    clearInterval(clockTimer);
  });

  return { service, serviceError, jobs, jobsLoading, jobsError, selectedId, selectedJob, credits, balance, creditLoading, creditBusy, creditError, creditNotice, actionBusy, actionError, actionNotice, now, activeCount, canSubmit, loadService, loadJobs, loadCredits, checkin, submit, cancel, select, showJob, refresh };
}
