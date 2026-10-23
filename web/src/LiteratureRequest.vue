<script setup lang="ts">
import { computed, ref } from "vue";
import Icon from "./Icon.vue";
import type { CreditBalance, LiteratureJob, LiteratureServiceStatus, User } from "./api";
import { findAvailableLiterature } from "./literatureState";

const props = defineProps<{
  user: User | null;
  balance: CreditBalance | null;
  service: LiteratureServiceStatus | null;
  serviceError: string;
  canSubmit: boolean;
  busy: boolean;
  checkedInBusy: boolean;
  creditError: string;
  creditNotice: string;
  jobs: LiteratureJob[];
  now: number;
}>();
const emit = defineEmits<{ submit: [title: string]; reuse: [id: string]; login: []; tasks: []; credits: []; checkin: []; xrd: [] }>();
const title = ref("");
const reusableJob = computed(() => props.user?.verified ? findAvailableLiterature(props.jobs, title.value, props.now) : null);
const serviceLabel = computed(() => props.serviceError ? "文献服务连接中断" : !props.service ? "正在连接文献服务" : !props.service.enabled ? "文献服务暂未开放" : props.service.worker_online ? "文献求助服务可用" : "文献服务暂时不可用");
function submit() {
  if (!props.user) emit("login");
  else if (reusableJob.value) emit("reuse", reusableJob.value.id);
  else if (props.canSubmit && title.value.trim()) emit("submit", title.value.trim());
}
</script>

<template>
  <section class="content-wrap service-heading"><h1>文献求助</h1><p>输入完整文献标题，获取 PDF 全文。</p></section>
  <div class="content-wrap literature-layout">
    <section class="panel literature-request-panel">
      <div class="section-heading"><div class="title-with-icon"><span class="section-icon pink"><Icon name="search" /></span><h2>提交文献求助</h2></div><span class="soft-label">不限每日篇数</span></div>
      <form class="literature-request-form" @submit.prevent="submit">
        <label for="literature-title">文献标题</label>
        <textarea id="literature-title" v-model="title" rows="3" maxlength="2000" placeholder="粘贴完整英文或中文文献标题" required :disabled="busy" aria-describedby="literature-title-hint"></textarea>
        <p id="literature-title-hint" class="input-hint">每篇成功获取 1 积分；提交时冻结，未成功退回。</p>
        <div class="literature-submit-row"><span v-if="reusableJob">已有有效全文，可直接查看下载</span><button class="button primary" type="submit" :disabled="busy || (!!user && ((!canSubmit && !reusableJob) || !title.trim()))"><span v-if="busy" class="spinner"></span>{{ busy ? '正在提交' : reusableJob ? '查看已有 PDF' : user ? '提交文献求助' : '登录后求助' }}<Icon v-if="!busy" name="arrow" :size="17" /></button></div>
      </form>
      <div v-if="serviceError || (service && (!service.enabled || !service.worker_online))" class="inline-note warning" role="status"><Icon name="info" :size="17" /><span>{{ serviceLabel }}</span></div>
      <div v-if="user && !user.verified" class="inline-note warning"><Icon name="mail" :size="17" /><span>完成邮箱验证后即可提交文献求助。</span><button class="text-button" @click="emit('login')">前往账号</button></div>
      <div v-else-if="user && balance && balance.available < 1" class="inline-note"><Icon name="info" :size="17" /><span>积分不足，可签到领取积分；已有有效全文可免费复用。</span><button class="text-button" @click="emit('credits')">查看积分</button></div>
    </section>
    <aside class="panel literature-workbench">
      <div class="section-heading"><h2>我的积分</h2><span class="mini-dot"></span></div>
      <template v-if="user">
        <p class="workbench-email">{{ user.email }}</p>
        <div class="credit-mini-values"><div><span>可用积分</span><strong>{{ balance?.available ?? '—' }}</strong></div><div><span>冻结中</span><strong>{{ balance?.frozen ?? '—' }}</strong></div></div>
        <button class="button primary full-width" :disabled="checkedInBusy || busy || !balance || balance.checked_in" @click="emit('checkin')"><Icon name="check" :size="17" />{{ checkedInBusy ? '签到中…' : balance?.checked_in ? '今日已签到' : '每日签到 +1 积分' }}</button>
        <p class="small-muted checkin-time">每日签到按北京时间更新</p>
        <p v-if="creditError" class="module-message error-text" role="alert">{{ creditError }}</p>
        <p v-else-if="creditNotice" class="module-message success-text" role="status">{{ creditNotice }}</p>
        <div class="workbench-links"><button class="text-button" @click="emit('credits')">积分流水<Icon name="arrow" :size="14" /></button><button class="text-button" @click="emit('tasks')">我的任务<Icon name="arrow" :size="14" /></button></div>
      </template>
      <template v-else><div class="workbench-avatar"><Icon name="user" :size="27" /></div><p class="side-lead">注册赠 10 积分</p><p class="small-muted center">每日签到再获 1 积分<br />文献与 XRD 共用同一账号</p><button class="button light full-width" @click="emit('login')">登录 / 注册<Icon name="arrow" :size="16" /></button></template>
    </aside>
    <p class="service-policy">最长等待 24 小时；全文获取后保留 3 天，到期前可免费重复下载。</p>
  </div>
</template>
