import { httpRequest } from "@/lib/request";
import type { ImageReference, ImageStorageSettings, R2Account, R2Domain } from "@/lib/api";

export const DEFAULT_IMAGE_STORAGE: ImageStorageSettings = {
  enabled: false, mode: "local", webdav_url: "", webdav_username: "", webdav_password: "",
  webdav_root_path: "chatgpt2api/images", public_base_url: "",
  retention_minutes: 60, cleanup_interval_seconds: 60, usage_refresh_seconds: 300,
  usage_max_age_seconds: 900, cache_ttl_seconds: 60, cors_origins: ["*"], r2_accounts: [],
};

// 预算、保留期和 R2 分配/防护参数保存不改变现有图片 URL 或停读状态；
// 域名与公开访问变更由各自的显式动作通知。旧 WebDAV 图片仍依赖连接配置。
export function imageStorageSettingsAffectImages(previous: ImageStorageSettings, next: ImageStorageSettings): boolean {
  return (["mode", "enabled", "public_base_url", "webdav_url", "webdav_username", "webdav_password", "webdav_root_path"] as const)
    .some((key) => previous[key] !== next[key]);
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
    access_key_id: "", secret_access_key: "", api_token: "", capacity_bytes: 8_000_000_000,
    a_budget: 800_000, b_warn: 7_000_000, b_budget: 8_000_000,
    billing_cycle_day: 1, billing_cycle_confirmed: false, active_domain_id: "", domains: [],
    rate_limit_requests: 120, restrict_methods: true,
  };
}

export type DomainCheck = {
  domain_id: string; ok: boolean; error?: string; checked_at?: string;
  source: "server"; sample_url?: string;
};
export type RuntimeDomain = R2Domain & {
  status?: string | { ownership?: string; ssl?: string };
  protected?: boolean;
  check?: DomainCheck;
  sample_url?: string;
};
export type R2Usage = {
  local_bytes?: number; pending_bytes?: number; image_count?: number;
  cloud_a?: number; cloud_b?: number; a_local?: number;
  cloud_bytes?: number; cloud_bucket_bytes?: number;
  cloud_bucket_a?: number; cloud_bucket_b?: number; cloud_object_count?: number;
  fetched_at?: string; period_start?: string; period_end?: string;
  complete?: boolean; error?: string; access_paused?: boolean;
  protection_error?: string; shutdown_error?: string; restore_error?: string; maintenance_error?: string;
  managed_disabled?: boolean; domains_checked?: boolean; domains_safe?: boolean;
  verified_domains?: string[];
  [key: string]: unknown;
};
export type R2AccountRuntime = { id: string; status: string; usage: R2Usage; domains: RuntimeDomain[] };
export type ImageStorageOverview = {
  settings: ImageStorageSettings; accounts: R2AccountRuntime[];
  legacy?: Record<string, unknown>;
};
export type StorageActionResult = { ok: boolean; error?: string; status?: number };
export type CleanupResult = { removed?: number; failed?: number; pending?: number; [key: string]: unknown };
const root = "/api/image-storage";
const accountPath = (id: string) => `${root}/accounts/${encodeURIComponent(id)}`;
const domainPath = (id: string, domain: string) => `${accountPath(id)}/domains/${encodeURIComponent(domain)}`;

export const getImageStorageOverview = () => httpRequest<ImageStorageOverview>(root);
export const saveImageStorageSettings = (body: ImageStorageSettings) =>
  httpRequest<ImageStorageOverview>(root, { method: "PUT", body: { ...body, enabled: body.mode === "r2" || body.enabled } });
export const testR2Account = (id: string) =>
  httpRequest<{ result: StorageActionResult }>(`${accountPath(id)}/test`, { method: "POST", body: {} });
export const refreshR2Usage = (id: string) =>
  httpRequest<{ result: R2Usage }>(`${accountPath(id)}/usage/refresh`, { method: "POST", body: {} });
export const getR2Zones = (id: string) => httpRequest<{ items: Array<{ id: string; name: string }> }>(`${accountPath(id)}/zones`);
export const getR2Domains = (id: string) => httpRequest<{ items: RuntimeDomain[]; managed_enabled: boolean }>(`${accountPath(id)}/domains`);
export const addR2Domain = (id: string, domain: string, zone_id: string) =>
  httpRequest<{ item: RuntimeDomain }>(`${accountPath(id)}/domains`, { method: "POST", body: { domain, zone_id } });
export const updateR2Domain = (id: string, domain: string, body: { active?: boolean; enabled?: boolean }) =>
  httpRequest<{ item: RuntimeDomain }>(domainPath(id, domain), { method: "PATCH", body });
export const deleteR2Domain = (id: string, domain: string) =>
  httpRequest<{ ok: boolean }>(domainPath(id, domain), { method: "DELETE" });
export const checkR2Domains = (id: string, domain_id?: string) =>
  httpRequest<{ items: DomainCheck[] }>(`${accountPath(id)}/domains/check`, { method: "POST", body: { domain_id } });
export const applyR2Protection = (id: string, body: { rate_limit_requests: number; restrict_methods: boolean; cors_origins: string[] }) =>
  httpRequest<{ result: StorageActionResult }>(`${accountPath(id)}/protection`, { method: "PUT", body });
export const restoreR2Access = (id: string) =>
  httpRequest<{ result: StorageActionResult }>(`${accountPath(id)}/access/restore`, { method: "POST", body: {} });
export const cleanupExpiredImages = () => httpRequest<{ result: CleanupResult }>(`${root}/cleanup`, { method: "POST", body: {} });
export const cleanupLegacyImages = () => httpRequest<{ result: CleanupResult }>(`${root}/legacy/cleanup`, { method: "POST", body: { confirm: true } });
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
