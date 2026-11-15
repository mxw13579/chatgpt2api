# R2 图片存储使用说明

## 行为

- Images 默认或 `response_format=b64_json`：只返回 Base64，不上传、不保存。Chat/Responses 图片同样不保存。
- Images `response_format=url` 及网页图片任务：按当前存储模式保存。R2 模式下每张图上传一次，返回 `公开访问地址/images/...`。
- 服务器只保存图片索引（`data/image_index.json`）和每账号本月上传次数（`data/r2_usage.json`），不保存原图或缩略图。
- 没有可用账号或上传失败时直接报错，不回退本地/WebDAV。

## 多账号分摊免费额度

R2 免费额度按 Cloudflare 账号计算：每月 100 万次 A 类操作、1000 万次 B 类操作、10 GB 存储。

- **A 类（上传）**：每张图计 1 次，连接测试也计 1 次；请求前计数，失败不退回。每个账号设置「每月上传上限」，默认 900,000（为免费额度留 10% 余量），按 UTC 自然月重置。
- **选择规则**：在已启用且未达上限的账号中，选「本月已用 / 上限」比例最低的账号。全部达到上限时新请求失败。
- **存储**：图片默认保留 60 分钟后删除（删除不计费），桶内常驻量很小，不单独统计。
- **B 类（读取）**：浏览器直接从自定义域名读取，本项目不统计。依靠对象的 `Cache-Control` 让 CDN 缓存命中，并在 Cloudflare 配置限速规则防刷。

## 接入步骤

### 一键开通（推荐）

每个 Cloudflare 账号只需在后台做一次准备：

1. 开通 R2（需绑定支付方式，免费额度内不扣费），并把图片域名所在站点接入该账号。
2. 在 **My Profile → API Tokens**（用户 Token）或 **Manage Account → Account API Tokens**（账号 Token）创建自定义 Token，权限：Account · Workers R2 Storage · Edit，Zone · Zone · Read，Zone · DNS · Edit。

然后在 **设置 → 图片存储 → 一键开通** 填入该 Token 和图片域名（如 `img.example.com`）。项目会自动建桶（桶名取自域名，如 `img-example-com`）、绑定自定义域名（DNS 记录由 Cloudflare 自动创建），并添加已启用的账号。新桶的 `r2.dev` 公开访问默认关闭。

Token 原文不保存：配置中只保存由它推导出的 S3 凭据（Access Key ID 为 Token ID，Secret 为 Token 值的 SHA-256），该凭据可读写此账号下所有 R2 桶，但不能修改 DNS。在 Cloudflare 删除该 Token 会使凭据失效。

域名证书生效需几分钟，之后点击「测试连接」，再把存储模式切换为 R2 并保存。限速规则不自动创建，需要时在该域名的 Zone 手动添加（例如对 `/images/` 路径每 IP 10 秒 120 次）。

### 手动填写

1. 在 Cloudflare 创建一个专用图片桶（不要与备份共用），绑定自定义域名并关闭 `r2.dev` 公开访问。
2. 创建一个仅限该桶对象读写的 R2 API Token，记下 Access Key ID / Secret Access Key。
3. 在 **设置 → 图片存储** 添加账号，填写 CF Account ID、桶名、S3 凭据、公开访问地址（如 `https://img.example.com`），勾选启用并保存。
4. 点击「测试连接」，然后把存储模式切换为 R2 并保存。

## 账号变更

- 停用账号只停止新上传，已有图片仍按保留期删除。
- 账号下仍有未删除图片时，不能移除账号、更换 CF Account ID 或桶，也不能清空凭据；可以轮换凭据或修改公开访问地址。
- 修改公开访问地址后，网页历史和管理页按图片 ID 重新解析出新地址；外部已复制的旧链接不会改写。

## 过期与缓存

默认图片保留 60 分钟，每 60 秒处理一批最多 100 个到期对象；删除成功才移除索引，失败的下次重试。默认对象头：

```http
Cache-Control: public, max-age=0, s-maxage=60, must-revalidate
```

到期删除后，CDN 缓存最长再保留 `s-maxage` 秒。用户已下载的副本无法撤回。

## 旧数据

切换到 R2 不会迁移或删除本地/WebDAV 旧图片。需要清理时在设置页执行「显式清理旧本地图」，该操作不删除 R2 对象和 WebDAV 远端文件。
