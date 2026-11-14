"use client";

import localforage from "localforage";
import type { ImageReference } from "@/lib/api";
import { isImageUnavailable, isR2Image, r2ReferenceFromUrl } from "@/lib/image-reference";

type CachedImageRecord = {
  version: 1;
  source: string;
  blob: Blob;
  cachedAt: number;
};

export type ResolvedCachedImage = {
  source: string;
  src: string;
  blob: Blob | null;
  fromCache: boolean;
};

const imageCacheStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "image_cache",
});
const memoryCache = new Map<string, { src: string; blob: Blob }>();
const pendingLoads = new Map<string, Promise<ResolvedCachedImage>>();

function isInlineSource(source: string) {
  return source.startsWith("data:") || source.startsWith("blob:");
}

function normalizeSource(source: string) {
  const value = String(source || "").trim();
  if (!value || isInlineSource(value) || typeof window === "undefined") {
    return value;
  }
  try {
    return new URL(value, window.location.origin).href;
  } catch {
    return value;
  }
}

function cacheKey(source: string) {
  return `image-cache:v1:${source}`;
}

function isCachedImageRecord(value: unknown, source: string): value is CachedImageRecord {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<CachedImageRecord>;
  return record.version === 1 && record.source === source && record.blob instanceof Blob && record.blob.size > 0;
}

function rememberBlob(source: string, blob: Blob) {
  const existing = memoryCache.get(source);
  if (existing) return existing.src;
  const src = URL.createObjectURL(blob);
  memoryCache.set(source, { src, blob });
  return src;
}

export function getImmediateCachedImageSource(source: string, reference?: ImageReference): string | null {
  reference = reference || r2ReferenceFromUrl(source);
  if (isImageUnavailable(reference)) return null;
  const normalized = normalizeSource(source);
  // R2 只用当前远程 URL，绝不读取历史 Blob 缓存。
  if (isR2Image(reference)) return normalized || null;
  if (!normalized) return null;
  if (isInlineSource(normalized)) return normalized;
  return memoryCache.get(normalized)?.src || null;
}

export async function resolveCachedImage(source: string, reference?: ImageReference): Promise<ResolvedCachedImage> {
  reference = reference || r2ReferenceFromUrl(source);
  if (isImageUnavailable(reference)) throw new Error("图片已过期或暂不可访问");
  const normalized = normalizeSource(source);
  if (!normalized) {
    throw new Error("图片地址为空");
  }
  if (isR2Image(reference)) {
    return { source: normalized, src: normalized, blob: null, fromCache: false };
  }
  if (isInlineSource(normalized)) {
    return { source: normalized, src: normalized, blob: null, fromCache: true };
  }

  const memory = memoryCache.get(normalized);
  if (memory) {
    return { source: normalized, src: memory.src, blob: memory.blob, fromCache: true };
  }
  const pending = pendingLoads.get(normalized);
  if (pending) return pending;

  const loading = (async () => {
    try {
      const stored = await imageCacheStorage.getItem<CachedImageRecord>(cacheKey(normalized));
      if (isCachedImageRecord(stored, normalized)) {
        return {
          source: normalized,
          src: rememberBlob(normalized, stored.blob),
          blob: stored.blob,
          fromCache: true,
        };
      }
    } catch {
      // IndexedDB 不可用时继续走网络，并保留当前页面的内存缓存。
    }

    const response = await fetch(normalized);
    if (!response.ok) {
      throw new Error(`图片加载失败：HTTP ${response.status}`);
    }
    const blob = await response.blob();
    if (!blob.size) {
      throw new Error("图片加载失败：响应为空");
    }
    const resolved = {
      source: normalized,
      src: rememberBlob(normalized, blob),
      blob,
      fromCache: false,
    };
    try {
      await imageCacheStorage.setItem(cacheKey(normalized), {
        version: 1,
        source: normalized,
        blob,
        cachedAt: Date.now(),
      } satisfies CachedImageRecord);
    } catch {
      // 浏览器存储空间不足时仍返回已下载的内存 Blob。
    }
    return resolved;
  })();

  pendingLoads.set(normalized, loading);
  try {
    return await loading;
  } finally {
    pendingLoads.delete(normalized);
  }
}

// 下载/编辑是显式读取；R2 内容只在调用者内存中使用，不写 IndexedDB 或全局 Blob 缓存。
export async function readImageBlob(source: string, reference?: ImageReference): Promise<Blob> {
  reference = reference || r2ReferenceFromUrl(source);
  if (isImageUnavailable(reference)) throw new Error("图片已过期或暂不可访问");
  if (!isR2Image(reference)) {
    const cached = await resolveCachedImage(source, reference);
    if (cached.blob) return cached.blob;
  }
  const response = await fetch(source, { mode: "cors", credentials: "omit", cache: "no-store", redirect: "error" });
  if (!response.ok) throw new Error(`图片读取失败：HTTP ${response.status}`);
  const blob = await response.blob();
  if (isImageUnavailable(reference)) throw new Error("图片已过期或暂不可访问");
  if (!blob.size) throw new Error("图片读取失败：响应为空");
  return blob;
}

export async function downloadImageSource(source: string, name: string, reference?: ImageReference) {
  const blob = await readImageBlob(source, reference);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  URL.revokeObjectURL(url);
}

export async function deleteCachedImageSources(sources: Array<string | undefined>): Promise<void> {
  const normalizedSources = [...new Set(sources.map((source) => normalizeSource(source || "")).filter(Boolean))];
  await Promise.allSettled(normalizedSources.map((source) => pendingLoads.get(source)));
  await Promise.allSettled(
    normalizedSources.map(async (source) => {
      if (isInlineSource(source)) return;
      const memory = memoryCache.get(source);
      if (memory) {
        URL.revokeObjectURL(memory.src);
        memoryCache.delete(source);
      }
      await imageCacheStorage.removeItem(cacheKey(source));
    }),
  );
}

export async function clearCachedImages(): Promise<void> {
  await Promise.allSettled([...pendingLoads.values()]);
  for (const cached of memoryCache.values()) {
    URL.revokeObjectURL(cached.src);
  }
  memoryCache.clear();
  await imageCacheStorage.clear();
}
