<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { request, type AdminPage, type PublicAnnouncement } from "./api";

const props = defineProps<{ version: number }>();
const items = ref<PublicAnnouncement[]>([]);
const nextOffset = ref<number | null>(null);
const loading = ref(false);
const error = ref("");
let alive = true;
let generation = 0;
async function load(offset = 0) {
  const current = ++generation;
  loading.value = true; error.value = "";
  try {
    const value = await request<AdminPage<PublicAnnouncement>>(`/announcements?offset=${offset}&limit=20`);
    if (!alive || current !== generation) return;
    const merged = offset === 0 ? value.items : [...items.value, ...value.items];
    items.value = [...new Map(merged.map((item) => [item.id, item])).values()];
    nextOffset.value = value.next_offset;
  } catch (cause) {
    if (alive && current === generation) error.value = cause instanceof Error ? cause.message : "公告暂未读取成功。";
  } finally {
    if (alive && current === generation) loading.value = false;
  }
}
function date(value: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" });
}
watch(() => props.version, () => { void load(0); }, { immediate: true });
onBeforeUnmount(() => { alive = false; ++generation; });
</script>

<template>
  <section v-if="items.length" class="content-wrap public-announcements" aria-label="网站公告">
    <div class="panel"><h2>网站公告</h2><details v-for="item in items" :key="item.id"><summary><strong>{{ item.title }}</strong><time :datetime="item.published_at ?? undefined">{{ date(item.published_at) }}</time></summary><p class="plain-text">{{ item.body }}</p></details><p v-if="error" class="module-message" role="status">{{ error }}</p><button v-if="nextOffset !== null" class="text-button" :disabled="loading" @click="load(nextOffset!)">{{ loading ? '读取中…' : '更多公告' }}</button></div>
  </section>
</template>
