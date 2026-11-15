"use client";

import { cn } from "@/lib/utils";
import type { ImageReference } from "@/lib/api";
import { isImageExpired, isImageUnavailable } from "@/lib/image-reference";
import { useImageExpiry } from "@/hooks/use-image-expiry";

type ImageThumbnailProps = {
  src: string;
  reference?: ImageReference;
  alt?: string;
  className?: string;
  imageClassName?: string;
};

export function ImageThumbnail({ src, reference, alt = "", className, imageClassName }: ImageThumbnailProps) {
  const now = useImageExpiry(reference ? [reference] : []);
  const blocked = isImageUnavailable(reference, now) || !src;

  return (
    <span className={cn("block overflow-hidden bg-stone-100", className)}>
      {blocked ? <span className="flex h-full items-center justify-center p-2 text-center text-xs text-stone-500">{isImageExpired(reference, now) ? "图片已过期" : "暂不可访问"}</span> : <img
        src={src}
        alt={alt}
        className={cn("h-full w-full object-cover", imageClassName)}
        loading="lazy"
        decoding="async"
      />}
    </span>
  );
}
