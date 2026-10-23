import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { ApiError, request, requestKey, type AbtemArchiveSource, type AbtemJob, type AbtemPage, type AbtemParameters, type AbtemServiceStatus, type AbtemSource, type AbtemSubmission, type User } from "./api";
import { isAbtemPending, parameterError } from "./abtemState";

export function useAbtem(user: Ref<User | null>, onUnauthorized: (cause: unknown) => void, refreshCredits: () => Promise<unknown>) {
  const service = ref<AbtemServiceStatus | null>(null);
  const serviceError = ref("");
  const jobs = ref<AbtemJob[]>([]);
  const jobsLoading = ref(false);
  const jobsError = ref("");
  const jobsNextOffset = ref<number | null>(null);
  const sources = ref<AbtemArchiveSource[]>([]);
  const sourcesLoading = ref(false);
  const sourcesError = ref("");
  const sourcesLoaded = ref(false);
  const sourcesNextOffset = ref<number | null>(null);
  const selectedId = ref("");
  const selectedJob = computed(() => jobs.value.find((job) => job.id === selectedId.value) ?? null);
  const actionBusy = ref(false);
  const actionError = ref("");
  const actionNotice = ref("");
  const pendingSubmission = ref<AbtemSubmission | null>(null);
  const activeCount = computed(() => jobs.value.filter((job) => isAbtemPending(job.state)).length);
  // 服务端已开放时允许排队；执行器忙碌或暂离线不等于停止接收队列。
  const canSubmit = computed(() => !!user.value?.verified && !!service.value?.enabled && !serviceError.value && !actionBusy.value && !pendingSubmission.value && !activeCount.value);
  const jobPages = new Map<number, AbtemPage<AbtemJob>>();
  let sessionVersion = 0;
  let serviceRequest = 0;
  let jobsRequest = 0;
  let sourcesRequest = 0;
  let detailsRequest = 0;
  let alive = true;
  let pendingAccount = "";
  const current = (version: number, id: string) => alive && sessionVersion === version && user.value?.id === id && user.value.verified;
  const message = (cause: unknown) => cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  function fail(cause: unknown, target: Ref<string>, version: number, id: string) {
    if (!current(version, id)) return;
    target.value = message(cause);
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized(cause);
  }
  function persistPending(value: AbtemSubmission | null) {
    pendingSubmission.value = value;
    if (!pendingAccount) return;
    try {
      if (value) sessionStorage.setItem(`highscore.abtem.pending.${pendingAccount}`, JSON.stringify(value));
      else sessionStorage.removeItem(`highscore.abtem.pending.${pendingAccount}`);
    } catch { /* 存储不可用时保留当前页面中的原请求，仍可安全核对。 */ }
  }
  function restorePending(id: string) {
    try {
      const saved = sessionStorage.getItem(`highscore.abtem.pending.${id}`);
      if (!saved) return;
      const value = JSON.parse(saved) as AbtemSubmission;
      if (typeof value.request_key === "string" && value.request_key.length <= 128 && value.parameters && value.source && ["upload", "archive"].includes(value.source.type)) {
        pendingSubmission.value = value;
        actionNotice.value = "上次提交结果待确认，请先核对提交结果。";
      }
    } catch { /* 损坏或不可用的浏览器存储不进入请求。 */ }
  }
  function invalidateJobs() { ++jobsRequest; ++detailsRequest; jobsLoading.value = false; }
  function upsert(job: AbtemJob) { jobs.value = [job, ...jobs.value.filter((item) => item.id !== job.id)]; }
  function checkedJob(job: AbtemJob): AbtemJob {
    if (!job || typeof job.id !== "string" || job.kind !== "abtem" || !job.credits || !job.parameters || !Array.isArray(job.parameters.zone_axis))
      throw new Error("未读取到完整模拟任务，请刷新或核对提交结果。");
    return job;
  }
  async function loadService() {
    if (!user.value?.verified) return;
    const id = user.value.id, version = sessionVersion, sequence = ++serviceRequest;
    try {
      const result = await request<AbtemServiceStatus>("/abtem/status");
      if (!current(version, id) || sequence !== serviceRequest) return;
      service.value = result; serviceError.value = "";
    } catch (cause) { if (sequence === serviceRequest) fail(cause, serviceError, version, id); }
  }
  async function loadJobs(more = false, afterWrite = false) {
    if (!user.value?.verified || jobsLoading.value || (actionBusy.value && !afterWrite) || (more && jobsNextOffset.value === null)) return;
    const id = user.value.id, version = sessionVersion, sequence = ++jobsRequest;
    const offset = more ? jobsNextOffset.value! : 0;
    jobsLoading.value = true;
    try {
      const result = await request<AbtemPage<AbtemJob>>(`/abtem/jobs?offset=${offset}&limit=50`);
      if (!current(version, id) || sequence !== jobsRequest) return;
      const pages = more ? new Map(jobPages) : new Map<number, AbtemPage<AbtemJob>>();
      pages.set(offset, result);
      // 轮询更新首页时保留用户已经展开的历史页，并逐页按编号去重。
      const sameFirstPage = JSON.stringify(result.items.map((job) => job.id)) === JSON.stringify(jobPages.get(0)?.items.map((job) => job.id));
      if (!more && result.next_offset !== null && sameFirstPage) for (const [pageOffset, page] of jobPages) if (pageOffset > 0) pages.set(pageOffset, page);
      const items: AbtemJob[] = [];
      for (const [, page] of [...pages].sort(([left], [right]) => left - right))
        for (const job of page.items) if (!items.some((item) => item.id === job.id)) items.push(checkedJob(job));
      const selection = selectedId.value;
      if (selection && !items.some((job) => job.id === selection)) {
        const confirmed = more ? jobs.value.find((job) => job.id === selection) : null;
        if (confirmed) items.push(confirmed);
        else {
          try {
            const detail = checkedJob(await request<AbtemJob>(`/abtem/jobs/${encodeURIComponent(selection)}`));
            if (!current(version, id) || sequence !== jobsRequest) return;
            items.push(detail);
          } catch (cause) {
            if (!current(version, id) || sequence !== jobsRequest) return;
            if (!(cause instanceof ApiError && cause.status === 404)) throw cause;
          }
        }
      }
      const changed = JSON.stringify(jobs.value.map((job) => [job.id, job.credits.status])) !== JSON.stringify(items.map((job) => [job.id, job.credits.status]));
      jobPages.clear(); for (const [pageOffset, page] of pages) jobPages.set(pageOffset, page);
      const lastPage = [...pages].sort(([left], [right]) => right - left)[0]?.[1];
      jobs.value = items; jobsNextOffset.value = lastPage?.next_offset ?? null; jobsError.value = "";
      if (!selectedId.value || !items.some((job) => job.id === selectedId.value)) selectedId.value = items[0]?.id ?? "";
      if (changed && !actionBusy.value) void refreshCredits();
    } catch (cause) { if (sequence === jobsRequest) fail(cause, jobsError, version, id); }
    finally { if (current(version, id) && sequence === jobsRequest) jobsLoading.value = false; }
  }
  async function loadSources(more = false) {
    if (!user.value?.verified || sourcesLoading.value || (more && sourcesNextOffset.value === null)) return;
    const id = user.value.id, version = sessionVersion, sequence = ++sourcesRequest;
    const offset = more ? sourcesNextOffset.value! : 0;
    sourcesLoading.value = true;
    try {
      const result = await request<AbtemPage<AbtemArchiveSource>>(`/abtem/sources?offset=${offset}&limit=50`);
      if (!current(version, id) || sequence !== sourcesRequest) return;
      const items = more ? [...sources.value] : [];
      for (const item of result.items) if (!items.some((old) => old.job_id === item.job_id && old.code === item.code)) items.push(item);
      sources.value = items; sourcesNextOffset.value = result.next_offset ?? null; sourcesError.value = ""; sourcesLoaded.value = true;
    } catch (cause) { if (sequence === sourcesRequest) fail(cause, sourcesError, version, id); }
    finally { if (current(version, id) && sequence === sourcesRequest) sourcesLoading.value = false; }
  }
  async function submitSnapshot(snapshot: AbtemSubmission) {
    if (!user.value?.verified || actionBusy.value) return null;
    const { id, csrf_token } = user.value, version = sessionVersion;
    actionBusy.value = true; actionError.value = ""; actionNotice.value = "";
    invalidateJobs(); persistPending(snapshot);
    try {
      const job = checkedJob(await request<AbtemJob>("/abtem/jobs", { method: "POST", body: JSON.stringify(snapshot) }, csrf_token));
      if (!current(version, id)) return null;
      invalidateJobs(); upsert(job); selectedId.value = job.id; persistPending(null);
      actionNotice.value = "模拟任务已提交，进度与结果将在本页显示。";
      await Promise.all([loadJobs(false, true), refreshCredits()]);
      return current(version, id) ? job : null;
    } catch (cause) {
      if (!current(version, id)) return null;
      // 服务端先按原键恢复已接受任务；明确业务拒绝可释放表单，鉴权失败仍须保留原请求。
      if (cause instanceof ApiError && cause.status > 0 && cause.status < 500 && ![401, 403].includes(cause.status)) persistPending(null);
      fail(cause, actionError, version, id);
      if (pendingSubmission.value) actionNotice.value = "提交结果待确认，请核对原请求后继续。";
      return null;
    } finally { if (current(version, id)) actionBusy.value = false; }
  }
  async function submit(source: AbtemSource, parameters: AbtemParameters) {
    if (!canSubmit.value || !service.value) return null;
    const validation = parameterError(parameters, service.value.limits);
    if (validation) { actionError.value = validation; return null; }
    const snapshot = JSON.parse(JSON.stringify({ request_key: requestKey(), source, parameters })) as AbtemSubmission;
    return submitSnapshot(snapshot);
  }
  async function reconcileSubmission() {
    if (!pendingSubmission.value) return null;
    return submitSnapshot(pendingSubmission.value);
  }
  async function cancel(job: AbtemJob) {
    if (!user.value?.verified || actionBusy.value || !isAbtemPending(job.state) || job.cancel_requested) return;
    const { id, csrf_token } = user.value, version = sessionVersion;
    actionBusy.value = true; actionError.value = ""; actionNotice.value = ""; invalidateJobs();
    try {
      const updated = checkedJob(await request<AbtemJob>(`/abtem/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: "{}" }, csrf_token));
      if (!current(version, id)) return;
      invalidateJobs(); upsert(updated); selectedId.value = updated.id;
      actionNotice.value = updated.cancel_requested && isAbtemPending(updated.state) ? "已申请取消，计算停止后将退回冻结积分。" : "取消请求已处理，请查看任务与积分状态。";
      await Promise.all([loadJobs(false, true), refreshCredits()]);
    } catch (cause) {
      fail(cause, actionError, version, id);
      if (current(version, id) && cause instanceof ApiError && (!cause.status || cause.status >= 500)) actionNotice.value = "取消结果待确认，请刷新任务状态；重复取消不会重复退分。";
    } finally { if (current(version, id)) actionBusy.value = false; }
  }
  async function showJob(jobId: string) {
    if (!user.value?.verified || actionBusy.value) return;
    invalidateJobs();
    const id = user.value.id, version = sessionVersion, sequence = ++detailsRequest;
    selectedId.value = jobId;
    if (jobs.value.some((job) => job.id === jobId)) return;
    try {
      const job = checkedJob(await request<AbtemJob>(`/abtem/jobs/${encodeURIComponent(jobId)}`));
      if (!current(version, id) || sequence !== detailsRequest) return;
      upsert(job); selectedId.value = job.id; actionError.value = "";
    } catch (cause) { if (sequence === detailsRequest) fail(cause, actionError, version, id); }
  }
  async function refresh() { await Promise.all([loadService(), loadJobs()]); }

  watch(() => `${user.value?.id ?? ""}:${user.value?.verified ?? false}:${user.value?.csrf_token ?? ""}`, () => {
    ++sessionVersion; ++serviceRequest; ++jobsRequest; ++sourcesRequest; ++detailsRequest;
    // 只清除当前界面。原账号的待核对请求按账号隔离保留，重新登录后继续使用原键。
    pendingAccount = user.value?.id ?? "";
    service.value = null; serviceError.value = ""; jobs.value = []; jobsLoading.value = false; jobsError.value = ""; jobsNextOffset.value = null;
    jobPages.clear();
    sources.value = []; sourcesLoading.value = false; sourcesError.value = ""; sourcesLoaded.value = false; sourcesNextOffset.value = null;
    selectedId.value = ""; actionBusy.value = false; actionError.value = ""; actionNotice.value = ""; pendingSubmission.value = null;
    if (user.value?.verified) { restorePending(user.value.id); void refresh(); }
  }, { immediate: true, flush: "sync" });
  const jobsTimer = setInterval(() => { if (activeCount.value && !actionBusy.value) void loadJobs(); }, 4000);
  const serviceTimer = setInterval(() => { void loadService(); }, 30000);
  onBeforeUnmount(() => { alive = false; ++sessionVersion; clearInterval(jobsTimer); clearInterval(serviceTimer); });
  return { service, serviceError, jobs, jobsLoading, jobsError, jobsNextOffset, sources, sourcesLoading, sourcesError, sourcesLoaded, sourcesNextOffset, selectedId, selectedJob, actionBusy, actionError, actionNotice, pendingSubmission, activeCount, canSubmit, loadService, loadJobs, loadSources, submit, reconcileSubmission, cancel, showJob, refresh };
}
