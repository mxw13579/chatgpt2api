# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。使用 PostgreSQL 时还需要单独备份数据库。

## 使用 GitHub Actions 发布自己的镜像

工作流：`.github/workflows/docker-publish.yml`（Actions 页面名称为 `Publish Docker Image`）。

镜像在 GitHub Actions 上构建并发布到 **GitHub Container Registry（GHCR）**，不是 Docker Hub；工作流不会自动连接或更新服务器。
镜像路径自动使用当前 GitHub 仓库名并转为小写。本仓库 `mxw13579/chatgpt2api` 对应：

```text
ghcr.io/mxw13579/chatgpt2api
```

### 触发方式和标签

| 操作 | 发布的镜像标签 |
| --- | --- |
| 推送到 `main` | `latest`、`main`、`sha-<完整提交 SHA>` |
| 推送版本标签，例如 `v1.9.6` | `v1.9.6`、`1.9.6`、`1.9`、`sha-<完整提交 SHA>` |
| 在 Actions 页面手动运行 | 所选分支或标签对应的镜像标签，以及 `sha-<完整提交 SHA>`；只有选择 `main` 才更新 `latest` |

`latest` 跟随 `main`，版本标签构建不会覆盖它。预发布版本建议使用完整标签，例如 `v1.9.6-rc.1`。
镜像同时支持 `linux/amd64` 和 `linux/arm64`。构建成功后，Actions 的 Summary 页面会列出镜像标签及可固定部署的 digest 引用。

首次使用：

1. 将工作流提交并推送到自己的 GitHub 仓库。Fork 仓库如尚未启用 Actions，需要先在 Actions 页面启用。
2. 在 Actions 页面查看 `Publish Docker Image`，或通过 `Run workflow` 选择 `main` 手动触发。
3. 工作流使用 GitHub 自动提供的 `GITHUB_TOKEN`，已声明 `contents: read` 和 `packages: write`，无需额外配置发布凭据。
4. 如果已有同名 GHCR 包但发布提示权限不足，在包的设置中确认当前仓库具有 Actions 写入权限；组织策略也必须允许发布包。

发布版本示例（标签名请使用尚未发布的新版本，并同步维护 `VERSION`）：

```bash
git tag v1.9.6
git push origin v1.9.6
```

GHCR 包首次发布通常为私有。可在 GitHub 的 Packages 中将包设为 Public，允许服务器匿名拉取；若保持私有，在服务器上使用具有 `read:packages` 权限的 PAT（classic）登录，交互提示的密码填写该令牌：

```bash
docker login ghcr.io -u mxw13579
```

部署统一从 GHCR 拉取镜像；工作流不再额外导出、上传离线镜像归档。公开包可匿名拉取，私有包使用上述只读凭据登录。需要固定版本时，使用版本标签或 Actions Summary 中的 digest 引用。

不要将 PAT、生产 `.env`、账号数据或生产密钥提交到仓库。镜像内初始化空 `config.json`，构建上下文排除本机配置和数据；生产配置通过服务器原有挂载和环境变量提供，并设置强认证密钥。

### 将已有服务器切换到自己的镜像

仓库默认的 `docker-compose.yml` 仍引用上游 `ghcr.io/basketikun/chatgpt2api:latest`。仅添加工作流不会自动修改服务器上的镜像来源。

已有部署应在**服务器原有 Compose 文件**中，只修改主应用服务的 `image`，不要用仓库模板覆盖整个文件。以下假设主服务名为 `app`：

```yaml
services:
  app:
    image: ghcr.io/mxw13579/chatgpt2api:latest
    # 原有 ports、volumes、environment、depends_on 等配置保持不变。
```

如果该服务原来有 `build:` 配置，切换为拉取镜像后可将其移除。保留原有的 PostgreSQL 服务、数据库卷、`STORAGE_BACKEND=postgres`、数据库连接配置，以及原有 `57950:80` 端口映射。

确认镜像发布成功、完成配置和数据库备份后，在已有部署目录执行：

```bash
cd /data/docker/codex-pool/chatgpt2api
# 先确认主应用服务名；若不是 app，请替换下面命令中的 app。
docker compose config --services
docker compose pull app
docker compose up -d --no-deps --no-build app
docker compose ps
docker compose logs --tail=100 app
```

此流程只更新主应用容器，不重新部署数据库。生产环境也可以将 `latest` 换成版本标签，或 Actions Summary 中的 `ghcr.io/mxw13579/chatgpt2api@sha256:...` 引用，以固定版本和便于回滚。

