# 169BT 系统架构设计

> 目标：把已验证的采集链路 + 已完成的 UI 原型，落成一个**可长期无人值守运行**的系统。
> 后端 **Python**（本机实测 3.14.6，要求 ≥3.10）；前端沿用现有 `ui/`（零构建原生 JS），由后端静态分发。
> 需求依据：`REQUIREMENTS.md`（编号 C/W/E/T/S 直接引用）；UI 依据：`ui/DESIGN.md`。
> 本文档所有数字均为**本机实测**，来源见「附录 A：实测验证记录」。

---

## 0. 设计约束

架构必须满足的硬约束，全部来自实测事实：

| 约束 | 来源 | 对架构的强制要求 |
|---|---|---|
| 采集会触发**账号封禁**风险 | 风险表 🔴 | 采集必须**串行 + 严格限速**，绝不可并发抓帖 |
| **登录失败次数按 IP 限 5 次** | 实测 A.6 | 登录是**稀缺资源**；必须「先离线验证验证码，再提交登录」 |
| RSS 只回 20 条、无 ed2k | 事实 1、2 | 必须「RSS 发现 + 逐帖抓详情」两段式 |
| ed2k 需登录 + 感谢才可见 | 事实 3–6、A.4 | 会话管理是**核心基础设施**，不是附属功能 |
| Cookie 30 天有效 | 事实 10 | 登录是**低频**操作（约每月一次）→ 验证码方案可以慢 |
| 日发帖峰值 41 帖 | 事实 12 | 数据量极小，SQLite 足够，无需分布式 |
| 归档日 ≠ 商品発売日 | §3.3 | 领域模型必须区分 `post_date` 与 `release_date` |
| 日期非连续 | W-4 | 相邻日期必须**查库**得到，不能 `date±1` |
| Emby 可能不可达 | E-7 | 浏览路径**不得**依赖 Emby 实时可用 |
| TG 可能限流 | 风险表 🟡 | 转发失败不得阻塞采集与浏览 |
| 验证码单图**只有约 3 次校验机会** | 实测 A.5 | 每张图最多提交 2 个候选，超出即作废该图 |

**一句话概括设计取向**：把「有风险、会失败、要慢」的抓取链路关进一个串行 worker 里；
把「要快、要稳」的浏览链路做成纯粹的本地 SQLite 读。

---

## 1. 技术选型：为什么是 Python

### 1.1 结论

| 层 | 选型 | 版本（本机实测） |
|---|---|---|
| 运行时 | **Python** | 3.14.6（`ddddocr` 要求 ≥3.10） |
| Web 框架 | **FastAPI + Uvicorn** | 0.141.1 / 0.53.0 |
| HTTP 客户端 | **httpx** | 0.28.1 |
| 验证码 | **ddddocr** | 1.6.1（onnxruntime 1.30.0） |
| HTML 解析 | **selectolax**（主）+ lxml（备） | 0.4.12 / 6.1.3 |
| 存储 | **Python 内置 `sqlite3`** | SQLite 3.53.3 |
| 调度 | **APScheduler** | 3.11.3 |
| 密钥加密 | **cryptography**（AES-256-GCM） | 50.0.1 |
| 前端 | **原生 JS + 手写 CSS（零构建）** | 沿用现有 `ui/` |

### 1.2 为什么不用 Go —— 实测证据（含一处重要更正）

> ⚠️ **更正**：本节早先版本称「Go 的 ddddocr 移植版在模块代理上不存在」。
> **该结论是错误的。** 当时用手工拼接的 proxy URL 查询，得到了 `not found`，
> 而同一方法查询**已知存在**的 `PuerkitoBio/goquery` 也返回 `not found`——
> 这个自带的对照本应立刻否定该结论，却被忽略了。
> 改用 `go list -m -versions`（真实查询机制）后，这些模块**全部存在**，
> 且其中 `tensafe/goddddocr` 已经**实际跑通并与 Python 逐字比对**（见附录 A.12）。

**Go 侧确实有可用实现。** 实测存在的移植版：

```
github.com/yangbin1322/go-ddddocr   v1.0.0 v1.0.1
github.com/tensafe/goddddocr        v1.0.0 v1.0.1
github.com/okatu-loli/ddddocr-go    v0.0.1 v0.0.2
github.com/Changbaiqi/ddddocr-go    v0.0.1 … v0.1.4（14 个版本）
```

`tensafe/goddddocr` 的完成度相当高：模型、字符集、`onnxruntime.dylib`
全部用 `go:embed` 内嵌，有 CI、release、benchmark。
**本机零配置跑通**，且在 40 张图上与 Python 的输出**逐字比对**：

```
Go ModelOld  ≡ Py default : 37/40 = 92.5%
Go ModelBeta ≡ Py beta    : 40/40 = 100.0%   ← beta 模型完全一致
```

**那么为什么最终仍选 Python？** 不是「Go 做不到」，而是三点权衡：

| 维度 | Go (`tensafe/goddddocr`) | Python (`ddddocr`) |
|---|---|---|
| 社区与维护 | **0 star**，创建于 2026-05-17（约 4 个月），单一维护者 | **14.7k star**，2021 年至今，6 位贡献者，持续更新 |
| 构建要求 | **必须 cgo**（`CGO_ENABLED=0` 直接编译失败）；`GOOS=linux` 交叉编译失败 | 无编译步骤，纯 wheel 安装 |
| 部署体积 | 单二进制 **125.6 MB**（模型+库内嵌） | venv **429 MB** |
| 运行时副作用 | 首次运行释放 **36.7 MB** dylib 到 `~/Library/Caches/goddddocr/` | 无 |
| 与 Python 输出一致性 | 92.5% / 100% | — |
| 迁移成本 | 需重写全部业务代码 | 需求文档 §6 原本就建议此栈 |

**决定**：Go 方案技术上完全可行（甚至部署体积更小），但其 OCR 依赖是一个
**4 个月大、0 star、单一维护者**的项目，而验证码恰是整个系统唯一
「外部依赖 + 概率性」的环节。把它压在这么新的依赖上，风险与收益不匹配。

Python 的 `ddddocr` 有 14.7k star、5 年历史，且**需求文档 §6 原本就推荐它**。
**故选 Python**——理由从「Go 做不到」修正为「Go 能做，但生态成熟度不值得赌」。

> **这条更正的架构意义**：由于两者输出等价，`captcha.Solver` 被设计为**接口**（§5.1.3）。
> 若将来 Go 生态成熟，或你更看重「单二进制部署」，
> 可以只替换这一个实现，业务代码零改动。
> **附录 A.12 保留了完整的 Go 侧实测数据，供你自行判断。**

---

## 2. 进程与部署模型

### 2.1 单进程 + 后台 worker

```
┌─────────────────────────── uvicorn (单进程) ───────────────────────────┐
│                                                                        │
│   ┌────────────┐   ┌──────────────┐   ┌───────────────┐               │
│   │  FastAPI   │   │  Collector   │   │  Emby Syncer  │               │
│   │  (async)   │   │  (串行任务)  │   │  (串行任务)   │               │
│   └─────┬──────┘   └──────┬───────┘   └───────┬───────┘               │
│         │                 │                   │                        │
│         │  读             │  写(串行)         │  写(串行)              │
│         └────────┬────────┴─────────┬─────────┘                        │
│                  ▼                  ▼                                  │
│           ┌──────────────────────────────┐                             │
│           │  SQLite (WAL)                │                             │
│           │  单写连接 + 多读连接          │                             │
│           └──────────────────────────────┘                             │
│                                                                        │
│   StaticFiles: ui/ (index.html, app.js, styles.css, …)                 │
└────────────────────────────────────────────────────────────────────────┘
        │                    │                      │
        ▼                    ▼                      ▼
   浏览器              169bt.com                 Emby / Telegram
```

**为什么单进程**：日数据量 ~41 行，部署目标是「一个人用的私人工具」。
引入 Redis/Postgres/Celery 只会增加运维面，不解决任何实际问题。

**为什么不用 `aiosqlite`**：本机实测 `sqlite3.threadsafety == 3`（serialized），
`sqlite3` 模块本身线程安全。采集是**串行**的，写竞争不存在；
读操作走线程池即可。**少一个依赖，少一类 bug。**

### 2.2 子命令

```
169bt serve                      # 默认：HTTP + 采集 + Emby 同步（全功能）
169bt serve --no-collect         # 只读模式（调试前端 / 只浏览）
169bt collect                    # 一次性采集（给 cron 用，跑完即退）
169bt backfill --days 30         # 历史回填
169bt login                      # ★ 交互式登录，刷新 Cookie（约每月一次）
169bt emby sync                  # 一次性 Emby 核对
169bt migrate                    # 手动执行数据库迁移
169bt doctor                     # 自检：Cookie / Emby / TG / DB / 剩余登录次数
```

`serve` 与 `collect` 共用同一套采集代码，区别仅在调度由谁驱动（APScheduler vs cron）。
这样「进程内调度」与「系统 cron」两种部署方式都支持，不必二选一。

> **`login` 是独立子命令，且刻意不做自动重试。** 理由见 §5.2：登录失败次数按 IP 计，
> 每次失败都是不可逆的消耗。自动重试会把额度烧光，反而导致真正需要登录时无法登录。

### 2.3 部署形态

| 形态 | 适用 | 说明 |
|---|---|---|
| **A. 全功能单进程** | 推荐 | `169bt serve`，APScheduler 驱动采集与同步 |
| **B. 采集分离** | 想用系统 cron | `169bt serve --no-collect` + crontab `*/10 * * * * 169bt collect` |
| **C. 只读** | 前端调试 | `169bt serve --no-collect`，数据靠别处灌入 |

> 形态 B 的价值：采集失败不会拖垮 Web 服务；反之亦然。

---

## 3. 目录结构

