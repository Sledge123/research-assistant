<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import Icon from "./Icon.vue";
import PymatgenResult from "./PymatgenResult.vue";
import { API_BASE, ApiError, request, requestKey, type User, type CreditBalance } from "./api";
import { pendingPymatgen, savePendingPymatgen } from "./pymatgenSession";

type Operation = "analyze" | "convert" | "xrd" | "supercell" | "compare";
interface Source { type: "upload" | "archive"; filename?: string; content_base64?: string; job_id?: string; code?: string }
interface ArchiveSource { job_id: string; code: string; phase_name: string; source: string }
interface MaterialJob {
  id: string; operation: Operation; state: string; phase: string; progress: number;
  input_name: string; created_at: string; updated_at: string; message: string;
  cancel_requested: boolean; download_available: boolean; download_expires_at: string | null;
  file_expired: boolean; summary: Record<string, unknown> | null;
  credits: { reserved: number; charged: number; status: "frozen" | "charged" | "refunded" | "not_required" };
}
interface Page<T> { items: T[]; next_offset: number | null }
interface Status { enabled: boolean; worker_online: boolean; message: string; limits: Record<string, unknown>; costs: Record<Operation, number> }
const props = defineProps<{ user: User | null; balance?: CreditBalance | null; historyOnly?: boolean; initialJobId?: string | null }>();
const emit = defineEmits<{ login: []; start: []; authError: [cause: unknown]; creditsChanged: [] }>();
const tools: { key: Operation; label: string; description: string }[] = [
  { key: "analyze", label: "结构分析", description: "晶胞、坐标与空间群" },
  { key: "convert", label: "格式转换", description: "CIF 与 POSCAR" },
  { key: "xrd", label: "XRD衍射峰生成", description: "衍射峰表与科学图" },
  { key: "supercell", label: "超胞生成", description: "按三个方向扩胞" },
  { key: "compare", label: "结构比较", description: "比较两个结构" },
];
const operation = ref<Operation>("analyze");
const sources = ref<(Source | null)[]>([null, null]);
const sourceModes = ref(["upload", "upload"]);
const sourceLabels = ref(["", ""]);
const archiveChoices = ref(["", ""]);
const archives = ref<ArchiveSource[]>([]);
const archiveOffset = ref<number | null>(0);
const sourcesLoading = ref(false);
const status = ref<Status | null>(null);
const jobs = ref<MaterialJob[]>([]);
const nextOffset = ref<number | null>(null);
const selected = ref<MaterialJob | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const serviceError = ref("");
const actionNotice = ref("");
const pending = ref<Record<string, unknown> | null>(null);
const now = ref(Date.now());
const wavelength = ref(1.54056);
const thetaMin = ref(5);
const thetaMax = ref(90);
const symprec = ref(0.01);
const angleTolerance = ref(5);
const scaling = ref([2, 2, 2]);
const ltol = ref(0.2);
const stol = ref(0.3);
const matchAngle = ref(5);
let generation = 0;
let reading = [0, 0];
let pagesLoaded = 0;
let sourcesLoaded = false;
let alive = true;
let polling = false;
let timer: ReturnType<typeof setInterval> | undefined;
const isPending = (job: MaterialJob) => ["queued", "running"].includes(job.state);
const active = computed(() => jobs.value.find(isPending));
const cost = computed(() => status.value?.costs[operation.value] ?? null);
const creditLabel = (job: MaterialJob) => job.credits.status === "not_required" ? "历史免费任务" :
  job.credits.status === "frozen" ? `已冻结 ${job.credits.reserved} 积分` :
  job.credits.status === "charged" ? `已扣除 ${job.credits.charged} 积分` : `已退回 ${job.credits.reserved} 积分`;
const canDownload = computed(() => !!selected.value?.download_available && !selected.value.file_expired &&
  !!selected.value.download_expires_at && Date.parse(selected.value.download_expires_at) > now.value);
const label = (key: string) => tools.find(tool => tool.key === key)?.label ?? "材料分析";
const stateLabel = (job: MaterialJob) => job.cancel_requested && isPending(job) ? "取消处理中" :
  ({ queued: "排队中", running: "计算中", succeeded: "已完成", failed: "未完成", cancelled: "已取消", timed_out: "已超时" } as Record<string, string>)[job.state] ?? "状态待确认";
