"use client";

import { useEffect, useState } from "react";
import type { ImageReference } from "@/lib/api";

// 只在最近到期时唤醒 UI；页面重新可见时也检查，不发图片或健康探测请求。
export function useImageExpiry(images: ImageReference[]) {
  const [now, setNow] = useState(() => Date.now());
  const deadline = Math.min(...images.map((image) => Date.parse(image.expires_at || ""))
    .filter((expires) => Number.isFinite(expires) && expires > now));
  useEffect(() => {
    const update = () => setNow(Date.now());
    const timer = Number.isFinite(deadline)
      ? window.setTimeout(update, Math.min(2_147_483_647, Math.max(0, deadline - Date.now() + 1)))
      : undefined;
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [deadline, now]);
  return Math.max(now, Date.now());
}