```
169bt/
├── pyproject.toml
├── ARCHITECTURE.md
├── REQUIREMENTS.md
├── ui/                          # 前端（原样保留，零改动）
├── src/bt169/
│   ├── __main__.py              # CLI 入口、子命令装配（argparse）
│   ├── config.py                # 配置加载与校验
│   ├── crypto.py                # AES-256-GCM + 密码哈希
│   ├── db.py                    # 连接、WAL、迁移、单写连接
│   ├── migrations/*.sql
│   ├── models.py                # 领域对象 + DTO
│   ├── repo/
│   │   ├── posts.py             # 帖子仓储
│   │   ├── archive.py           # 归档日期 / 相邻导航
│   │   └── settings.py          # KV 配置仓储
│   ├── source/                  # ★ 169bt 论坛抓取（最复杂、最有风险的模块）
│   │   ├── base.py              # Source 协议
│   │   ├── forum.py             # httpx 客户端、限速、代理
│   │   ├── session.py           # Cookie 持久化与失效检测
│   │   ├── captcha.py           # ★ 验证码：预校验 + 多候选
│   │   ├── login.py             # 登录流程（含额度保护）
│   │   ├── thanks.py            # 感谢解锁
│   │   ├── discover.py          # RSS 发现
│   │   ├── backfill.py          # 列表页翻页回填
│   │   └── parse.py             # HTML → 领域对象
│   ├── collector.py             # 采集编排：状态机、重试、退避
│   ├── imagecache.py            # ★ 图片下载 + 缩放 + WebP（§5.8）
│   ├── emby/
│   │   ├── client.py            # Emby REST 客户端
│   │   └── matcher.py           # 番号提取与匹配（★ 防误判）
│   ├── telegram.py              # Bot API 客户端
│   ├── api/
│   │   ├── app.py               # FastAPI 应用装配
│   │   ├── deps.py              # 依赖注入（db / settings / images）
│   │   ├── auth.py              # 访问密码哈希 + 面板会话（S-6）
│   │   ├── gate.py              # ★ 门禁中间件（真正的安全边界）+ CSRF
│   │   ├── static.py            # ui/ 静态分发
│   │   └── routes/
│   │       ├── auth.py          # /api/auth/{login,logout,me}（S-6）
│   │       ├── posts.py         # 帖子 / 归档（含硬删除）
│   │       ├── images.py        # /img/* 分发（immutable）
│   │       ├── status.py        # /api/status（会话/采集器状态）
│   │       ├── forward.py       # TG 转发
│   │       ├── emby.py
│   │       ├── settings.py
│   │       └── health.py
│   └── scheduler.py             # APScheduler 驱动
├── tests/
│   ├── fixtures/
│   │   ├── html/                # ★ 录制的真实页面（已感谢/未感谢/无 ed2k）
│   │   └── captcha/             # ★ 30 张服务端已验证的验证码 + labels.json
│   └── test_*.py
├── data/                        # 运行时数据（不入版本控制）
│   ├── 169bt.db
│   └── images/                  # ★ 本地化图片（约 1 GB/年）
└── scripts/
    └── login_once.py            # 人工触发的一次性登录（不改动 DB 状态）
```

---

## 4. 数据层

### 4.1 修正后的 schema

> **修正了两处需求文档缺陷**：① 导航查询引用了不存在的 `deleted` 列；
> ② ~~软删除是「删除 + 撤销」（W-9）的必要条件~~
> **已改为硬删除**（用户决策）：删除即从数据库移除行与图片。
> 为同时满足 W-9 的「可撤销」，前端采用**延迟删除窗口**（见 `FRONTEND.md` §13）：
> 点击后 5 秒内可撤销（此时**未发任何请求**），超时才真正删除。
> 因此 schema 不需要 `deleted_at` 列。

```sql
CREATE TABLE posts (
  tid           INTEGER PRIMARY KEY,   -- 帖子 ID，天然去重
  title         TEXT    NOT NULL,
  code          TEXT,                  -- 番号，如 START-624（归一化大写）
  actress       TEXT,
  release_date  TEXT,                  -- 商品発売日 (YYYY-MM-DD)
  size          TEXT,
  cover_img     TEXT,
  detail_img    TEXT,
  ed2k          TEXT,
  post_date     TEXT    NOT NULL,      -- 归档依据 (YYYY-MM-DD, UTC+8)
  post_time     TEXT,                  -- 完整时间戳 (RFC3339, UTC+8)
  status        TEXT    NOT NULL,      -- pending|thanked|done|failed|nolink
  retry_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,                  -- 最近一次失败原因（诊断用）
  next_retry_at TEXT,                  -- 退避调度时间
  emby_status   TEXT,                  -- none|in_library（缓存，非真实来源）
  emby_item_id  TEXT,
  emby_checked  TEXT,                  -- 上次核对时间
  tg_sent_at    TEXT,                  -- NULL = 未转发
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL
);

-- 浏览路径只查已采集帖（★ 硬删除：无 deleted_at，删掉即不存在）
CREATE INDEX idx_browse   ON posts(post_date)      WHERE status='done';
CREATE INDEX idx_pending  ON posts(next_retry_at)  WHERE status IN ('pending','failed','thanked');
CREATE INDEX idx_code     ON posts(code)           WHERE code IS NOT NULL;
CREATE INDEX idx_emby     ON posts(emby_checked);
CREATE INDEX idx_tg       ON posts(tg_sent_at)     WHERE tg_sent_at IS NULL;

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  encrypted  INTEGER NOT NULL DEFAULT 0,   -- 1 = value 为密文
  updated_at TEXT NOT NULL
);

-- 论坛会话（Cookie 单行表）
CREATE TABLE forum_session (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  cookies     TEXT    NOT NULL,             -- JSON 序列化的 cookie 列表
  username    TEXT,
  obtained_at TEXT    NOT NULL,
  expires_at  TEXT,                         -- 预计失效（30 天）
  valid       INTEGER NOT NULL DEFAULT 1,
  -- ★ 登录额度（实测：按 IP 限 5 次）
  login_attempts_left INTEGER,              -- 最近一次响应解析出的剩余次数
  last_login_attempt  TEXT                  -- 上次真正提交登录的时间
);

CREATE TABLE panel_session (
  token_hash TEXT PRIMARY KEY,        -- 随机 32 字节 token 的哈希
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
```

> **为什么 `cookies` 用 TEXT/JSON 而不是 BLOB**：Python 侧用 `httpx.Cookies`，
> 其 jar 可直接迭代出 `(name, value, domain, path)` 四元组，JSON 存最自然，
> 且 `sqlite3` CLI 里可直接查看，便于排障。

### 4.2 状态机

```
                  ┌──────────┐
   发现新 tid ───▶ │ pending  │  已入库，等待处理
                  └────┬─────┘
                       │ 感谢成功
                       ▼
                  ┌──────────┐
                  │ thanked  │  已解锁，等待抓正文
                  └────┬─────┘
              抓到 ed2k │        │ 正文无 ed2k（感谢了但没链接）
                       ▼        ▼
                  ┌────────┐  ┌────────┐
                  │  done  │  │ nolink │  ← 需人工复查
                  └────────┘  └────────┘
                       ▲
        任意步骤失败    │  重试成功
                  ┌────┴─────┐
                  │  failed  │  next_retry_at 退避调度
                  └──────────┘
```

- `nolink` 是**新增状态**：需求里说「状态保持 `thanked` 但 ed2k 为空时标记待复查」，
  用显式状态比「靠 `ed2k IS NULL` 推断」更清晰，也便于在 UI 上单独筛出。
- 只有 `status='done'` 的帖进入浏览视图。

### 4.3 关键查询

```sql
-- 归档日期列表（W-2 / W-4：天然跳过无帖日期）
SELECT post_date, COUNT(*) AS n
FROM posts
WHERE status = 'done'
GROUP BY post_date
ORDER BY post_date ASC;

-- 相邻日期导航（W-12 边界禁用靠返回 NULL 判断）
-- 更早：SELECT post_date FROM … WHERE post_date < ? ORDER BY post_date DESC LIMIT 1
-- 更晚：SELECT post_date FROM … WHERE post_date > ? ORDER BY post_date ASC  LIMIT 1
```

> 导航**必须**用这两条 SQL，不能 `date ± 1 day`（W-4 明确要求）。
> 本机实测：`EXPLAIN QUERY PLAN` 显示 `SCAN posts USING INDEX idx_browse`，
> 部分索引被实际采用。

### 4.4 并发访问

本机实测（附录 A.7）：

- WAL 模式下**读写不互斥**：写事务未提交时，读连接看到旧快照（0 行），提交后看到新值
- 两个连接同时 `BEGIN IMMEDIATE` → `database is locked` → **必须串行化写**

因此：

```python
# 写连接：单例 + 全局锁（串行化所有写）
_write = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
_write.execute("PRAGMA journal_mode=WAL")
_write.execute("PRAGMA synchronous=NORMAL")   # WAL 下 NORMAL 已足够安全
_write.execute("PRAGMA busy_timeout=5000")
_write.execute("PRAGMA foreign_keys=ON")
_write_lock = threading.Lock()

# 读连接：每线程一个（sqlite3 连接不跨线程共享）
_read_local = threading.local()
```

写入量极小（峰值 41 行/天 + Emby 状态更新），单写者不构成瓶颈。

---

## 5. 模块设计

### 5.1 `source` —— 论坛抓取

**职责**：把「169bt 论坛」这个不可靠、有风险的外部系统，封装成一个可靠、可测的接口。

```python
class Source(Protocol):
    def discover(self, opts: DiscoverOpts) -> list[Candidate]:
        """发现新帖（RSS 优先，必要时回退列表页）"""

    def fetch(self, tid: int) -> Post:
        """抓取单帖详情；若 ed2k 未解锁则自动「感谢」后重取"""

    def backfill(self, frm: date, to: date, yield_: Callable[[Post], None]) -> None:
        """按时间范围回填历史"""

    def health(self) -> HealthStatus:
        """自检：Cookie 是否有效、能否访问目标版块"""

@dataclass(slots=True)
class Candidate:
    tid: int
    title: str
    post_time: datetime      # 已归一化为 UTC+8

@dataclass(slots=True)
class Post:
    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None     # 商品発売日
    size: str | None
    cover_img: str | None
    detail_img: str | None
    ed2k: str | None             # None = 未解锁
    post_date: str               # YYYY-MM-DD, UTC+8 ← 归档依据
    post_time: datetime
    unlocked: bool
```

调用方**完全不知道**：Cookie 怎么存、验证码怎么识别、感谢怎么触发、
限速怎么加、镜像怎么切换。这些全在 `source` 内部。

**POST 重定向（PRG）——实测事实（2026-09-19）**：

- 感谢插件（`thanksplugin:thanks`）首次感谢后返回 **301**（而非 302）
  跳回帖子页——Discuz `dheader()` 的历史行为，PRG 模式；
  重复感谢则返回 200 提示页（「已感謝過了」，幂等）。
- 因此 `ForumClient.post()` **默认跟随 3xx**（上限 5 跳防环，
  跳登录页仍抛 `LoginRequired`）；曾因把 301 当错误，感谢在服务端
  明明已成功、15 帖却被误标 failed。
- inajax 接口（登录 POST）响应体即判定结果，不走 PRG——
  调用时显式传 `follow_redirects=False` 保持严格。

#### 5.1.1 会话管理（`session.py`）

Cookie 持久化到 SQLite，进程重启不丢。启动时自动检测有效性：

```python
# 判定「会话失效」的信号（任一命中即视为失效）
#   1. HTTP 302 跳转到 member.php?mod=logging
#   2. 响应体含 "您需要登录"        ← 实测确认该串存在（A.4）
#   3. 目标版块内容缺失
```

失效后触发重新登录；登录失败则**告警但不抛异常**（采集暂停，浏览继续）。

#### 5.1.2 限速（`forum.py`）

双重限速，缺一不可：

```python
# 1. 全局最小间隔：上限保护
# 2. 每帖随机抖动：模仿人类，避免固定间隔特征
delay = random.uniform(2.0, 5.0)   # C-7 要求「2–5 秒随机」
```

> **只做固定间隔不够**——固定 3 秒本身就是可识别特征。
> 并发度**恒为 1**。这不是性能取舍，是账号安全要求。

