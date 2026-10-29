// 待确认请求只在当前登录会话内保存，跨页面保留同一个请求标识。
const pending = new Map<string, Record<string, unknown>>();

export function pendingPymatgen(userId: string): Record<string, unknown> | null {
  return pending.get(userId) ?? null;
}

export function savePendingPymatgen(userId: string, value: Record<string, unknown> | null): void {
  if (value) pending.set(userId, value);
  else pending.delete(userId);
}

export function forgetPendingPymatgen(): void {
  pending.clear();
}
