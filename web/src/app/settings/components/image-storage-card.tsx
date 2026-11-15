"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { LoaderCircle, Pencil, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { ImageStorageSettings, R2Account } from "@/lib/api";
import {
  DEFAULT_IMAGE_STORAGE, cleanupExpiredImages, getImageStorageOverview,
  imageStorageSettingsAffectImages, newR2Account, notifyImageStorageChanged, provisionR2Account, saveImageStorageSettings, testR2Account,
  type CleanupResult, type ImageStorageOverview, type R2AccountSummary, type StorageActionResult,
} from "@/lib/image-storage";
import { useSettingsStore } from "../store";

const inputClass = "h-10 rounded-xl border-stone-200 bg-white";
const dialogClass = "max-h-[90vh] overflow-y-auto";
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
function NumberField({ label, value, onChange, min = 1, hint }: {
  label: string; value: number; onChange: (value: number) => void; min?: number; hint?: string;
}) {
  return <Field label={label} hint={hint}><Input required type="number" min={min} step={1} value={Number.isNaN(value) ? "" : value} onChange={(event) => onChange(event.target.valueAsNumber)} className={inputClass} /></Field>;
}

function AccountCard({ account, runtime, locked, onToggle, onTest, onEdit, onRemove }: {
  account: R2Account; runtime?: R2AccountSummary; locked: boolean;
  onToggle: (enabled: boolean) => void; onTest: () => void; onEdit: () => void; onRemove: () => void;
}) {
  const used = runtime?.monthly_uploads || 0;
  const ratio = used / account.monthly_upload_limit;
  const nearLimit = ratio >= 0.9;
  const hasObjects = (runtime?.image_count || 0) > 0;
  return <div className="flex min-w-0 flex-col gap-4 rounded-xl border border-stone-200 bg-white p-4">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h4 className="truncate font-medium text-stone-900">{account.name || "未命名账号"}</h4>
        {account.public_base_url
          ? <a href={account.public_base_url} target="_blank" rel="noreferrer" className="block truncate text-xs text-stone-500 underline-offset-2 hover:text-stone-900 hover:underline">{account.public_base_url.replace(/^https?:\/\//, "")}</a>
          : <span className="block text-xs text-stone-400">未设置公开域名</span>}
      </div>
      <label className="flex shrink-0 items-center gap-2 text-sm text-stone-700"><Checkbox checked={account.enabled} onCheckedChange={(checked) => onToggle(checked === true)} />启用</label>
    </div>
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 text-xs text-stone-500">
        <span>本月上传</span>
        <span className={nearLimit ? "font-medium text-rose-600" : ""}>{count(used)} / {count(account.monthly_upload_limit)}</span>
      </div>
      <div role="progressbar" aria-label="本月上传用量" aria-valuemin={0} aria-valuemax={account.monthly_upload_limit} aria-valuenow={used} className="h-2 overflow-hidden rounded-full bg-stone-100">
        <div className={`h-full rounded-full ${nearLimit ? "bg-rose-500" : "bg-stone-800"}`} style={{ width: `${Math.min(ratio, 1) * 100}%` }} />
      </div>
      <p className="text-xs text-stone-500">{runtime ? `当前图片 ${count(runtime.image_count)} 张` : "新账号，保存后生效"}</p>
    </div>
    <div className="mt-auto flex flex-wrap gap-2">
      <Button type="button" variant="outline" size="sm" disabled={locked || !runtime} onClick={onTest}>测试连接</Button>
      <Button type="button" variant="outline" size="sm" onClick={onEdit}><Pencil className="size-3.5" />编辑</Button>
      <Button type="button" variant="outline" size="sm" disabled={hasObjects} title={hasObjects ? "仍有图片未清理，不能移除" : undefined} className="text-rose-600" onClick={onRemove}><Trash2 className="size-3.5" />移除</Button>
    </div>
  </div>;
}

function AccountDialog({ account, hasObjects, onSave, onClose }: {
  account: R2Account; hasObjects: boolean; onSave: (account: R2Account) => void; onClose: () => void;
}) {
  const [draft, setDraft] = useState(account);
  const set = (changes: Partial<R2Account>) => setDraft((current) => ({ ...current, ...changes }));
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className={dialogClass}>
      <DialogHeader><DialogTitle>编辑 R2 账号</DialogTitle><DialogDescription>确定后还需点击「保存图片存储」才会生效。</DialogDescription></DialogHeader>
      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); onSave(draft); }}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="名称"><Input className={inputClass} value={draft.name} onChange={(event) => set({ name: event.target.value })} /></Field>
          <Field label="CF Account ID" hint={hasObjects ? "仍有图片未清理，不能更换账号。" : undefined}><Input className={inputClass} disabled={hasObjects} value={draft.account_id} onChange={(event) => set({ account_id: event.target.value.trim() })} /></Field>
          <Field label="桶名" hint="对象前缀固定 images/。"><Input className={inputClass} disabled={hasObjects} value={draft.bucket} onChange={(event) => set({ bucket: event.target.value.trim() })} /></Field>
          <Field label="公开访问地址" hint="桶绑定的自定义域名，如 https://img.example.com"><Input className={inputClass} value={draft.public_base_url} onChange={(event) => set({ public_base_url: event.target.value.trim() })} placeholder="https://" /></Field>
          {([ ["access_key_id", "S3 Access Key ID", draft.has_access_key_id], ["secret_access_key", "S3 Secret Access Key", draft.has_secret_access_key] ] as const).map(([key, label, stored]) =>
            <Field key={key} label={label} hint={stored ? "已配置；留空保留，可输入新值轮换。" : undefined}><Input type="password" autoComplete="new-password" className={inputClass} value={draft[key]} onChange={(event) => set({ [key]: event.target.value })} placeholder={stored ? "留空保留已存凭据" : "输入凭据"} /></Field>)}
          <NumberField label="每月上传上限" value={draft.monthly_upload_limit} onChange={(monthly_upload_limit) => set({ monthly_upload_limit })} hint="免费额度为 100 万次 A 类操作/月，默认留 10% 余量。按 UTC 自然月重置。" />
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>取消</Button>
          <Button type="submit" className="rounded-xl bg-stone-950 text-white">确定</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}