#### 5.1.3 验证码（`captcha.py`）—— 本次实测的重点

**这是整个系统唯一有「外部依赖 + 概率性」的环节，因此单独设计。**

**实测事实**（附录 A.5 / A.6）：

| 事实 | 数值 |
|---|---|
| 单模型准确率（无偏，n=40） | 默认 **50.0%** / beta **55.0%** |
| 两候选（默认+beta）取或 | **67.5%** |
| 平均每图 check 次数 | 1.50 |
| 单图可校验次数 | **约 3 次**，超出后该图的码失效 |
| 每次重新取图 | **轮换**为新码 |
| check 端点是否消耗码 | **不消耗**（同一码可重复校验，直到 3 次上限） |
| 大小写是否敏感 | **不敏感**（`EcTk` / `ECTK` 均可通过） |
| 生产策略实测（每图 2 候选，最多 6 张图） | **40/40 求解成功**，平均 **1.48** 张图 |

**关键设计：先离线校验验证码，再提交登录。**

Discuz 提供了 `misc.php?mod=seccode&action=check` 端点，它**独立于登录表单**。
这意味着我们可以在**不提交登录**的前提下，确认验证码是否正确：

**架构设计：Solver 是可替换接口。**

由于实测证明 Go 与 Python 的 OCR 输出等价（附录 A.12），验证码求解器被抽象为接口：

```python
class Solver(Protocol):
    def classify(self, png: bytes) -> str: ...
```

| 实现 | 依赖 | 状态 |
|---|---|---|
| `PythonSolver` | `ddddocr`（14.7k ★） | **默认**。需求文档 §6 原本推荐 |
| `GoSidecarSolver` | 子进程调用 Go 侧车（内嵌模型+dylib） | 备选。实测输出等价，适合想「零 Python 依赖」时 |
| `ManualSolver` | 无 | 降级：提示人工输入（每月一次，可接受） |

这样「Go vs Python」从一个**架构级**决定降级为一个**可替换的实现细节**。

```python
def solve_and_verify(client, idhash, solver_d, solver_b) -> str | None:
    """取图 → OCR 多候选 → 用 check 端点预校验 → 返回已确认正确的码。

    每张图最多消耗 2 次 check（实测上限约 3 次，留 1 次余量）。
    最多换 6 张图（实测 40/40 在 6 张内求解成功，平均 1.48 张）。
    """
    for _ in range(6):
        img = client.fetch_captcha(idhash)          # 每次取图 → 新码
        candidates = dedupe(
            solver_d.classify(img),                # 实测 50.0%
            solver_b.classify(img),                # 实测 55.0%
        )
        for code in candidates[:2]:                 # ★ 硬上限 2 次 check
            if client.check_captcha(idhash, code):  # 不消耗、不触发登录计数
                return code
    return None
```

> **这条设计把「登录失败」从概率事件变成了确定性事件。**
> 实测验证（附录 A.6）：用**不存在的用户名** + 预校验通过的验证码提交登录，
> 服务器返回的是「登录失败，您还可以尝试 N 次」（**账号/密码**错误），
> 而**不是**「抱歉，验证码填写错误」。
> 说明验证码门禁**已被真正通过**，且预校验结果对登录提交有效。

**验证码端点**（实测）：

```
取图    GET  misc.php?mod=seccode&update={毫秒时间戳}&idhash={loginhash}
            ⚠️ 必须带 Accept: image/* 与 Referer，否则返回 13 字节 "Access Denied"
校验    GET  misc.php?mod=seccode&action=check&inajax=1
             &modid=member::logging&idhash={loginhash}&secverify={code}
        →  <?xml version="1.0" encoding="utf-8"?>
            <root><![CDATA[succeed]]></root>      ← 注意是 CDATA 包裹！
```

> ⚠️ **踩坑记录**：解析时必须剥掉 `<![CDATA[…]]>`。
> 若直接 `body.startswith("succeed")` 会**永远为假**，
> 导致把「100% 失败」当成测量结果（本次实测中确实先踩了这个坑，误差极大）。

**候选生成策略**：

- 同时跑 `beta=False` 与 `beta=True` 两个模型（两者误差不相关，取或显著提升）
- 实测 `set_ranges()` 限制字符集**无收益**，故不使用
- 实测**图像预处理反而严重降低准确率**，故不做任何预处理，直接把原图喂给模型

预处理对照实测（n=30，命中 = 两模型取或命中真值，**不消耗 check 额度**）：

```
  raw            30/30 = 100.0%   ← 原始图最佳
  up3x           30/30 = 100.0%   （放大无损，但无收益）
  autocontrast   29/30 =  96.7%
  median3        12/30 =  40.0%   ← 中值去噪
  bin160          9/30 =  30.0%   ← 二值化
  bin128          9/30 =  30.0%
  bin100          4/30 =  13.3%   ← 二值化（阈值 100）
```

> 二值化把准确率从 100% 打到 13–30%，中值去噪打到 40%。
> 根因：验证码带**彩色噪声**，二值化把噪声一并变成实心黑点，反而增加干扰。
> **结论：直接把原 PNG 喂给模型。**（ADR-5）

**为什么不做「重试登录」**：登录失败计数按 IP 计（实测 4→3→2→1，且**换用户名不重置**）。
重试登录会把 5 次额度烧光。**先校验后登录**使验证码错误根本不消耗登录额度。

#### 5.1.4 解析（`parse.py`）

所有解析用**容错正则**，字段缺失置空而非报错（风险表「字段格式变动」）。
DOM 定位用 `selectolax`（比正则稳、比 lxml 快），正则只用于**字段值提取**。

```python
RE_CODE    = re.compile(r'(?i)\b([A-Z]{2,6}-\d{2,5})\b')
RE_ACTRESS = re.compile(r'\[出演者\]\s*[:：]\s*(.+)')
RE_RELEASE = re.compile(r'商品発売日\s*[:：]\s*(\d{4}/\d{2}/\d{2})')
RE_SIZE    = re.compile(r'\[影片大小\]\s*[:：]\s*(.+)')
RE_ED2K    = re.compile(r'ed2k://\|file\|[^|]+\|\d+\|[0-9A-Fa-f]+\|/')
```

★ **`值@注解` 约定**：站点把「值 + 补充说明」写成一行，用 `@` 分隔——
`[影片大小]：7GB@NO Watermark`、`[有码无码]：有码@无水印`、
`[種子期限]：1天@Please Seed`。所以 `7GB` 是值，`NO Watermark` 是注解。
取值的字段一律过 `_strip_annotation()`（只切**第一个** `@`，注解自身还可能带 `@`），
否则注解会跟着值一起入库。已入库的脏值由 `005_fix_size.sql` 洗，
`bt169 doctor` 会检查是否残留。

**测试策略**：`tests/fixtures/html/` 存真实帖子页 HTML（已感谢 / 未感谢 / 无 ed2k 三种），
解析测试**离线跑**，不依赖网络。这是整个项目最需要测试覆盖的地方。

### 5.2 `login` —— 登录、自动重登录与额度保护

#### 5.2.1 登录流程

登录流程（实测已验证，附录 A.6）：

```
1. GET  member.php?mod=logging&action=login
        → 取 formhash + loginhash + Cookie
2. solve_and_verify()                      ★ 先用 check 端点确认验证码
3. POST member.php?mod=logging&action=login&loginsubmit=yes
        &loginhash={LH}&inajax=1
   body: formhash, referer, loginfield=username, username, password,
         questionid=0, answer=, seccodehash={LH},
         seccodemodid=member::logging, seccodeverify={CODE},
         cookietime=2592000, loginsubmit=true
4. 解析响应：
   ├─ "欢迎您回来"                 → 成功，持久化 Cookie
   ├─ "验证码填写错误"             → ★ 不应出现（预校验已保证）；出现即视为实现 bug，告警
   └─ "登录失败，您还可以尝试 N 次" → 密码错误；记录 N，并**停止一切登录尝试**
```

**额度保护（硬规则）**：

```python
MAX_LOGIN_ATTEMPTS = 5            # 实测：按 IP 计
SAFETY_MARGIN      = 1            # 永远留 1 次
WINDOW_SECONDS     = 900          # ★ 实测推断：Discuz 默认 5 次 / 900 秒自动重置

# 规则 1：只有预校验通过的验证码才允许提交登录
# 规则 2：解析出剩余次数 N 后写回 DB；N <= SAFETY_MARGIN 时拒绝再次提交
# 规则 3：连续两次登录提交间隔 >= 10 分钟（避免误触发风控）
# 规则 4：自动重登录每轮【只提交 1 次】；失败即熔断，不重试（§5.2.2）
```

> ⚠️ **关于「5 次额度」的更正**：早先记录为「永久限制」，不准确。
> 查证 Discuz 源码（`source/function/function_member.php` 的 `logincheck()`）：
>
> ```php
> $return = (!$login || (TIMESTAMP - $login['lastupdate'] > 900)) ? 5 : max(0, 5 - $login['count']);
> ```
>
> **默认是 5 次 / 900 秒自动重置**（计数存服务端 `common_failedlogin` 表，按 IP，不在 Cookie）。
> 实测确认 Cookie 罐中无计数项。
> **但该默认值来自 X1.5–X3.3 源码，新版行为可能有差异** → 实现时须实测校准。
>
> 另实测确认：**提交错误验证码不消耗额度**（返回「验证码填写错误」，不进入计数逻辑）。
> 这恰好证明「先预校验」策略有效，也意味着**剩余次数无法廉价读取**
> （只有真正失败的登录响应才返回它）。

#### 5.2.2 自动重登录（无感续期）

**目标**：让会话失效对用户**无感**。核心思路是**主动续期**而非被动修复。

```
① 主动续期（主要）—— 在额度充裕时从容换新 Cookie
   Cookie 有效期 30 天 → 第 25 天自动重登录
   此时无紧急压力、额度满格、失败可从容处理

② 被动修复（兜底）—— 会话提前失效
   采集中检测到失效信号 → 触发重登录
```

**为什么主动续期是核心**：被动修复时系统已经「瞎了」（抓不到数据），
而主动续期时一切正常 —— 这消除了「凌晨 3 点静默失效」场景。

**会话失效信号**（`session.py`）：

| 信号 | 判定 |
|---|---|
| 详情页出现「您需要登录」 | 确定性失效 |
| HTTP 302 → `member.php?mod=logging` | 确定性失效 |
| ed2k 位置为「回复可见」但 Cookie 存在 | 确定性失效 |

> ★ **必须区分「会话失效」与「帖子本身无 ed2k」**（后者是正常 `nolink` 状态）。
> 判定依据：失效时**同一批帖子全部**拿不到 ed2k；`nolink` 只是个别帖子。

**重登录流程（带熔断）**：

