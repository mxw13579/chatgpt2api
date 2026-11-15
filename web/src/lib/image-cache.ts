"use client";

import type { ImageReference } from "@/lib/api";
import { isImageUnavailable, r2ReferenceFromUrl } from "@/lib/image-reference";

// 下载是显式读取；图片内容只在调用者内存中使用，不写任何浏览器缓存。
export async function readImageBlob(source: string, reference?: ImageReference): Promise<Blob> {
  reference = reference || r2ReferenceFromUrl(source);
  if (isImageUnavailable(reference)) throw new Error("图片已过期或暂不可访问");
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
