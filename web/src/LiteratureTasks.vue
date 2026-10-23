<script setup lang="ts">
import { computed, ref } from "vue";
import Icon from "./Icon.vue";
import { API_BASE, type LiteratureJob } from "./api";
import { canDownloadLiterature, creditStateLabel, formatLiteratureDate, isLiteratureExpired, isLiteraturePending, literatureStateLabel } from "./literatureState";

const props = defineProps<{ jobs: LiteratureJob[]; selected: LiteratureJob | null; busy: boolean; loading: boolean; error: string; actionError: string; notice: string; now: number; canSubmit: boolean }>();
const emit = defineEmits<{ selectJob: [id: string]; cancel: [job: LiteratureJob]; candidate: [job: LiteratureJob, id: string]; renew: [job: LiteratureJob]; request: []; refresh: [] }>();
const filter = ref("all");
const visibleJobs = computed(() => props.jobs.filter((job) => filter.value === "all" || (filter.value === "active" ? isLiteraturePending(job.state) : !isLiteraturePending(job.state))));
function displayState(job: LiteratureJob) {
  if (isLiteratureExpired(job, props.now)) return "全文已过期";
  if (job.state === "succeeded" && job.stage === "ready") return canDownloadLiterature(job, props.now) ? "可下载" : "文件不可用";
  return literatureStateLabel(job.state, job.stage);
}
</script>