```
检测到失效 / 到达续期时间
   │
   ├─▶ ① 立即暂停采集            ← 防止持续消耗额度
   │
   ├─▶ ② 检查 900s 窗口
   │      距上次失败 < 900s ？ ──是──▶ 等待窗口过去，不尝试
   │
   ├─▶ ③ solve_and_verify()      ← ★ 此阶段不消耗登录额度
   │      最多 6 张图，每图 ≤2 候选（实测 40/40 在 6 张内成功）
   │
   ├─▶ ④ 提交登录  ← ★ 预算 1 次
   │      成功 ──▶ 持久化 Cookie + 恢复采集 + TG「已自动重新登录」
   │      失败 ──▶ ⑤
   │
   └─▶ ⑤ 熔断 + TG 告警「需人工介入」
          不再自动重试（避免烧光额度）
```

**为什么预算只有 1 次**：

- 预校验机制让验证码**几乎不会错**（实测 40/40 求解成功）
- 若 1 次仍失败 → 是**账号/密码错误**或 **IP 被限** → 重试无意义，只会烧额度
- 无限重试是**最危险的模式**：密码改了你不知道，额度烧光后连人工登录都做不了

**「无感」的边界**：

| 场景 | 用户体验 |
|---|---|
| 主动续期成功 | **完全无感**（你永远不会知道发生过） |
| 会话提前失效 + 修复成功 | 无感（最多列表延迟几分钟） |
| 修复失败 | **TG 告警**「Cookie 失效且自动登录失败，请检查账号密码」 |

> 第三行是**必须的** —— 否则退化为「静默失效」，这正是 `/api/status` 要避免的。

**手动出口必须保留**：`169bt login` 子命令是自动流程失败时的唯一出口。

#### 5.2.3 会话状态暴露

```python
# 写入 forum_session 表，由 GET /api/status 暴露（FRONTEND.md §14.1）
expires_at       = obtained_at + 30d      # 预计失效
last_relogin_at  = 上次自动重登录时间
relogin_state    = 'ok' | 'retrying' | 'failed' | 'blocked'
last_error       = 最近一次错误（脱敏）
```

### 5.3 `collector` —— 采集编排

**职责**：驱动 `source`，管理状态机、重试退避、以及「一轮采集」的原子性。

```python
class Collector:
    def run_once(self) -> Stats:
        """执行一轮：发现 → 去重 → 逐帖抓取 → 入库"""
```

编排逻辑：

```
1. discover()                    → 候选 tid 列表
2. 过滤已入库的 tid（C-2 增量去重）
3. for each 新 tid:              ← 串行，每帖间隔 2–5s
     a. fetch(tid)               → 内部自动感谢解锁
     b. 解析字段 + ed2k
     c. upsert 到 DB（status=done / nolink）
     d. 每帖之间 sleep(2–5s 随机)
4. 处理 failed 队列（next_retry_at <= now），指数退避
5. 记录 Stats（新增 / 失败 / 耗时）
6. 若有新增：触发 on_job_done 回调     ← 采完立即查 Emby（见 §5.4）
```

**完成回调（`on_job_done`）**：

- 收到**本次采到**的 tid 列表（跳过的不算）；全跳过则不触发。
- **时序契约：回调返回之后任务才落 done**——前端轮询看到 done
  就能保证 Emby 徽章已写入库。回调异常只记日志，不影响任务状态。
- 取消 / 会话失效路径不触发（用户主动停或没采到东西，不必等
  一次 Emby 往返）。
- CLI 路径不接线（`bt169 collect` 无 Emby 需求，保持默认 None）。

**退避策略**（C-7）：

```python
backoff = min(base * 2 ** retry_count, 3600) * random.uniform(0.8, 1.2)
# retry_count 上限 5 次后进入「人工介入」告警
```

**连续失败熔断**：连续 5 帖失败 → 暂停本轮，发 TG 告警。
防止把「账号被封」误判为「网络抖动」而疯狂重试。

### 5.4 `emby` —— 入库标记

**职责**：拉取 Emby 媒体库，按**番号**匹配本地帖，把结果**写回 DB**。

**架构要点：浏览路径绝不调用 Emby。**

```
Emby Syncer (定时器：emby.cron 或默认 15 分钟；或采集任务收尾时立即触发)
    │
    ├─ 拉取媒体库全部条目（IncludeItemTypes=Movie，只要 Name/Path/Id）
    ├─ 从条目名中提取番号 → 建索引 {code: item_id}
    ├─ 批量匹配本地 posts.code（采集路径只匹配本次采到的 tid）
    └─ 写回 posts.emby_status / emby_item_id / emby_checked

浏览请求 → 读 posts.emby_status（纯本地，永不阻塞）  ← E-7 失败降级
```

**定时节奏（E-6）——与 RSS 轮询同一套 cron 机制**：

- 设置页 Emby 分区新增 `emby.cron`（如 `*/30 * * * *`）；保存时
  落库前 `parse_cron` 校验并归一，非法 → 400 `bad_cron`（与
  `site.rss_cron` 同一规则，同一代码路径）。
- `EmbyScheduler.next_delay` 闭包每次触发后重读 `emby.cron`——
  改表达式**无需重启**（同 E-9 的模式）。
- `emby_delay_seconds()`：空 → 默认 15 分钟（`EMBY_FALLBACK_SECONDS`，
  即 E-6 现行为不变）；非法 → 默认 + 警告日志（旧库/手改库不能弄死
  线程）；合法 → 距下次触发秒数，夹到 ≥ 1 s 防 NTP 回拨忙循环。
- 与 RSS 空 cron 语义不同：RSS 空 = 醒来但不干活（抓论坛烧额度），
  Emby 空 = 按默认节奏干活（同步只读本地 + 一次 Emby API）。

**采集完成后的立即检查（手动 + RSS 两条路径共用）**：

- `Collector.on_job_done` 回调（见 §5.3）在任务落 done **之前**触发，
  携带本次采到的 tid；装配层（`routes/collect.py`）把它接到 Emby。
- 回调用 `EmbySyncer(only_tids=…)` 只查本次采到的帖子——万帖库里
  其余帖子刚被定时器查过，不必陪跑；新卡片的「已入库」角标
  在采集结束的瞬间就出现，不等 15 分钟。
- 「先查 Emby、后落 done」的时序契约：前端轮询看到 done 时徽章
  必已写入库，不会出现「任务完成但角标缺席」的窗口。
- 三条同步路径（手动刷新 / 定时器 / 采集回调）共用 `routes/emby.py`
  的模块级 `_sync_lock`；锁被占用（正在同步）时采集回调直接跳过——
  定时器稍后会覆盖，不排队。
- 全程不抛异常：未配置 Emby → debug 日志；Emby 不可达 →
  `SyncResult(error=…)`，采集任务状态不受影响（E-7）。

#### 5.4.0 媒体库列表按用户过滤（E-8）

设置页的「媒体库」下拉从两个不同端点取数，**id 字段名不一样**：

| 场景 | 端点 | id 字段 | 实测结果 |
|---|---|---|---|
| 配了 `emby.username` 且能找到 | `/Users/{userId}/Views` | **`Id`** | muse 11 库 / xy 8 / ym 8 |
| 没配，或用户名找不到 | `/Library/VirtualFolders` | **`ItemId`** | 22 库（管理员视角） |

**两个 id 命名空间一致**（muse「有码」`Id=1373` == VirtualFolders `ItemId=1373`），
所以 `emby.library` 已存的值**不需要迁移**。

★ **过滤不是安全边界**（用户拍板）：它只是「帮你少看几个库」。所以用户名找不到时
**退回全部库**并返回 `warning`，而不是报错或给空列表。

响应形状：

```json
{ "ok": true, "libraries": [{"id": "1373", "name": "有码"}],
  "source": "user", "filtered_by": "muse", "warning": null }
```

- `source="user"`：已按该用户权限过滤
- `source="all"`：管理员视角（没配用户名，或用户名找不到 → `warning` 非空）

★ **用户名匹配大小写敏感、精确**（ADR-20）。

#### 5.4.1 番号匹配（`matcher.py`）—— E-5 防误判

**朴素 `in` 判断是错的**：`START-62` 会命中 `START-624`。本机实测复现：

```
本地番号 START-62，Emby 只有 "START-624 配送中NTR …"
  朴素 Contains 判定已入库? True    ← ★ 假阳性
  双向提取+精确比对判定?   False   ← 正确
```

正确做法是**双向提取 + 精确比对**：

```python
CODE_RE = re.compile(r'(?i)(?:^|[^A-Za-z0-9])([A-Z]{2,6}-\d{2,5})(?:[^0-9]|$)')

def extract_codes(name: str) -> set[str]:
    """从 Emby 条目名中提取「番号形状」的 token（而不是拿本地番号去 in）"""
    # "START-624 配送中NTR…"            → {"START-624"}
    # "[4K] START-624 [中文字幕]"       → {"START-624"}
    # "s169bbs.com@START-624_[4K].mkv"  → {"START-624"}
    return {m.group(1).upper() for m in CODE_RE.finditer(name)}
```

这样 `START-62` 与 `START-624` 是**两个不同的 token**，天然不会误判。

**表驱动测试已实测 13/13 通过**（附录 A.8），覆盖：
`START-624` / `START-62` / `START-6240` / `start-624` / `START624`（无连字符，不提取）/
`SSIS-001` / `SSIS-01` / 中文前缀 / 域名前缀 / 超长字母 / 超长数字。

### 5.5 `telegram` —— ed2k 转发

**传输层（2026-09 起）：MTProto（用户账号）。** 探针实测：Bot API 对
「bot 发消息给另一 bot」直接返回 `400 USER_BOT_TO_BOT_DISABLED`
（NS Bot 未开 Bot-to-Bot 模式，且该开关在对方服务手里），Bot 路径对
NS Bot 不可达。因此转发链路改用 Telethon 用户账号发送——与用户手动
发送同构，对方服务必然按真人消息处理。

```
传输层统一契约（duck typing）：
  send(text) -> message_id        # MtpSender（Telethon）与 TelegramClient（Bot API）同构
  test() -> None                  # 设置页「测试」按钮

业务层 telegram.py（不变）：forward_post / forward_many
  —— 幂等（tg_sent_at）、串行 + 3s 间隔、FloodWait/429 停手
```

登录向导（`tglogin.py`）：三步状态机 `start(phone)→verify(code)→[password]`，
进程内单例、5 分钟超时、临时客户端与转发长连接隔离。**发码冷却（用户要求）**：
`start` 无论成败 30 秒内再点 → 429（剩余秒数透传前端禁用按钮）——发码是
Telegram 严格限频操作，反复触发会让 FLOOD_WAIT 越拉越长。端点：
`POST /api/tg/login/{start,verify,password,cancel}`；验证码错/密码错 → 422 结构化
code（`tg_code_invalid`/`tg_password_invalid`），状态保留可重试。

凭据（`tg.api_id` / `tg.api_hash` / `tg.session` / `tg.target`）：
`api_hash` 与 StringSession **等于账号本身**，均走 SecretBox 加密落库
（键名最后一段 `api_hash`/`session` 已入 `SECRET_FIELDS`）。登录用
一次性 CLI `bt169 tg-login`（交互式手机号/验证码），Web 进程不做交互登录。
`get_sender()` 是进程内单例：凭据签名不变即复用长连接。

