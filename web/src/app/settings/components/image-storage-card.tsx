"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { LoaderCircle, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { syncImageStorage, testImageStorageConnection, type ImageStorageMode, type ImageStorageSettings, type R2Account } from "@/lib/api";
import {
  DEFAULT_IMAGE_STORAGE, addR2Domain, applyR2Protection, checkR2Domains, cleanupExpiredImages,
  cleanupLegacyImages, deleteR2Domain, getImageStorageOverview, getR2Domains, getR2Zones,
  newR2Account, notifyImageStorageChanged, refreshR2Usage, restoreR2Access,
  saveImageStorageSettings, testR2Account, updateR2Domain,
  type CleanupResult, type DomainCheck, type ImageStorageOverview, type R2AccountRuntime,
  type RuntimeDomain, type StorageActionResult,
} from "@/lib/image-storage";
import { useSettingsStore } from "../store";

const inputClass = "h-10 rounded-xl border-stone-200 bg-white";
const statusLabels: Record<string, string> = {
  eligible: "可分配", disabled: "已停用新上传", unconfigured: "配置不全",
  cycle_unconfirmed: "计费周期待确认", domains_unprotected: "域名/防护未就绪",
  usage_stale: "用量缺失或过期", capacity_exhausted: "容量预算不足",
  a_exhausted: "A 类预算耗尽", b_exhausted: "B 类预算耗尽", access_paused: "公开访问已暂停",
};
const count = (value?: number) => value === undefined ? "未获取" : value.toLocaleString("zh-CN");
const bytes = (value?: number) => value === undefined ? "未获取" : `${(value / 1_000_000_000).toFixed(3)} GB`;
const time = (value?: string) => value ? new Date(value).toLocaleString("zh-CN") : "未获取";
const origins = (settings: ImageStorageSettings) => settings.cors_origins.map((origin) => origin.trim()).filter(Boolean);
function requireOK(result: StorageActionResult) {
  if (!result.ok) throw new Error(result.error || "操作未完成，请检查权限、域名及用量状态");
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

type RunAction = (label: string, action: () => Promise<string | void>) => Promise<void>;

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

  const run: RunAction = async (label, action) => {
    setBusy(label); setError(""); setResult("");
    try {
      const message = await action();
      setResult(message || `${label}已完成`);
      toast.success(message || `${label}已完成`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : `${label}失败`;
      setError(message); toast.error(message);
    } finally {
      // 失败也读取最新状态，展示已完成的部分操作，绝不伪装全部成功。
      try { await reload(); notifyImageStorageChanged(); }
      catch { setError((previous) => `${previous ? `${previous}；` : ""}状态刷新失败，请重新读取后再操作`); }
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
    try {
      const latest = await getImageStorageOverview();
      // 域名只通过专用动作修改；预算/凭据表单不回写旧的域名切换快照。
      const r2_accounts = settings.r2_accounts.map((account) => {
        const current = latest.settings.r2_accounts.find((item) => item.id === account.id);
        return current ? { ...account, domains: current.domains, active_domain_id: current.active_domain_id } : account;
      });
      accept(await saveImageStorageSettings({ ...settings, r2_accounts, cors_origins: origins(settings) }));
      notifyImageStorageChanged(); setResult("图片存储配置已保存"); toast.success("图片存储配置已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally { setBusy(""); }
  };
  const refreshPage = async () => {
    if (dirty && !window.confirm("放弃未保存的图片存储修改并重新读取？密钥输入不会保留。")) return;
    setBusy("重新读取"); setError("");
    try { await reload(); } catch { setError("读取图片存储失败"); } finally { setBusy(""); }
  };
  const locked = Boolean(busy) || dirty;
  const refreshAllUsage = async () => {
    const failed: string[] = [];
    for (const account of settings?.r2_accounts || []) {
      try {
        const { result } = await refreshR2Usage(account.id);
        if (!result.complete || result.error) failed.push(account.name || account.id);
      } catch { failed.push(account.name || account.id); }
    }
    if (failed.length) throw new Error(`以下账号用量刷新失败或不完整：${failed.join("、")}。其余账号已处理，请查看各账号最近错误。`);
  };

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
            <NumberField label="用量刷新间隔（秒）" value={settings.usage_refresh_seconds} onChange={(usage_refresh_seconds) => patch({ usage_refresh_seconds })} />
            <NumberField label="用量有效期（秒）" value={settings.usage_max_age_seconds} onChange={(usage_max_age_seconds) => patch({ usage_max_age_seconds })} hint="超过后停止新上传，不将旧用量归零。" />
            <NumberField label="对象 CDN 缓存 TTL（秒）" value={settings.cache_ttl_seconds} onChange={(cache_ttl_seconds) => patch({ cache_ttl_seconds })} hint="使用源站响应头；不强制设置 Free Edge TTL。" />
          </div>
          <p className="rounded-xl border border-stone-200 bg-stone-50 p-3 text-xs leading-6 text-stone-600">Base64 请求不保存到服务器、不上传 R2。R2 无本地/WebDAV 回退；停用或切换模式不会删除账号和已有对象。统计存在延迟，预算不是零账单保证；分钟级过期不能撤销已下载的离线副本。</p>
          {settings.mode === "webdav" || settings.mode === "both" ? <div className="space-y-3 rounded-xl border border-stone-200 p-4">
            <h3 className="font-medium">WebDAV</h3><div className="grid gap-4 md:grid-cols-2">
              {([ ["webdav_url", "WebDAV URL"], ["webdav_username", "用户名"], ["webdav_password", "密码"], ["webdav_root_path", "远端目录"], ["public_base_url", "公开访问前缀（可留空）"] ] as const).map(([key, label]) => <Field key={key} label={label}><Input className={inputClass} type={key === "webdav_password" ? "password" : "text"} value={settings[key]} onChange={(event) => patch({ [key]: event.target.value })} autoComplete="off" /></Field>)}
            </div><p className="text-xs text-stone-500">先保存，再测试或将现有本地图补传至 WebDAV；公开前缀留空使用应用访问地址。</p>
            <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={locked} onClick={() => void run("测试 WebDAV", async () => { const { result } = await testImageStorageConnection(); requireOK(result); })}>测试 WebDAV</Button>
              <Button type="button" variant="outline" disabled={locked} onClick={() => { if (window.confirm("将现有本地图全量同步到 WebDAV？")) void run("同步 WebDAV", async () => { const { result } = await syncImageStorage(); return `上传 ${result.uploaded}，跳过 ${result.skipped}，失败 ${result.failed}`; }); }}>全量同步</Button></div>
          </div> : null}
          <Field label="R2 CORS 允许的 Origin（每行一项）" hint="填完整 Origin（如 https://app.example.com），或 *。允许 GET/HEAD 和 Range，不带凭据；CORS 不是防刷。保存后须对相关账号点击「应用防护与 CORS」。"><Textarea className="rounded-xl border-stone-200 bg-white" value={settings.cors_origins.join("\n")} onChange={(event) => patch({ cors_origins: event.target.value.split("\n") })} /></Field>
          <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">R2 账号 · {settings.r2_accounts.length} 个 / 可分配 {overview?.accounts.filter((account) => account.status === "eligible").length || 0} 个</h3>
            <Button type="button" variant="outline" onClick={() => patch({ r2_accounts: [...settings.r2_accounts, newR2Account()] })}><Plus className="size-4" />添加账号草稿</Button></div>
          {settings.r2_accounts.map((account) => {
            const runtime = overview?.accounts.find((item) => item.id === account.id);
            const hasObjects = Boolean((runtime?.usage.image_count || 0) > 0 || (runtime?.usage.pending_bytes || 0) > 0 || (runtime?.usage.local_bytes || 0) > 0);
            return <details key={account.id} className="rounded-xl border border-stone-200 p-4" open={!runtime}>
              <summary className="cursor-pointer text-sm font-medium">{account.name || "未命名账号"} · {account.bucket || "未填写桶"} · {statusLabels[runtime?.status || ""] || runtime?.status || "未保存草稿"} · {account.active_domain_id || "未选择当前域名"}</summary>
              <div className="mt-4 space-y-4">
                <AccountUsage account={account} runtime={runtime} />
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                  <Field label="名称"><Input className={inputClass} value={account.name} onChange={(event) => patchAccount(account.id, { name: event.target.value })} /></Field>
                  <Field label="CF Account ID" hint={hasObjects ? "存在对象/预占，不能更换账号身份。" : "同一 CF 账号只能登记一次。"}><Input className={inputClass} disabled={hasObjects} value={account.account_id} onChange={(event) => patchAccount(account.id, { account_id: event.target.value.trim() })} /></Field>
                  <Field label="专用图片桶" hint="不要使用含备份或敏感文件的桶；对象前缀固定 images/。"><Input className={inputClass} disabled={hasObjects} value={account.bucket} onChange={(event) => patchAccount(account.id, { bucket: event.target.value.trim() })} /></Field>
                  {([ ["access_key_id", "S3 Access Key ID", account.has_access_key_id], ["secret_access_key", "S3 Secret Access Key", account.has_secret_access_key], ["api_token", "CF 管理 API Token", account.has_api_token] ] as const).map(([key, label, stored]) => <Field key={key} label={label} hint={stored ? "已配置；留空保留，可输入新值轮换。" : "尚未配置"}><Input type="password" autoComplete="new-password" className={inputClass} value={account[key]} onChange={(event) => patchAccount(account.id, { [key]: event.target.value })} placeholder={stored ? "留空保留已存凭据" : "输入凭据"} /></Field>)}
                  <NumberField label="容量预算（十进制 GB）" min={0.000000001} step={0.000000001} value={account.capacity_bytes / 1_000_000_000} onChange={(value) => patchAccount(account.id, { capacity_bytes: Math.round(value * 1_000_000_000) })} />
                  <NumberField label="A 类预算 / 周期" value={account.a_budget} onChange={(a_budget) => patchAccount(account.id, { a_budget })} />
                  <NumberField label="B 类预警 / 周期" value={account.b_warn} max={account.b_budget} onChange={(b_warn) => patchAccount(account.id, { b_warn })} />
                  <NumberField label="B 类保护 / 周期" value={account.b_budget} onChange={(b_budget) => patchAccount(account.id, { b_budget })} />
                  <NumberField label="计费周期起始日（UTC，1–28）" max={28} value={account.billing_cycle_day} onChange={(billing_cycle_day) => patchAccount(account.id, { billing_cycle_day, billing_cycle_confirmed: false })} />
                  <NumberField label="每 IP / 10 秒请求阈值" value={account.rate_limit_requests} onChange={(rate_limit_requests) => patchAccount(account.id, { rate_limit_requests })} />
                </div>
                <div className="flex flex-wrap gap-5 text-sm text-stone-700">
                  <label className="flex items-center gap-2"><Checkbox checked={account.billing_cycle_confirmed} onCheckedChange={(checked) => patchAccount(account.id, { billing_cycle_confirmed: checked === true })} />已核对实际计费周期（不是最近 30 天）</label>
                  <label className="flex items-center gap-2"><Checkbox checked={account.enabled} onCheckedChange={(checked) => patchAccount(account.id, { enabled: checked === true })} />参与新图片分配</label>
                  <label className="flex items-center gap-2"><Checkbox checked={account.restrict_methods} onCheckedChange={(checked) => patchAccount(account.id, { restrict_methods: checked === true })} />公开域名仅 GET / HEAD / OPTIONS</label>
                </div>
                <p className="text-xs text-stone-500">账号停用只停止新上传，仍保留旧图清理。存在对象时仅允许凭据轮换，不可移除账号/改桶。启用分配不等于恢复公开访问。</p>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" variant="outline" disabled={locked || !runtime} onClick={() => void run("测试账号", async () => { const { result } = await testR2Account(account.id); requireOK(result); return "对象连接/权限测试完成（实际操作消耗 A 类额度）"; })}>连接/权限测试</Button>
                  <Button type="button" variant="outline" disabled={locked || !runtime} onClick={() => void run("刷新用量", async () => { const { result } = await refreshR2Usage(account.id); if (!result.complete || result.error) throw new Error(result.error || "统计不完整，不能视为可用预算"); })}>刷新用量</Button>
                  <Button type="button" variant="outline" disabled={locked || !runtime} onClick={() => { if (window.confirm("应用本项目免费规则与 CORS，关闭 r2.dev 和所有未纳管的公开绑定？若防护应用失败，可能停用该桶全部公开入口。不会覆盖其他规则或升级付费；同 Zone /images/ 路径共用限速规则。")) void run("应用防护与 CORS", async () => { const { result } = await applyR2Protection(account.id, { rate_limit_requests: account.rate_limit_requests, restrict_methods: account.restrict_methods, cors_origins: origins(settings) }); requireOK(result); }); }}>应用防护与 CORS</Button>
                  <Button type="button" variant="outline" disabled={locked || !runtime} onClick={() => { if (window.confirm(`重新检查预算及防护，并恢复公开访问？拟启用配置域名：${account.domains.filter((domain) => domain.enabled).map((domain) => domain.domain).join("、") || "无（请检查域名配置）"}。切换域名不能绕过 B 类停读。`)) void run("恢复公开访问", async () => { const { result } = await restoreR2Access(account.id); requireOK(result); }); }}>恢复公开访问</Button>
                  <Button type="button" variant="outline" disabled={hasObjects} className="text-rose-600" onClick={() => { if (window.confirm("从配置中移除此账号？存在对象时后端将拒绝；需保存生效。")) patch({ r2_accounts: settings.r2_accounts.filter((item) => item.id !== account.id) }); }}><Trash2 className="size-4" />移除账号</Button>
                </div>
                {runtime ? <DomainManager account={account} runtime={runtime} locked={locked} run={run} /> : <p className="text-xs text-stone-500">保存草稿后可读取 Zone、绑定域名；未完整配置的账号保持停用。</p>}
              </div>
            </details>;
          })}
          <div className="flex flex-wrap items-center justify-end gap-3"><span className="text-xs text-stone-500">{dirty ? "有未保存修改：先保存，再执行云端操作；切换页签会丢弃未保存输入。" : "当前配置已读取；云端动作不会隐式保存其他表单。"}</span><Button type="submit" disabled={!dirty || Boolean(busy)} className="rounded-xl bg-stone-950 text-white"><Save className="size-4" />保存图片存储</Button></div>
        </fieldset>
      </form>
      <div className="space-y-3 border-t border-stone-200 pt-4">
        <p className="text-xs leading-6 text-stone-500">切换域名仅改应用重新解析出的历史链接，不改外部已复制 URL，不复制图片。服务器检查不代表当前浏览器可达；不会自动故障切换。R2 到期后应用停止加载，清理及 CDN 收敛不是精确到秒。</p>
        {overview?.legacy ? <p className="text-xs text-stone-500">旧数据统计：{Object.entries(overview.legacy).filter(([, value]) => typeof value === "number").map(([key, value]) => `${key}: ${value}`).join(" · ") || "请按需执行显式清理"}</p> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={locked} onClick={() => void run("刷新全部账号用量", refreshAllUsage)}>刷新全部用量</Button>
          <Button type="button" variant="outline" disabled={locked} onClick={() => void run("清理到期图片", async () => cleanupSummary((await cleanupExpiredImages()).result))}>立即清理到期 / 预占对象</Button>
          <Button type="button" variant="outline" disabled={locked} className="border-rose-200 text-rose-600" onClick={() => { if (window.confirm("不可恢复：清除旧本地原图、缩略图和旧任务内嵌 Base64。不会迁移、不会删除 R2 对象或远端 WebDAV 图片，也不会清除用户已下载的副本。确认继续？")) void run("清理旧本地图", async () => cleanupSummary((await cleanupLegacyImages()).result)); }}>显式清理旧本地图 / 任务图片内容</Button>
        </div>
      </div>
    </> : null}
  </CardContent></Card>;
}

function AccountUsage({ account, runtime }: { account: R2Account; runtime?: R2AccountRuntime }) {
  const usage = runtime?.usage;
  if (!usage) return <p className="text-xs text-stone-500">未读取用量。首次使用需确认计费周期、完成域名防护并刷新统计。</p>;
  const effectiveA = usage.cloud_a === undefined && usage.a_local === undefined ? undefined : Math.max(usage.cloud_a || 0, usage.a_local || 0);
  const warned = usage.cloud_b !== undefined && usage.cloud_b >= account.b_warn;
  return <div className="space-y-2 rounded-lg bg-stone-50 p-3 text-xs leading-6 text-stone-600">
    <div className="grid gap-x-4 md:grid-cols-2">
      <span>项目已存 {bytes(usage.local_bytes)} / 预占 {bytes(usage.pending_bytes)} / 图片 {count(usage.image_count)}</span>
      <span>云端账号容量 {bytes(usage.cloud_bytes)} / 图片桶 {bytes(usage.cloud_bucket_bytes)} / 预算 {bytes(account.capacity_bytes)}</span>
      <span>A 云端 {count(usage.cloud_a)} / 本地累计 {count(usage.a_local)} / 预算 {count(account.a_budget)}</span>
      <span className={warned ? "font-medium text-amber-700" : ""}>B 云端 {count(usage.cloud_b)} / 预警 {count(account.b_warn)} / 停读 {count(account.b_budget)}{warned ? " · 已到预警阈值" : ""}</span>
      <span>桶级 A {count(usage.cloud_bucket_a)} / B {count(usage.cloud_bucket_b)} / 账号对象数 {count(usage.cloud_object_count)}</span>
      <span>估算预算余量 A {effectiveA === undefined ? "未知" : count(Math.max(0, account.a_budget - effectiveA))} / B {usage.cloud_b === undefined ? "未知" : count(Math.max(0, account.b_budget - usage.cloud_b))}（非保证免费余额）</span>
    </div>
    <p>统计周期（UTC）：{usage.period_start || "未获取"} → {usage.period_end || "未获取"}；更新时间：{time(usage.fetched_at)}；{usage.complete ? "统计完整" : "统计未获取/不完整"}</p>
    <p>r2.dev：{usage.managed_disabled === true ? "已关闭" : usage.managed_disabled === false ? "未确认关闭" : "未检查"}；全部公开入口：{usage.domains_safe === true ? "已验证防护" : "未验证安全"}；公开访问：{usage.access_paused ? "额度保护暂停，须显式恢复" : "未标记暂停"}</p>
    {usage.error ? <p role="alert" className="text-rose-600">最近操作错误：{usage.error}（保留上次统计，不归零）</p> : null}
    {([ ["防护配置", usage.protection_error], ["关闭公开入口", usage.shutdown_error], ["恢复公开访问", usage.restore_error], ["定时维护", usage.maintenance_error] ] as const).map(([label, message]) => message ? <p key={label} role="alert" className="text-rose-600">{label}：{message}</p> : null)}
  </div>;
}

function DomainManager({ account, runtime, locked, run }: { account: R2Account; runtime: R2AccountRuntime; locked: boolean; run: RunAction }) {
  const [domains, setDomains] = useState<RuntimeDomain[] | null>(null);
  const [managedEnabled, setManagedEnabled] = useState<boolean | null>(null);
  const [zones, setZones] = useState<Array<{ id: string; name: string }>>([]);
  const [zoneId, setZoneId] = useState("");
  const [domain, setDomain] = useState("");
  const [checks, setChecks] = useState<DomainCheck[]>([]);
  const [browserChecks, setBrowserChecks] = useState<Record<string, string>>({});
  const [browserBusy, setBrowserBusy] = useState("");
  useEffect(() => {
    setDomains(runtime.domains);
    setManagedEnabled(typeof runtime.usage.managed_disabled === "boolean" ? !runtime.usage.managed_disabled : null);
  }, [runtime]);
  const items = domains || runtime.domains || [];
  const readDomains = async () => {
    const data = await getR2Domains(account.id);
    setDomains(data.items); setManagedEnabled(data.managed_enabled);
  };
  const domainAction: RunAction = (label, action) => run(label, async () => {
    const message = await action();
    await readDomains();
    return message;
  });
  const browserCheck = async (item: RuntimeDomain, sample?: string) => {
    if (!sample) return;
    const checkedAt = () => new Date().toLocaleString("zh-CN");
    setBrowserBusy(item.id);
    try {
      const url = new URL(sample);
      if (url.protocol !== "https:" || url.hostname !== item.domain || url.username || url.password) throw new Error("invalid sample");
      const response = await fetch(url.href, { mode: "cors", credentials: "omit", redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10_000) });
      if (!response.ok) { setBrowserChecks((current) => ({ ...current, [item.id]: `当前浏览器：HTTP ${response.status} · ${checkedAt()}` })); return; }
      const blob = await response.blob();
      setBrowserChecks((current) => ({ ...current, [item.id]: `当前浏览器：CORS 读取${blob.size ? "成功" : "为空"} · ${checkedAt()}` }));
    } catch {
      setBrowserChecks((current) => ({ ...current, [item.id]: `当前浏览器：读取失败（网络 / TLS / CORS / 超时 / 重定向），不能据此判定被墙 · ${checkedAt()}` }));
    } finally { setBrowserBusy(""); }
  };
  const disabled = locked || Boolean(browserBusy);
  return <div className="space-y-3 border-t border-stone-200 pt-4">
    <h4 className="text-sm font-medium">域名与防护 · 当前 {account.active_domain_id || "未选择"}</h4>
    <p className="text-xs leading-6 text-stone-500">先保存凭据 → 读取 Zone → 绑定停用域名 → 应用防护 → 启用 → 设为当前 → 刷新用量。无自动探测或切换。免费限速每 IP/10 秒，超限 Block 10 秒；同 Zone 共用一条 /images/ 规则。防护失败请检查权限、规则槽和证书，不会自动升级付费。</p>
    <div className="flex flex-wrap gap-2">
      <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void run("读取实际域名绑定", readDomains)}>读取绑定 / 证书状态</Button>
      <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void run("读取同账号 Zone", async () => { setZones((await getR2Zones(account.id)).items); })}>读取可用 Zone</Button>
      <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void run("服务器检查全部域名", async () => { setChecks((await checkR2Domains(account.id)).items); return "服务器检查已返回，请查看每个域名的结果（检查可能消耗 B 类额度）"; })}>服务器立即检查</Button>
    </div>
    <p className="text-xs text-stone-500">r2.dev 实际绑定：{managedEnabled === null ? "尚未手动读取" : managedEnabled ? "启用（请应用防护关闭）" : "关闭"}</p>
    <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
      <Input aria-label="新增域名" className={inputClass} disabled={disabled} placeholder="images.example.com（不含协议或路径）" value={domain} onChange={(event) => setDomain(event.target.value)} />
      <Select value={zoneId} onValueChange={setZoneId} disabled={disabled || zones.length === 0}><SelectTrigger className={inputClass}><SelectValue placeholder="先读取，再选择同账号 Zone" /></SelectTrigger><SelectContent>{zones.map((zone) => <SelectItem key={zone.id} value={zone.id}>{zone.name}</SelectItem>)}</SelectContent></Select>
      <Button type="button" variant="outline" disabled={disabled || !zoneId || !domain.trim()} onClick={() => void domainAction("绑定停用域名", async () => { await addR2Domain(account.id, domain.trim(), zoneId); setDomain(""); })}>绑定（初始停用）</Button>
    </div>
    {items.length === 0 ? <p className="text-xs text-stone-500">尚无域名或未读取绑定。</p> : items.map((item) => {
      const check = checks.find((entry) => entry.domain_id === item.id) || item.check;
      const sample = check?.sample_url || item.sample_url;
      const active = account.active_domain_id === item.id;
      const protectedDomain = item.protected === true;
      const cfStatus = typeof item.status === "string" ? item.status : item.status ? `归属 ${item.status.ownership || "未知"} / SSL ${item.status.ssl || "未知"}` : "未获取";
      return <div key={item.id} className="space-y-2 rounded-lg border border-stone-200 p-3 text-xs leading-6">
        <p className="break-all font-medium">{item.domain} {active ? "· 当前返回域名" : "· 备用/实际绑定"} · {item.enabled ? "已启用" : "已停用"} · {protectedDomain ? "已验证防护" : "未验证防护"}</p>
        <p className="text-stone-500">Zone {item.zone_id || "未纳管"} · CF {cfStatus}</p>
        <p className="text-stone-500">服务器检查：{check ? `${check.ok ? "成功" : check.error || "失败/无有效图片样本"} · ${time(check.checked_at)}` : "未检查"}</p>
        <p className="text-stone-500">{browserChecks[item.id] || "当前浏览器：未检查"}</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={disabled || active || !item.enabled || !protectedDomain || Boolean(runtime.usage.access_paused)} onClick={() => { if (window.confirm(`手动切换当前返回域名为 ${item.domain}？外部旧链接不会改写，也不会复制图片。`)) void domainAction("切换当前域名", async () => { await updateR2Domain(account.id, item.id, { active: true }); }); }}>设为当前</Button>
          <Button type="button" size="sm" variant="outline" disabled={disabled || (!item.enabled && (!protectedDomain || Boolean(runtime.usage.access_paused)))} onClick={() => void domainAction(item.enabled ? "停用域名" : "启用域名", async () => { await updateR2Domain(account.id, item.id, { enabled: !item.enabled }); })}>{item.enabled ? "停用" : "启用"}</Button>
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => void run("服务器检查域名", async () => { const data = await checkR2Domains(account.id, item.id); setChecks((current) => [...current.filter((entry) => entry.domain_id !== item.id), ...data.items]); return "检查已返回，请查看来源、时间及错误阶段"; })}>服务器检查</Button>
          <Button type="button" size="sm" variant="outline" disabled={disabled || !sample || !item.enabled || Boolean(runtime.usage.access_paused)} onClick={() => void browserCheck(item, sample)}>{browserBusy === item.id ? "浏览器读取中…" : "当前浏览器检查"}</Button>
          <Button type="button" size="sm" variant="outline" className="text-rose-600" disabled={disabled || (active && account.enabled)} onClick={() => { if (window.confirm(`解绑 ${item.domain}？其旧链接将不可用，当前域名须先切换或停用账号。`)) void domainAction("解绑域名", async () => { await deleteR2Domain(account.id, item.id); }); }}>解绑</Button>
        </div>
        {!sample ? <p className="text-stone-500">无浏览器检查样本；先运行服务器检查。有未过期图片时才可探测，不会专门上传探针。</p> : null}
      </div>;
    })}
  </div>;
}