const phaseLabel = (phase: string) => ({ preparing: "检查结构", calculating: "正在计算", packaging: "整理结果", verifying: "核对结果" } as Record<string, string>)[phase] ?? "等待计算";
const dateLabel = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
const same = (id: string, version: number) => alive && props.user?.id === id && generation === version;
function showError(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  if (cause instanceof ApiError && cause.status === 401) emit("authError", cause);
}
function checkedJob(value: unknown): MaterialJob {
  const job = value as Partial<MaterialJob> | null;
  if (!job || typeof job !== "object" || typeof job.id !== "string" || !/^[a-f0-9]{32}$/.test(job.id)
    || !tools.some(tool => tool.key === job.operation)
    || !["queued", "running", "succeeded", "failed", "cancelled", "timed_out"].includes(String(job.state))
    || typeof job.phase !== "string" || typeof job.input_name !== "string" || typeof job.message !== "string"
    || typeof job.progress !== "number" || !Number.isInteger(job.progress) || job.progress < 0 || job.progress > 100
    || typeof job.created_at !== "string" || !Number.isFinite(Date.parse(job.created_at))
    || typeof job.updated_at !== "string" || !Number.isFinite(Date.parse(job.updated_at))
    || typeof job.cancel_requested !== "boolean" || typeof job.download_available !== "boolean" || typeof job.file_expired !== "boolean"
    || !(job.download_expires_at === null || typeof job.download_expires_at === "string" && Number.isFinite(Date.parse(job.download_expires_at)))
    || !(job.summary === null || typeof job.summary === "object" && !Array.isArray(job.summary))
    || !job.credits || !Number.isInteger(job.credits.reserved) || job.credits.reserved < 0
    || !Number.isInteger(job.credits.charged) || job.credits.charged < 0 || job.credits.charged > job.credits.reserved
    || !["frozen", "charged", "refunded", "not_required"].includes(job.credits.status))
    throw new ApiError(502, "任务回执暂不完整，请核对上次提交或稍后刷新。");
  return job as MaterialJob;
}
async function refresh(append = false) {
  if (!props.user?.verified || polling) return;
  const id = props.user.id, version = generation;
  polling = true; loading.value = true;
  try {
    const offset = append ? nextOffset.value : 0;
    if (offset === null) return;
    const [page, service] = await Promise.all([
      request<Page<MaterialJob>>(`/pymatgen/jobs?offset=${offset}`), request<Status>("/pymatgen/status"),
    ]);
    if (!same(id, version)) return;
    if (!page || !Array.isArray(page.items) || !(page.next_offset === null || Number.isInteger(page.next_offset) && page.next_offset >= 0)
      || !service || typeof service.enabled !== "boolean" || typeof service.worker_online !== "boolean" || typeof service.message !== "string"
      || !service.costs || !tools.every(tool => Number.isInteger(service.costs[tool.key]) && service.costs[tool.key] > 0))
      throw new ApiError(502, "任务列表回执暂不完整，请稍后刷新。");
    page.items = page.items.map(checkedJob);
    const creditChanged = JSON.stringify(jobs.value.map(job => [job.id, job.credits])) !== JSON.stringify(page.items.map(job => [job.id, job.credits]));
    jobs.value = append ? [...jobs.value, ...page.items.filter(item => !jobs.value.some(old => old.id === item.id))] :
      [...page.items, ...jobs.value.filter(old => !page.items.some(item => item.id === old.id))];
    if (append || pagesLoaded <= 1) nextOffset.value = page.next_offset;
    pagesLoaded = append ? pagesLoaded + 1 : Math.max(1, pagesLoaded);
    status.value = service; serviceError.value = "";
    if (!selected.value && props.initialJobId) {
      const requested = checkedJob(await request<unknown>(`/pymatgen/jobs/${encodeURIComponent(props.initialJobId)}`));
      if (!same(id, version)) return;
      selected.value = requested;
    }
    if (!selected.value && page.items.length) selected.value = page.items[0]!;
    if (selected.value) {
      const selectedId = selected.value.id;
      const current = checkedJob(await request<unknown>(`/pymatgen/jobs/${encodeURIComponent(selectedId)}`));
      if (same(id, version) && selected.value?.id === selectedId) selected.value = current;
    }
    if (same(id, version) && creditChanged) emit("creditsChanged");
  } catch (cause) {
    if (same(id, version)) {
      serviceError.value = cause instanceof Error ? cause.message : "材料工具连接暂时中断。";
      if (cause instanceof ApiError && cause.status === 401) emit("authError", cause);
    }
  } finally { if (same(id, version)) { loading.value = false; polling = false; } }
}
async function loadSources(append = false) {
  if (!props.user?.verified || sourcesLoading.value || !append && sourcesLoaded) return;
  const id = props.user.id, version = generation;
  const offset = append ? archiveOffset.value : 0;
  if (offset === null) return;
  sourcesLoading.value = true;
  try {
    const page = await request<Page<ArchiveSource>>(`/pymatgen/sources?offset=${offset}`);
    if (!same(id, version)) return;
    archives.value = append ? [...archives.value, ...page.items] : page.items;
    archiveOffset.value = page.next_offset;
    sourcesLoaded = true;
  } catch (cause) { if (same(id, version)) showError(cause); }
  finally { if (same(id, version)) sourcesLoading.value = false; }
}
function selectArchive(index: number) {
  const item = archives.value.find(item => `${item.job_id}:${item.code}` === archiveChoices.value[index]);
  sources.value[index] = item ? { type: "archive", job_id: item.job_id, code: item.code } : null;
  sourceLabels.value[index] = item ? `${item.code} · ${item.phase_name}` : "";
}
function changeSource(index: number) {
  reading[index] = (reading[index] ?? 0) + 1;
  sources.value[index] = null; sourceLabels.value[index] = ""; archiveChoices.value[index] = "";
  if (sourceModes.value[index] === "archive") void loadSources();
}
async function upload(event: Event, index: number) {
  const file = (event.target as HTMLInputElement).files?.[0];
  reading[index] = (reading[index] ?? 0) + 1;
  const readVersion = reading[index], version = generation, id = props.user?.id;
  sources.value[index] = null; sourceLabels.value[index] = ""; error.value = "";
  if (!file || !id) return;
  try {
    if (!file.size || file.size > 512 * 1024 || Array.from(file.name).length > 180 || /[/\\:\x00-\x1f]/.test(file.name) || file.name !== file.name.trim())
      throw new Error("请选择不超过512 KiB的结构文件，文件名不能包含路径。");
    if (!/\.cif$/i.test(file.name) && !/^(?:POSCAR|CONTCAR)$/i.test(file.name) && !/\.(?:poscar|vasp)$/i.test(file.name))
      throw new Error("请选择 CIF 或包含明确元素的 POSCAR 文件。");
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (!bytes.length || bytes.length !== file.size) throw new Error("结构文件读取不完整，请重新选择。");
    let binary = "";
    for (let start = 0; start < bytes.length; start += 8192) binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
    if (!same(id, version) || readVersion !== reading[index]) return;
    sources.value[index] = { type: "upload", filename: file.name, content_base64: btoa(binary) };
    sourceLabels.value[index] = file.name;
  } catch (cause) { if (same(id, version)) showError(cause); }
}
function parameters(): Record<string, unknown> {
  if (operation.value === "analyze") return { symprec: symprec.value, angle_tolerance: angleTolerance.value };
  if (operation.value === "xrd") return { wavelength_angstrom: wavelength.value, two_theta_min: thetaMin.value, two_theta_max: thetaMax.value };
  if (operation.value === "supercell") return { scaling: scaling.value.map(Number) };
  if (operation.value === "compare") return { ltol: ltol.value, stol: stol.value, angle_tol: matchAngle.value, primitive_cell: true, scale: false, allow_subset: false };
  return {};
}
async function submit(reconcile = false) {
  if (!props.user?.verified || busy.value || active.value && !reconcile) return;
  const id = props.user.id, version = generation;
  error.value = ""; actionNotice.value = "";
  if (!reconcile) {
    const count = operation.value === "compare" ? 2 : 1;
    const inputs = sources.value.slice(0, count);
    if (inputs.some(input => !input)) { error.value = count === 2 ? "请先选择两个结构。" : "请先选择一个结构。"; return; }
    pending.value = { request_key: requestKey(), operation: operation.value, inputs, parameters: parameters(), pricing_version: 1 };
    savePendingPymatgen(id, pending.value);
  }
  if (!pending.value) return;
  busy.value = true;
  try {
    const job = checkedJob(await request<unknown>("/pymatgen/jobs", { method: "POST", body: JSON.stringify(pending.value) }, props.user.csrf_token));
    if (!same(id, version)) return;
    pending.value = null; savePendingPymatgen(id, null); selected.value = job;
    jobs.value = [job, ...jobs.value.filter(old => old.id !== job.id)];
    actionNotice.value = `任务已提交，${creditLabel(job)}。`;
    emit("creditsChanged");
    await refresh();
  } catch (cause) {
    if (!same(id, version)) return;
    if (cause instanceof ApiError && cause.status > 0 && cause.status < 500) {
      pending.value = null; savePendingPymatgen(id, null);
    }
    showError(cause);
  } finally { if (same(id, version)) busy.value = false; }
}
async function cancel(job: MaterialJob) {
  if (!props.user?.verified || busy.value) return;
  const id = props.user.id, version = generation;
  busy.value = true; error.value = "";
  try {
    const changed = checkedJob(await request<unknown>(`/pymatgen/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: "{}" }, props.user.csrf_token));
    if (!same(id, version)) return;
    if (selected.value?.id === job.id) selected.value = changed;
    jobs.value = jobs.value.map(old => old.id === changed.id ? changed : old);
    actionNotice.value = changed.state === "cancelled" ? "任务已取消。" : "正在停止计算，请稍候。";
    emit("creditsChanged");
  } catch (cause) { if (same(id, version)) showError(cause); }
  finally { if (same(id, version)) busy.value = false; }
}
watch(() => props.user?.id, () => {
  ++generation; reading = reading.map(value => value + 1); pagesLoaded = 0; sourcesLoaded = false;
  polling = false; busy.value = false; loading.value = false; sourcesLoading.value = false;
  jobs.value = []; selected.value = null; status.value = null; archives.value = [];
  pending.value = props.user ? pendingPymatgen(props.user.id) : null;
  sources.value = [null, null]; sourceLabels.value = ["", ""]; error.value = ""; serviceError.value = ""; actionNotice.value = "";
  if (timer) clearInterval(timer);
  if (props.user?.verified) {
    void refresh();
    timer = setInterval(() => { now.value = Date.now(); void refresh(); }, 4000);
  }
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { alive = false; ++generation; if (timer) clearInterval(timer); });
</script>

<template>
  <section class="content-wrap materials-workspace">
    <div class="materials-heading"><div><h1>{{ historyOnly ? '材料工具任务' : '材料工具' }}</h1><p>XRD衍射峰生成成功扣 2 积分，其余工具每次成功扣 1 积分。</p></div><button v-if="historyOnly" class="secondary-button" @click="emit('start')">使用材料工具</button><button v-else class="text-button" :disabled="loading" @click="refresh()">刷新</button></div>
    <section v-if="!user?.verified" class="panel materials-login"><Icon name="grid" :size="28" /><h2>{{ user ? '验证邮箱后使用材料工具' : '登录后使用材料工具' }}</h2><p>上传结构文件，计算完成后在本页查看和下载结果。</p><button class="primary-button" @click="emit('login')">{{ user ? '前往账号' : '登录 / 注册' }}</button></section>
    <template v-else>
      <p v-if="serviceError || status && !status.enabled || status?.message" class="module-message" role="status">{{ serviceError || status?.message || '材料工具暂未开放。' }}</p>
      <p v-if="error" class="module-message error-text" role="alert">{{ error }}</p>
      <p v-if="actionNotice" class="module-message" role="status">{{ actionNotice }}</p>
      <div class="materials-layout">
        <section v-if="!historyOnly" class="panel materials-form">
          <div class="section-heading"><h2>选择工具</h2><span v-if="balance" class="materials-cost">可用 {{ balance.available }} 积分</span></div>
          <div class="materials-tool-tabs" aria-label="材料工具"><button v-for="tool in tools" :key="tool.key" type="button" :class="{ active: operation === tool.key }" :aria-pressed="operation === tool.key" :disabled="busy || !!pending" @click="operation = tool.key"><strong>{{ tool.label }}</strong><small>{{ tool.description }}<template v-if="status"> · {{ status.costs[tool.key] }} 积分</template></small></button></div>
          <form @submit.prevent="submit()">
            <fieldset :disabled="busy || !!pending" class="materials-fields">
              <div v-for="index in (operation === 'compare' ? 2 : 1)" :key="index" class="materials-input">
                <h3>{{ operation === 'compare' ? `结构 ${index === 1 ? 'A' : 'B'}` : '输入结构' }}</h3>
                <div class="materials-source-tabs"><label><input v-model="sourceModes[index - 1]" type="radio" :name="`material-source-${index}`" value="upload" @change="changeSource(index - 1)" />上传文件</label><label><input v-model="sourceModes[index - 1]" type="radio" :name="`material-source-${index}`" value="archive" @change="changeSource(index - 1)" />本人结构 CIF</label></div>
                <label v-if="sourceModes[index - 1] === 'upload'" class="materials-upload">选择 CIF / POSCAR<input :aria-label="operation === 'compare' ? `上传结构${index === 1 ? 'A' : 'B'}` : '上传结构文件'" type="file" @change="upload($event, index - 1)" /><span>单文件不超过512 KiB</span></label>
                <template v-else><select v-model="archiveChoices[index - 1]" :aria-label="`选择本人结构${index}`" @change="selectArchive(index - 1)"><option value="">{{ sourcesLoading ? '正在读取结构…' : '请选择已生成的结构 CIF' }}</option><option v-for="item in archives" :key="`${item.job_id}:${item.code}`" :value="`${item.job_id}:${item.code}`">{{ item.code }} · {{ item.phase_name }}</option></select><button v-if="archiveOffset !== null" type="button" class="text-button" :disabled="sourcesLoading" @click="loadSources(true)">加载更多结构</button><p v-if="!archives.length && !sourcesLoading" class="materials-note">可先在 XRD 晶相中生成本人结构 CIF。</p></template>
                <p v-if="sourceLabels[index - 1]" class="materials-selected-file">已选择：{{ sourceLabels[index - 1] }}</p>
              </div>
              <div v-if="operation === 'analyze'" class="materials-parameters"><label>位置容差（Å）<input v-model.number="symprec" type="number" step="0.0001" min="0.0001" max="0.1" required /></label><label>角度容差（°）<input v-model.number="angleTolerance" type="number" step="0.1" min="0.1" max="10" required /></label></div>
              <div v-if="operation === 'xrd'" class="materials-parameters"><label>波长（Å）<input v-model.number="wavelength" type="number" step="0.00001" min="0.1" max="5" required /></label><label>起始2θ（°）<input v-model.number="thetaMin" type="number" step="0.1" min="0.1" max="170" required /></label><label>终止2θ（°）<input v-model.number="thetaMax" type="number" step="0.1" min="0.1" max="170" required /></label></div>
              <div v-if="operation === 'supercell'" class="materials-parameters"><label v-for="(axis, index) in ['a', 'b', 'c']" :key="axis">{{ axis }}方向倍数<input v-model.number="scaling[index]" type="number" step="1" min="1" max="4" required /></label></div>
              <div v-if="operation === 'compare'" class="materials-parameters"><label>长度容差<input v-model.number="ltol" type="number" min="0.01" max="0.5" step="0.01" required /></label><label>位点容差<input v-model.number="stol" type="number" min="0.01" max="0.8" step="0.01" required /></label><label>角度容差（°）<input v-model.number="matchAngle" type="number" min="0.1" max="10" step="0.1" required /></label></div>
            </fieldset>
            <p class="materials-note">提交时冻结{{ cost === null ? '相应' : cost }}积分，成功后扣除，失败或取消退回；有效期内重复下载免费。仅支持有序、满占有结构，每个输入最多512个原子。成功结果保留3天。</p>
            <button v-if="pending" type="button" class="primary-button materials-submit" :disabled="busy" @click="submit(true)">{{ busy ? '正在核对…' : '核对上次提交' }}</button><button v-else type="submit" class="primary-button materials-submit" :disabled="busy || !!active || !status?.enabled || cost === null">{{ busy ? '正在提交…' : active ? '已有材料任务处理中' : cost === null ? '正在读取费用…' : `开始计算 · ${cost} 积分` }}</button>
          </form>
        </section>
        <div class="materials-results" :class="{ 'materials-history-wide': historyOnly }">
          <section class="panel materials-result" aria-live="polite">
            <div class="section-heading"><h2>计算结果</h2><span v-if="selected" class="status-pill" :class="selected.state">{{ stateLabel(selected) }}</span></div>
            <template v-if="selected"><h3>{{ label(selected.operation) }} · {{ selected.input_name }}</h3><p class="materials-note">{{ dateLabel(selected.created_at) }} · {{ creditLabel(selected) }}</p><div v-if="isPending(selected)" class="materials-progress"><progress :value="selected.progress" max="100" /><p>{{ selected.state === 'queued' ? '已排队，结果将在本页显示。' : phaseLabel(selected.phase) }}</p><button type="button" class="secondary-button" :disabled="busy || selected.cancel_requested" @click="cancel(selected)">取消任务</button></div><template v-else-if="selected.state === 'succeeded'"><dl v-if="selected.summary" class="materials-summary"><div v-if="selected.summary.formula"><dt>化学式</dt><dd>{{ selected.summary.formula }}</dd></div><div v-if="selected.summary.atom_count !== undefined"><dt>原子数</dt><dd>{{ selected.summary.atom_count }}</dd></div><div v-if="selected.summary.spacegroup_symbol"><dt>计算空间群</dt><dd>{{ selected.summary.spacegroup_symbol }} {{ selected.summary.spacegroup_number }}</dd></div><div v-if="selected.summary.peak_count !== undefined"><dt>理论衍射峰</dt><dd>{{ selected.summary.peak_count }}</dd></div><div v-if="selected.summary.matched !== undefined"><dt>比较结果</dt><dd>{{ selected.summary.matched ? '在所选容差下匹配' : '在所选容差下不匹配' }}</dd></div></dl><img v-if="selected.operation === 'xrd' && canDownload" :src="`${API_BASE}/pymatgen/jobs/${encodeURIComponent(selected.id)}/image/xrd`" alt="根据输入结构计算的XRD衍射峰图" class="materials-xrd" /><template v-if="canDownload"><a class="primary-button materials-download" :href="`${API_BASE}/pymatgen/jobs/${encodeURIComponent(selected.id)}/download`" :download="`materials-${selected.id}.zip`">下载完整结果<Icon name="download" :size="17" /></a><p class="materials-note">有效至{{ dateLabel(selected.download_expires_at) }}（北京时间），期内可重复下载。</p></template><p v-else class="materials-note">结果文件已到期。</p><PymatgenResult v-if="canDownload && user" :key="selected.id" :job-id="selected.id" :user-id="user.id" :operation="selected.operation" @auth-error="emit('authError', $event)" /></template><p v-if="selected.message && !isPending(selected)" class="materials-note">{{ selected.message }}</p></template><p v-else class="materials-empty">{{ loading ? '正在读取任务…' : '选择结构并提交后，结果会在这里显示。' }}</p>
          </section>
          <section class="panel materials-history"><div class="section-heading"><h2>历史任务</h2><button class="text-button" :disabled="loading" @click="refresh()">刷新</button></div><p v-if="!jobs.length" class="materials-empty">{{ loading ? '正在读取…' : '尚无材料工具任务。' }}</p><button v-for="job in jobs" :key="job.id" class="materials-history-row" :class="{ selected: selected?.id === job.id }" @click="selected = job"><span><strong>{{ label(job.operation) }}</strong><small>{{ job.input_name }}</small><small>{{ dateLabel(job.created_at) }} · {{ creditLabel(job) }}</small></span><span class="status-pill" :class="job.state">{{ stateLabel(job) }}</span></button><button v-if="nextOffset !== null" class="text-button" :disabled="loading" @click="refresh(true)">加载更早任务</button></section>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.materials-workspace { padding-top: 32px; padding-bottom: 48px; }
.materials-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
.materials-heading h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: -.5px; }
.materials-heading p, .materials-note { color: #6d6f7c; line-height: 1.7; font-size: 13px; }
.materials-heading p { margin: 0; }
.materials-layout { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 24px; align-items: start; }
.materials-form, .materials-result, .materials-history { padding: 25px; min-width: 0; }
.materials-cost { color: #287b64; background: #edf7f1; border-radius: 6px; font-size: 12px; padding: 4px 9px; }
.materials-tool-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 22px 0; }
.materials-tool-tabs button { text-align: left; border: 1px solid #e9e7eb; background: #fcfbfc; border-radius: 10px; padding: 11px 12px; cursor: pointer; }
.materials-tool-tabs button.active { border-color: #b33e67; background: #fff4f7; color: #933758; }
.materials-tool-tabs strong, .materials-tool-tabs small { display: block; }
.materials-tool-tabs strong { font-size: 14px; font-weight: 600; }
.materials-tool-tabs small { margin-top: 5px; font-size: 11px; color: #777581; }
.materials-fields { border: 0; padding: 0; margin: 0; min-width: 0; }
.materials-input { margin: 18px 0 23px; }
.materials-input h3 { font-size: 14px; font-weight: 600; margin-bottom: 12px; }
.materials-source-tabs { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 13px; font-size: 13px; }
.materials-source-tabs label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.materials-source-tabs input { accent-color: #a63b60; }
.materials-upload { display: grid; gap: 10px; padding: 18px; border: 1px dashed #d9d3da; border-radius: 10px; font-size: 13px; background: #fdfcfd; }
.materials-upload input { max-width: 100%; font: inherit; font-size: 12px; }
.materials-upload span { font-size: 12px; color: #87818b; }
.materials-input select { width: 100%; padding: 10px; border: 1px solid #dedbe1; border-radius: 7px; background: white; font: inherit; font-size: 13px; }
.materials-selected-file { font-size: 12px; color: #37725f; overflow-wrap: anywhere; }
.materials-parameters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.materials-parameters label { display: grid; gap: 8px; font-size: 12px; color: #65616c; }
.materials-parameters input { width: 100%; min-width: 0; padding: 10px; border: 1px solid #dedbe1; border-radius: 7px; font: inherit; background: white; color: #33323c; }
.materials-submit { width: 100%; justify-content: center; margin-top: 8px; }
.materials-results { display: grid; gap: 24px; min-width: 0; }
.materials-result h3 { font-size: 14px; font-weight: 500; margin: 22px 0 8px; overflow-wrap: anywhere; }
.materials-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; padding: 18px 0; margin: 0; }
.materials-summary div { min-width: 0; }
.materials-summary dt { font-size: 12px; color: #8b8590; margin-bottom: 6px; }
.materials-summary dd { font-size: 16px; margin: 0; overflow-wrap: anywhere; }
.materials-progress progress { width: 100%; accent-color: #a83d64; }
.materials-progress p { font-size: 13px; color: #77717e; }
.materials-xrd { width: 100%; height: auto; border-radius: 8px; margin: 10px 0 18px; background: white; }
.materials-download { display: inline-flex; align-items: center; gap: 12px; text-decoration: none; margin-top: 12px; }
.materials-empty { padding: 28px 0; font-size: 13px; color: #8b8590; line-height: 1.8; }
.materials-history-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; padding: 14px 10px; border: 0; border-bottom: 1px solid #f0edf1; background: transparent; text-align: left; cursor: pointer; }
.materials-history-row.selected { background: #fff7f9; border-radius: 8px; }
.materials-history-row > span:first-child { min-width: 0; }
.materials-history-row strong, .materials-history-row small { display: block; overflow-wrap: anywhere; }
.materials-history-row strong { font-size: 13px; font-weight: 600; }
.materials-history-row small { margin-top: 5px; font-size: 11px; color: #8a8490; }
.materials-history-wide { grid-column: 1 / -1; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }
.materials-login { text-align: center; padding: 48px 25px; }
.materials-login h2 { font-size: 20px; margin: 18px 0 12px; }
.materials-login p { color: #77717e; font-size: 14px; }
.materials-login button { margin-top: 18px; }
button:disabled { opacity: .55; cursor: not-allowed; }
@media (max-width: 800px) { .materials-layout, .materials-history-wide { grid-template-columns: minmax(0, 1fr); } .materials-workspace { padding-top: 22px; } .materials-heading h1 { font-size: 24px; } .materials-form, .materials-result, .materials-history { padding: 20px; } }
@media (max-width: 380px) { .materials-parameters { grid-template-columns: minmax(0, 1fr); } }
</style>