Bot API 客户端（`TelegramClient`）保留，仅用于**通知推送**
（设置页测试按钮的 Bot 通道）；转发不再走它。

#### 5.5.1 `sendMessage` vs `forwardMessage` —— 澄清需求 §8.9

需求 T-1 写「**转发** ed2k 到 TG Bot」，但 Telegram 的 `forwardMessage`
要求**源消息已存在于某个会话**。我们手里只有一段 ed2k **文本**，没有可转发的消息对象。

**结论：必须用 `sendMessage`。** 需求里的「转发」是中文口语意义上的「发过去」，
不是 Telegram API 的 `forwardMessage`。

#### 5.5.2 批量语义（T-2 / 需求 §8.10）

两种都实现，由配置决定：

| 模式 | 行为 | 适用 |
|---|---|---|
| `single` | 当日全部 ed2k 合成**一条**消息（换行分隔） | 想一眼看全、减少刷屏 |
| `per_post` | 逐帖一条消息 | 想让 Bot 侧**逐条**入队下载 |

> 若 Bot 侧是 aria2/qBittorrent 插件，**`per_post` 更可能正确**——
> 一条含多个链接的消息可能只被解析出第一个。**默认 `per_post`**。

#### 5.5.3 幂等与限流（T-7 / T-8）

```python
# 幂等：发送前检查 tg_sent_at，成功后写入
# 但「已发送」不等于「已下载」——UI 的语义是「已提交」
if post.tg_sent_at is not None:
    raise AlreadySent

# 限流：Telegram 群组限制约 20 条/分钟
# 429 响应带 retry_after，必须遵守
if resp.status_code == 429:
    sleep(resp.json()["parameters"]["retry_after"])
```

批量转发时**串行发送 + 每条间隔 ≥ 1s**，避免触发 429。

### 5.6 `api` —— FastAPI

**职责**：把 SQLite 里的数据暴露给前端；静态分发 `ui/`。

**关键设计：API 字段名直接对齐前端**（`app.js` 消费的字段）：

```python
class PostDTO(BaseModel):
    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover: str | None          # ← 映射自 cover_img
    detail: str | None         # ← 映射自 detail_img
    ed2k: str | None
    post_date: str
    emby_in_library: bool      # E-2 角标数据源
    tg_sent_at: str | None = None
```

> 这样前端只需把 `ui/data.js` 的 `window.DEMO_POSTS` 换成 `fetch('/api/posts?date=…')`，
> **渲染逻辑一行不用改**。

静态资源用 `StaticFiles`：

```python
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
```

### 5.7 `scheduler` —— 调度

```python
# 采集：每 5 分钟（需求 §8.5，默认取偏保守值）
sched.add_job(collector.run_once, "interval", minutes=5,  max_instances=1)

# Emby 同步：每 15 分钟
sched.add_job(emby_sync, "interval", minutes=15, max_instances=1)

# 会话自检：每 6 小时
sched.add_job(source_health, "interval", hours=6, max_instances=1)

# ★ 会话主动续期：每 12 小时检查一次（剩 <=5 天时触发重登录，§5.2.2）
sched.add_job(session_renew_if_needed, "interval", hours=12, max_instances=1)
```

用 `interval` 触发器而非 cron 表达式：需求是**固定间隔**，不是「每天 2 点」。
`max_instances=1` 保证不重入（与「采集串行」的硬约束一致）。

**启动时立刻跑一次**，不等第一个 tick。同时保证优雅退出：

```python
# 收到 SIGINT/SIGTERM → 停止调度 → 等当前帖抓完（最多 30s）→ 退出
```

---

### 5.8 `imagecache` —— 图片本地化（性能核心）

**职责**：把图床图片下载、缩放、转为 WebP 并本地存储，使前端读同源路径。

**实测依据**（`FRONTEND.md` §11.2）：

```
源图      : 2184×1542, 863.7 KB（均值 n=5）
卡片实际需要: 300×200  → 仅用 1.8% 像素
缩放后    : 600px WebP q80 → 65 KB（省 91%）
转码耗时  : 42.7 ms/张
```

**规格**：

| 用途 | 尺寸 | 格式 | 体积 |
|---|---|---|---|
| 卡片缩略图 | 600×400 | WebP q80 | 65–67 KB |
| 灯箱大图 | 1200×800 | WebP q80 | ~150 KB |

**存储布局**：

```
data/images/
└── ab/                          # 哈希前 2 位分桶（256 目录）
    ├── ab3f9c1d-600.webp
    └── ab3f9c1d-1200.webp
```

- 文件名 = `sha1(源 URL)[:16]` → **天然去重**（同图多帖引用只存一份）
- 分桶避免单目录 10 万文件（约 400 文件/目录/年）

**关键设计**：

```python
class ImageCache:
    def ensure(self, src_url: str) -> str:
        """下载 + 缩放 + 存储，返回本地相对路径。已存在则直接返回。"""
        key = sha1(src_url.encode()).hexdigest()[:16]
        out = self.root / key[:2] / f"{key}-600.webp"
        if out.exists():
            return f"/img/{key[:2]}/{key}-600.webp"
        # 条件请求：图床有 etag（实测）→ 重复采集不重下
        resp = self.http.get(src_url, headers={"If-None-Match": self.etags.get(key, "")})
        if resp.status_code == 304:
            return f"/img/{key[:2]}/{key}-600.webp"
        ...
```

**为什么在采集时做而不是请求时做**：

- 采集是**串行限速**的（每帖 2–5 s），43 ms 转码可忽略
- 请求时做会让首次浏览变慢，且并发转码会吃 CPU
- 预生成后 `StaticFiles` 直接分发，零开销

**删除联动**（§5.6）：硬删除帖子时必须删除图片，但需**引用计数**保护：

```sql
SELECT COUNT(*) FROM posts WHERE cover_img LIKE '%'||?||'%' OR detail_img LIKE '%'||?||'%';
```

计数为 0 才删文件。顺序：**先删行、提交事务、再删文件**
（失败时只产生孤儿文件，`169bt doctor` 可清理；反序会产生裂图，更糟）。

**分发缓存头**：`Cache-Control: public, max-age=31536000, immutable`
（文件名含内容哈希 → 可永久缓存）。

---

## 6. 关键流程

### 6.1 一轮采集

```
APScheduler (5min)
  │
  ├─▶ Collector.run_once()
  │     │
  │     ├─▶ source.discover()
  │     │     └─ GET forum.php?mod=rss&fid=192      ← 实测可匿名访问（A.9）
  │     │        → 20 条 item → 解析 tid + pubDate
  │     │        → pubDate(UTC) → 归一化 UTC+8      ← C-9
  │     │
  │     ├─▶ repo.filter_new(tids)                   ← C-2 去重
  │     │
  │     └─▶ for each new tid:                       ← 串行！
  │           │
  │           ├─▶ source.fetch(tid)
  │           │     ├─ GET viewthread&tid=…         取 formhash
  │           │     ├─ POST plugin.php?id=thanksplugin:thanks  ← C-4 幂等
  │           │     ├─ GET viewthread&tid=…         重取
  │           │     └─ parse → Post
  │           │
  │           ├─▶ repo.upsert(post)
  │           └─▶ sleep(2–5s 随机)                  ← C-7
  │
  └─▶ Stats{new: n, failed: m, elapsed: d}  → 日志 / TG 告警
```

### 6.2 登录（约每月一次，人工触发）

```
169bt login
  │
  ├─▶ 检查登录额度（DB 里的 login_attempts_left）
  │     └─ <= 1 → 拒绝执行，提示等待重置
  │
  ├─▶ GET 登录页 → formhash + loginhash
  ├─▶ solve_and_verify()                ★ 最多 6 张图，每图 2 候选
  │     └─ 全部失败 → 中止（未提交任何登录请求，零额度消耗）
  │
  ├─▶ POST 登录（携带已确认的验证码）
  │     ├─ 成功 → 持久化 Cookie + 更新 expires_at(+30 天)
  │     └─ 失败 → 记录剩余次数，**停止**，告警
  │
  └─▶ 立即用新 Cookie 验证一次 health()
```

### 6.3 Emby 核对

```
APScheduler (15min) 或 POST /api/emby/refresh
  │
  ├─▶ emby.list_items(library_id)      ← 只拉 Name/Path/Id
  │     └─ 失败？→ 记日志，保持上次 emby_status，**不报错**（E-7）
  │
  ├─▶ matcher.build_index(items)       ← extract_codes + normalize
  │
  ├─▶ repo.list_codes()                ← 本地全部番号
  │
  ├─▶ 求交集 → 更新 emby_status / emby_item_id / emby_checked
  │
  └─▶ 前端下次 GET /api/posts 即看到角标（无需推送）
```

### 6.4 卡片「下载」= TG 转发

```
点击卡片「下载」
  │
  ├─▶ POST /api/posts/{tid}/forward
  │     │
  │     ├─ 校验：TG 已配置？(T-4) ed2k 非空？(T-5)
  │     ├─ 校验：tg_sent_at IS NULL？(T-7 幂等)
  │     ├─▶ telegram.send_message(chat_id, ed2k)
  │     │     └─ 429 → 读 retry_after 退避重试
  │     ├─▶ repo.mark_tg_sent(tid, now)
  │     └─▶ 200 {ok: true, message_id: …}
  │
  └─▶ 前端 Toast：「已提交下载 START-624」(T-6)
```

---

## 7. HTTP API 契约

统一前缀 `/api`。错误响应：

```json
{ "error": { "code": "tg_not_configured", "message": "尚未配置 Telegram Bot" } }
```