<template>
  <div v-if="actionError || notice" class="module-banner" :class="actionError ? 'notice-error' : 'notice-success'" :role="actionError ? 'alert' : 'status'"><Icon :name="actionError ? 'info' : 'check'" :size="18" />{{ actionError || notice }}</div>
  <div v-if="error" class="module-banner notice-error" role="alert"><Icon name="info" :size="18" /><span>{{ error }}</span><button class="text-button" :disabled="loading" @click="emit('refresh')">重试读取</button></div>
  <div class="task-layout literature-tasks">
    <section class="panel tasks-list">
      <div class="list-tabs" aria-label="文献任务筛选"><button v-for="item in [{ id:'all', label:'全部文献' },{ id:'active', label:'处理中' },{ id:'done', label:'已结束' }]" :key="item.id" :class="{ active: filter === item.id }" :aria-pressed="filter === item.id" @click="filter = item.id">{{ item.label }}</button><span>{{ visibleJobs.length }} 个任务</span></div>
      <div v-if="!visibleJobs.length" class="empty-state compact"><Icon name="file" :size="36" /><h3>{{ loading ? '正在读取文献任务' : '这里还没有文献任务' }}</h3><p>{{ loading ? '请稍候。' : '输入文献标题，开始一次全文求助。' }}</p><button v-if="!loading" class="text-button" @click="emit('request')">去求助<Icon name="arrow" :size="15" /></button></div>
      <button v-for="job in visibleJobs" :key="job.id" class="task-row literature-task-row" :class="{ selected: selected?.id === job.id }" :aria-pressed="selected?.id === job.id" @click="emit('selectJob', job.id)"><span class="task-row-icon"><Icon name="file" /></span><span class="task-row-copy"><strong>{{ job.canonical_title || job.title }}</strong><span>{{ formatLiteratureDate(job.created_at) }} · {{ creditStateLabel(job.credit_status) }}</span></span><span class="status-pill" :class="isLiteratureExpired(job, now) ? 'cancelled' : job.state">{{ displayState(job) }}</span></button>
    </section>
    <section class="panel task-detail literature-detail" aria-label="文献任务详情">
      <template v-if="selected">
        <div class="section-heading"><h2>文献任务详情</h2><span class="status-pill" :class="isLiteratureExpired(selected, now) ? 'cancelled' : selected.state">{{ displayState(selected) }}</span></div>
        <p class="detail-id">#{{ selected.id }}</p>
        <h3 class="literature-detail-title">{{ selected.canonical_title || selected.title }}</h3>
        <p v-if="selected.canonical_title && selected.canonical_title !== selected.title" class="small-muted original-title">提交标题：{{ selected.title }}</p>
        <div v-if="selected.doi" class="detail-row"><span>DOI</span><strong class="doi-value">{{ selected.doi }}</strong></div>
        <div class="detail-row"><span>创建时间</span><strong>{{ formatLiteratureDate(selected.created_at) }}</strong></div>
        <div class="detail-row"><span>积分状态</span><strong>{{ creditStateLabel(selected.credit_status) }}</strong></div>
        <div v-if="isLiteraturePending(selected.state)" class="detail-row"><span>等待截止</span><strong>{{ formatLiteratureDate(selected.deadline) }}</strong></div>
        <div v-if="selected.last_checked_at" class="detail-row"><span>最近检查</span><strong>{{ formatLiteratureDate(selected.last_checked_at) }}</strong></div>
        <div v-if="isLiteraturePending(selected.state) && selected.next_check_at" class="detail-row"><span>下次检查</span><strong>{{ formatLiteratureDate(selected.next_check_at) }}</strong></div>
        <div v-if="selected.ready_at" class="detail-row"><span>获取时间</span><strong>{{ formatLiteratureDate(selected.ready_at) }}</strong></div>
        <div v-if="selected.download_expires_at" class="detail-row" :class="{ 'error-text': isLiteratureExpired(selected, now) }"><span>全文到期</span><strong>{{ formatLiteratureDate(selected.download_expires_at) }}</strong></div>
        <p class="small-muted detail-timezone">以上时间均为北京时间</p>
        <div v-if="selected.message" class="detail-message">{{ selected.message }}</div>
        <div v-if="selected.state === 'needs_selection'" class="literature-candidates">
          <h3>请选择要获取的文献</h3><p class="small-muted">请核对标题、期刊与 DOI。</p>
          <p v-if="!selected.candidates.length" class="module-message error-text" role="status">当前任务尚未返回可选候选，请刷新任务状态。</p>
          <article v-for="candidate in selected.candidates" :key="candidate.id" class="literature-candidate"><strong>{{ candidate.title }}</strong><span v-if="candidate.journal || candidate.year">{{ [candidate.journal, candidate.year].filter(Boolean).join(' · ') }}</span><small v-if="candidate.doi">DOI：{{ candidate.doi }}</small><button class="button light" :disabled="busy" @click="emit('candidate', selected, candidate.id)">选择此文献<Icon name="check" :size="15" /></button></article>
        </div>
        <div v-if="isLiteratureExpired(selected, now)" class="inline-note warning"><Icon name="info" :size="17" /><span>全文已过期。可重新提交求助，获取成功后再扣 1 积分。</span></div>
        <div v-else-if="canDownloadLiterature(selected, now)" class="inline-note"><Icon name="download" :size="17" /><span>PDF 有效期内可重复下载，不额外扣分。</span></div>
        <div class="detail-actions"><a v-if="canDownloadLiterature(selected, now)" class="button primary" :href="`${API_BASE}/literature/jobs/${encodeURIComponent(selected.id)}/download`"><Icon name="download" :size="17" />下载 PDF</a><button v-if="isLiteraturePending(selected.state)" class="button neutral" :disabled="busy" @click="emit('cancel', selected)">取消求助</button><button v-if="isLiteratureExpired(selected, now) || ['failed', 'cancelled'].includes(selected.state)" class="button light" :disabled="busy || !canSubmit" @click="emit('renew', selected)"><Icon name="refresh" :size="16" />{{ isLiteratureExpired(selected, now) ? '重新获取 · 冻结 1 积分' : '重新求助 · 冻结 1 积分' }}</button></div>
      </template>
      <div v-else class="empty-state compact"><Icon name="file" :size="35" /><h3>查看文献进度</h3><p>选择一项文献任务，查看候选、积分状态与 PDF 下载。</p></div>
    </section>
  </div>
</template>
