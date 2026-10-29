<script setup lang="ts">
import Icon from "./Icon.vue";
import type { CreditBalance, CreditSummary, User } from "./api";
import { formatLiteratureDate } from "./literatureState";

defineProps<{ user: User | null; balance: CreditBalance | null; summary: CreditSummary | null; loading: boolean; busy: boolean; actionBusy: boolean; error: string; notice: string }>();
const emit = defineEmits<{ login: []; checkin: []; refresh: []; job: [id: string, kind?: "xrd" | "literature" | "abtem" | "pymatgen" | null]; request: [] }>();
function signed(value: number) { return value > 0 ? `+${value}` : String(value); }
</script>

<template>
  <div class="page-heading"><div><span class="tiny-kicker">站内积分与签到</span><h1>我的积分<span class="heading-dot">.</span></h1><p>注册赠 10 积分，每日签到赠 1 积分；签到按北京时间更新。</p></div><button v-if="user" class="button neutral" :disabled="loading || busy || actionBusy" @click="emit('refresh')"><Icon name="refresh" :size="17" />{{ loading ? '刷新中' : '刷新积分' }}</button></div>
  <div v-if="!user" class="panel empty-state"><div class="empty-icon"><Icon name="user" :size="35" /></div><h2>登录后查看积分与流水</h2><p>文献求助、XRD 归档、电镜模拟和材料工具共用一套积分。</p><button class="button primary" @click="emit('login')">登录 / 注册<Icon name="arrow" :size="16" /></button></div>
  <template v-else>
    <div v-if="error || notice" class="module-banner" :class="error ? 'notice-error' : 'notice-success'" :role="error ? 'alert' : 'status'"><Icon :name="error ? 'info' : 'check'" :size="18" />{{ error || notice }}</div>
    <div class="credits-overview">
      <article class="panel credit-balance-card"><span>可用积分</span><strong>{{ balance?.available ?? '—' }}<small>分</small></strong><p>全站功能共用</p></article>
      <article class="panel credit-balance-card frozen"><span>冻结积分</span><strong>{{ balance?.frozen ?? '—' }}<small>分</small></strong><p>任务处理中暂时冻结</p></article>
      <article class="panel checkin-card"><div><span class="section-icon pink"><Icon name="check" /></span><h2>每日签到</h2></div><p>{{ balance?.checked_in ? '今天已完成签到，明天再来。' : '今天签到，领取 1 积分。' }}</p><button class="button primary" :disabled="busy || actionBusy || !balance || balance.checked_in" @click="emit('checkin')">{{ busy ? '签到中…' : balance?.checked_in ? '今日已签到' : '签到领取 +1 积分' }}</button></article>
    </div>
    <div class="credits-rule-note"><Icon name="info" :size="18" /><p>文献每篇、XRD 每张成功卡片各 1 积分；电镜模拟两项结果全部成功共 1 积分。材料工具中，XRD衍射峰生成每次成功 2 积分，其余每次成功 1 积分。提交时冻结，未成功退回。检索与有效期内重复下载免费。文献和模拟结果保留 3 天，过期重新提交按新任务计费。</p><button class="text-button" @click="emit('request')">去求助<Icon name="arrow" :size="15" /></button></div>
    <section class="panel credits-ledger"><div class="section-heading"><h2>积分流水</h2><span class="small-muted">{{ summary ? `${summary.items.length} 条记录` : '正在读取记录' }}</span></div>
      <div v-if="!summary" class="empty-state compact"><Icon name="history" :size="30" /><h3>{{ loading ? '正在读取积分流水' : '积分流水暂未读取成功' }}</h3><p>{{ error || '请刷新重试。' }}</p></div>
      <div v-else-if="!summary.items.length" class="empty-state compact"><Icon name="history" :size="30" /><h3>暂无积分流水</h3><p>积分变动会在这里留下记录。</p></div>
      <div v-else class="ledger-table-wrap" tabindex="0" role="region" aria-label="积分流水表，可横向滚动"><table class="ledger-table"><thead><tr><th scope="col">变动事项</th><th scope="col">可用变动</th><th scope="col">冻结变动</th><th scope="col">时间（北京）</th><th scope="col">相关任务</th></tr></thead><tbody><tr v-for="item in summary.items" :key="item.id"><td>{{ item.label }}</td><td :class="{ 'success-text': item.amount > 0, 'error-text': item.amount < 0 }">{{ signed(item.amount) }}</td><td>{{ signed(item.frozen_amount) }}</td><td>{{ formatLiteratureDate(item.created_at) }}</td><td><button v-if="item.job_id" class="text-button" @click="emit('job', item.job_id, item.job_kind)">查看任务<Icon name="arrow" :size="14" /></button><span v-else class="muted">—</span></td></tr></tbody></table></div>
    </section>
  </template>
</template>
