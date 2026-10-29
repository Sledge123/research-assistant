<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import AnalyticsPanel from "./AnalyticsPanel.vue";
import { ApiError, request, requestKey, type AdminAccountType, type AdminActivity, type AdminOverview, type AdminPage, type AdminUser, type AdminUserDetail, type Announcement, type User } from "./api";

const props = defineProps<{ user: User; sessionGeneration?: number }>();
const emit = defineEmits<{ unauthorized: [cause: unknown]; announcementsChanged: []; runtime: [] }>();
const tabs = [
  { id: "overview", label: "概览" }, { id: "users", label: "用户信息" },
  { id: "credits", label: "积分调整" }, { id: "announcements", label: "网站公告" },
  { id: "analytics", label: "访问统计" }, { id: "settings", label: "运行设置" },
] as const;
type Tab = typeof tabs[number]["id"];
const tab = ref<Tab>("overview");
const allowed = computed(() => props.user.is_admin && props.user.verified);
const overview = ref<AdminOverview | null>(null);
const error = ref("");
const notice = ref("");
const loading = ref(false);
const activityPage = ref<AdminPage<AdminActivity> | null>(null);
const activityOffset = ref(0);
const activityLoading = ref(false);
const visibleActivity = computed(() => activityPage.value?.items ?? overview.value?.recent_activity ?? []);
const userQuery = ref("");
const appliedUserQuery = ref("");
const userAccountType = ref<AdminAccountType>("regular");
const userAccountTypes = [{ value: "regular", label: "正式账号" }, { value: "debug", label: "调试账号" }, { value: "all", label: "全部" }] as const;
const userPage = ref<AdminPage<AdminUser> | null>(null);
const userOffset = ref(0);
const usersLoading = ref(false);
const selectedUserId = ref<string | null>(null);
const userDetail = ref<AdminUserDetail | null>(null);
const detailLoading = ref(false);
interface CreditIntent {
  owner: string;
  targetId: string;
  targetEmail: string;
  amount: number;
  reason: string;
  request_key: string;
  phase: "confirm" | "unknown" | "rejected";
}
const creditAmount = ref("");
const creditReason = ref("");
const creditIntent = ref<CreditIntent | null>(null);
const adjusting = ref(false);
const storageBlocked = ref(false);
const announcementPage = ref<AdminPage<Announcement> | null>(null);
const announcementOffset = ref(0);
const announcementsLoading = ref(false);
const selectedAnnouncement = ref<Announcement | null>(null);
const announcementEditor = ref(false);
const announcementTitle = ref("");
const announcementBody = ref("");
const announcementSaving = ref(false);
const announcementConflict = ref(false);
interface AnnouncementIntent {
  owner: string;
  operation: "create" | "update";
  title: string;
  body: string;
  request_key?: string;
  id?: string;
  status?: "draft" | "published";
  revision?: number;
}
const announcementIntent = ref<AnnouncementIntent | null>(null);
const announcementConfirmation = ref<AnnouncementIntent | null>(null);
const announcementStorageBlocked = ref(false);
let alive = true;
let generation = 0;
let overviewSequence = 0;
let activitySequence = 0;
let userSequence = 0;
let detailSequence = 0;
let announcementSequence = 0;
let editorSequence = 0;
function context() {
  const id = props.user.id, version = generation;
  return () => alive && allowed.value && props.user.id === id && generation === version;
}
function fail(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : "操作未完成，请重试。";
  if (cause instanceof ApiError && cause.status === 401) emit("unauthorized", cause);
}
async function loadOverview() {
  if (!allowed.value) return;
  const current = context(), sequence = ++overviewSequence;
  loading.value = true; error.value = "";
  try {
    const value = await request<AdminOverview>("/admin/overview");
    if (current() && sequence === overviewSequence) overview.value = value;
  } catch (cause) {
    if (current() && sequence === overviewSequence) fail(cause);
  } finally {
    if (current() && sequence === overviewSequence) loading.value = false;
  }
}
async function loadUsers(offset = 0, query = appliedUserQuery.value) {
  if (!allowed.value) return;
  const current = context(), sequence = ++userSequence;
  usersLoading.value = true; error.value = "";
  const parameters = new URLSearchParams({ q: query, account_type: userAccountType.value, offset: String(offset), limit: "20" });
  try {
    const value = await request<AdminPage<AdminUser>>(`/admin/users?${parameters}`);
    if (!current() || sequence !== userSequence) return;
    userPage.value = value; userOffset.value = offset; appliedUserQuery.value = query;
  } catch (cause) {
    if (current() && sequence === userSequence) fail(cause);
  } finally {
    if (current() && sequence === userSequence) usersLoading.value = false;
  }
}
function selectUserAccountType(value: AdminAccountType) {
  if (value === userAccountType.value) return;
  userAccountType.value = value; userPage.value = null; userOffset.value = 0;
  ++detailSequence; selectedUserId.value = null; userDetail.value = null; detailLoading.value = false;
  if (!creditIntent.value) { creditAmount.value = ""; creditReason.value = ""; }
  void loadUsers(0);
}
async function loadActivity(offset = 0) {
  if (!allowed.value) return;
  const current = context(), sequence = ++activitySequence;
  activityLoading.value = true;
  try {
    const value = await request<AdminPage<AdminActivity>>(`/admin/activity?offset=${offset}&limit=20`);
    if (current() && sequence === activitySequence) { activityPage.value = value; activityOffset.value = offset; }
  } catch (cause) {
    if (current() && sequence === activitySequence) fail(cause);
  } finally {
    if (current() && sequence === activitySequence) activityLoading.value = false;
  }
}
async function selectUser(id: string) {
  if (!allowed.value) return;
  const current = context(), sequence = ++detailSequence;
  selectedUserId.value = id; userDetail.value = null; detailLoading.value = true; error.value = "";
  if (!creditIntent.value) { creditAmount.value = ""; creditReason.value = ""; }
  try {
    const value = await request<AdminUserDetail>(`/admin/users/${encodeURIComponent(id)}`);
    if (current() && sequence === detailSequence && selectedUserId.value === id) userDetail.value = value;
  } catch (cause) {
    if (current() && sequence === detailSequence && selectedUserId.value === id) fail(cause);
  } finally {
    if (current() && sequence === detailSequence) detailLoading.value = false;
  }
}
function storageKey(kind: string) { return `research-assistant:admin:${props.user.id}:${kind}`; }
function persistCredit() {
  try {
    if (creditIntent.value) sessionStorage.setItem(storageKey("credit-intent"), JSON.stringify(creditIntent.value));
    else sessionStorage.removeItem(storageKey("credit-intent"));
    storageBlocked.value = false; return true;
  } catch {
    storageBlocked.value = true; error.value = "浏览器无法保存待确认请求，请启用会话存储后再试。"; return false;
  }
}
function restoreCredit() {
  creditIntent.value = null; storageBlocked.value = false;
  try {
    const raw = sessionStorage.getItem(storageKey("credit-intent"));
    if (!raw) return;
    const value = JSON.parse(raw) as CreditIntent;
    if (value.owner !== props.user.id || typeof value.targetId !== "string" || !value.targetId || typeof value.targetEmail !== "string"
        || !Number.isSafeInteger(value.amount) || !value.amount || Math.abs(value.amount) > 1000000
        || typeof value.reason !== "string" || !value.reason.trim() || value.reason.length > 200
        || typeof value.request_key !== "string" || !value.request_key || value.request_key.length > 128
        || !["confirm", "unknown", "rejected"].includes(value.phase)) throw new Error();
    creditIntent.value = value; creditAmount.value = String(value.amount); creditReason.value = value.reason;
  } catch {
    storageBlocked.value = true; error.value = "待确认的积分请求无法读取，请先核对用户流水，勿重复调整。";
  }
}
function prepareCredit() {
  if (!allowed.value || !userDetail.value || creditIntent.value || adjusting.value || storageBlocked.value) return;
  error.value = ""; notice.value = "";
  const text = String(creditAmount.value).trim(), amount = Number(text), reason = creditReason.value.trim();
  if (!/^[+-]?\d+$/.test(text) || !Number.isSafeInteger(amount) || !amount || Math.abs(amount) > 1000000) {
    error.value = "调整积分须为非零整数，范围为 -1000000 至 1000000。"; return;
  }
  if (!reason || reason.length > 200) { error.value = "请填写 1 至 200 字的调整原因。"; return; }
  creditIntent.value = { owner: props.user.id, targetId: userDetail.value.user.id, targetEmail: userDetail.value.user.email,
    amount, reason, request_key: requestKey(), phase: "confirm" };
  persistCredit();
}
function cancelCredit() {
  if (adjusting.value || !creditIntent.value || creditIntent.value.phase === "unknown") return;
  creditIntent.value = null; persistCredit();
}
async function submitCredit() {
  const intent = creditIntent.value;
  if (!allowed.value || !intent || adjusting.value || intent.owner !== props.user.id) return;
  const current = context();
  intent.phase = "unknown";
  if (!persistCredit()) return;
  adjusting.value = true; error.value = ""; notice.value = "";
  try {
    const value = await request<{ adjustment: { id: string | number }; credits: { available: number; frozen: number } }>(`/admin/users/${encodeURIComponent(intent.targetId)}/credits`,
      { method: "POST", body: JSON.stringify({ amount: intent.amount, reason: intent.reason, request_key: intent.request_key }) }, props.user.csrf_token);
    if (!current()) return;
    if (userDetail.value?.user.id === intent.targetId) {
      userDetail.value.credits.available = value.credits.available; userDetail.value.credits.frozen = value.credits.frozen;
    }
    creditIntent.value = null; persistCredit(); creditAmount.value = ""; creditReason.value = "";
    notice.value = "积分调整已确认，余额以服务器记录为准。";
    if (selectedUserId.value === intent.targetId) void selectUser(intent.targetId);
    void loadUsers(userOffset.value); void loadOverview();
  } catch (cause) {
    if (current()) {
      if (cause instanceof ApiError && [400, 404, 409, 422].includes(cause.status)) { intent.phase = "rejected"; persistCredit(); }
      fail(cause);
    }
  } finally {
    if (current()) adjusting.value = false;
  }
}
function checkCreditLedger() {
  if (!creditIntent.value) return;
  tab.value = "credits"; void selectUser(creditIntent.value.targetId);
}
async function loadAnnouncements(offset = 0) {
  if (!allowed.value) return;
  const current = context(), sequence = ++announcementSequence;
  announcementsLoading.value = true;
  try {
    const value = await request<AdminPage<Announcement>>(`/admin/announcements?offset=${offset}&limit=20`);
    if (current() && sequence === announcementSequence) { announcementPage.value = value; announcementOffset.value = offset; }
  } catch (cause) {
    if (current() && sequence === announcementSequence) fail(cause);
  } finally {
    if (current() && sequence === announcementSequence) announcementsLoading.value = false;
  }
}
function editAnnouncement(value: Announcement | null) {
  if (announcementIntent.value || announcementSaving.value) return;
  ++editorSequence; announcementConfirmation.value = null;
  selectedAnnouncement.value = value ? { ...value } : null; announcementEditor.value = true;
  announcementTitle.value = value?.title ?? ""; announcementBody.value = value?.body ?? "";
  announcementConflict.value = false; error.value = ""; notice.value = "";
}
function persistAnnouncement() {
  try {
    if (announcementIntent.value) sessionStorage.setItem(storageKey("announcement-intent"), JSON.stringify(announcementIntent.value));
    else sessionStorage.removeItem(storageKey("announcement-intent"));
    announcementStorageBlocked.value = false; return true;
  } catch {
    announcementStorageBlocked.value = true; error.value = "浏览器无法保存待确认公告请求，请启用会话存储后再试。"; return false;
  }
}
function restoreAnnouncement() {
  announcementIntent.value = null; announcementStorageBlocked.value = false;
  try {
    const raw = sessionStorage.getItem(storageKey("announcement-intent"));
    if (!raw) return;
    const value = JSON.parse(raw) as AnnouncementIntent;
    if (value.owner !== props.user.id || !["create", "update"].includes(value.operation)
        || typeof value.title !== "string" || !value.title.trim() || value.title.length > 120
        || typeof value.body !== "string" || !value.body.trim() || value.body.length > 10000) throw new Error();
    if (value.operation === "create" && (typeof value.request_key !== "string" || !value.request_key || value.request_key.length > 128)) throw new Error();
    if (value.operation === "update" && (typeof value.id !== "string" || !value.id || !Number.isSafeInteger(value.revision)
        || value.revision! < 1 || !["draft", "published"].includes(value.status!))) throw new Error();
    announcementIntent.value = value;
    announcementTitle.value = value.title; announcementBody.value = value.body; announcementEditor.value = true;
    if (value.operation === "update") selectedAnnouncement.value = { id: value.id!, revision: value.revision!, title: value.title, body: value.body,
      status: value.status!, created_at: "", updated_at: "", published_at: null };
  } catch {
    announcementStorageBlocked.value = true; error.value = "待确认公告请求无法读取，请先核对公告记录。";
  }
}
function announcementPayload(status?: "draft" | "published"): AnnouncementIntent | null {
  if (!allowed.value || announcementIntent.value || announcementSaving.value || announcementStorageBlocked.value || announcementConflict.value) return null;
  const title = announcementTitle.value.trim(), body = announcementBody.value;
  error.value = ""; notice.value = "";
  if (!title || title.length > 120 || !body.trim() || body.length > 10000) {
    error.value = "公告标题须为 1 至 120 字，正文须为 1 至 10000 字。"; return null;
  }
  if (!selectedAnnouncement.value) return { owner: props.user.id, operation: "create", title, body, request_key: requestKey() };
  return { owner: props.user.id, operation: "update", id: selectedAnnouncement.value.id, title, body,
    status: status ?? selectedAnnouncement.value.status, revision: selectedAnnouncement.value.revision };
}
function saveAnnouncement() {
  const intent = announcementPayload();
  if (intent) void writeAnnouncement(intent);
}
function confirmAnnouncement(status: "draft" | "published") {
  if (!selectedAnnouncement.value) return;
  const intent = announcementPayload(status);
  if (intent) announcementConfirmation.value = intent;
}
async function writeAnnouncement(intent: AnnouncementIntent | null) {
  if (!intent || !allowed.value || intent.owner !== props.user.id || announcementSaving.value) return;
  const current = context(), sequence = editorSequence;
  announcementIntent.value = { ...intent }; announcementConfirmation.value = null;
  if (!persistAnnouncement()) return;
  announcementSaving.value = true; error.value = ""; notice.value = "";
  try {
    const body = intent.operation === "create" ? { title: intent.title, body: intent.body, request_key: intent.request_key }
      : { title: intent.title, body: intent.body, status: intent.status, revision: intent.revision };
    const value = await request<Announcement>(intent.operation === "create" ? "/admin/announcements" : `/admin/announcements/${encodeURIComponent(intent.id!)}`,
      { method: intent.operation === "create" ? "POST" : "PATCH", body: JSON.stringify(body) }, props.user.csrf_token);
    if (!current() || sequence !== editorSequence) return;
    selectedAnnouncement.value = value; announcementTitle.value = value.title; announcementBody.value = value.body;
    announcementIntent.value = null; persistAnnouncement(); announcementConflict.value = false; announcementEditor.value = true;
    notice.value = value.status === "published" ? "公告已发布。" : "公告已保存为草稿。";
    emit("announcementsChanged"); void loadAnnouncements(announcementOffset.value);
  } catch (cause) {
    if (!current() || sequence !== editorSequence) return;
    if (cause instanceof ApiError && [400, 404, 422].includes(cause.status)) {
      announcementIntent.value = null; persistAnnouncement(); fail(cause);
    } else if (intent.operation === "update" && cause instanceof ApiError && cause.status === 409) {
      announcementConflict.value = true; announcementIntent.value = null; persistAnnouncement();
      error.value = "公告版本已变化或内容未变。请重新加载并核对后再保存，当前编辑已保留。";
    } else fail(cause);
  } finally {
    if (current() && sequence === editorSequence) announcementSaving.value = false;
  }
}
async function reloadAnnouncement() {
  const id = selectedAnnouncement.value?.id ?? announcementIntent.value?.id;
  if (!id || !allowed.value || announcementSaving.value) return;
  const current = context(), sequence = ++editorSequence;
  announcementSaving.value = true; error.value = "";
  try {
    let offset: number | null = 0;
    const seen = new Set<number>();
    while (offset !== null && !seen.has(offset)) {
      seen.add(offset);
      const value: AdminPage<Announcement> = await request(`/admin/announcements?offset=${offset}&limit=20`);
      if (!current() || sequence !== editorSequence) return;
      const actual = value.items.find((item) => item.id === id);
      if (actual) {
        ++announcementSequence; announcementPage.value = value; announcementOffset.value = offset; announcementsLoading.value = false;
        announcementIntent.value = null; persistAnnouncement(); selectedAnnouncement.value = { ...actual };
        announcementTitle.value = actual.title; announcementBody.value = actual.body; announcementConflict.value = false;
        notice.value = "已重新加载公告，请核对后再保存。"; emit("announcementsChanged"); return;
      }
      offset = value.next_offset;
    }
    error.value = "未在公告列表中找到此公告，请刷新列表核对。";
  } catch (cause) {
    if (current() && sequence === editorSequence) fail(cause);
  } finally {
    if (current() && sequence === editorSequence) announcementSaving.value = false;
  }
}
function selectTab(next: Tab) {
  tab.value = next; error.value = ""; notice.value = "";
  if (next === "overview") void loadOverview();
  if (next === "users" || next === "credits") void loadUsers(0);
  if (next === "announcements") void loadAnnouncements(0);
  if (next === "settings") emit("runtime");
}
function jobState(state: string) {
  return ({ queued: "排队中", running: "处理中", waiting: "等待中", succeeded: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消", needs_attention: "待核查", needs_selection: "待选择" } as Record<string, string>)[state] ?? "待核查";
}
function signed(value: number) { return value > 0 ? `+${value}` : String(value); }
function accountLabel(user: AdminUser) {
  if (!user.is_debug) return "正式账号";
  const features = { research_library: "科研资料库", literature: "文献求助", pymatgen: "材料工具", xrd: "XRD", abtem: "abTEM" };
  return user.debug_feature && features[user.debug_feature] ? `调试 · ${features[user.debug_feature]}` : "历史调试";
}
function activityLabel(action: string) {
  return ({ admin_credit_adjustment: "积分调整", announcement_create: "新建公告草稿", announcement_edit: "编辑公告", announcement_publish: "发布公告", announcement_withdraw: "撤回公告" } as Record<string, string>)[action] ?? "管理操作";
}
function date(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
watch([() => props.user.id, () => props.user.is_admin, () => props.user.verified, () => props.sessionGeneration], () => {
  ++generation; overview.value = null; error.value = ""; notice.value = ""; loading.value = false; tab.value = "overview";
  activityPage.value = null; activityOffset.value = 0; activityLoading.value = false;
  userQuery.value = ""; appliedUserQuery.value = ""; userAccountType.value = "regular"; userPage.value = null; userOffset.value = 0; usersLoading.value = false;
  selectedUserId.value = null; userDetail.value = null; detailLoading.value = false;
  creditAmount.value = ""; creditReason.value = ""; adjusting.value = false;
  if (allowed.value) restoreCredit(); else creditIntent.value = null;
  announcementPage.value = null; announcementOffset.value = 0; announcementsLoading.value = false;
  selectedAnnouncement.value = null; announcementEditor.value = false; announcementTitle.value = ""; announcementBody.value = "";
  announcementSaving.value = false; announcementConflict.value = false; announcementConfirmation.value = null;
  if (allowed.value) restoreAnnouncement(); else announcementIntent.value = null;
  if (allowed.value) void loadOverview();
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { alive = false; ++generation; });
</script>

<template>
  <div v-if="allowed" class="admin-center">
    <div class="page-heading"><div><span class="tiny-kicker">管理</span><h1>系统管理中心<span class="heading-dot">.</span></h1></div></div>
    <nav class="admin-tabs" aria-label="系统管理中心栏目"><button v-for="item in tabs" :key="item.id" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="selectTab(item.id)">{{ item.label }}</button></nav>
    <div v-if="error || notice" class="module-banner" :class="error ? 'notice-error' : 'notice-success'" :role="error ? 'alert' : 'status'">{{ error || notice }}</div>
    <section v-if="creditIntent" class="panel admin-credit-confirm" role="alertdialog" aria-labelledby="admin-credit-confirm-title">
      <h2 id="admin-credit-confirm-title">{{ creditIntent.phase === 'confirm' ? '确认积分调整' : creditIntent.phase === 'rejected' ? '积分调整未提交成功' : '积分调整结果待确认' }}</h2>
      <p>目标用户：<strong>{{ creditIntent.targetEmail }}</strong></p><p>调整：<strong>{{ signed(creditIntent.amount) }} 分</strong></p><p class="plain-text">原因：{{ creditIntent.reason }}</p>
      <p v-if="creditIntent.phase === 'unknown'" class="module-message">请先核对流水，或使用原请求重试。待确认期间不能提交新的调整。</p>
      <div class="admin-action-row"><button class="button primary" :disabled="adjusting" @click="submitCredit">{{ adjusting ? '正在核对…' : creditIntent.phase === 'confirm' ? '确认调整' : '使用原请求重试' }}</button><button class="button neutral" :disabled="adjusting" @click="checkCreditLedger">查看该用户流水</button><button v-if="creditIntent.phase === 'confirm'" class="text-button" :disabled="adjusting" @click="cancelCredit">取消调整</button><button v-if="creditIntent.phase === 'rejected'" class="text-button" :disabled="adjusting" @click="cancelCredit">修改后重新确认</button></div>
    </section>
    <section v-if="announcementIntent" class="panel admin-pending-announcement" role="status"><h2>公告请求结果待确认</h2><p>{{ announcementIntent.title }}</p><p class="module-message">原始正文已保留，请使用原请求重试；不要重复新建。</p><button class="button neutral" :disabled="announcementSaving" @click="writeAnnouncement(announcementIntent)">重试原公告请求</button></section>
    <section v-if="tab === 'overview'" aria-label="系统概览">
      <div class="section-heading"><h2>网站概览</h2><button class="button neutral" :disabled="loading" @click="loadOverview">{{ loading ? '读取中…' : '刷新概览' }}</button></div>
      <div class="admin-overview-grid">
        <article class="panel" aria-label="账号统计"><span>正式账号</span><strong>{{ overview?.users.regular_total ?? '—' }}</strong><small>已验证 {{ overview?.users.regular_verified ?? '—' }} · 管理员 {{ overview?.users.regular_admins ?? '—' }}</small><small>调试账号 {{ overview?.users.debug_total ?? '—' }} · 全部账号 {{ overview?.users.total ?? '—' }}</small></article>
        <article class="panel"><span>任务总数</span><strong>{{ overview ? overview.jobs.xrd_total + overview.jobs.literature_total : '—' }}</strong><small>XRD {{ overview?.jobs.xrd_total ?? '—' }} · 文献 {{ overview?.jobs.literature_total ?? '—' }}</small></article>
        <article class="panel"><span>进行中 / 排队</span><strong>{{ overview?.jobs.running ?? '—' }} / {{ overview?.jobs.queued ?? '—' }}</strong></article>
        <article class="panel"><span>全站可用积分</span><strong>{{ overview?.credits.available ?? '—' }}</strong><small>冻结 {{ overview?.credits.frozen ?? '—' }} 分</small></article>
      </div>
      <section class="panel admin-activity"><div class="section-heading"><h2>近期管理操作</h2><button class="button neutral" :disabled="activityLoading" @click="loadActivity(0)">{{ activityLoading ? '读取中…' : '查看操作记录' }}</button></div><p v-if="!visibleActivity.length" class="module-message">暂无管理操作记录。</p><article v-for="item in visibleActivity" :key="item.id" class="admin-record"><strong>{{ activityLabel(item.action) }}<template v-if="item.amount !== null && item.amount !== undefined"> · {{ signed(item.amount) }} 分</template></strong><span>操作人：{{ item.actor_email || '—' }}<template v-if="item.target_email"> · 用户：{{ item.target_email }}</template> · {{ date(item.created_at) }}</span><p v-if="item.title" class="plain-text">{{ item.title }}</p><p v-if="item.reason" class="plain-text">原因：{{ item.reason }}</p></article><div v-if="activityPage" class="admin-pagination"><button class="button neutral" :disabled="activityLoading || activityOffset === 0" @click="loadActivity(Math.max(0, activityOffset - 20))">上一页动态</button><span>共 {{ activityPage.total }} 条</span><button class="button neutral" :disabled="activityLoading || activityPage.next_offset === null" @click="loadActivity(activityPage.next_offset!)">下一页动态</button></div></section>
    </section>
    <AnalyticsPanel v-else-if="tab === 'analytics'" :user="user" :session-generation="sessionGeneration" @unauthorized="emit('unauthorized', $event)" />
    <section v-else-if="tab === 'users' || tab === 'credits'" aria-label="用户与积分管理">
      <div class="admin-account-filter" role="group" aria-label="账号类型"><button v-for="item in userAccountTypes" :key="item.value" class="button" :class="userAccountType === item.value ? 'primary' : 'neutral'" :aria-pressed="userAccountType === item.value" type="button" @click="selectUserAccountType(item.value)">{{ item.label }}</button></div>
      <form class="admin-user-search" aria-label="查找用户" @submit.prevent="loadUsers(0, userQuery.trim())"><label for="admin-user-query">用户邮箱</label><div><input id="admin-user-query" v-model="userQuery" type="search" maxlength="254" placeholder="输入邮箱查找用户" /><button class="button primary" :disabled="usersLoading" type="submit">{{ usersLoading ? '查找中…' : '查找用户' }}</button></div></form>
      <div class="admin-user-layout">
        <section class="panel admin-user-list" aria-label="用户列表">
          <div class="section-heading"><h2>{{ tab === 'credits' ? '选择调整积分的用户' : '用户信息' }}</h2><span class="small-muted">{{ userPage ? `共 ${userPage.total} 人` : '读取中…' }}</span></div>
          <p v-if="userPage && !userPage.items.length" class="module-message">未找到匹配用户。</p>
          <article v-for="row in userPage?.items ?? []" :key="row.id" class="admin-user-row" :class="{ selected: selectedUserId === row.id }"><button class="text-button" @click="selectUser(row.id)">{{ row.email }}</button><span class="admin-account-label">{{ accountLabel(row) }}</span><span>{{ row.is_admin ? '管理员' : '用户' }} · {{ row.verified ? '已验证' : '未验证' }}</span><small>可用 {{ row.available }} · 冻结 {{ row.frozen }} 分</small></article>
          <div v-if="userPage" class="admin-pagination"><button class="button neutral" :disabled="usersLoading || userOffset === 0" @click="loadUsers(Math.max(0, userOffset - 20))">上一页</button><span>{{ userPage.total ? `${userOffset + 1}–${userOffset + userPage.items.length}` : '0' }} / {{ userPage.total }}</span><button class="button neutral" :disabled="usersLoading || userPage.next_offset === null" @click="loadUsers(userPage.next_offset!)">下一页</button></div>
        </section>
        <section class="panel admin-user-detail" aria-label="用户详情">
          <p v-if="!userDetail" class="module-message">{{ detailLoading ? '正在读取用户详情…' : '选择用户，查看信息与积分流水。' }}</p>
          <template v-else>
            <div class="section-heading"><h2>{{ userDetail.user.email }}</h2><button class="text-button" :disabled="detailLoading" @click="selectUser(userDetail.user.id)">刷新详情</button></div>
            <p class="admin-account-label">{{ accountLabel(userDetail.user) }}</p>
            <dl class="admin-detail-grid"><div><dt>注册时间（北京）</dt><dd>{{ date(userDetail.user.created_at) }}</dd></div><div><dt>邮箱验证</dt><dd>{{ userDetail.user.verified ? '已验证' : '未验证' }}</dd></div><div><dt>角色</dt><dd>{{ userDetail.user.is_admin ? '管理员' : '用户' }}</dd></div><div><dt>可用 / 冻结积分</dt><dd>{{ userDetail.credits.available }} / {{ userDetail.credits.frozen }}</dd></div><div><dt>XRD / 文献任务</dt><dd>{{ userDetail.user.xrd_jobs }} / {{ userDetail.user.literature_jobs }}</dd></div></dl>
            <button v-if="tab === 'users'" class="button neutral" @click="tab = 'credits'">调整此用户积分</button>
            <form v-else class="admin-credit-form admin-detail-section" aria-label="积分调整" @submit.prevent="prepareCredit"><h3>调整积分</h3><p class="module-message">目标：{{ userDetail.user.email }}；正数增加、负数扣除，仅调整可用积分。</p><label for="admin-credit-amount">调整分数</label><input id="admin-credit-amount" v-model="creditAmount" type="text" inputmode="numeric" placeholder="如 10 或 -5" maxlength="16" :disabled="!!creditIntent || adjusting || storageBlocked" required /><label for="admin-credit-reason">调整原因</label><textarea id="admin-credit-reason" v-model="creditReason" rows="3" maxlength="200" :disabled="!!creditIntent || adjusting || storageBlocked" required /><button class="button primary" :disabled="!!creditIntent || adjusting || storageBlocked" type="submit">核对调整</button></form>
            <section class="admin-detail-section"><h3>近期任务</h3><p v-if="!userDetail.recent_jobs.length" class="module-message">暂无任务。</p><article v-for="job in userDetail.recent_jobs" :key="job.id" class="admin-record"><strong>{{ job.title || job.codes?.join('、') || (job.kind === 'literature' ? '文献求助' : 'XRD 任务') }}</strong><span>{{ jobState(job.state) }} · {{ date(job.created_at) }}</span></article></section>
            <section class="admin-detail-section"><h3>积分流水</h3><p v-if="!userDetail.credits.items.length" class="module-message">暂无积分流水。</p><article v-for="entry in userDetail.credits.items" :key="entry.id" class="admin-record"><strong>{{ entry.label }} · {{ signed(entry.amount) }} 分</strong><span>冻结变动 {{ signed(entry.frozen_amount) }} · {{ date(entry.created_at) }}</span><p v-if="entry.reason">原因：{{ entry.reason }}</p><small v-if="entry.operator_email">操作人：{{ entry.operator_email }}</small></article></section>
          </template>
        </section>
      </div>
    </section>
    <section v-else-if="tab === 'announcements'" aria-label="网站公告管理">
      <div class="section-heading"><h2>网站公告</h2><div class="admin-action-row"><button class="button neutral" :disabled="announcementsLoading" @click="loadAnnouncements(announcementOffset)">刷新公告</button><button class="button primary" :disabled="!!announcementIntent || announcementSaving" @click="editAnnouncement(null)">新建公告</button></div></div>
      <div class="admin-announcement-layout">
        <section class="panel admin-announcement-list" aria-label="公告列表"><p v-if="announcementPage && !announcementPage.items.length" class="module-message">暂无公告，创建后先保存为草稿。</p><article v-for="item in announcementPage?.items ?? []" :key="item.id" class="admin-announcement-row"><button class="text-button" :disabled="!!announcementIntent || announcementSaving" @click="editAnnouncement(item)">{{ item.title }}</button><span class="soft-label">{{ item.status === 'published' ? '已发布' : '草稿' }}</span><small>{{ date(item.updated_at) }}</small></article><div v-if="announcementPage" class="admin-pagination"><button class="button neutral" :disabled="announcementsLoading || announcementOffset === 0" @click="loadAnnouncements(Math.max(0, announcementOffset - 20))">上一页</button><span>共 {{ announcementPage.total }} 条</span><button class="button neutral" :disabled="announcementsLoading || announcementPage.next_offset === null" @click="loadAnnouncements(announcementPage.next_offset!)">下一页</button></div></section>
        <section class="panel admin-announcement-editor"><p v-if="!announcementEditor" class="module-message">选择公告进行编辑，或创建新草稿。</p><form v-else aria-label="公告编辑" @submit.prevent="saveAnnouncement"><div class="section-heading"><h2>{{ selectedAnnouncement ? '编辑公告' : '新建草稿' }}</h2><span v-if="selectedAnnouncement" class="soft-label">{{ selectedAnnouncement.status === 'published' ? '已发布' : '草稿' }}</span></div><label for="admin-announcement-title">标题</label><input id="admin-announcement-title" v-model="announcementTitle" maxlength="120" :disabled="!!announcementIntent || announcementSaving || !!announcementConfirmation" required /><label for="admin-announcement-body">正文（纯文本）</label><textarea id="admin-announcement-body" v-model="announcementBody" rows="10" maxlength="10000" :disabled="!!announcementIntent || announcementSaving || !!announcementConfirmation" required /><p class="small-muted">{{ announcementBody.length }} / 10000 字；换行与文字按原文显示。</p><div class="admin-action-row"><button class="button primary" type="submit" :disabled="announcementSaving || !!announcementIntent || !!announcementConfirmation || announcementConflict || announcementStorageBlocked">{{ announcementSaving ? '保存中…' : selectedAnnouncement ? '保存修改' : '保存草稿' }}</button><button v-if="selectedAnnouncement?.status === 'draft'" class="button neutral" type="button" :disabled="announcementSaving || !!announcementIntent || !!announcementConfirmation || announcementConflict" @click="confirmAnnouncement('published')">发布公告</button><button v-if="selectedAnnouncement?.status === 'published'" class="button neutral" type="button" :disabled="announcementSaving || !!announcementIntent || !!announcementConfirmation || announcementConflict" @click="confirmAnnouncement('draft')">撤回公告</button></div>
          <div v-if="announcementConfirmation" class="admin-publish-confirm" role="alertdialog" aria-label="公告状态确认"><p>{{ announcementConfirmation.status === 'published' ? '确认向所有访客发布此公告？' : '确认撤回此公告，停止公开展示？' }}</p><strong>{{ announcementConfirmation.title }}</strong><div class="admin-action-row"><button class="button primary" type="button" @click="writeAnnouncement(announcementConfirmation)">{{ announcementConfirmation.status === 'published' ? '确认发布' : '确认撤回' }}</button><button class="button neutral" type="button" @click="announcementConfirmation = null">取消</button></div></div>
          <button v-if="announcementConflict || announcementIntent?.operation === 'update'" class="button neutral" type="button" :disabled="announcementSaving" @click="reloadAnnouncement">重新加载此公告（放弃当前编辑）</button>
        </form></section>
      </div>
    </section>
    <section v-else-if="tab === 'settings'" aria-label="运行设置"><slot name="settings" /></section>
  </div>
</template>

<style scoped>
.admin-account-filter { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
.admin-account-filter > .button { min-width: 0; padding: 8px 14px; }
.admin-account-label { margin: 0; overflow-wrap: anywhere; font-size: 12px; color: #67768a; }
</style>
