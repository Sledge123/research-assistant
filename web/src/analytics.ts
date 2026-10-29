import { API_BASE } from "./api";

export type AnalyticsPeriod = "today" | "yesterday" | "7d" | "30d";
export interface VisitMetrics { pv: number; uv: number; ip: number; new_visitors: number }
export interface VisitGroup { name: string; label?: string; pv: number; uv: number; ip: number }
export interface AnalyticsReport {
  period: AnalyticsPeriod;
  timezone: string;
  start_date: string;
  end_date: string;
  summary: VisitMetrics;
  series: (VisitMetrics & { label: string })[];
  pages: VisitGroup[];
  browsers: VisitGroup[];
  systems: VisitGroup[];
  devices: VisitGroup[];
  referrers: VisitGroup[];
  retention_days: number;
  first_collected_at: string | null;
}

const pages = new Set(["literature", "search", "library", "abtem", "pymatgen", "tasks", "credits", "account", "admin"]);
let pending: Promise<void> = Promise.resolve();

// 串行首次上报，使后续页面复用服务器签发的独立访客Cookie。
// 业务请求不等待此队列；不传入当前URL、账号、搜索词或验证参数。
export function trackPage(page: string): Promise<void> {
  if (!pages.has(page) || navigator.doNotTrack === "1"
      || (navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl) return Promise.resolve();
  let referrer = "";
  try {
    const previous = new URL(document.referrer);
    if (["https:", "http:"].includes(previous.protocol) && previous.hostname !== window.location.hostname) referrer = previous.hostname;
  } catch { /* 无来源页面 */ }
  // 与服务器字段规则一致，非法来源不影响一次正常页面访问。
  if (!/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(referrer)) referrer = "";
  const body = JSON.stringify({ page, event_id: crypto.randomUUID().replaceAll("-", ""), referrer });
  pending = pending.then(async () => {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 4000);
    try {
      await fetch(`${API_BASE}/analytics/pageview`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body, keepalive: true, signal: abort.signal });
    } catch { /* 统计不可用时保持业务正常，避免重试制造额外PV。 */ }
    finally { clearTimeout(timer); }
  });
  return pending;
}