function AddAccountDialog({ token, domain, setToken, setDomain, blocked, onProvision, onManual, onClose }: {
  token: string; domain: string; setToken: (value: string) => void; setDomain: (value: string) => void;
  blocked: string; onProvision: () => void; onManual: () => void; onClose: () => void;
}) {
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className={dialogClass}>
      <DialogHeader><DialogTitle>添加 R2 账号</DialogTitle><DialogDescription>一键开通会自动建桶并绑定域名；也可以手动填写已有的桶。</DialogDescription></DialogHeader>
      <form className="space-y-4 rounded-xl border border-stone-200 p-4" onSubmit={(event) => { event.preventDefault(); onProvision(); }}>
        <h4 className="text-sm font-medium">一键开通</h4>
        <Field label="Cloudflare API Token" hint="权限：Workers R2 Storage 编辑、Zone 读取、DNS 编辑；用户 Token 和账号 Token 均可。仅本次使用，不保存原文。"><Input required type="password" autoComplete="new-password" className={inputClass} value={token} onChange={(event) => setToken(event.target.value.trim())} /></Field>
        <Field label="图片域名" hint="域名所在站点需已接入该 Cloudflare 账号。"><Input required className={inputClass} value={domain} onChange={(event) => setDomain(event.target.value.trim())} placeholder="img.example.com" /></Field>
        {blocked ? <p className="text-xs text-amber-700">{blocked}</p> : null}
        <Button type="submit" disabled={Boolean(blocked)} className="rounded-xl bg-stone-950 text-white">一键开通</Button>
      </form>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-stone-200 p-4">
        <div><h4 className="text-sm font-medium">手动填写</h4><p className="mt-1 text-xs text-stone-500">已有桶和 S3 凭据时使用。</p></div>
        <Button type="button" variant="outline" onClick={onManual}>手动填写</Button>
      </div>
    </DialogContent>
  </Dialog>;
}