| 方法 | 路径 | 请求 | 响应 | 需求 |
|---|---|---|---|---|
| GET | `/api/health` | — | `{status, version}`（**无需认证**，供 Lucky/存活探测） | — |
| GET | `/api/dates` | — | `[{date, count}]` 升序 | W-2 W-4 |
| GET | `/api/posts` | `?date=` | `[PostDTO]` | W-5~W-8 |
| GET | `/api/archive/export` | `?date=` | `text/plain`，一行一个 ed2k | W-1 |
| DELETE | `/api/posts/{tid}` | — | `204`（★ 硬删除：行 + 图片） | W-9 W-11 |
| POST | `/api/posts/{tid}/delete` | — | `204`（同上，供 `sendBeacon` 使用） | W-9 |
| POST | `/api/collect` | `{from_date, to_date}` | `202 {job}`；`409` 已有任务；`400` 范围非法 | C-10 |
| GET | `/api/collect/status` | — | `{job \| null, recent[]}`（前端轮询） | C-10 |
| GET | `/api/collect/jobs/{id}` | — | `{job}`；`404` | C-10 |
| POST | `/api/collect/jobs/{id}/cancel` | — | `200 {job}`；`409` 未在跑 | C-10 M-7 |
| GET | `/api/status` | — | 会话/采集器状态（`FRONTEND.md` §14.1） | F-C8 |
| GET | `/img/{hash}-{w}.webp` | — | 本地化缩略图（`immutable`） | F-C10 |
| POST | `/api/posts/{tid}/retry` | — | `202`（重排采集） | C-8 |
| POST | `/api/posts/{tid}/forward` | — | `{ok, message_id}` | W-13 T-1 |
| POST | `/api/archive/forward` | `?date=` | `{sent, skipped, failed}` | T-2 |
| GET | `/api/emby/status` | `?date=` | `{tid: bool}` | E-2 |
| POST | `/api/emby/refresh` | — | `202` | E-6 |
| GET | `/api/emby/libraries` | — | `[{id, name}]` | S-8 |
| GET | `/api/settings` | — | 密钥**脱敏** | S-1~S-9 |
| PUT | `/api/settings` | `{section, values}` | `200` | S-5 |
| POST | `/api/settings/test/emby` | — | `{ok, detail}` | — |
| POST | `/api/settings/test/telegram` | — | `{ok, detail, channels:{mtp:{ok,detail}, bot:{ok,detail}}}` | 双通道独立报告 |
| POST | `/api/tg/login/start` | `{phone}` | `{ok}` | 冷却期内 429 + `retry_after` |
| POST | `/api/tg/login/verify` | `{code}` | `{ok}` 或 `{need_password}` | 验证码错 422 `tg_code_invalid` |
| POST | `/api/tg/login/password` | `{password}` | `{ok}` | 密码错 422 `tg_password_invalid` |
| POST | `/api/tg/login/cancel` | — | `{ok}` | 断开临时客户端 |
| POST | `/api/auth/login` | `{password}` | `Set-Cookie` | S-6 |
| POST | `/api/auth/logout` | — | `204` | S-6 |
| GET | `/api/auth/me` | — | `{authenticated}` | S-6 |

**`PUT /api/settings` 按分区提交**，对应 UI 的五个独立保存按钮（S-5）。
**每次请求只能改一个分区**——分区边界显式，避免客户端一次误改多处：

```json
{ "section": "proxy", "values": { "type": "socks5", "host": "127.0.0.1", "port": "7890" } }
```

**脱敏规则**：`GET /api/settings` 对 `password` / `token` / `apikey` 等密钥字段
只返回**固定长度占位符** `"••••••••"`（8 个 `•`），**绝不回传明文、也不回传长度或末位**。
若前端提交的值等于占位符，则视为「未修改」而保留原值。

> 不使用「末 4 位提示」写法：`hint: "••••1a2b"` 会泄露密钥尾部，
> 固定长度占位符同时不泄露真实长度。见 `FRONTEND.md` §9.3。

---

## 8. 并发模型

| 组件 | 并发度 | 说明 |
|---|---|---|
| FastAPI | N（每请求） | 无状态，只读为主 |
| Collector | **1** | 串行，账号安全要求（硬约束） |
| Emby Syncer | 1 | 独立节奏，与采集解耦 |
| Session Health | 1 | 低频 |

**共享状态**：仅 SQLite。通过「单写连接 + 全局锁」串行化，无需内存锁。

**采集与同步不会互相阻塞**：采集慢（每帖 2–5s）但只写 posts；
Emby 同步只更新 `emby_*` 字段。二者在写锁上短暂排队，互不等待。

**阻塞调用不占事件循环**：`httpx` 用同步客户端（采集链路本身是串行的，异步无收益），
在 FastAPI 中通过线程池调用，避免阻塞 uvicorn 的事件循环。

**优雅退出**：所有 worker 监听停止信号；采集循环在「帖与帖之间」检查，
保证不在请求中途被砍断。

---

## 9. 安全

| 项 | 设计 |
|---|---|
| **密钥落库** | `emby_api_key` / `tg_bot_token` / `proxy_pass` 用 **AES-256-GCM** 加密（`settings.encrypted=1`） |
| **主密钥** | 首次启动生成 32 字节随机密钥 → `169bt.key`（`0600`）；或从 `BT169_SECRET_KEY` 环境变量注入 |
| **访问密码** | 存 **PBKDF2-SHA256** 哈希（60 万次迭代），不存明文（S-6）。见 ADR-17 |
| **面板会话** | 随机 32 字节 token，**存哈希**，`HttpOnly` + `SameSite=Lax` + `Secure`（HTTPS 时）；默认 30 天 |
| **认证中间件** | 除 `/api/auth/*` 与静态资源外，全部要求已认证 |
| **API 不回传密钥** | 见 §7 脱敏规则 |
| **代理密码** | 同样加密存储，日志中一律打码 |
| **日志脱敏** | 自定义 logging filter，过滤一切 token/password/api_key |
| **CSRF** | `SameSite=Lax` + 对写操作校验 `Origin` |
| **绑定地址** | 默认 `127.0.0.1:8080`，**不默认暴露公网**；需要时显式 `--host 0.0.0.0` |

> 需求 §8.13 问「访问密码仅前端门禁还是后端校验」。
> **架构结论：必须后端校验。** 前端门禁（现有 UI）不是安全边界——
> 静态资源可被任何人下载，API 才是真正的边界。

> **账号凭据（需求 §8.1）**：`username` / `password` 属最高敏感级。
> 建议放 `.env`（`0600`）而非 DB，**不入库、不进日志、不进 API 响应**。

---

## 10. 可观测性

```python
log.info("collect.round.done", extra={"new": 3, "failed": 0, "elapsed": d})
log.warning("session.invalid", extra={"reason": "redirect_to_login"})
log.error("emby.sync.failed", exc_info=True)     # 但不影响浏览
```

**`/api/health` 返回的信号**，足以判断系统是否健康：

| 信号 | 含义 | 不健康时 |
|---|---|---|
| `cookie_valid` | 论坛会话可用 | 触发重登录；仍失败则 TG 告警 |
| `login_attempts_left` | ★ 剩余登录额度 | ≤1 时禁止自动登录尝试 |
| `emby_ok` | Emby 可达 | 角标停更，但浏览正常 |
| `tg_ok` | Bot 配置有效 | 转发按钮禁用（T-4） |
| `last_collect_at` | 上次成功采集时间 | 超过 30 分钟未更新 → 异常 |

**TG 告警**（复用同一 Bot）：会话失效、连续采集失败、Emby 长时间不可达、登录额度耗尽。
**告警必须限流**（同一事件 1 小时内只发一次），否则会刷屏。

---

## 11. 测试策略

| 层 | 方式 | 重点 |
|---|---|---|
| **验证码** | `tests/fixtures/captcha/`（**30 张服务端已验证**） | ★ 准确率回归：改动模型参数后必须重跑，防止静默退化 |
| **解析** | 离线 fixtures | ★ 最高优先。真实页面，覆盖已感谢/未感谢/无 ed2k |
| **番号匹配** | 表驱动（13 例已实测通过） | ★ 防误判：`START-62` vs `START-624`、大小写、前缀 |
| **时区归一化** | 表驱动 | UTC pubDate → UTC+8 日期；跨日边界（16:00 UTC = 次日 00:00 +8） |
| **repo** | 内存 SQLite | 迁移、硬删除（含图片）、相邻日期导航（含空档） |
| **api** | `TestClient` | 契约字段名与前端一致；脱敏不泄露密钥 |
| **telegram** | 假 Bot API | 429 退避、幂等 |
| **emby** | 假 Emby | 不可达时降级不报错 |
| **source** | **不联网** | 用假服务器喂录制 HTML；限速逻辑用假时钟 |
| **端到端** | Playwright（复用 `/tmp/169bt-test/`） | 前端接真后端后，重跑既有 54 项 |

**不做的事**：不为 `source` 写联网集成测试——会触发真实账号操作，风险高于收益。

> ⚠️ **验证码 fixture 有选择偏差**：`tests/fixtures/captcha/` 的 30 张是
> 「至少一个模型命中」才被保存的（否则无法获得真值），因此测得的准确率
> （默认 86.7% / 取或 100%）**高于**无偏估计（50.0% / 67.5%）。
> 该 fixture 的用途是**回归检测**（改动后准确率是否下降），
> **不可**用于宣称绝对准确率。无偏数字以附录 A.5 的配对实验为准。

---

## 12. 实施路线

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0** | `config` + `db` + 迁移 + `169bt migrate` | ✅ **已完成**（126 测试通过；见 `docs/superpowers/plans/2026-09-18-backend-foundation.md`） |
| **P1** | `source` 只读部分：`discover` + `fetch`（复用已存 Cookie）+ `parse` | ✅ **已完成**（`source/parse.py` + `source/forum.py` + `source/session.py`） |
| **P2** | `captcha` + `login` + 额度保护 | ✅ **已完成**（`source/captcha.py` + `source/login.py` + `bt169 login`）。★ **登录 POST 已实测**：对线上论坛提交过一次失败登录，服务端返回「您还可以尝试 4 次」——证明 formhash/loginhash/idhash 三件套、请求头、额度解析全部正确。该次事故同时暴露了「额度保护只写在文档里、代码从未实现」，已补上（规则 2/3）。⚠️ **成功**登录（拿到有效 Cookie）仍需真实凭据 |
| **P3** | `collector` + 状态机 + 限速退避 | 🔶 **大部分完成**：按日期范围采集（C-10）真实站跑通（13 帖）；感谢解锁（C-4）与图片本地化（W-15）已实现；**定时轮询（C-1）待做** |
| **P4** | `api` 只读接口 + 静态分发 | 前端从 API 取数渲染，Playwright 54 项回归绿 |
| **P5** | 写接口：删除/撤销、`auth`、`settings` + 加密 | ✅ **已完成**：设置真落盘、密钥 AES-GCM 加密、脱敏正确；**访问门禁已服务端校验**（`api/gate.py` + `api/auth.py`，PBKDF2-SHA256 哈希 + 会话 token 只存哈希 + CSRF Origin 校验） |
| **P6** | `telegram` + 转发接口 | 卡片「下载」真的发到 TG；`per_post` 批量可用 |
| **P7** | `emby` + 匹配 + 同步 | 角标按番号正确显示，`START-62`/`START-624` 不误判 |
| **P8** | `scheduler` + 健康检查 + TG 告警 | 无人值守跑 7 天无人工介入 |

**关键：P4 之前前端不动。** 前端改造只有一处——
把 `ui/data.js` 的 `window.DEMO_POSTS` 换成 API 适配层，渲染逻辑不变。

---

## 13. 架构决策记录

