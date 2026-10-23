<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import {
  API_BASE,
  ApiError,
  request,
  type ResearchLibraryItem,
  type ResearchLibraryPage,
  type ResearchLibraryStorage,
  type User,
} from "./api";

const props = defineProps<{ user: User | null }>();
const emit = defineEmits<{ login: [] }>();

type ItemStatus = ResearchLibraryItem["status"];
interface EditForm {
  id: string;
  revision: number;
  title: string;
  category: string;
  description: string;
  license: string;
  source_url: string;
  status: "draft" | "published" | "archived";
}

const items = ref<ResearchLibraryItem[]>([]);
const categories = ref<string[]>([]);
const storage = ref<ResearchLibraryStorage | null>(null);
const total = ref(0);
const nextOffset = ref<number | null>(null);
const offset = ref(0);
const searchText = ref("");
const selectedCategory = ref("");
const selectedStatus = ref<"all" | "draft" | "published" | "archived" | "deleting">("all");
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const selectedFile = ref<File | null>(null);
const editing = ref<EditForm | null>(null);
const uploadForm = reactive({ title: "", category: "通用资料", description: "", license: "", source_url: "", status: "draft" as "draft" | "published" });

const isAdmin = computed(() => Boolean(props.user?.verified && props.user.is_admin));

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes / 1024;
  for (const unit of units) {
    if (value < 1024 || unit === "GiB") return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${unit}`;
    value /= 1024;
  }
  return `${value.toFixed(1)} GiB`;
}

function formatDate(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value * 1000));
}

function statusLabel(status: ItemStatus) {
  return ({ draft: "草稿", published: "已发布", archived: "已下架", deleting: "待清理" } as const)[status];
}

async function load(next = 0) {
  if (!props.user) {
    items.value = [];
    total.value = 0;
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({ offset: String(next), limit: "20" });
    const query = searchText.value.trim();
    if (query) params.set("q", query);
    if (selectedCategory.value) params.set("category", selectedCategory.value);
    const admin = isAdmin.value;
    if (admin && selectedStatus.value !== "all") params.set("status", selectedStatus.value);
    const path = admin ? "/admin/library" : "/library";
    const result = await request<ResearchLibraryPage>(`${path}?${params.toString()}`);
    items.value = result.items;
    categories.value = result.categories;
    storage.value = result.storage ?? null;
    total.value = result.total;
    nextOffset.value = result.next_offset;
    offset.value = next;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "资料暂时无法读取，请稍后重试。";
  } finally {
    loading.value = false;
  }
}

function search() {
  void load(0);
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0] ?? null;
  selectedFile.value = file;
  if (file && !uploadForm.title.trim()) uploadForm.title = file.name.replace(/\.[^.]+$/, "");
  error.value = "";
}

function fileToBase64(file: File) {
  return file.arrayBuffer().then((buffer) => {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
  });
}

async function upload() {
  if (!props.user || !isAdmin.value || !selectedFile.value) return;
  if (selectedFile.value.size > 20 * 1024 * 1024) {
    error.value = "单个文件不能超过 20 MiB。";
    return;
  }
  if (uploadForm.status === "published" && !uploadForm.license.trim()) {
    error.value = "发布资料前请填写许可或权属说明。";
    return;
  }
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const content_base64 = await fileToBase64(selectedFile.value);
    await request<{ item: ResearchLibraryItem; storage: ResearchLibraryStorage }>("/admin/library", {
      method: "POST",
      body: JSON.stringify({ ...uploadForm, filename: selectedFile.value.name, content_base64 }),
    }, props.user.csrf_token);
    notice.value = uploadForm.status === "published" ? "资料已发布。" : "资料已保存为草稿。";
    uploadForm.title = "";
    uploadForm.description = "";
    uploadForm.license = "";
    uploadForm.source_url = "";
    uploadForm.status = "draft";
    selectedFile.value = null;
    if (fileInput.value) fileInput.value.value = "";
    await load(0);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "上传未完成，请稍后重试。";
  } finally {
    busy.value = false;
  }
}

function beginEdit(item: ResearchLibraryItem) {
  if (item.status === "deleting") return;
  editing.value = {
    id: item.id,
    revision: item.revision,
    title: item.title,
    category: item.category,
    description: item.description,
    license: item.license,
    source_url: item.source_url,
    status: item.status === "archived" ? "archived" : item.status,
  };
  error.value = "";
  notice.value = "";
}

async function saveEdit() {
  if (!props.user || !editing.value) return;
  if (editing.value.status === "published" && !editing.value.license.trim()) {
    error.value = "发布资料前请填写许可或权属说明。";
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    const { id, ...body } = editing.value;
    await request(`/admin/library/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }, props.user.csrf_token);
    editing.value = null;
    notice.value = "资料信息已更新。";
    await load(offset.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "修改未完成，请稍后重试。";
  } finally {
    busy.value = false;
  }
}