export function ImageStorageCard() {
  const [overview, setOverview] = useState<ImageStorageOverview | null>(null);
  const [settings, setSettings] = useState<ImageStorageSettings | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [editing, setEditing] = useState<R2Account | null>(null);
  const [adding, setAdding] = useState(false);
  const [cfToken, setCfToken] = useState("");
  const [cfDomain, setCfDomain] = useState("");
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

  const run = async (label: string, action: () => Promise<string | void>, changesImages = false) => {
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
  const runtimeOf = (id: string) => overview?.accounts.find((item) => item.id === id);
  return <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm"><CardContent className="space-y-5 p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">图片存储</h2><p className="mt-1 text-xs leading-6 text-stone-500">Base64 响应不保存；URL 响应上传到本月已用比例最低的启用账号；全部账号达到上限时 URL 请求失败。</p></div>
      <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void refreshPage()}><RefreshCw className="size-4" />重新读取</Button>
    </div>
    {busy ? <p role="status" className="flex items-center gap-2 text-sm text-stone-500"><LoaderCircle className="size-4 animate-spin" />{busy}…</p> : null}
    {error ? <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
    {result ? <p role="status" className="rounded-xl border border-stone-200 bg-stone-50 p-3 text-sm text-stone-700">{result}</p> : null}
    {settings ? <>
      <form ref={formRef} onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <fieldset disabled={Boolean(busy)} className="min-w-0 space-y-5">
          <h3 className="font-medium">R2 账号 · {settings.r2_accounts.length} 个</h3>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {settings.r2_accounts.map((account) => <AccountCard key={account.id} account={account} runtime={runtimeOf(account.id)} locked={locked}
              onToggle={(enabled) => patch({ r2_accounts: settings.r2_accounts.map((item) => item.id === account.id ? { ...item, enabled } : item) })}
              onTest={() => void run("测试连接", async () => { const { result } = await testR2Account(account.id); requireOK(result); return "连接成功（计 1 次 A 类操作）"; })}
              onEdit={() => setEditing(account)}
              onRemove={() => { if (window.confirm("从配置中移除此账号？保存后生效。")) patch({ r2_accounts: settings.r2_accounts.filter((item) => item.id !== account.id) }); }} />)}
            <button type="button" onClick={() => setAdding(true)} className="flex min-h-36 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-stone-300 p-4 text-sm text-stone-500 transition-colors hover:border-stone-400 hover:bg-stone-50 hover:text-stone-900 disabled:pointer-events-none disabled:opacity-50">
              <Plus className="size-5" />添加账号
            </button>
          </div>
          <div className="space-y-3 rounded-xl border border-stone-200 p-4">
            <h3 className="font-medium">全局设置</h3>
            <div className="grid gap-4 md:grid-cols-3">
              <NumberField label="新图片保留（分钟）" value={settings.retention_minutes} onChange={(retention_minutes) => patch({ retention_minutes })} hint="不改变旧图已记录的到期时间。" />
              <NumberField label="过期清理间隔（秒）" value={settings.cleanup_interval_seconds} onChange={(cleanup_interval_seconds) => patch({ cleanup_interval_seconds })} />
              <NumberField label="CDN 缓存 TTL（秒）" min={0} value={settings.cache_ttl_seconds} onChange={(cache_ttl_seconds) => patch({ cache_ttl_seconds })} hint="写入对象的 Cache-Control s-maxage。" />
            </div>
            <Button type="button" variant="outline" disabled={locked} onClick={() => void run("清理到期图片", async () => cleanupSummary((await cleanupExpiredImages()).result), true)}>立即清理到期图片</Button>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-3"><span className="text-xs text-stone-500">{dirty ? "有未保存修改：保存后才能测试连接、一键开通或清理。" : "当前配置已读取。"}</span><Button type="submit" disabled={!dirty || Boolean(busy)} className="rounded-xl bg-stone-950 text-white"><Save className="size-4" />保存图片存储</Button></div>
        </fieldset>
      </form>
      {/* 弹窗放在表单之外：React 事件会穿过 portal 冒泡，否则弹窗内提交会触发整页保存。 */}
      {editing ? <AccountDialog key={editing.id} account={editing} hasObjects={(runtimeOf(editing.id)?.image_count || 0) > 0} onClose={() => setEditing(null)}
        onSave={(account) => {
          const exists = settings.r2_accounts.some((item) => item.id === account.id);
          patch({ r2_accounts: exists ? settings.r2_accounts.map((item) => item.id === account.id ? account : item) : [...settings.r2_accounts, account] });
          setEditing(null);
        }} /> : null}
      {adding ? <AddAccountDialog token={cfToken} domain={cfDomain} setToken={setCfToken} setDomain={setCfDomain} blocked={dirty ? "有未保存修改：请先保存图片存储，再一键开通。" : ""} onClose={() => setAdding(false)}
        onManual={() => { setAdding(false); setEditing(newR2Account()); }}
        onProvision={() => {
          setAdding(false);
          // 失败时保留输入，重新打开弹窗即可修正后重试。
          void run("一键开通", async () => { await provisionR2Account(cfToken, cfDomain); setCfToken(""); setCfDomain(""); return "已建桶、绑定域名并添加账号；域名证书生效需几分钟，之后可测试连接"; }, true);
        }} /> : null}
    </> : null}
  </CardContent></Card>;
}
