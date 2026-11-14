"use client";

import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/utils";
import type { ImageReference } from "@/lib/api";
import { isImageExpired, isImageUnavailable, isR2Image } from "@/lib/image-reference";
import { useImageExpiry } from "@/hooks/use-image-expiry";

type ImageThumbnailProps = {
  src: string;
  thumbnailSrc?: string;
  reference?: ImageReference;
  alt?: string;
  className?: string;
  imageClassName?: string;
};

export function getImageThumbnailUrl(src: string) {
  // 自定义域名没有应用的缩略图路由，不改写远端 URL。
  if (/^(https?:)?\/\//i.test(src)) {
    if (typeof window === "undefined") return src;
    try { if (new URL(src, window.location.origin).origin !== window.location.origin) return src; }
    catch { return src; }
  }
  const marker = "/images/";
  const index = src.indexOf(marker);
  if (index < 0) return src;
  return `${src.slice(0, index)}/image-thumbnails/${src.slice(index + marker.length)}`;
}

export function ImageThumbnail({ src, thumbnailSrc, reference, alt = "", className, imageClassName }: ImageThumbnailProps) {
  const now = useImageExpiry(reference ? [reference] : []);
  const r2 = isR2Image(reference);
  const blocked = isImageUnavailable(reference, now) || !src;
  const initialSrc = useMemo(() => r2 ? src : thumbnailSrc || getImageThumbnailUrl(src), [src, thumbnailSrc, r2]);
  const [currentSrc, setCurrentSrc] = useState(initialSrc);

  useEffect(() => {
    setCurrentSrc(initialSrc);
  }, [initialSrc]);

  return (
    <span className={cn("block overflow-hidden bg-stone-100", className)}>
      {blocked ? <span className="flex h-full items-center justify-center p-2 text-center text-xs text-stone-500">{isImageExpired(reference, now) ? "图片已过期" : "暂不可访问"}</span> : <img
        src={r2 ? src : currentSrc}
        alt={alt}
        className={cn("h-full w-full object-cover", imageClassName)}
        loading="lazy"
        decoding="async"
        onError={() => {
          if (!r2 && currentSrc !== src) {
            setCurrentSrc(src);
          }
        }}
      />}
    </span>
  );
}
