"use client";

import { useEffect, useState } from "react";

import { getImmediateCachedImageSource, resolveCachedImage } from "@/lib/image-cache";
import { isImageUnavailable, isR2Image } from "@/lib/image-reference";
import type { ImageReference } from "@/lib/api";
import { useImageExpiry } from "@/hooks/use-image-expiry";

type CachedSourceState = {
  source: string;
  src: string | null;
};

export function useCachedImageSource(source: string, enabled = true, reference?: ImageReference) {
  const now = useImageExpiry(reference ? [reference] : []);
  const blocked = isImageUnavailable(reference, now);
  const r2 = isR2Image(reference);
  const [state, setState] = useState<CachedSourceState>(() => ({
    source,
    src: r2 || blocked ? null : getImmediateCachedImageSource(source),
  }));
  const currentSrc = state.source === source ? state.src : getImmediateCachedImageSource(source);

  useEffect(() => {
    if (r2 || blocked || !enabled) return;
    const immediate = getImmediateCachedImageSource(source);
    setState({ source, src: immediate });
    if (!source || immediate) return;

    let active = true;
    void resolveCachedImage(source)
      .then((resolved) => {
        if (active) setState({ source, src: resolved.src });
      })
      .catch(() => {
        // 跨域图片无法由脚本缓存时回退到浏览器原生图片加载。
        if (active) setState({ source, src: source });
      });
    return () => {
      active = false;
    };
  }, [enabled, source, r2, blocked]);

  if (!enabled || blocked) return null;
  return r2 ? source || null : currentSrc;
}
