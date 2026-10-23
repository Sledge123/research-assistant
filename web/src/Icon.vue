<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 });
const paths: Record<string, string> = {
  search: "m21 21-4.3-4.3M19 10.5a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0Z",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  history: "M3 10a9 9 0 1 1 1.3 7M3 3v7h7m2-4v6l4 2",
  user: "M20 21v-2a7 7 0 0 0-14 0v2m11-14a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  mail: "M3 5h18v14H3zm0 0 9 8 9-8",
  check: "m5 12 4 4L19 6",
  close: "m6 6 12 12M6 18 18 6",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  info: "M12 11v6m0-10v.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
  shield: "M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Zm-4 10 3 3 5-6",
  file: "M14 2H4v20h16V8l-6-6Zm0 0v6h6M8 13h8m-8 4h5",
  refresh:
    "M20 7v5h-5M4 17v-5h5M5.2 7a8 8 0 0 1 13.2-2L20 7M4 17l1.6 2a8 8 0 0 0 13.2-2",
  logout: "M9 4H3v16h6m5-13 5 5-5 5M8 12h13",
  grid: "M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z",
};
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.file" />
  </svg>
</template>
