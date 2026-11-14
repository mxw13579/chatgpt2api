"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { LoaderCircle, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { syncImageStorage, testImageStorageConnection, type ImageStorageMode, type ImageStorageSettings, type R2Account } from "@/lib/api";
import {
  DEFAULT_IMAGE_STORAGE, cleanupExpiredImages, cleanupLegacyImages, getImageStorageOverview,
  imageStorageSettingsAffectImages, newR2Account, notifyImageStorageChanged, saveImageStorageSettings, testR2Account,
  type CleanupResult, type ImageStorageOverview, type StorageActionResult,
} from "@/lib/image-storage";
import { useSettingsStore } from "../store";

const inputClass = "h-10 rounded-xl border-stone-200 bg-white";
const count = (value: number) => value.toLocaleString("zh-CN");
function requireOK(result: StorageActionResult) {
  if (!result.ok) throw new Error(result.error || "操作未完成，请检查账号凭据和桶名");
}
function cleanupSummary(result: CleanupResult) {
  return Object.entries(result).filter(([, value]) => typeof value === "number")
    .map(([key, value]) => `${({ removed: "已清理", failed: "失败", pending: "待处理" } as Record<string, string>)[key] || key}: ${value}`).join("，") || "请刷新查看清理状态";
}
function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="block space-y-2 text-sm text-stone-700"><span>{label}</span>{children}{hint ? <span className="block text-xs leading-5 text-stone-500">{hint}</span> : null}</label>;
}
function NumberField({ label, value, onChange, min = 1, max, step = 1, hint }: {
  label: string; value: number; onChange: (value: number) => void; min?: number; max?: number; step?: number; hint?: string;
}) {
  return <Field label={label} hint={hint}><Input required type="number" min={min} max={max} step={step} value={Number.isNaN(value) ? "" : value} onChange={(event) => onChange(event.target.valueAsNumber)} className={inputClass} /></Field>;
}

type RunAction = (label: string, action: () => Promise<string | void>, changesImages?: boolean) => Promise<void>;