| # | 决策 | 理由 | 备选与否决原因 |
|---|---|---|---|
| ADR-1 | **Python + FastAPI + SQLite** | 需求文档 §6 原本推荐；`ddddocr` 14.7k ★、5 年历史；Go 移植版实测可用但仅 0 ★/4 个月（§1.2） | Go：技术上可行（输出等价，二进制仅 125MB），但把唯一的外部依赖压在 4 个月新项目上风险不匹配。**已抽象为可替换接口** |
| ADR-2 | **采集串行、并发度恒为 1** | 账号封禁是最高风险（🔴） | 并发抓取：快，但可能封号，不可接受 |
| ADR-3 | **验证码「先预校验、再提交登录」** | 实测 check 端点独立于登录；使验证码错误不消耗登录额度 | 直接提交登录：每错一次烧 1/5 额度 |
| ADR-4 | **每图最多 2 个候选、最多 6 张图** | 实测单图仅约 3 次校验机会；实测 40/40 在 6 张内求解成功 | 无限重试：会把该图作废，且拖慢 |
| ADR-5 | **验证码不做图像预处理** | 实测二值化使准确率从 100% 降到 13–30%，中值去噪降到 40% | 二值化/去噪：实测有害 |
| ADR-6 | **登录只在 `169bt login` 中发生，不自动重试** | 额度按 IP 计（5 次），自动重试会静默烧光 | 自动重登：会把额度耗尽，导致真需要时无法登录 |
| ADR-7 | **硬删除（行 + 图片）** | 用户决策：不留数据；W-9 的可撤销由前端**延迟删除窗口**满足（`FRONTEND.md` §13） | 软删除：用户明确不要 |
| ADR-8 | **浏览路径不调用 Emby** | E-7 要求 Emby 不可达不影响浏览 | 实时查库：Emby 挂掉则页面卡死 |
| ADR-9 | **番号匹配用「双向提取 + 精确比对」** | 实测朴素 `in` 产生假阳性 | `in`：会误判；纯正则边界：边角情况多 |
| ADR-10 | **TG 用 `sendMessage` 而非 `forwardMessage`** | 我们只有文本，没有可转发的消息对象 | `forwardMessage`：语义不成立 |
| ADR-11 | **API 字段名对齐前端（`cover`/`detail`）** | 前端渲染逻辑零改动 | 让前端改：无谓改动，且已验证的 UI 不该动 |
| ADR-12 | **访问密码后端校验** | 前端门禁不是安全边界 | 仅前端：API 可被直接调用 |
| ADR-13 | **密钥 AES-256-GCM 落库** | 配置文件可能被备份/误传 | 明文存储：泄露即失守 |
| ADR-14 | **用 APScheduler 的 interval 而非 cron** | 需求是固定间隔，不是日历式调度 | cron 表达式：语义不匹配 |
| ADR-15 | **用内置 `sqlite3` 而非 `aiosqlite`** | 实测 `threadsafety==3`；采集串行，写竞争不存在 | aiosqlite：多一个依赖，无收益 |
| ADR-16 | **API 契约锁定 `PostDTO`** | 前后端解耦点单一，改一处即可 | — |
| ADR-17 | **访问密码用 PBKDF2-SHA256（60 万次）而非 argon2id** | `hashlib.pbkdf2_hmac` 属标准库，零依赖；个人自用、单用户、低频登录，不需要 argon2 的抗 GPU 优势 | argon2-cffi：多一个 C 扩展依赖，收益在本题场景下不可测量 |
| ADR-18 | **`PUT /api/settings` 一次只改一个分区**（`{section, values}`） | 对应 UI 的五个独立保存按钮（S-5）；分区边界显式，客户端无法一次误改多处 | 扁平 patch：无法表达「分区级」语义，且与独立保存按钮不对应 |
| ADR-19 | **媒体库列表按用户过滤（E-8），且**不是**安全边界** | 用户拍板：填了 `emby.username` 就走 `/Users/{id}/Views`，只列该用户能访问的库 | `/Library/VirtualFolders`（管理员视角）：实测给 22 个库，其中大半是别的用户的 |
| ADR-20 | **用户名匹配大小写敏感、精确** | 用户明确要求；`Muse` ≠ `muse`，不静默匹配到别人 | 大小写不敏感：会让用户以为配对了，实际在用别人的权限 |
| ADR-21 | **找不到用户时退回全部库 + 警示提示，而非报错** | 过滤只是「帮你少看几个库」；报错会让设置页整个用不了，且用户只是想先看看有哪些库 | 报错/空列表：用户无法自救，也看不到可选项 |

---

## 14. 待确认（阻塞实现）

架构已定，但以下 4 项需要你拍板才能开工：

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | **TG 语义**：确认用 `sendMessage`？批量用 `per_post` 还是 `single`？ | `sendMessage` + `per_post`（ADR-10，§5.5.2） |
| 2 | **采集频率**：5 分钟 / 10 分钟？ | **5 分钟**（20 条窗口，峰值 41 帖/天，余量充足） |
| 3 | **Emby 匹配范围**：指定媒体库还是全库？ | 设置页指定的**单个**媒体库（E-4） |
| 4 | **部署形态**：本机 `127.0.0.1` 还是服务器？是否需要 HTTPS？ | 先本机；若要外网访问**必须**加 HTTPS |

另有三项**非阻塞**但需知晓：

- ⚠️ **登录额度已消耗**：本次实测用掉本机 IP 的 4 次登录尝试（用于验证预校验有效性）。
  P2 阶段前请确认额度已重置（`169bt doctor` 会显示）。
- **图片本地化**（需求 §8.2）：**已升为 P3 核心组件**，见 §5.8 `imagecache`。
  实测依据：源图 863.7 KB/张，卡片只需 300×200（1.8% 像素），
  缩放为 600px WebP 后 65 KB（省 91%）。同时解决图床不稳与 PWA 离线。
- **历史回填范围**（需求 §8.4）：`169bt backfill --days N` 已支持，N 由你定。

---

## 附：与需求文档的差异汇总

实现时以下三处**以本文档为准**，需求文档已同步修正：

| 项 | 需求文档原文 | 本文档 | 原因 |
|---|---|---|---|
| `deleted` 列 | 导航查询用 `deleted=0`，但 schema 无此列 | **不新增列**：改为硬删除（用户决策），可撤销由前端延迟删除窗口实现 | 原矛盾消解 |
| 状态机 | `pending\|thanked\|done\|failed` | 增加 `nolink` | 显式优于用 `ed2k IS NULL` 推断 |
| API 字段名 | `cover_img` / `detail_img` | 响应中用 `cover` / `detail` | 对齐前端 `app.js` 既有消费方式 |

---

## 附录 A：实测验证记录

> 全部在本机执行，命令与结果可复现。环境：macOS / arm64，Python 3.14.6，Go 1.24.0。

### A.1 Python 依赖可用性

```
ddddocr 1.6.1 安装成功，依赖解析结果：
  onnxruntime 1.30.0      （cp314 wheel 存在）
  opencv-python 5.0.0.93  （cp37-abi3 wheel，兼容 3.14）
  numpy 2.5.3 / pillow 12.3.0
识别耗时：默认模型 39 ms/张，beta 模型 4 ms/张
```

> ⚠️ **修正一处早期误判**：`opencv-python` 的 wheel 文件名是
> `opencv_python-5.0.0.93-cp37-abi3-macosx_13_0_arm64.whl`——
> 含 `cp37-abi3`（稳定 ABI），**没有** `cp314` 字样，但**在 3.14 上可用**。
> 若只按 `cp314` 字符串匹配判断可用性会得到假阴性。

### A.2 Go ddddocr 移植版是否存在 —— ⚠️ 一次错误的测量

**这是本次工作中最严重的一次误判，保留全过程作为教训。**

**错误做法**：手工拼接 goproxy 的 `@latest` URL 查询：

```
github.com/yangbin1322/go-ddddocr   → not found
github.com/tensafe/goddddocr        → not found
github.com/okatu-loli/ddddocr-go    → not found
github.com/Changbaiqi/ddddocr-go    → not found
对照：github.com/glebarez/go-sqlite → v1.23.0 ✓
```

当时注意到「已知存在的 `goquery` 也返回 not found」，并写下了
「代理查询本身可能不可靠，不能单独作为库不存在的证据」——
**但随后仍然把这个结论写进了架构文档，且用它支撑了技术选型。**
自带的对照实验已经否定了结论，却被忽略了。

**正确做法**：用 `go list -m -versions`（Go 工具链的真实查询机制）：

```
github.com/yangbin1322/go-ddddocr   v1.0.0 v1.0.1
github.com/tensafe/goddddocr        v1.0.0 v1.0.1
github.com/okatu-loli/ddddocr-go    v0.0.1 v0.0.2
github.com/Changbaiqi/ddddocr-go    v0.0.1 … v0.1.4
github.com/PuerkitoBio/goquery      v0.1.1 … v1.13.0   ← 对照：正常返回
```

**全部存在**，且已实际跑通并量化对比（附录 A.12）。

**教训**：
1. **不要用 HTTP 直接手搓包管理器查询**——工具链自己的命令才权威
2. **对照实验如果否定了你的结论，就必须推翻结论**，不能只当作「保留意见」
3. **不可验证的否定性结论（「X 不存在」）不该写进文档并用于决策**
4. 正确形式应为「用 A 方法查询未找到，待用 B 方法复核」

> 这也正是要求「所有结论必须实测」的原因——
> 本次错误恰恰发生在「以为已经实测了」的地方。

### A.3 验证码端点与抓取要点

```
登录页    GET  https://169bt.com/member.php?mod=logging&action=login
               → formhash + loginhash + Cookie(SlDj_2132_*)
取图      GET  misc.php?mod=seccode&update={ms}&idhash={loginhash}
               → 100×30 PNG, 8-bit, colortype 2 (RGB), 约 6.6–7.3 KB, 4 字符
               ⚠️ 必须带 Accept: image/* + Referer，否则 13 字节 "Access Denied"
校验      GET  misc.php?mod=seccode&action=check&inajax=1
               &modid=member::logging&idhash={loginhash}&secverify={code}
               → <root><![CDATA[succeed|invalid]]></root>
```

**抓取到的 12 张样本的列分段分析**（`/tmp/169bt-captcha/ascii.py` 纯 Python PNG 解码）：

```
c01: 灰阶 0–254, 列分段 [13, 18, 15, 3]  → 4 段
c02: 灰阶 0–239, 列分段 [63, 27]         → 仅 2 段（字符粘连/重叠）
c03: 灰阶 0–254, 列分段 [3, 26, 3, 17, 8] → 5 段
```

→ 噪声极重且字符**相互粘连**，简单分割 + 模板匹配必然失败，正是 CNN 擅长的形态。

### A.4 登录 / 感谢流程（实测验证通过）

```
1. GET  member.php?mod=logging&action=login    → formhash + loginhash + Cookie
2. solve_and_verify()                          → 已确认正确的验证码
3. POST member.php?mod=logging&action=login&loginsubmit=yes&loginhash=<LH>&inajax=1
   body: formhash, referer, loginfield=username, username, password,
         questionid=0, answer=, seccodehash=<LH>,
         seccodemodid=member::logging, seccodeverify=<CODE>,
         cookietime=2592000, loginsubmit=true
   → "欢迎您回来，新手上路 ymxh" ✅

感谢解锁：
1. GET  forum.php?mod=viewthread&tid=<TID>     → formhash
2. POST plugin.php?id=thanksplugin:thanks&action=thanks
   body: tid, formhash, saying=, thanksubmit=true
   → 首次：返回含 ed2k 页面 ✅ / 重复：返回「已感謝過了」（幂等）✅
3. GET  forum.php?mod=viewthread&tid=<TID>
   → 出现 "本帖在感謝作者後顯示的內容" + ed2k 明文 ✅
```

