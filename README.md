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
| `bt169 migrate` | 建库 / 升级 schema（幂等） |
| `bt169 doctor` | 环境自检，逐项 ✓/✗ |
| `bt169 --version` | 版本 |

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
