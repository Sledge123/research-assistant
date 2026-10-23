<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import Icon from "./Icon.vue";
import { API_BASE, type AbtemArchiveSource, type AbtemJob, type AbtemParameters, type AbtemServiceStatus, type AbtemSource, type User } from "./api";
import { abtemCreditLabel, abtemStateLabel, canDownloadAbtem, isAbtemPending, MAX_CIF_BYTES, parameterError, readCifUpload } from "./abtemState";
import { formatLiteratureDate } from "./literatureState";

const props = defineProps<{
  user: User | null; service: AbtemServiceStatus | null; serviceError: string;
  jobs: AbtemJob[]; selected: AbtemJob | null; jobsLoading: boolean; jobsError: string; jobsNextOffset: number | null;
  sources: AbtemArchiveSource[]; sourcesLoading: boolean; sourcesError: string; sourcesLoaded: boolean; sourcesNextOffset: number | null;
  busy: boolean; canSubmit: boolean; actionError: string; notice: string; pendingSubmission: boolean; now: number;
}>();
const emit = defineEmits<{
  login: []; submit: [source: AbtemSource, parameters: AbtemParameters]; cancel: [job: AbtemJob]; selectJob: [id: string];
  refresh: []; sources: [more: boolean]; moreJobs: []; reconcile: []; xrd: [];
}>();
const sourceType = ref<"upload" | "archive">("upload");
const upload = ref<(AbtemSource & { type: "upload" }) | null>(null);
const selectedSource = ref("");
const fileError = ref("");
const formError = ref("");
const fileReading = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const imageErrors = ref<Record<string, boolean>>({});
const downloadBusy = ref(false);
const downloadError = ref("");
const parameters = reactive<AbtemParameters>({ energy_kev: 200, zone_axis: [0, 0, 1], thickness_nm: 2, defocus_nm: 10, cs_mm: 1, aperture_mrad: 20 });
let defaultsApplied = false;
let fileSequence = 0;
let alive = true;
const sourceKey = (item: AbtemArchiveSource) => JSON.stringify([item.job_id, item.code]);
const archive = computed(() => props.sources.find((item) => sourceKey(item) === selectedSource.value));
const canDownload = computed(() => !!props.selected && canDownloadAbtem(props.selected, props.now));
const progress = computed(() => Math.min(100, Math.max(0, Math.round(props.selected?.progress ?? 0))));
const fileLimit = computed(() => Math.min(MAX_CIF_BYTES, props.service?.limits.max_cif_bytes ?? MAX_CIF_BYTES));
const formDisabled = computed(() => props.busy || props.pendingSubmission || !props.user?.verified);
const serviceMessage = computed(() => props.serviceError || (!props.service ? "" : !props.service.enabled || !props.service.worker_online ? props.service.message || "电镜模拟暂时不可用，已提交任务将保留。" : ""));
const hasSource = computed(() => sourceType.value === "upload" ? !!upload.value : !!archive.value);
const submitDisabled = computed(() => !!props.user && (!props.canSubmit || !hasSource.value || fileReading.value));
const imageUrl = (kind: "saed" | "hrtem") => `${API_BASE}/abtem/jobs/${encodeURIComponent(props.selected!.id)}/image/${kind}`;
function setDefaults() {
  if (!props.service || defaultsApplied) return;
  Object.assign(parameters, props.service.defaults, { zone_axis: [...props.service.defaults.zone_axis] });
  defaultsApplied = true;
}
watch(() => props.service, setDefaults, { immediate: true });
watch(() => `${props.user?.id ?? ""}:${props.user?.csrf_token ?? ""}`, () => {
  ++fileSequence; upload.value = null; selectedSource.value = ""; fileError.value = ""; formError.value = ""; fileReading.value = false;
  sourceType.value = "upload"; defaultsApplied = false; imageErrors.value = {};
  downloadBusy.value = false; downloadError.value = "";
  if (fileInput.value) fileInput.value.value = "";
  setDefaults();
});
watch(() => props.selected?.id, () => { imageErrors.value = {}; downloadBusy.value = false; downloadError.value = ""; });
async function chooseFile(event: Event) {
  const sequence = ++fileSequence, account = props.user?.id;
  const file = (event.target as HTMLInputElement).files?.[0];
  upload.value = null; fileError.value = ""; formError.value = "";
  if (!file) return;
  fileReading.value = true;
  try {
    const source = await readCifUpload(file, fileLimit.value);
    if (alive && sequence === fileSequence && account === props.user?.id) upload.value = source;
  } catch (cause) {
    if (alive && sequence === fileSequence && account === props.user?.id) fileError.value = cause instanceof Error ? cause.message : "文件读取失败，请重新选择。";
  } finally { if (alive && sequence === fileSequence && account === props.user?.id) fileReading.value = false; }
}
function changeSource(type: "upload" | "archive") {
  sourceType.value = type; formError.value = "";
  if (type === "archive" && !props.sourcesLoading) emit("sources", false);
}
function refresh() {
  imageErrors.value = {}; downloadError.value = ""; emit("refresh");
  if (sourceType.value === "archive" && !props.sourcesLoading) emit("sources", false);
}
async function downloadResult() {
  if (!canDownload.value || !props.selected || !props.user?.verified || downloadBusy.value) return;
  const jobId = props.selected.id, accountId = props.user.id, token = props.user.csrf_token;
  const current = () => alive && props.selected?.id === jobId && props.user?.id === accountId && props.user.csrf_token === token;
  downloadBusy.value = true; downloadError.value = "";
  try {
    const response = await fetch(`${API_BASE}/abtem/jobs/${encodeURIComponent(jobId)}/download`, { credentials: "same-origin" });
    if (!current()) return;
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: unknown } | null;
      if (!current()) return;
      downloadError.value = typeof body?.detail === "string" ? body.detail : "模拟结果下载未完成，请刷新后重试。";
      if ([401, 403, 404, 410].includes(response.status)) emit("refresh");
      return;
    }
    if (!response.headers.get("Content-Type")?.includes("application/zip")) throw new Error("invalid-download");
    const blob = await response.blob();
    if (!current()) return;
    if (!blob.size) throw new Error("empty-download");
    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url; link.download = `abtem-${jobId}.zip`;
      document.body.appendChild(link); link.click(); link.remove();
    } finally { URL.revokeObjectURL(url); }
  } catch {
    if (current()) downloadError.value = "模拟结果下载未完成，请检查网络后重试。";
  } finally { if (current()) downloadBusy.value = false; }
}
function submit() {
  if (!props.user) { emit("login"); return; }
  if (!props.canSubmit || !props.service || fileReading.value) return;
  formError.value = parameterError(parameters, props.service.limits);
  if (formError.value) return;
  const source: AbtemSource | null = sourceType.value === "upload" ? upload.value : archive.value ? { type: "archive", job_id: archive.value.job_id, code: archive.value.code } : null;
  if (!source) { formError.value = "请上传 CIF 或选择一项已生成的结构。"; return; }
  emit("submit", source, { ...parameters, zone_axis: [...parameters.zone_axis] });
}
onBeforeUnmount(() => { alive = false; ++fileSequence; });
</script>

