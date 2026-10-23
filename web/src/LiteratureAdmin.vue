<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import Icon from "./Icon.vue";
import { ApiError, request, type LiteratureAdminStatus, type User } from "./api";

const props = defineProps<{ user: User }>();
const emit = defineEmits<{ unauthorized: [cause: unknown]; saved: [] }>();
const status = ref<LiteratureAdminStatus | null>(null);
const enabled = ref(false);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const notice = ref("");
let generation = 0;
let requestVersion = 0;
let alive = true;
function context() {
  const id = props.user.id, version = generation;
  return () => alive && generation === version && props.user.id === id && props.user.is_admin && props.user.verified;
}
function failure(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : "操作未完成，请稍后重试。";
  if (cause instanceof ApiError && cause.status === 401) emit("unauthorized", cause);
}
async function load() {
  if (!alive || !props.user.is_admin || !props.user.verified) return;
  const current = context(), version = ++requestVersion;
  loading.value = true;
  try {
    const value = await request<LiteratureAdminStatus>("/admin/literature");
    if (!current() || version !== requestVersion) return;
    status.value = value;
    enabled.value = value.enabled;
    error.value = "";
  } catch (cause) {
    if (current() && version === requestVersion) failure(cause);
  } finally {
    if (current() && version === requestVersion) loading.value = false;
  }
}
async function save() {
  if (!alive || !props.user.is_admin || !props.user.verified || !status.value || saving.value || loading.value) return;
  const current = context(); ++requestVersion;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    await request("/admin/literature", { method: "PATCH", body: JSON.stringify({ enabled: enabled.value }) }, props.user.csrf_token);
    if (!current()) return;
    await load();
    if (!current()) return;
    notice.value = "文献服务设置已保存。";
    emit("saved");
  } catch (cause) {
    if (current()) failure(cause);
  } finally {
    if (current()) saving.value = false;
  }
}
watch([() => props.user.id, () => props.user.is_admin, () => props.user.verified], () => {
  ++generation; ++requestVersion; status.value = null; enabled.value = false;
  loading.value = false; saving.value = false; error.value = ""; notice.value = ""; void load();
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { alive = false; ++generation; });
</script>

<template>
  <section v-if="user.is_admin && user.verified" class="literature-admin-section">
    <div class="section-heading admin-section-heading"><div><h2>文献求助服务</h2><p class="small-muted">独立管理文献队列与服务开放状态。</p></div><button class="button neutral" :disabled="loading || saving" @click="load"><Icon name="refresh" :size="16" />{{ loading ? '刷新中' : '刷新文献状态' }}</button></div>
    <div v-if="error || notice" class="module-banner" :class="error ? 'notice-error' : 'notice-success'" :role="error ? 'alert' : 'status'"><Icon :name="error ? 'info' : 'check'" :size="18" />{{ error || notice }}</div>
    <div class="admin-metrics literature-admin-metrics"><article class="panel"><span>文献执行器</span><strong :class="status?.worker_online ? 'success-text' : 'muted'">{{ !status ? '—' : status.worker_online ? '在线' : '离线' }}</strong></article><article class="panel"><span>进行中 / 排队</span><strong>{{ status?.active_jobs ?? '—' }} / {{ status?.queue_length ?? '—' }}</strong></article><article class="panel"><span>待处理任务</span><strong>{{ status?.needs_attention ?? '—' }}</strong></article></div>
    <form class="panel admin-settings" @submit.prevent="save"><div class="section-heading"><h2>文献服务开关</h2><span class="soft-label">管理员设置</span></div><div class="detail-row"><span>服务账号就绪</span><strong>{{ !status ? '—' : status.accounts_ready ? '已就绪' : '未就绪' }}</strong></div><p v-if="status?.message" class="module-message">{{ status.message }}</p><label class="setting-toggle"><span><strong>开放文献求助</strong><small>文献成功后提供站内 PDF 下载，按站内积分规则结算。</small></span><input v-model="enabled" type="checkbox" :disabled="!status || loading || saving" /></label><button class="button primary" type="submit" :disabled="!status || loading || saving">{{ saving ? '保存中…' : '保存文献设置' }}<Icon name="check" :size="17" /></button></form>
  </section>
</template>
