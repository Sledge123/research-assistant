<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ApiError, request, type User } from "./api";
import type { AnalyticsPeriod, AnalyticsReport, VisitMetrics } from "./analytics";

const props = defineProps<{ user: User; sessionGeneration?: number }>();
const emit = defineEmits<{ unauthorized: [cause: unknown] }>();
const allowed = computed(() => props.user.is_admin && props.user.verified);
const periods: { id: AnalyticsPeriod; label: string }[] = [
  { id: "today", label: "今日" }, { id: "yesterday", label: "昨日" }, { id: "7d", label: "近7天" }, { id: "30d", label: "近30天" },
];
const metrics: { id: keyof VisitMetrics; label: string }[] = [
  { id: "pv", label: "浏览量 PV" }, { id: "uv", label: "访客数 UV" }, { id: "ip", label: "独立 IP" }, { id: "new_visitors", label: "新访客" },
];
const period = ref<AnalyticsPeriod>("7d");
const metric = ref<keyof VisitMetrics>("pv");
const report = ref<AnalyticsReport | null>(null);
const loading = ref(false), error = ref("");
const maximum = computed(() => Math.max(1, ...(report.value?.series.map(item => item[metric.value]) ?? [])));
const distributions = computed(() => report.value ? [
  { title: "浏览器", items: report.value.browsers }, { title: "操作系统", items: report.value.systems },
  { title: "设备", items: report.value.devices }, { title: "访问来源", items: report.value.referrers },
] : []);
let alive = true, generation = 0;
async function load() {
  const sequence = ++generation, owner = props.user.id, session = props.sessionGeneration;
  report.value = null; error.value = ""; loading.value = false;
  if (!allowed.value) return;
  const current = () => alive && allowed.value && sequence === generation && owner === props.user.id && session === props.sessionGeneration;
  loading.value = true;
  try {
    const value = await request<AnalyticsReport>(`/admin/analytics?period=${period.value}`);
    if (current()) report.value = value;
  } catch (cause) {
    if (current()) {
      error.value = cause instanceof Error ? cause.message : "访问统计暂时不可用，请稍后刷新。";
      if (cause instanceof ApiError && cause.status === 401) emit("unauthorized", cause);
    }
  } finally { if (current()) loading.value = false; }
}
function choose(next: AnalyticsPeriod) { if (next !== period.value) { period.value = next; void load(); } }
function number(value: number) { return value.toLocaleString("zh-CN"); }
function label(value: string) { return value.includes(" ") ? value.slice(-5) : value.slice(5); }
watch([() => props.user.id, () => props.user.is_admin, () => props.user.verified, () => props.sessionGeneration], () => {
  period.value = "7d"; metric.value = "pv"; void load();
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { alive = false; ++generation; });
</script>

<template>
  <section v-if="allowed" class="analytics" aria-label="网站访问统计">
    <div class="section-heading analytics-heading"><div><h2>访问统计</h2><p class="small-muted">北京时间 · 从功能启用时开始记录</p></div><button class="button neutral" :disabled="loading" @click="load">{{ loading ? '读取中…' : '刷新统计' }}</button></div>
    <nav class="analytics-periods" aria-label="统计时段"><button v-for="item in periods" :key="item.id" class="button neutral" :class="{ selected: period === item.id }" :aria-pressed="period === item.id" @click="choose(item.id)">{{ item.label }}</button></nav>
    <p v-if="error" class="module-message error-text" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="module-message" role="status">正在读取访问统计…</p>
    <template v-else-if="report">
      <p class="small-muted">{{ report.start_date }} 至 {{ report.end_date }}</p>
      <div class="admin-overview-grid analytics-metrics"><article v-for="item in metrics" :key="item.id" class="panel"><span>{{ item.label }}</span><strong>{{ number(report.summary[item.id]) }}</strong></article></div>
      <p v-if="!report.summary.pv" class="module-message">所选时段暂无访问记录。</p>
      <section class="panel analytics-trend" aria-label="访问趋势"><div class="section-heading"><h3>访问趋势</h3><label class="analytics-metric-select">指标<select v-model="metric" aria-label="趋势指标"><option v-for="item in metrics" :key="item.id" :value="item.id">{{ item.label }}</option></select></label></div>
        <div class="analytics-chart" role="img" :aria-label="`${metrics.find(item => item.id === metric)?.label}趋势，最高${maximum}，详细数值见趋势明细`"><div v-for="item in report.series" :key="item.label" class="analytics-column" :title="`${item.label}：${item[metric]}`"><div class="analytics-bar-space"><span class="analytics-bar" :style="{ height: `${item[metric] / maximum * 100}%` }"></span></div><span class="analytics-tick">{{ label(item.label) }}</span></div></div>
        <details class="analytics-details"><summary>查看趋势明细</summary><div class="analytics-table-wrap"><table class="analytics-table"><thead><tr><th>时间</th><th>PV</th><th>UV</th><th>IP</th><th>新访客</th></tr></thead><tbody><tr v-for="item in report.series" :key="item.label"><th scope="row">{{ item.label }}</th><td>{{ number(item.pv) }}</td><td>{{ number(item.uv) }}</td><td>{{ number(item.ip) }}</td><td>{{ number(item.new_visitors) }}</td></tr></tbody></table></div></details>
      </section>
      <section class="panel analytics-pages" aria-label="页面访问排名"><h3>页面访问</h3><div class="analytics-table-wrap"><table class="analytics-table"><thead><tr><th>页面</th><th>PV</th><th>UV</th><th>IP</th></tr></thead><tbody><tr v-for="item in report.pages" :key="item.name"><th scope="row">{{ item.label }}</th><td>{{ number(item.pv) }}</td><td>{{ number(item.uv) }}</td><td>{{ number(item.ip) }}</td></tr><tr v-if="!report.pages.length"><td colspan="4">暂无数据</td></tr></tbody></table></div></section>
      <div class="analytics-distributions"><section v-for="group in distributions" :key="group.title" class="panel"><h3>{{ group.title }}</h3><div class="analytics-table-wrap"><table class="analytics-table"><thead><tr><th>类别</th><th>PV</th><th>UV</th><th>IP</th></tr></thead><tbody><tr v-for="item in group.items" :key="item.name"><th scope="row">{{ item.name }}</th><td>{{ number(item.pv) }}</td><td>{{ number(item.uv) }}</td><td>{{ number(item.ip) }}</td></tr><tr v-if="!group.items.length"><td colspan="4">暂无数据</td></tr></tbody></table></div></section></div>
      <p class="small-muted analytics-definition">UV 按浏览器标识去重，IP 按地址去重。各时段独立去重，可能受隐私设置影响；记录最长保留 {{ report.retention_days }} 天。</p>
    </template>
  </section>
</template>

<style scoped>
.analytics { min-width: 0; }
.analytics-heading { margin-bottom: 16px; }
.analytics-heading p { margin: 5px 0 0; }
.analytics-periods { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.analytics-periods .selected { color: #2058b0; border-color: #7da2e6; background: #edf3ff; }
.analytics-metrics { margin: 14px 0 22px; }
.analytics-trend, .analytics-pages { padding: 22px; margin-bottom: 20px; }
.analytics h3 { font-size: 16px; margin: 0 0 14px; }
.analytics-trend .section-heading h3 { margin: 0; }
.analytics-metric-select { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #64748b; }
.analytics-metric-select select { padding: 7px; border: 1px solid #dbe3ee; border-radius: 7px; background: white; font: inherit; color: #334155; }
.analytics-chart { display: flex; align-items: stretch; gap: 5px; height: 210px; margin: 24px 0 16px; border-bottom: 1px solid #dbe3ee; }
.analytics-column { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.analytics-bar-space { display: flex; flex: 1; align-items: flex-end; background: linear-gradient(to top, #edf2f8 1px, transparent 1px); background-size: 100% 25%; }
.analytics-bar { display: block; width: 100%; max-width: 46px; margin: 0 auto; background: #4f80ce; border-radius: 4px 4px 0 0; }
.analytics-tick { font-size: 10px; color: #64748b; padding-top: 8px; text-align: center; height: 25px; white-space: nowrap; }
.analytics-column:not(:first-child):not(:last-child):nth-child(even) .analytics-tick { visibility: hidden; }
.analytics-details summary { cursor: pointer; color: #46658b; font-size: 13px; padding: 8px 0; }
.analytics-table-wrap { overflow-x: auto; }
.analytics-table { border-collapse: collapse; width: 100%; font-size: 13px; }
.analytics-table th, .analytics-table td { border-bottom: 1px solid #edf1f6; padding: 12px 10px; text-align: right; white-space: nowrap; font-weight: 400; }
.analytics-table th:first-child, .analytics-table td:first-child { text-align: left; white-space: normal; overflow-wrap: anywhere; }
.analytics-table thead th { color: #64748b; font-weight: 500; }
.analytics-distributions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.analytics-distributions .panel { padding: 22px; min-width: 0; }
.analytics-definition { margin-top: 20px; line-height: 1.8; }
@media (max-width: 700px) {
  .analytics-distributions { grid-template-columns: 1fr; gap: 14px; }
  .analytics-trend, .analytics-pages, .analytics-distributions .panel { padding: 16px; }
  .analytics-chart { gap: 3px; height: 180px; }
  .analytics-column:not(:first-child):not(:last-child) .analytics-tick { visibility: hidden; }
  .analytics-table th, .analytics-table td { padding: 10px 6px; }
}
</style>
