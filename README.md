# 169bt 归档台

个人自用的 4K 帖归档与 ED2K 提取工具。后端 Python + FastAPI + SQLite，
前端零构建原生 ESM。

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
| `bt169 collect --from D --to D [--fid N]` | 采集指定日期范围（前台阻塞、实时进度） |
| `bt169 migrate` | 建库 / 升级 schema（幂等） |
| `bt169 doctor` | 环境自检，逐项 ✓/✗ |
| `bt169 --version` | 版本 |

> **登录额度是稀缺资源**（Discuz 默认 **5 次 / 900 秒**，按 IP 计）。
> `bt169 login` 会在提交前检查额度（剩余 ≤1 或距上次提交 <600 秒则
> 拒绝），且只在验证码**预校验通过**后才提交——实测提交错误验证码
> **不消耗**额度。

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

## 文档

| 文档 | 内容 |
|---|---|
| `REQUIREMENTS.md` | 需求与已实测事实 |
| `ARCHITECTURE.md` | 后端架构、数据模型、API 契约、ADR |
| `FRONTEND.md` | 前端架构与技术选型 |
| `ui/DESIGN.md` | UI 设计系统与交互 |
| `docs/superpowers/plans/` | 实施计划 |

## 部署

公网访问由 **Lucky 反代**负责 HTTPS。uvicorn 只监听 `127.0.0.1`，
不要把它直接暴露到公网。

反代必须注入 `X-Forwarded-Proto: https`，否则 PWA 的 Service Worker
与 manifest 会因为非安全上下文而失败。

## 主密钥

首次 `serve` 时会在 `data/169bt.key` 生成 32 字节随机密钥（权限 `0600`），
用于加密数据库中的敏感字段（AES-256-GCM）。

- **丢失密钥 = 已存密码无法解密**，需要重新填写
- 容器化部署可用 `BT169_SECRET_KEY`（base64 编码的 32 字节）注入，
  避免把密钥写进镜像层
- `data/` 已在 `.gitignore` 中，不会进版本库
