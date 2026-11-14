import type { ImageReference } from "@/lib/api";

export function isR2Image(image?: ImageReference): boolean {
  return Boolean(image && (image.image_id || image.storage === "r2"));
}

export function isImageExpired(image?: ImageReference, now = Date.now()): boolean {
  if (!image) return false;
  if (image.expired) return true;
  if (!image.expires_at) return false;
  const expires = Date.parse(image.expires_at);
  // 无效期限不能视作永久有效。
  return !Number.isFinite(expires) || expires <= now;
}

export function isImageUnavailable(image?: ImageReference, now = Date.now()): boolean {
  return Boolean(image?.unavailable) || isImageExpired(image, now);
}

// 日志只有公共 URL；仅识别冻结契约的 R2 key 格式，再由鉴权解析接口确认。
// 不把任意外部图片地址交给服务端下载，也不把它当作可永久缓存的本地图。
export function r2ReferenceFromUrl(value: string): ImageReference | undefined {
  let path: string;
  try { path = new URL(value, "https://app.invalid").pathname; } catch { return undefined; }
  if (!/^\/images\/\d{4}\/\d{2}\/\d{2}\/[a-f0-9-]{32,36}\.[a-z0-9]+$/i.test(path)) return undefined;
  return { image_id: path.slice(1), storage: "r2" };
}

export function imageReference(image: ImageReference): ImageReference {
  return {
    image_id: image.image_id, account_id: image.account_id, expires_at: image.expires_at,
    expired: image.expired, unavailable: image.unavailable, storage: image.storage,
  };
}
