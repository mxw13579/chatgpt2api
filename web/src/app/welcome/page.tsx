"use client";

import { Card, CardContent } from "@/components/ui/card";
import webConfig from "@/constants/common-env";
import { useAuthGuard } from "@/lib/use-auth-guard";

export default function WelcomePage() {
  const { isCheckingAuth, session } = useAuthGuard();
  if (isCheckingAuth || !session) {
    return null;
  }

  return (
    <div className="grid min-h-[calc(100vh-5rem)] w-full place-items-center px-4 py-6">
      <Card className="w-full max-w-[505px] rounded-[30px] border-white/80 bg-white/95 shadow-[0_28px_90px_rgba(28,25,23,0.10)]">
        <CardContent className="space-y-4 p-6 text-center sm:p-8">
          <h1 className="text-2xl font-semibold tracking-tight text-stone-950">请通过 API 使用</h1>
          <p className="text-sm leading-6 text-stone-500">
            当前账号没有网页功能，只能通过 API 使用本服务。请使用登录密钥作为 API Key，接口地址为
          </p>
          <code className="inline-block rounded-xl bg-stone-100 px-3 py-2 font-mono text-sm text-stone-800">{webConfig.apiUrl.replace(/\/$/, "") || window.location.origin}/v1</code>
        </CardContent>
      </Card>
    </div>
  );
}