export function ImageStorageCard() {
  const [overview, setOverview] = useState<ImageStorageOverview | null>(null);
  const [settings, setSettings] = useState<ImageStorageSettings | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const formRef = useRef<HTMLFormElement>(null);

  const accept = useCallback((data: ImageStorageOverview) => {
    const next = { ...DEFAULT_IMAGE_STORAGE, ...data.settings };
    setOverview(data);
    setSettings(next);
    setDirty(false);
    // 同步显示快照，不重新加载或丢弃其他设置页尚未保存的编辑。
    useSettingsStore.setState((state) => state.config ? { config: { ...state.config, image_storage: next } } : {});
  }, []);
  const reload = useCallback(async () => accept(await getImageStorageOverview()), [accept]);
  useEffect(() => {
    setBusy("加载图片存储");
    void reload().catch(() => setError("加载图片存储失败，请重试")).finally(() => setBusy(""));
  }, [reload]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const run: RunAction = async (label, action, changesImages = false) => {
    setBusy(label); setError(""); setResult("");
    try {
      const message = await action();
      setResult(message || `${label}已完成`);
      toast.success(message || `${label}已完成`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : `${label}失败`;
      setError(message); toast.error(message);
    } finally {
      // 失败也读取最新状态；变更可能部分生效，即使读取失败仍通知图片视图。
      try { await reload(); }
      catch { setError((previous) => `${previous ? `${previous}；` : ""}状态刷新失败，请重新读取后再操作`); }
      if (changesImages) notifyImageStorageChanged();
      setBusy("");
    }
  };
  const patch = (changes: Partial<ImageStorageSettings>) => {
    setSettings((current) => current ? { ...current, ...changes } : current); setDirty(true);
  };
  const patchAccount = (id: string, changes: Partial<R2Account>) => {
    if (settings) patch({ r2_accounts: settings.r2_accounts.map((account) => account.id === id ? { ...account, ...changes } : account) });
  };
  const save = async () => {
    if (!settings || !formRef.current?.reportValidity()) return;
    setBusy("保存"); setError(""); setResult("");
    let changesImages = false;
    try {
      const latest = await getImageStorageOverview();
      changesImages = imageStorageSettingsAffectImages(latest.settings, settings);
      accept(await saveImageStorageSettings(settings));
      setResult("图片存储配置已保存"); toast.success("图片存储配置已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      // 请求失败不代表配置未提交；只通知可能影响现有图片的保存尝试。
      if (changesImages) notifyImageStorageChanged();
      setBusy("");
    }
  };
  const refreshPage = async () => {
    if (dirty && !window.confirm("放弃未保存的图片存储修改并重新读取？密钥输入不会保留。")) return;
    setBusy("重新读取"); setError("");
    try { await reload(); } catch { setError("读取图片存储失败"); } finally { setBusy(""); }
  };
  const locked = Boolean(busy) || dirty;
  return <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm"><CardContent className="space-y-5 p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">图片存储</h2><p className="mt-1 text-xs leading-6 text-stone-500">独立保存，不受基础设置覆盖。保存后刷新/关闭页面可恢复配置和运行状态；未保存编辑仅在当前页内存中。</p></div>
      <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void refreshPage()}><RefreshCw className="size-4" />重新读取</Button>
    </div>
    {busy ? <p role="status" className="flex items-center gap-2 text-sm text-stone-500"><LoaderCircle className="size-4 animate-spin" />{busy}…</p> : null}
    {error ? <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
    {result ? <p role="status" className="rounded-xl border border-stone-200 bg-stone-50 p-3 text-sm text-stone-700">{result}</p> : null}
    {settings ? <>
      <form ref={formRef} onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <fieldset disabled={Boolean(busy)} className="space-y-5">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="存储模式"><Select value={settings.mode} onValueChange={(mode) => patch({ mode: mode as ImageStorageMode, enabled: mode !== "local" })}><SelectTrigger className={inputClass}><SelectValue /></SelectTrigger><SelectContent>
              <SelectItem value="local">仅本机</SelectItem><SelectItem value="webdav">仅 WebDAV</SelectItem><SelectItem value="both">本机 + WebDAV</SelectItem><SelectItem value="r2">多账号 R2（仅元数据落盘）</SelectItem>
            </SelectContent></Select></Field>
            <NumberField label="R2 新图片保留（分钟）" value={settings.retention_minutes} onChange={(retention_minutes) => patch({ retention_minutes })} hint="不改变旧图已记录的到期时间。" />
            <NumberField label="R2 过期清理间隔（秒）" value={settings.cleanup_interval_seconds} onChange={(cleanup_interval_seconds) => patch({ cleanup_interval_seconds })} />
            <NumberField label="对象 CDN 缓存 TTL（秒）" min={0} value={settings.cache_ttl_seconds} onChange={(cache_ttl_seconds) => patch({ cache_ttl_seconds })} hint="写入对象的 Cache-Control s-maxage。" />
          </div>
          <p className="rounded-xl border border-stone-200 bg-stone-50 p-3 text-xs leading-6 text-stone-600">Base64 请求不保存到服务器、不上传 R2。R2 模式按「本月已用比例最低」选择启用的账号，每张图计 1 次上传（A 类操作）；全部账号达到上限时新请求失败，不回退本地/WebDAV。自定义域名、关闭 r2.dev、限速规则请在 Cloudflare 后台配置。</p>
          {settings.mode === "webdav" || settings.mode === "both" ? <div className="space-y-3 rounded-xl border border-stone-200 p-4">
            <h3 className="font-medium">WebDAV</h3><div className="grid gap-4 md:grid-cols-2">
              {([ ["webdav_url", "WebDAV URL"], ["webdav_username", "用户名"], ["webdav_password", "密码"], ["webdav_root_path", "远端目录"], ["public_base_url", "公开访问前缀（可留空）"] ] as const).map(([key, label]) => <Field key={key} label={label}><Input className={inputClass} type={key === "webdav_password" ? "password" : "text"} value={settings[key]} onChange={(event) => patch({ [key]: event.target.value })} autoComplete="off" /></Field>)}
            </div><p className="text-xs text-stone-500">先保存，再测试或将现有本地图补传至 WebDAV；公开前缀留空使用应用访问地址。</p>
            <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={locked} onClick={() => void run("测试 WebDAV", async () => { const { result } = await testImageStorageConnection(); requireOK(result); })}>测试 WebDAV</Button>
              <Button type="button" variant="outline" disabled={locked} onClick={() => { if (window.confirm("将现有本地图全量同步到 WebDAV？")) void run("同步 WebDAV", async () => { const { result } = await syncImageStorage(); return `上传 ${result.uploaded}，跳过 ${result.skipped}，失败 ${result.failed}`; }, true); }}>全量同步</Button></div>
          </div> : null}
          <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">R2 账号 · {settings.r2_accounts.length} 个</h3>
            <Button type="button" variant="outline" onClick={() => patch({ r2_accounts: [...settings.r2_accounts, newR2Account()] })}><Plus className="size-4" />添加账号</Button></div>
          {settings.r2_accounts.map((account) => {
            const runtime = overview?.accounts.find((item) => item.id === account.id);
            const hasObjects = (runtime?.image_count || 0) > 0;
            return <div key={account.id} className="space-y-4 rounded-xl border border-stone-200 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
                <span className="font-medium">{account.name || "未命名账号"}</span>
                <span className="text-xs text-stone-500">{runtime ? `本月上传 ${count(runtime.monthly_uploads)} / ${count(account.monthly_upload_limit)} · 当前图片 ${count(runtime.image_count)}` : "未保存"}</span>
              </div>
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                <Field label="名称"><Input className={inputClass} value={account.name} onChange={(event) => patchAccount(account.id, { name: event.target.value })} /></Field>
                <Field label="CF Account ID" hint={hasObjects ? "仍有图片未清理，不能更换账号。" : undefined}><Input className={inputClass} disabled={hasObjects} value={account.account_id} onChange={(event) => patchAccount(account.id, { account_id: event.target.value.trim() })} /></Field>
                <Field label="桶名" hint="对象前缀固定 images/。"><Input className={inputClass} disabled={hasObjects} value={account.bucket} onChange={(event) => patchAccount(account.id, { bucket: event.target.value.trim() })} /></Field>
                {([ ["access_key_id", "S3 Access Key ID", account.has_access_key_id], ["secret_access_key", "S3 Secret Access Key", account.has_secret_access_key] ] as const).map(([key, label, stored]) => <Field key={key} label={label} hint={stored ? "已配置；留空保留，可输入新值轮换。" : undefined}><Input type="password" autoComplete="new-password" className={inputClass} value={account[key]} onChange={(event) => patchAccount(account.id, { [key]: event.target.value })} placeholder={stored ? "留空保留已存凭据" : "输入凭据"} /></Field>)}
                <Field label="公开访问地址" hint="桶绑定的自定义域名，如 https://img.example.com"><Input className={inputClass} value={account.public_base_url} onChange={(event) => patchAccount(account.id, { public_base_url: event.target.value.trim() })} placeholder="https://" /></Field>
                <NumberField label="每月上传上限" value={account.monthly_upload_limit} onChange={(monthly_upload_limit) => patchAccount(account.id, { monthly_upload_limit })} hint="免费额度为 100 万次 A 类操作/月，默认留 10% 余量。按 UTC 自然月重置。" />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm text-stone-700"><Checkbox checked={account.enabled} onCheckedChange={(checked) => patchAccount(account.id, { enabled: checked === true })} />启用（参与新图片分配）</label>
                <Button type="button" variant="outline" disabled={locked || !runtime} onClick={() => void run("测试连接", async () => { const { result } = await testR2Account(account.id); requireOK(result); return "连接成功（计 1 次 A 类操作）"; })}>测试连接</Button>
                <Button type="button" variant="outline" disabled={hasObjects} className="text-rose-600" onClick={() => { if (window.confirm("从配置中移除此账号？保存后生效。")) patch({ r2_accounts: settings.r2_accounts.filter((item) => item.id !== account.id) }); }}><Trash2 className="size-4" />移除</Button>
              </div>
            </div>;
          })}
          <div className="flex flex-wrap items-center justify-end gap-3"><span className="text-xs text-stone-500">{dirty ? "有未保存修改：先保存再测试连接。" : "当前配置已读取。"}</span><Button type="submit" disabled={!dirty || Boolean(busy)} className="rounded-xl bg-stone-950 text-white"><Save className="size-4" />保存图片存储</Button></div>
        </fieldset>
      </form>
      <div className="space-y-3 border-t border-stone-200 pt-4">
        {overview?.legacy ? <p className="text-xs text-stone-500">旧数据统计：{Object.entries(overview.legacy).filter(([, value]) => typeof value === "number").map(([key, value]) => `${key}: ${value}`).join(" · ") || "请按需执行显式清理"}</p> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={locked} onClick={() => void run("清理到期图片", async () => cleanupSummary((await cleanupExpiredImages()).result), true)}>立即清理到期图片</Button>
          <Button type="button" variant="outline" disabled={locked} className="border-rose-200 text-rose-600" onClick={() => { if (window.confirm("不可恢复：清除旧本地原图、缩略图和旧任务内嵌 Base64。不会迁移、不会删除 R2 对象或远端 WebDAV 图片，也不会清除用户已下载的副本。确认继续？")) void run("清理旧本地图", async () => cleanupSummary((await cleanupLegacyImages()).result), true); }}>显式清理旧本地图 / 任务图片内容</Button>
        </div>
      </div>
    </> : null}
  </CardContent></Card>;
}