async function removeItem(item: ResearchLibraryItem) {
  if (!props.user || !isAdmin.value) return;
  if (!window.confirm(`删除“${item.title}”及其文件？此操作无法撤销。`)) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await request(`/admin/library/${encodeURIComponent(item.id)}`, { method: "DELETE" }, props.user.csrf_token);
    notice.value = "资料已删除。";
    if (items.value.length === 1 && offset.value > 0) offset.value = Math.max(0, offset.value - 20);
    await load(offset.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "删除未完成，请刷新后重试。";
    await load(offset.value);
  } finally {
    busy.value = false;
  }
}

async function download(item: ResearchLibraryItem) {
  if (!props.user) return;
  error.value = "";
  try {
    const endpoint = isAdmin.value ? `/admin/library/${item.id}/download` : `/library/${item.id}/download`;
    const response = await fetch(`${API_BASE}${endpoint}`, { credentials: "same-origin" });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: unknown } | null;
      throw new ApiError(response.status, typeof body?.detail === "string" ? body.detail : "下载未完成，请稍后重试。");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = item.filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "下载未完成，请稍后重试。";
  }
}

onMounted(() => void load(0));
watch(() => props.user?.id, () => void load(0));
</script>

<template>
  <main class="content-wrap research-library-page">
    <header class="library-heading">
      <div>
        <span class="tiny-kicker">科研资料</span>
        <h1>资料库<span class="heading-dot">.</span></h1>
        <p>集中保存和获取科研文献、数据与方法资料。</p>
      </div>
      <button v-if="user" class="button neutral" type="button" :disabled="loading" @click="load(offset)">刷新</button>
    </header>

    <section v-if="!user" class="panel library-login">
      <h2>登录后查看资料</h2>
      <p>使用已验证邮箱登录后，可浏览和下载已发布资料。</p>
      <button class="button primary" type="button" @click="emit('login')">登录 / 注册</button>
    </section>

    <template v-else>
      <section v-if="isAdmin && storage" class="library-storage panel" aria-label="资料库容量">
        <div><span>已用容量</span><strong>{{ formatBytes(storage.used_bytes) }} / {{ formatBytes(storage.quota_bytes) }}</strong></div>
        <div><span>可继续存放</span><strong>{{ formatBytes(storage.available_bytes) }}</strong></div>
        <div><span>服务器可用</span><strong>{{ formatBytes(storage.server_free_bytes) }}</strong></div>
        <p>单个文件上限 {{ formatBytes(storage.max_file_bytes) }}；服务器空闲低于 {{ formatBytes(storage.minimum_free_bytes) }} 时会暂停上传。</p>
      </section>

      <section v-if="isAdmin" class="panel library-upload">
        <div class="library-section-heading"><div><h2>添加资料</h2><p>上传后可保存草稿，填写许可或权属说明后发布。</p></div></div>
        <form class="library-form" @submit.prevent="upload">
          <label>标题<input v-model="uploadForm.title" required maxlength="160" autocomplete="off" /></label>
          <label>分类<input v-model="uploadForm.category" required maxlength="60" placeholder="例如：论文、数据、方法" /></label>
          <label class="library-wide">简介<textarea v-model="uploadForm.description" maxlength="2000" rows="3" /></label>
          <label>许可 / 权属说明<input v-model="uploadForm.license" maxlength="160" placeholder="发布前必填" /></label>
          <label>来源链接<input v-model="uploadForm.source_url" type="url" maxlength="512" placeholder="https://…" /></label>
          <label class="library-wide">文件<input ref="fileInput" type="file" accept=".pdf,.docx,.pptx,.xlsx,.csv,.tsv,.txt,.md,.rtf,.cif,.pdb,.ent,.xyz,.vasp,.poscar,.mrc,.dm3,.dm4,.h5,.hdf5,.zip,.png,.jpg,.jpeg" required @change="onFileChange" /><small>支持 PDF、Office、表格、结构与常见科研数据文件；单个文件不超过 20 MiB。</small></label>
          <label>保存方式<select v-model="uploadForm.status"><option value="draft">仅保存草稿</option><option value="published">发布给已验证用户</option></select></label>
          <div class="library-form-action"><button class="button primary" type="submit" :disabled="busy || loading || !selectedFile">{{ busy ? "正在保存…" : uploadForm.status === "published" ? "上传并发布" : "上传为草稿" }}</button></div>
        </form>
      </section>

      <div v-if="error" class="page-notice notice-error" role="alert">{{ error }}</div>
      <div v-if="notice" class="page-notice notice-success" role="status">{{ notice }}</div>

      <section class="panel library-browser">
        <div class="library-section-heading"><div><h2>{{ isAdmin ? "资料管理" : "可用资料" }}</h2><p>{{ total }} 项</p></div></div>
        <form class="library-filters" @submit.prevent="search">
          <label class="library-query">搜索资料<input v-model="searchText" maxlength="120" placeholder="标题、简介、分类或文件名" /></label>
          <label>分类<select v-model="selectedCategory"><option value="">全部分类</option><option v-for="category in categories" :key="category" :value="category">{{ category }}</option></select></label>
          <label v-if="isAdmin">状态<select v-model="selectedStatus"><option value="all">全部</option><option value="draft">草稿</option><option value="published">已发布</option><option value="archived">已下架</option><option value="deleting">待清理</option></select></label>
          <button class="button neutral" type="submit" :disabled="loading">筛选</button>
        </form>

        <div v-if="loading && items.length === 0" class="library-empty">正在读取资料…</div>
        <div v-else-if="items.length === 0" class="library-empty">{{ isAdmin ? "资料库暂时为空，可上传一份资料开始使用。" : "暂时没有已发布的资料。" }}</div>
        <div v-else class="library-list">
          <article v-for="item in items" :key="item.id" class="library-item">
            <div class="library-item-main">
              <div class="library-item-title"><h3>{{ item.title }}</h3><span class="library-tag">{{ item.category }}</span><span v-if="isAdmin" class="library-tag" :class="`status-${item.status}`">{{ statusLabel(item.status) }}</span></div>
              <p v-if="item.description" class="library-description">{{ item.description }}</p>
              <div class="library-meta"><span>{{ item.filename }}</span><span>{{ formatBytes(item.size_bytes) }}</span><span>{{ formatDate(item.created_at) }}</span></div>
              <p v-if="item.license" class="library-license">许可 / 权属：{{ item.license }}</p>
              <a v-if="item.source_url" class="library-source" :href="item.source_url" target="_blank" rel="noopener noreferrer">查看来源</a>
              <p v-if="isAdmin && item.sha256" class="library-hash">SHA256：{{ item.sha256 }}</p>
            </div>
            <div class="library-item-actions">
              <button class="button primary" type="button" :disabled="busy || !item.available || item.status === 'deleting'" @click="download(item)">{{ item.available ? "下载" : "文件不可用" }}</button>
              <template v-if="isAdmin">
                <button v-if="item.status !== 'deleting'" class="button neutral" type="button" :disabled="busy" @click="beginEdit(item)">编辑</button>
                <button class="button danger" type="button" :disabled="busy" @click="removeItem(item)">{{ item.status === "deleting" ? "完成清理" : "删除" }}</button>
              </template>
            </div>
          </article>
        </div>

        <div v-if="items.length" class="library-pagination">
          <button class="button neutral" type="button" :disabled="loading || offset === 0" @click="load(Math.max(0, offset - 20))">上一页</button>
          <span>第 {{ Math.floor(offset / 20) + 1 }} 页</span>
          <button class="button neutral" type="button" :disabled="loading || nextOffset === null" @click="load(nextOffset ?? offset)">下一页</button>
        </div>
      </section>

      <div v-if="editing" class="modal-backdrop" @click.self="editing = null">
        <section class="panel library-edit" role="dialog" aria-modal="true" aria-labelledby="library-edit-title">
          <div class="library-section-heading"><div><h2 id="library-edit-title">编辑资料</h2><p>修改后页面内容立即更新。</p></div><button class="icon-button" type="button" aria-label="关闭" @click="editing = null">×</button></div>
          <form class="library-form" @submit.prevent="saveEdit">
            <label>标题<input v-model="editing.title" required maxlength="160" /></label>
            <label>分类<input v-model="editing.category" required maxlength="60" /></label>
            <label class="library-wide">简介<textarea v-model="editing.description" maxlength="2000" rows="3" /></label>
            <label>许可 / 权属说明<input v-model="editing.license" maxlength="160" /></label>
            <label>来源链接<input v-model="editing.source_url" type="url" maxlength="512" /></label>
            <label>状态<select v-model="editing.status"><option value="draft">草稿</option><option value="published">已发布</option><option value="archived">下架</option></select></label>
            <div class="library-form-action"><button class="button neutral" type="button" :disabled="busy" @click="editing = null">取消</button><button class="button primary" type="submit" :disabled="busy">{{ busy ? "正在保存…" : "保存修改" }}</button></div>
          </form>
        </section>
      </div>
    </template>
  </main>