### 1.9.14 升级注意

- 「稳定代理运行时」（WARP / FlareSolverr）已移除，`docker-compose.warp.yml` 不再提供。它配置的代理地址此前已不被任何上游请求使用；需要代理时在「设置」中填写全局代理，或为单个账号填写代理。`config.json` 中遗留的 `proxy_runtime` 和 `chat_completion_cache` 不再读取，可手动删除。
- 原先开启了 `proxy_runtime.enabled` 和 `skip_ssl_verify` 的部署，升级后上游请求恢复校验证书。
- Git 存储后端已移除，`STORAGE_BACKEND=git` 会启动失败。升级前用 `scripts/migrate_storage.py` 迁移到 `json`、`sqlite` 或 `postgres`。
- `/v1/search`、`/v1/ppt/generations`、`/v1/psd/generations`、`/v1/editable-file-tasks`、`/files/` 和 `POST /api/accounts/export` 已移除。Chat / Responses 中的 `web_search` 工具不受影响。

### 1.9.12 升级注意

- sub2api、远程 CPA 服务器导入和 AI 审核已移除；`config.json` 中的相关配置以及 `data/` 下的 CPA、sub2api 配置文件不再读取，无需手动删除。
- 账号刷新间隔默认值改为 30 分钟。已有部署的 `refresh_account_interval_minute` 保存在 `config.json` 中，不会自动变化，需要时在「设置」中修改，重启后生效。

### 1.9.11 升级注意

移除本地和 WebDAV 图片存储、网页画图页面和图片管理页面，R2 成为唯一的图片存储方式：

- `response_format=url` 需要至少一个已启用且未达上限的 R2 账号，否则在生图前直接失败；默认及 `b64_json` 不受影响。升级前仍在使用本地或 WebDAV 存储的部署，请先在「设置 → 图片存储」配置好 R2。
- `config.json` 中 `image_storage` 的 `mode`、`enabled`、`webdav_*`、`public_base_url` 以及 `image_retention_days` 不再读取，无需手动删除。
- 升级时在停止旧容器后清除图片索引中的非 R2 条目（未使用过本地或 WebDAV 存储的部署可跳过）：

  ```bash
  python3 - <<'EOF'
  import json
  path = "data/image_index.json"
  doc = json.load(open(path))
  doc["items"] = {k: v for k, v in doc["items"].items() if v.get("storage") == "r2"}
  json.dump(doc, open(path, "w"), ensure_ascii=False)
  EOF
  ```

- 旧的本地图片目录 `data/images/`、`data/image_thumbnails/` 以及 `data/image_tasks.json`、`data/image_tags.json` 不再使用，确认无需保留后可手动删除。旧日志中指向本机 `/images/...` 的图片链接将失效。
- 非管理员用户登录网页后只显示 API 使用提示。

### 1.9.9 升级注意

R2 图片存储精简为按每月上传次数分配，不迁移旧 R2 配置：旧账号的域名、预算、计费周期和防护字段在读取时被忽略。已启用的 R2 账号需在「设置 → 图片存储」补填公开访问地址（桶绑定的自定义域名）并保存，再点击测试连接。自定义域名、关闭 r2.dev 和限速规则请在 Cloudflare 后台配置，详见 [R2 图片存储](r2-image-storage.md)。

### 1.9.7 升级注意

保留现有存储配置，不会自动启用 R2、迁移或删除旧图片。Images 默认及 `b64_json`、Chat/Responses 图片不再保存；需要持久化链接时显式使用 `response_format=url`，网页图片任务继续保存。R2 需要在「设置 → 图片存储」中配置账号、公开访问地址并测试连接，详见 [R2 图片存储](r2-image-storage.md)。升级前备份配置、数据库、图片索引及任务记录；应用更新无需重建 PostgreSQL。

### 1.9.6 升级注意

账号热路径改为按键增量持久化，无需数据库 schema 迁移。业务日志开始分片轮转，完成的账号刷新/重登进度增加 TTL 与容量限制。默认仍按单进程运行；升级前备份需要长期保留的日志，并为 Docker stdout 设置独立轮转。详细默认值、兼容边界及回滚说明见[存储、日志与进度保留](performance-and-retention.md)。

## 方式一：普通 Docker 部署

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

重启普通部署：

```bash
docker compose restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
