import { httpRequest } from "@/lib/request";
import type { ImageReference, ImageStorageSettings, R2Account } from "@/lib/api";

export const DEFAULT_IMAGE_STORAGE: ImageStorageSettings = {
  retention_minutes: 60, cleanup_interval_seconds: 60, cache_ttl_seconds: 60, r2_accounts: [],
};

// 保留期、启用状态和上传上限不改变现有图片 URL；只有账号及其公开访问地址变化才影响已有图片。
export function imageStorageSettingsAffectImages(previous: ImageStorageSettings, next: ImageStorageSettings): boolean {
  return JSON.stringify(previous.r2_accounts.map((item) => [item.id, item.public_base_url]))
    !== JSON.stringify(next.r2_accounts.map((item) => [item.id, item.public_base_url]));
}

export function newR2Account(): R2Account {
  // getRandomValues also works over HTTP, unlike secure-context-only randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  return {
    id, name: "新 R2 账号", enabled: false, account_id: "", bucket: "",
    access_key_id: "", secret_access_key: "", public_base_url: "", monthly_upload_limit: 900_000,
  };
}

export type R2AccountSummary = { id: string; month: string; monthly_uploads: number; image_count: number };
export type ImageStorageOverview = { settings: ImageStorageSettings; accounts: R2AccountSummary[] };
export type StorageActionResult = { ok: boolean; error?: string; status?: number };
export type CleanupResult = { removed?: number; failed?: number; pending?: number; [key: string]: unknown };
const root = "/api/image-storage";
const accountPath = (id: string) => `${root}/accounts/${encodeURIComponent(id)}`;

export const getImageStorageOverview = () => httpRequest<ImageStorageOverview>(root);
export const saveImageStorageSettings = (body: ImageStorageSettings) =>
  httpRequest<ImageStorageOverview>(root, { method: "PUT", body });
export const testR2Account = (id: string) =>
  httpRequest<{ result: StorageActionResult }>(`${accountPath(id)}/test`, { method: "POST", body: {} });
export const provisionR2Account = (api_token: string, domain: string) =>
  httpRequest<ImageStorageOverview>(`${root}/accounts/provision`, { method: "POST", body: { api_token, domain } });
export const cleanupExpiredImages = () => httpRequest<{ result: CleanupResult }>(`${root}/cleanup`, { method: "POST", body: {} });
export const resolveImageReferences = (images: ImageReference[]) =>
  httpRequest<{ items: Array<ImageReference & { url: string }> }>(`${root}/resolve`, {
    method: "POST", body: { images: images.map(({ image_id, account_id }) => ({ image_id, account_id })) },
  });

export const IMAGE_STORAGE_CHANGED = "image-storage-updated";
export function notifyImageStorageChanged() {
  window.dispatchEvent(new Event(IMAGE_STORAGE_CHANGED));
  // 仅跨标签通知时间戳，不保存配置或密钥。
  try { window.localStorage.setItem(IMAGE_STORAGE_CHANGED, String(Date.now())); }
  catch { /* 浏览器禁用存储时不影响已完成的保存；其他标签在重新聚焦时刷新。 */ }
}