</template>

<style scoped>
.research-library-page { padding-top: 38px; padding-bottom: 64px; }
.library-heading, .library-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.library-heading { margin-bottom: 26px; }
.library-heading h1 { margin: 6px 0; font-size: clamp(2rem, 4vw, 2.6rem); }
.library-heading p, .library-section-heading p { margin: 0; color: var(--muted, #6b7280); }
.library-section-heading { margin-bottom: 18px; }
.library-section-heading h2 { margin: 0 0 5px; font-size: 1.15rem; }
.library-storage { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 20px; }
.library-storage div { display: grid; gap: 5px; }
.library-storage span, .library-storage p, .library-upload p, .library-login p { color: var(--muted, #6b7280); }
.library-storage strong { font-size: 1.12rem; }
.library-storage p { grid-column: 1 / -1; margin: 0; font-size: .86rem; }
.library-upload, .library-browser, .library-login { margin-bottom: 20px; padding: clamp(18px, 3vw, 26px); }
.library-login { text-align: center; }
.library-login h2 { margin-top: 0; }
.library-login p { margin-bottom: 18px; }
.library-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.library-form label, .library-filters label { display: grid; gap: 7px; color: var(--text, #222); font-size: .9rem; font-weight: 600; }
.library-form input, .library-form textarea, .library-form select, .library-filters input, .library-filters select { width: 100%; min-width: 0; padding: 10px 12px; border: 1px solid var(--line, #d8dee8); border-radius: 10px; background: var(--surface, white); color: inherit; font: inherit; }
.library-form textarea { resize: vertical; }
.library-form input[type="file"] { padding: 8px; font-size: .82rem; }
.library-form small { color: var(--muted, #6b7280); font-weight: 400; line-height: 1.45; }
.library-wide { grid-column: 1 / -1; }
.library-form-action { display: flex; align-items: end; justify-content: flex-end; gap: 10px; }
.library-filters { display: grid; grid-template-columns: minmax(200px, 1fr) minmax(150px, .45fr) auto auto; align-items: end; gap: 12px; margin-bottom: 16px; }
.library-query { min-width: 0; }
.library-list { display: grid; gap: 12px; }
.library-item { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; padding: 18px 0; border-top: 1px solid var(--line, #e5e7eb); }
.library-item-main { min-width: 0; }
.library-item-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.library-item-title h3 { margin: 0; font-size: 1.05rem; }
.library-tag { padding: 3px 8px; border-radius: 999px; background: #eef3ff; color: #365690; font-size: .75rem; }
.library-tag.status-draft { background: #fff4d7; color: #805a00; }
.library-tag.status-archived, .library-tag.status-deleting { background: #f1f1f1; color: #5b5b5b; }
.library-description { margin: 9px 0; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.library-meta { display: flex; flex-wrap: wrap; gap: 7px 14px; color: var(--muted, #6b7280); font-size: .82rem; }
.library-license, .library-source, .library-hash { display: block; margin: 8px 0 0; font-size: .82rem; overflow-wrap: anywhere; }
.library-license, .library-hash { color: var(--muted, #6b7280); }
.library-source { color: var(--accent, #2c5cc5); }
.library-hash { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; word-break: break-all; }
.library-item-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.library-item-actions :deep(.button) { white-space: nowrap; }
.library-empty { padding: 36px 12px; text-align: center; color: var(--muted, #6b7280); }
.library-pagination { display: flex; justify-content: center; align-items: center; gap: 16px; padding-top: 20px; }
.library-pagination span { color: var(--muted, #6b7280); font-size: .9rem; }
.library-edit { width: min(680px, calc(100vw - 32px)); max-height: min(90vh, 850px); overflow: auto; padding: 22px; }
.library-edit .library-form-action { grid-column: 1 / -1; }
@media (max-width: 700px) {
  .research-library-page { padding-top: 25px; }
  .library-storage { grid-template-columns: 1fr 1fr; }
  .library-form, .library-filters { grid-template-columns: 1fr; }
  .library-wide { grid-column: auto; }
  .library-item { flex-direction: column; }
  .library-item-actions { justify-content: flex-start; }
  .library-form-action { align-items: stretch; }
  .library-form-action .button { flex: 1; }
}
</style>