**实测字段样本（tid=3986000）**：

★ 站点原文。`影片大小` 是 `值@注解` 格式，解析层只取 `7GB`。

```
番号      : START-624
标题      : [4K] START-624 配送中NTR 既婚ベテランドライバーの配送に…
出演者    : 本庄鈴
商品発売日: 2026/09/17
影片大小  : 7GB@NO Watermark
发帖日期  : 2026-9-14      ← 归档依据（≠ 商品発売日）
封面图    : https://www.imgccc.com/2026/09/14/7f7fa7216d7a.jpg (2184×1542, 944KB)
ed2k      : ed2k://|file|s169bbs.com@START-624_[4K].mkv|7515146184|0B17E95C…|/
```

### A.5 验证码准确率（配对实验，无偏）

同一批图上，两个模型各自独立判定，与服务器真值比对：

```
默认模型（beta=False）: 20/40 = 50.0%
beta 模型（beta=True） : 22/40 = 55.0%
两候选取或            : 27/40 = 67.5%
平均每图 check 次数   : 1.50
```

**关键机制实测**：

```
单图校验次数上限 : 约 3 次（第 3 次错误后该码失效）
重新取图         : 轮换为新码（旧码作废）
check 端点       : 不消耗码（同一码可重复校验，直到 3 次上限）
大小写           : 不敏感（EcTk / ECTK 均可通过）
```

**生产策略实测**：每图 2 候选、最多 6 张图 → **40/40 求解成功**（均得到服务端确认的码），
平均 **1.48** 张图，最多 6 张。取图数分布：`1张:29, 2张:7, 3张:2, 4张:1, 6张:1`。

> ⚠️ **口径说明**：该实验验证的是「**能拿到服务端确认正确的验证码**」（40/40），
> **不是**「40/40 登录成功」——后者需要提交登录，会消耗按 IP 计的 5 次额度，
> 因此**不能**用这种方式做批量实验。
> 「预校验通过的码在提交登录时确实被接受」已由 A.6 的对照实验证明。

### A.6 登录失败次数按 IP 限制（⚠️ 重要发现）

```
实测：用不存在的用户名连续提交登录，剩余次数变化 4 → 3 → 2 → 1
     换用户名不重置计数  →  ★ 限制是 per-IP，不是 per-username
```

**预校验有效性的判定实验**：

| 提交内容 | 服务器响应 | 结论 |
|---|---|---|
| 预校验通过的验证码 + 不存在的用户名 | 「登录失败，您还可以尝试 N 次」 | 验证码**已通过**，是账号问题 |
| 故意填错的验证码 | 「抱歉，验证码填写错误」 | 验证码**未通过** |

→ 证明「先 check 预校验、再提交登录」能保证登录请求**不因验证码错误而失败**。

> ⚠️ 本实验消耗了本机 IP 的 4 次登录额度。所有登录 POST 测试已停止。

### A.7 SQLite 并发与能力（Python 内置 `sqlite3`）

```
版本: 3.53.3   threadsafety = 3 (serialized，线程安全)

WAL 行为实测：
  journal_mode = wal
  写事务未提交时，读连接看到 0 行（WAL 快照隔离，读到旧版本）✓
  提交后读连接看到 1 行 ✓
  两个连接同时 BEGIN IMMEDIATE → database is locked
     → 证实必须串行化写（架构用单写连接 + 全局锁）

能力验证：
  部分索引     : 可用；EXPLAIN QUERY PLAN → SCAN posts USING INDEX idx_browse ✓
  RETURNING    : (1,) ✓
  窗口函数     : (1,) ✓
  row_factory  : dict 映射 ✓
  导航查询     : 更早 → 2026-09-10；更晚 → 2026-09-14（正确跳过空档）✓
  硬删除后导航 : 正确跳过空档；边界返回空 → 触发 W-12 禁用 ✓
```

### A.8 番号匹配（防误判，13/13 通过）

```
误判对照：
  本地番号 START-62，Emby 只有 "START-624 配送中NTR …"
    朴素 Contains 判定已入库? True    ← ★ 假阳性
    双向提取+精确比对判定?   False   ← 正确

表驱动（失败 0/13）：
  ✓ "START-624 配送中NTR 既婚ベテランドライバー"  → START-624  True
  ✓ "[4K] START-624 [中文字幕]"                 → START-624  True
  ✓ "s169bbs.com@START-624_[4K].mkv"            → START-624  True
  ✓ "START-624"                                 → START-62   False
  ✓ "START-624"                                 → START-6240 False
  ✓ "start-624 日本語"                          → START-624  True
  ✓ "SSIS-001 何か"                             → SSIS-001   True
  ✓ "SSIS-001"                                  → SSIS-01    False
  ✓ "START624"                                  → START-624  False（无连字符）
  ✓ "AB-12"                                     → AB-12      True
  ✓ "ABCDEFG-123"                               → ABCDEFG-123 False（7 字母超范围）
  ✓ "ABC-12345"                                 → ABC-12345  True
  ✓ "ABC-123456"                                → ABC-123456 False（6 位超范围）
```

### A.9 时区归一化（C-9）

```
RSS pubDate 为 UTC。实测 16:00Z 之后跨日：
  直接用 UTC 日期归档 → 归错一天
  归一化为 UTC+8     → 正确
```

### A.10 论坛可访问性侦察

```
https://169bt.com  → HTTP 200，直连约 0.9s（无需代理）
RSS 匿名可访问     : forum.php?mod=rss&fid=192 → 16KB XML（不受 auth 参数影响）
forum.php          : 57KB 正常渲染
版块               : fid 37 公告区 / 51 游客区 / 210 国产原创 / 212 无码原创
                     / 215 欧美原创 / 211 有码原创
公开帖（未登录）   : forum.php?mod=viewthread&tid=70458 → 显示「您需要登录」，
                     正文无 ed2k://（符合预期：ed2k 需登录 + 感谢）
```

### A.11 验证码语料库（回归测试用）

```
/tmp/169bt-captcha/corpus/  → 30 张 PNG + labels.json
  每张真值均经服务端 check 端点确认（非模型自证）

  默认模型单独命中: 26/30 = 86.7%
  beta 模型单独命中: 10/30 = 33.3%
  两候选取或命中  : 30/30 = 100.0%
  取图总轮数（含失败重取）: 44

⚠️ 有选择偏差：只有「至少一个模型命中」的图才能获得真值并被保存，
   故上述数字高于无偏估计（A.5 的 50.0% / 67.5%）。
   用途是回归检测，不可用于宣称绝对准确率。
```

### A.12 Go 侧实测（为选型决策保留证据）

> **背景**：§1.2 曾错误声称「Go 移植版不存在」。此处保留完整的 Go 侧实测，
> 便于将来复核选型。**这些数字是真实跑出来的，不是推测。**

**① 模块确实存在**（用 `go list -m -versions`，而非手工拼 URL）：

```
github.com/yangbin1322/go-ddddocr   v1.0.0 v1.0.1
github.com/tensafe/goddddocr        v1.0.0 v1.0.1
github.com/okatu-loli/ddddocr-go    v0.0.1 v0.0.2
github.com/Changbaiqi/ddddocr-go    v0.0.1 … v0.1.4
```

**② `tensafe/goddddocr` 零配置跑通**：

```
go.mod 依赖：yalue/onnxruntime_go v1.25.0 + disintegration/imaging
模型/字符集/dylib 全部 go:embed：
  assets/models/common.onnx      54,088,400 B
  assets/models/common_old.onnx  13,606,011 B
  assets/models/common_det.onnx  20,127,694 B
  third_party/onnxruntime/darwin_arm64/onnxruntime.dylib  36,679,544 B
```

**③ 与 Python 输出逐字比对（n=40 新图，未消耗 check 额度）**：

```
Go ModelOld  ≡ Py default : 37/40 = 92.5%
Go ModelBeta ≡ Py beta    : 40/40 = 100.0%   ← beta 模型完全一致
```

3 处不一致**全部出现在默认模型**，且 beta 模型均一致：

```
e_010: go_old='Emcb' py_default='EmCb'  (beta 均为 'Emcb')
e_014: go_old='Eqry' py_default='E0cy'  (beta 均为 'e0cyc')
e_033: go_old='cp5e' py_default='cr5e'  (beta 均为 'cP3E')
```

**④ 命中真值对比**（在 30 张已验证语料上）：

```
Go ModelOld   : 27/30 = 90.0%
Go ModelBeta  : 25/30 = 83.3%
Go 两模型取或 : 30/30 = 100.0%
Py default    : 26/30 = 86.7%（同一语料）
```

**⑤ 构建与部署特征**：

```
CGO_ENABLED=0 go build  → FAIL
  "github.com/yalue/onnxruntime_go: build constraints exclude all Go files"
  → cgo 是硬性要求

GOOS=linux go build     → FAIL（cgo 交叉编译工具链缺失）

默认构建产物            : 131,691,778 B ≈ 125.6 MB
otool -L                : 仅依赖系统库（libSystem/CoreFoundation/Security）
                          → 运行时不需要外部 dylib（已内嵌）
首次运行副作用          : 释放 36.7 MB 到 ~/Library/Caches/goddddocr/
                          onnxruntime-darwin-arm64-2475226c9bc2c5dd.dylib
```

**⑥ 社区成熟度**（web 搜索核实）：

```
tensafe/goddddocr : 0 star, 0 fork, 创建于 2026-05-17（约 4 个月）, MIT
sml2h3/ddddocr    : 14,690 star, 2,337 fork, 创建于 2021-07-14, 6 位贡献者
```

**结论**：Go 路径**技术可行且输出等价**，拒绝它的唯一理由是
**依赖成熟度**（0★/4 个月 vs 14.7k★/5 年），
以及它把「唯一的外部依赖」绑在一个新项目上。这是**风险判断，不是能力判断**。

**⑦ 决策的可逆性保障**：由于两者输出等价，
`captcha.Solver` 被设计为接口（§5.1.3）。若将来想切回 Go，
只需新增一个 `GoSidecarSolver`，**业务代码零改动**。

---

### A.13 环境基线

```
Python   : 3.14.6 (/opt/homebrew/bin/python3.14)
Go       : go1.24.0 darwin/arm64
Node     : v26.8.2
Playwright: 1.63.0
GOPROXY  : https://goproxy.cn,direct

已装（/tmp/169bt-ocr-venv）:
  ddddocr 1.6.1 / onnxruntime 1.30.0 / opencv-python 5.0.0.93
  numpy 2.5.3 / pillow 12.3.0 / httpx 0.28.1
  fastapi 0.141.1 / uvicorn 0.53.0 / apscheduler 3.11.3
  cryptography 50.0.1 / selectolax 0.4.12 / lxml 6.1.3

⚠️ Spotlight 风险：/tmp 不在 Spotlight 排除列表。
   已用 .metadata_never_index 标记 venv，避免重复此前的 CPU 打满事故。
```
