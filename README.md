# 169bt 归档台

个人自用的 4K 帖归档与 ED2K 提取工具。后端 Python + FastAPI + SQLite，
前端零构建原生 ESM。两个 ed2k 数据源：169bt 论坛采集（4K）+ 自建 AVdb 的
「LTX破解」板块（LTX）。

[![Release](https://img.shields.io/github/v/release/JinsFoni/169bt)](https://github.com/JinsFoni/169bt/releases)
[![Docker Image](https://img.shields.io/badge/ghcr.io-jinsfoni%2F169bt-blue)](https://github.com/JinsFoni/169bt/pkgs/container/169bt)

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/bt169 migrate     # 建库（幂等）
.venv/bin/bt169 doctor      # 环境自检
.venv/bin/bt169 serve       # → http://127.0.0.1:8899
```

## 命令

| 命令 | 作用 |
|---|---|
| `bt169 serve [--host H] [--port P] [--reload]` | 启动 Web 服务（默认只监听回环） |
| `bt169 login [--username U] [--password P]` | 登录论坛并保存会话（Cookie 30 天） |
| `bt169 tg-login` | 登录 Telegram 账号（MTProto，交互式），session 加密存入设置。用于 ed2k 转发 |
| `bt169 collect --from D --to D [--fid N] [--refetch]` | 采集指定日期范围（前台阻塞、实时进度）。`--refetch` = 已入库的帖子也重新解析（站点改版后修历史数据用） |
| `bt169 migrate` | 建库 / 升级 schema（幂等） |
| `bt169 doctor` | 环境自检，逐项 ✓/✗ |
| `bt169 --version` | 版本 |

> **登录额度是稀缺资源**（Discuz 默认 **5 次 / 900 秒**，按 IP 计）。
> `bt169 login` 会在提交前检查额度（剩余 ≤1 或距上次提交 <600 秒则
> 拒绝），且只在验证码**预校验通过**后才提交——实测提交错误验证码
> **不消耗**额度。

### 数据源：4K 与 LTX

顶栏品牌名右侧可在两个 ed2k 来源之间切换（选择记在浏览器里）：

| 标签 | 来源 | 怎么来 |
|---|---|---|
| **4K** | 169bt 论坛（`fid=192`） | RSS 发现 + 详情页采集，图片本地化，Emby 匹配 |
| **LTX** | 自建 [AVdb](https://github.com/li-peifeng/AVdb-Only) 实例的「LTX破解」板块 | 设置页填 AVdb 地址 + 账号 → 全量同步到本地镜像（658 条 / 约 4 秒） |

两者卡片、复制、转发到 Telegram 的行为完全一致；LTX 是**镜像**，所以没有
删除按钮（上游才是权威），封面经 AVdb 的图床代理按需本地化。

> LTX 打开时会按 600 秒 TTL **自动同步**一次；也可在设置 →「LTX 数据源」
> 手动点「立即同步」。AVdb 掉线时已同步的内容照常可翻。

### Emby 入库标记

设置 →「Emby」填服务器地址与 API Key（「用户」可选，用来把媒体的可选列表
按该用户权限过滤）。「媒体库」是**可多选**的勾选列表：一个都不勾 = 全部媒体库，
勾多个 = **任一库命中即算已入库**。

已入库的卡片在图片区左下角显示角标。入库状态每 15 分钟同步一次
（`emby.cron` 可改节奏），采集结束时还会立刻校对本次采到的帖子；
Emby 不可达时只是不显示角标，浏览与复制完全不受影响。

### 访问门禁（S-6）

在 Web 设置面板的「基础设置」里填**访问密码**即启用。启用后：

- `/api/*`（除 `/api/auth/*` 与 `/api/health`）与 `/img/*` 均要求会话
- 密码以 **PBKDF2-SHA256（60 万次）** 哈希落库，不存明文
- 会话 token 随机 32 字节，**库里只存 sha256 哈希**，30 天有效
- 桌面 + 手机可同时登录（多会话并存）；**改密码会踢掉所有设备**
- cookie 为 `HttpOnly` + `SameSite=Lax`；仅在 HTTPS 时加 `Secure`

> 门禁默认**关闭**（未设密码 = 不挡自己）。
> 清空密码即关闭门禁——需要自救时用。

> **部署提醒**：经 Lucky 等反代暴露到公网时，反代必须注入
> `X-Forwarded-Proto: https`，否则 cookie 不带 `Secure`（功能正常，
> 但安全性降级）。uvicorn 本身只监听 `127.0.0.1`。

## 测试

```bash
.venv/bin/pytest -v
```

## 发布 / 更新

- **版本发布**：在 GitHub 上创建 Release（tag 形如 `v1.0.0`）会自动触发
  多架构 Docker 镜像构建并推送 GHCR，无需配置任何 secret。
- **拉取镜像**：`docker pull ghcr.io/jinsfoni/169bt:latest`
  （另提供 `:1.0.0` / `:1.0` 版本号 tag）。
- **升级部署**：拉新镜像后 `docker compose up -d` 重建容器；数据库迁移
  在启动时自动执行（幂等）。

## 文档

| 文档 | 内容 |
|---|---|
| `REQUIREMENTS.md` | 需求与已实测事实 |
| `ARCHITECTURE.md` | 后端架构、数据模型、API 契约、ADR |
| `FRONTEND.md` | 前端架构与技术选型 |
| `ui/DESIGN.md` | UI 设计系统与交互 |
| `docs/superpowers/plans/` | 实施计划 |

## 部署

### Docker（推荐）

```bash
docker compose up -d          # 拉取镜像 + 启动，数据落 ./data
docker compose logs -f        # 跟日志
docker compose down           # 停止（数据保留）
```

`docker-compose.yml` 里的关键环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PUID` / `PGID` | `1000` | 容器内运行身份。**改成宿主数据目录属主的 uid/gid**（`id -u` / `id -g`），启动时自动建用户、`chown /data` 后降权运行——bind mount 到 NAS 上任意属主的目录都能读写 |
| `TZ` | `UTC` | 时区，影响归档日期归属；中国时区设 `Asia/Shanghai` |
| `BT169_SECRET_KEY` | 自动生成 | 主密钥（base64 的 32 字节，`openssl rand -base64 32`）。固定它，换容器后已存密码才能继续解密 |

GHCR 镜像已默认写在 `docker-compose.yml` 里（`image: ghcr.io/jinsfoni/169bt:latest`，
镜像在发布 Release 时自动构建，纯 push 代码不会构建）；想本地构建就换成 `build: .`。

镜像以 root 启动、由 `docker/entrypoint.sh` 建用户后 `gosu` 降权——root 只存活到 exec 之前；
若显式 `--user` 运行则 PUID/PGID 不生效，需自行保证数据目录可写。

### 公网暴露

公网访问由 **Lucky 反代**负责 HTTPS。uvicorn 只监听容器内 `0.0.0.0:8899`，
不要把端口直接暴露到公网，务必过反代。

反代必须注入 `X-Forwarded-Proto: https`，否则 PWA 的 Service Worker
与 manifest 会因为非安全上下文而失败。

## 主密钥

首次 `serve` 时会在 `data/169bt.key` 生成 32 字节随机密钥（权限 `0600`），
用于加密数据库中的敏感字段（AES-256-GCM）。

- **丢失密钥 = 已存密码无法解密**，需要重新填写
- 容器化部署可用 `BT169_SECRET_KEY`（base64 编码的 32 字节）注入，
  避免把密钥写进镜像层
- `data/` 已在 `.gitignore` 中，不会进版本库