<template>
  <section class="content-wrap service-heading"><h1>电镜模拟</h1><p>由 CIF 结构计算选区电子衍射（SAED）与高分辨透射电镜（HRTEM）。</p></section>
  <div class="content-wrap abtem-workspace">
    <section class="panel abtem-input">
      <div class="section-heading"><h2>结构与参数</h2></div>
      <p class="abtem-note">仅支持有序、满占有结构；部分占有与无序结构暂不支持。采用静态独立原子模型（IAM）。</p>
      <div v-if="serviceMessage && user?.verified" class="module-banner notice-error" role="alert"><Icon name="info" :size="18" /><span>{{ serviceMessage }}</span><button class="text-button" :disabled="busy" @click="refresh">重试</button></div>
      <div v-if="user && !user.verified" class="module-banner" role="status"><Icon name="info" :size="18" /><span>请先完成邮箱验证。</span></div>
      <form @submit.prevent="submit">
        <div class="content-types abtem-source-tabs" role="group" aria-label="结构来源"><button type="button" :disabled="formDisabled" :aria-pressed="sourceType === 'upload'" :class="{ active: sourceType === 'upload' }" @click="changeSource('upload')">上传 CIF</button><button type="button" :disabled="formDisabled" :aria-pressed="sourceType === 'archive'" :class="{ active: sourceType === 'archive' }" @click="changeSource('archive')">我的结构 CIF</button></div>
        <div v-if="sourceType === 'upload'" class="abtem-upload">
          <label for="abtem-cif">CIF 结构文件</label><input id="abtem-cif" ref="fileInput" type="file" accept=".cif" :disabled="formDisabled || fileReading" @change="chooseFile" />
          <p class="input-hint">最大 {{ Math.floor(fileLimit / 1024) }} KiB，结构和计算规模由服务器最终核对。</p><p v-if="upload" class="abtem-filename">已选择：{{ upload.filename }}</p><p v-if="fileReading" class="small-muted" role="status">正在读取结构文件…</p><p v-if="fileError" class="module-message error-text" role="alert">{{ fileError }}</p>
        </div>
        <div v-else class="abtem-sources">
          <p class="input-hint">复用本人在本站成功生成的结构 CIF。</p>
          <div v-if="sourcesError" class="module-message error-text" role="alert">{{ sourcesError }} <button class="text-button" :disabled="sourcesLoading" type="button" @click="emit('sources', sourcesLoaded)">重新读取</button></div>
          <p v-else-if="!sources.length && sourcesLoading" role="status" class="small-muted">正在读取结构…</p>
          <p v-else-if="!sources.length && sourcesLoaded" class="small-muted">暂无可复用的结构。<button class="text-button" type="button" @click="emit('xrd')">查询晶体结构<Icon name="arrow" :size="14" /></button></p>
          <label v-for="item in sources" :key="sourceKey(item)" class="abtem-source-item"><input v-model="selectedSource" type="radio" name="abtem-source" :value="sourceKey(item)" :disabled="formDisabled" /><span><strong>{{ item.code }} · {{ item.phase_name || '晶体结构' }}</strong><small>{{ item.source }}</small></span></label>
          <button v-if="sourcesNextOffset !== null" type="button" class="text-button abtem-more" :disabled="sourcesLoading || formDisabled" @click="emit('sources', true)">{{ sourcesLoading ? '读取中…' : '加载更多结构' }}</button>
        </div>
        <fieldset class="abtem-parameters abtem-basic-parameters" :disabled="formDisabled || !service"><legend>基本参数</legend>
          <div class="abtem-field"><label for="abtem-voltage">加速电压 <span>kV</span></label><input id="abtem-voltage" v-model.number="parameters.energy_kev" type="number" step="any" :min="service?.limits.energy_kev[0]" :max="service?.limits.energy_kev[1]" required /></div>
          <div class="abtem-field abtem-axis-field"><label>晶带轴 [uvw]</label><div class="abtem-zone-axis"><label v-for="(label, index) in ['u', 'v', 'w']" :key="label"><span>{{ label }}</span><input v-model.number="parameters.zone_axis[index]" :aria-label="`晶带轴 ${label}`" type="number" step="1" :min="service?.limits.zone_axis_component[0]" :max="service?.limits.zone_axis_component[1]" required /></label></div></div>
          <div class="abtem-field"><label for="abtem-thickness">样品厚度 <span>nm</span></label><input id="abtem-thickness" v-model.number="parameters.thickness_nm" type="number" step="any" :min="service?.limits.thickness_nm[0]" :max="service?.limits.thickness_nm[1]" required /></div>
          <div class="abtem-field"><label for="abtem-defocus">离焦量 <span>nm</span></label><input id="abtem-defocus" v-model.number="parameters.defocus_nm" type="number" step="any" :min="service?.limits.defocus_nm[0]" :max="service?.limits.defocus_nm[1]" required /></div>
        </fieldset>
        <details class="abtem-advanced"><summary>更多光学参数</summary><fieldset class="abtem-parameters" :disabled="formDisabled || !service"><legend class="sr-only">光学参数</legend><div class="abtem-field"><label for="abtem-cs">球差 <span>mm</span></label><input id="abtem-cs" v-model.number="parameters.cs_mm" type="number" step="any" :min="service?.limits.cs_mm[0]" :max="service?.limits.cs_mm[1]" required /></div><div class="abtem-field"><label for="abtem-aperture">孔径半角 <span>mrad</span></label><input id="abtem-aperture" v-model.number="parameters.aperture_mrad" type="number" step="any" :min="service?.limits.aperture_mrad[0]" :max="service?.limits.aperture_mrad[1]" required /></div></fieldset></details>
        <p v-if="formError" class="module-message error-text" role="alert">{{ formError }}</p>
        <div class="abtem-submit-row"><p>提交冻结 1 积分；两项结果全部成功并核对通过后共扣 1 分，失败或取消退回。</p><button class="button primary" type="submit" :disabled="submitDisabled">{{ busy ? '正在处理…' : !user ? '登录后模拟' : '开始模拟' }}<Icon v-if="!busy" name="arrow" :size="17" /></button></div>
      </form>
    </section>
    <section class="panel abtem-result">
      <div class="section-heading"><h2>模拟结果</h2><button class="text-button" :disabled="jobsLoading || busy" @click="refresh"><Icon name="refresh" :size="16" />刷新</button></div>
      <div v-if="actionError" class="module-banner notice-error" role="alert"><Icon name="info" :size="17" /><span>{{ actionError }}</span></div><p v-if="notice" class="abtem-note" role="status">{{ notice }}</p>
      <div v-if="downloadError" class="module-banner notice-error" role="alert"><Icon name="info" :size="17" /><span>{{ downloadError }}</span></div>
      <div v-if="pendingSubmission" class="abtem-pending" role="status"><p>原请求的提交结果待确认。核对会使用同一请求编号，不会创建重复扣分。</p><button class="button neutral" :disabled="busy" @click="emit('reconcile')">{{ busy ? '正在核对…' : '核对提交结果' }}</button></div>
      <template v-if="selected">
        <div class="abtem-result-title"><strong>{{ selected.input_name }}</strong><span class="status-pill" :class="selected.state">{{ selected.cancel_requested && isAbtemPending(selected.state) ? '取消处理中' : abtemStateLabel(selected.state) }}</span></div>
        <p class="abtem-note">{{ abtemCreditLabel(selected.credits) }}</p><p v-if="selected.message" class="detail-message" role="status">{{ selected.message }}</p>
        <div v-if="isAbtemPending(selected.state)" class="abtem-progress"><label :for="`abtem-progress-${selected.id}`">{{ selected.cancel_requested ? '等待计算停止' : selected.state === 'queued' ? '排队等待计算' : '正在计算 SAED 与 HRTEM' }}<span>{{ progress }}%</span></label><progress :id="`abtem-progress-${selected.id}`" :value="progress" max="100"></progress><p v-if="selected.state === 'queued'" class="input-hint">按提交顺序依次计算，关闭页面后任务继续。</p><button class="button neutral" :disabled="busy || selected.cancel_requested" @click="emit('cancel', selected)">{{ selected.cancel_requested ? '正在取消…' : '取消任务' }}</button></div>
        <template v-if="canDownload">
          <div class="abtem-figures"><figure v-for="kind in (['saed', 'hrtem'] as const)" :key="`${selected.id}-${kind}`"><figcaption>{{ kind === 'saed' ? '选区电子衍射 · SAED' : '高分辨透射电镜 · HRTEM' }}</figcaption><img v-if="!imageErrors[kind]" :src="imageUrl(kind)" :alt="kind === 'saed' ? '本任务计算所得 SAED 电子衍射图' : '本任务计算所得 HRTEM 高分辨像'" @error="imageErrors[kind] = true" /><p v-else class="abtem-image-error">预览读取未完成，请刷新后重试。</p></figure></div>
          <button type="button" class="button primary" :disabled="downloadBusy" @click="downloadResult"><Icon name="download" :size="17" />{{ downloadBusy ? '正在下载…' : '下载完整结果' }}</button><p class="input-hint">PNG / TIFF / NPY / CSV / JSON 与 CIF；有效期内重复下载免费。</p><p class="abtem-note">保留至 {{ formatLiteratureDate(selected.download_expires_at) }}（北京时间）</p>
        </template>
        <p v-else-if="selected.state === 'succeeded'" class="abtem-note">{{ selected.file_expired || (selected.download_expires_at && Date.parse(selected.download_expires_at) <= now) ? '结果已到期，文件已停止下载。参数与任务记录继续保留。' : '结果下载暂不可用，请刷新任务状态。' }}</p>
        <dl class="abtem-result-parameters"><div><dt>电压</dt><dd>{{ selected.parameters.energy_kev }} kV</dd></div><div><dt>晶带轴</dt><dd>[{{ selected.parameters.zone_axis.join(' ') }}]</dd></div><div><dt>厚度</dt><dd>{{ selected.parameters.thickness_nm }} nm</dd></div><div><dt>离焦</dt><dd>{{ selected.parameters.defocus_nm }} nm</dd></div><div><dt>球差</dt><dd>{{ selected.parameters.cs_mm }} mm</dd></div><div><dt>孔径半角</dt><dd>{{ selected.parameters.aperture_mrad }} mrad</dd></div></dl>
      </template>
      <div v-else class="empty-state compact"><Icon name="grid" :size="34" /><h3>{{ user ? jobsLoading ? '正在读取任务' : '暂无选中的模拟任务' : '登录后开始模拟' }}</h3><p>每次任务同时生成 SAED 与 HRTEM，结果保留 3 天。</p><button v-if="!user" class="button primary" @click="emit('login')">登录 / 注册</button></div>
    </section>
    <section class="panel abtem-history"><div class="section-heading"><h2>我的模拟任务</h2><span class="small-muted">{{ jobs.length }} 条记录</span></div><p v-if="jobsError" class="module-message error-text" role="alert">{{ jobsError }}</p><p v-if="!jobs.length" class="abtem-note">{{ jobsLoading ? '正在读取任务…' : '尚无模拟记录。' }}</p><button v-for="job in jobs" :key="job.id" class="abtem-history-row" :class="{ selected: selected?.id === job.id }" :disabled="busy" @click="emit('selectJob', job.id)"><span><strong>{{ job.input_name }}</strong><small>{{ formatLiteratureDate(job.created_at) }} · {{ abtemCreditLabel(job.credits) }}</small></span><span class="status-pill" :class="job.state">{{ job.cancel_requested && isAbtemPending(job.state) ? '取消处理中' : abtemStateLabel(job.state) }}</span></button><button v-if="jobsNextOffset !== null" class="text-button abtem-more" :disabled="jobsLoading || busy" @click="emit('moreJobs')">{{ jobsLoading ? '读取中…' : '加载更早任务' }}</button></section>
  </div>
</template>
