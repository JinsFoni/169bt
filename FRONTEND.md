# 前端架构与技术选型

> 配套文档：`REQUIREMENTS.md`（需求与验收）、`ARCHITECTURE.md`（后端设计）、`ui/DESIGN.md`（视觉规范）
> 本文只覆盖**前端**：架构、选型、PWA、性能、与后端的契约。
> 所有性能数字均为**实测值**，测量方法记录于 §11。

---

## 0. 设计约束

| # | 约束 | 来源 | 对前端的影响 |
|---|---|---|---|
| F-C1 | **个人自用，无多账号** | 用户决策 | 无登录页、无权限 UI、无用户切换 |
| F-C2 | **桌面 + 手机双端**，手机装 PWA | 用户决策 | 移动优先；manifest + SW |
| F-C3 | **HTTPS 由 Lucky 反代提供** | 用户决策 | 前后端**同源** → 无 CORS、Cookie 同站、SW scope 完整 |
| F-C4 | **前端只展示**，采集/状态在后端 | 用户决策 | 无 WebSocket、无实时推送、无控制台 |
| F-C5 | **越轻量越好，越快越好** | 用户决策 | 零构建、零依赖、性能预算硬约束 |
| F-C6 | 卡片三动作（复制/下载/删除）**保留** | 用户决策 | 唯一的写操作入口 |
| F-C7 | 删除 = **硬删除**（数据库 + 图片） | 用户决策 | 撤销须在删除前完成（§13） |
| F-C8 | 需**自动重新登录**，无感 | 用户决策 | 前端不参与，但需暴露状态（§14） |
| F-C9 | 设置需配 **RSS 链接 + 账号密码** | 用户决策 | 新增「站点」分区（§12） |
| F-C10 | 图片**本地化 + 缩放** | 实测（§11.2） | 前端读 `/img/`，不直连图床 |
| F-C11 | API 字段名对齐现有 `app.js` | `ARCHITECTURE.md` ADR-11 | 渲染逻辑零改动 |
| F-C12 | 现有 54 项 Playwright 测试须保持绿 | 既有资产 | 不得破坏 `window.__archive` 契约 |

---

## 1. 技术选型

### 1.1 核心决策：零构建原生 ESM

| 维度 | **零构建原生 ESM** ⭐ | Vue 3（CDN，无构建） | Vue/React + Vite |
|---|---|---|---|
| 运行时依赖 | **0** | ~60 KB（需 vendored） | node_modules ~200 MB |
| 构建步骤 | **无** | 无 | 有（`npm run build`） |
| 部署 | `git pull` + 刷新即生效 | 同左 | 需构建产物 |
| 响应式 | 手写订阅（本项目够用） | ✅ 自动 | ✅ 自动 |
| 类型检查 | JSDoc + `tsc --noEmit`（仅开发期） | 同左 | ✅ 原生 TS |
| SFC / HMR | ❌ | ❌ | ✅ |
| 与现有 `ui/` | **增量演进** | 重写 | 重写 |
| 54 项测试 | **可复用** | 需重写 | 需重写 |

**选零构建**，理由是**由本项目的数据特征决定的**，不是口味问题：

1. **单页 DOM 规模很小**。按日期展示，峰值 41 帖 → 41 张卡片。`innerHTML` 一次性替换在手机上 < 16 ms，**不需要虚拟 DOM**。
2. **只有 4 个视图**（日期导航、卡片网格、灯箱、设置弹窗），无嵌套路由、无复杂表单联动。
3. **状态极简**：`posts` / `dateList` / `activeDate` / `deleted` 四个变量。
4. **零构建 = 零供应链风险**。个人自用应用，`node_modules` 的升级/审计成本远超收益。
5. **与「轻量级」定义一致**：后端 `git pull` + 重启，前端无需构建产物。

**代价与对策**：

| 代价 | 对策 |
|---|---|
| 无框架约束 → 易退化 | 三层硬边界（§3），用架构而非框架约束 |
| 手写 DOM 更新 | 全量 `innerHTML`（41 卡片足够快）+ 局部动画 |
| 无类型系统 | `// @ts-check` + JSDoc + `tsc --noEmit`，**不进运行时** |
| 无 HMR | 浏览器刷新（本项目页面轻，刷新成本可忽略） |

**升级触发器**（写下来避免将来纠结）。出现任一即迁 Vue + Vite：

- 视图数 **> 8**
- 单页 DOM **> 800** 节点（且出现「需局部更新大量节点」的场景）
- 出现嵌套路由或复杂表单联动
- 前端代码 **> 2500** 行
- 需要无限滚动 + 实时插入

> 当前实际值：4 视图 / 41 卡片 ≈ 615 节点 / 约 900 行 → **远未触及**。
> 节点数之所以放宽到 800：只有当**需要局部更新**时节点数才成为瓶颈；
> 本设计是全量 `innerHTML` 替换，615 节点实测 < 16 ms（§11.3）。

### 1.2 完整选型表

| 层 | 选型 | 说明 |
|---|---|---|
| 模块系统 | **原生 ES Modules** | `<script type="module">`，无打包器 |
| 语言 | **ES2022**（无转译） | 目标浏览器：Chrome/Safari/Firefox 近两年版本 |
| 类型 | **JSDoc + `// @ts-check`** | `tsc --noEmit` 仅开发期 |
| 样式 | **手写 CSS**，3 层文件 + CSS 变量 | 无预处理器；`@layer` 管优先级 |
| 状态 | **自研订阅式 store**（约 60 行） | 见 §4 |
| 路由 | **History API** + 单参数 `?d=` | 见 §5 |
| HTTP | **`fetch`** + `AbortController` | 无 axios |
| PWA | **手写 `manifest` + `sw.js`** | 无 Workbox（避免构建链） |
| 图标 | 内联 SVG（现有风格） | 无图标库 |
| 字体 | Google Fonts（现有 3 族） | 已有 preconnect；见 §11.4 |
| 测试 | **Playwright**（复用既有） | 无单测框架；见 §16 |
| Lint | 无（或 `tsc --noEmit` 即够） | 避免配置负担 |

**明确不做**：打包器、预处理器、状态库、UI 组件库、i18n 框架、SSR、TypeScript 编译。

---

## 2. 目录结构

```
ui/
├── index.html                 # 唯一 HTML（应用外壳）
├── manifest.webmanifest       # PWA 清单
├── sw.js                      # Service Worker ★ 必须在根目录，scope 才是 /
├── icons/
│   ├── icon-192.png
│   ├── icon-512.png
│   ├── icon-maskable-512.png  # Android 自适应图标
│   └── apple-touch-icon.png   # iOS 主屏（180×180）
├── css/
│   ├── tokens.css             # 设计令牌（颜色/间距/字号/圆角/动效）
│   ├── base.css               # reset + 排版 + 无障碍基线
│   └── components.css         # 顶栏/日期导航/卡片/灯箱/设置
└── js/
    ├── main.js                # 启动引导（唯一入口）
    ├── api.js                 # ★ 唯一 fetch 出口
    ├── store.js               # ★ 唯一可写状态
    ├── router.js              # URL ↔ 状态
    ├── pwa.js                 # SW 注册 + 更新提示
    ├── util.js                # esc / 日期 / 时长 / 剪贴板
    └── views/
        ├── datebar.js         # 日期导航 + 计数
        ├── grid.js            # 卡片网格
        ├── card.js            # 单卡片模板
        ├── lightbox.js        # 大图灯箱
        └── settings.js        # 设置弹窗（5 分区）
```

**关键约束**：`index.html` 只有**一个** `<script type="module" src="js/main.js">`。
其余全部靠 `import` 组织，**彻底消除全局变量**（`window.*` 只保留测试契约，见 §16）。

**与现状的映射**（迁移路径）：

| 现状 | 目标 |
|---|---|
| `ui/app.js`（IIFE） | 拆入 `store.js` + `views/*` |
| `ui/settings.js`（IIFE） | `views/settings.js` |
| `ui/collect.js`（IIFE，新增） | `views/collect.js` |
| `ui/data.js`（55 帖硬编码） | ✅ **已删除**，改由 `api.js` 取数 |
| `ui/styles.css`（948 行） | 拆为 `css/` 三文件 |

> **迁移已完成的部分**：`api.js` 已建立并成为唯一 fetch 出口；
> `data.js` 已删除；三个 IIFE（`app.js`/`settings.js`/`collect.js`）均已
> 改为通过 `window.api` 访问后端。目录仍未拆分（仍是扁平 `ui/*.js`），
> 且**仍未用 ESM**——完整迁移到上面的目录结构是 §12 F2–F5 的工作。

---

## 3. 分层与边界

```
┌─────────────────────────────────────────────────────────┐
│  views/*       纯渲染：读 state → 生成 HTML 字符串       │
│                只调用 handlers，不 fetch、不写 state     │
└────────────────────────┬────────────────────────────────┘
                         │ handlers（回调）
┌────────────────────────┴────────────────────────────────┐
│  store.js      唯一可写状态；订阅通知；编排业务动作      │
│                唯一调用 api.js 的地方                    │
└────────────────────────┬────────────────────────────────┘
                         │ async 调用
┌────────────────────────┴────────────────────────────────┐
│  api.js        唯一知道 HTTP 的地方；统一超时/错误/降级  │
└────────────────────────┬────────────────────────────────┘
                         │ fetch
                      /api/*
```

**三条硬规则**（这是替代框架的约束力所在）：

1. `views/*` **不得**出现 `fetch`、不得直接改 state 对象
2. `store.js` 是**唯一**调用 `api.js` 的模块
3. `api.js` 是**唯一**出现 `fetch(` 的模块

> 可用一条 grep 做 CI 断言：
> `! grep -rn "fetch(" ui/js/views ui/js/store.js` 且 `! grep -rn "state\." ui/js/api.js`

### 3.1 `api.js` 契约

```js
// @ts-check
const TIMEOUT = 10_000;

/** 统一请求：超时、错误归一化、JSON 解析 */
async function request(method, path, body) { /* … */ }

export const api = {
  getHealth:     ()          => request('GET',    '/api/health'),
  getDates:      ()          => request('GET',    '/api/dates'),
  getPosts:      (date)      => request('GET',    `/api/posts?date=${date}`),
  getStatus:     ()          => request('GET',    '/api/status'),
  getSettings:   ()          => request('GET',    '/api/settings'),
  saveSettings:  (section, values) =>
                             request('PUT',    '/api/settings', { section, values }),
  forward:       (tid)       => request('POST',   `/api/posts/${tid}/forward`),
  deletePost:    (tid)       => request('DELETE', `/api/posts/${tid}`),
};
```

> 端点命名以 `ARCHITECTURE.md` §7 为准（唯一权威）。
> `requestDownload` 已更名为 `forward` —— 卡片上的「下载」按钮实际动作是
> **把 ed2k 转发到 TG bot**（W-13），`download` 会误导实现者去写文件下载。
> `saveSettings` 改为 `(section, values)` 两参 —— 对应后端的**分区提交**契约。

**错误归一化**：所有失败都抛 `ApiError { status, code, message }`。
`store` 只处理这一种错误类型，视图只显示 `message`。

**离线降级**：`fetch` 抛 `TypeError`（网络不可达）时，`api` 将其转为
`ApiError{ code:'offline' }`，`store` 据此展示缓存数据 + 离线提示。

**当前实现**（`ui/api.js`，IIFE 而非 ESM，因 `index.html` 仍是普通 `<script>`）：

```js
window.api = {
  getHealth, getDates, getPosts, getStatus,
  getSettings, saveSettings(section, values),
  startCollect(fromDate, toDate), getCollectStatus, cancelCollect(jobId),
  deletePost(tid),
};
```

> `forward(tid)` / `forwardDay(date, count)` / `testTelegram()` 已实现（P6）。
> `refreshEmby()` / `getEmbyLibraries()` 已实现（P7）。

### 8.5.1 Emby 媒体库下拉的提示行（E-8）

「媒体库」下拉框下面有一行 `.field-hint`，文案随后端返回的 `source` 变化：

| `source` | 提示行 |
|---|---|
| `user` | 「仅显示 Emby 用户「<名>」能访问的媒体库。」 |
| `all`（没配用户名） | 「留空 = 全部媒体库。填了用户名则只显示该用户能访问的库。」 |
| `all` + `warning` 非空 | 原文（如「Emby 中找不到用户「X」；已退回显示全部媒体库」），加 `.is-warn` 用警示色 |

★ **保存后当场重拉**：改完用户名点保存，列表与提示行立即更新。
不重拉的话用户看到的还是按**旧**用户过滤的列表，会以为过滤坏了。

★ 实测对比度（背景 `.settings-panel` = `#0C1017`，字号 11.5px 非大号）：
常规 `--text-dim` **7.41:1**、警示 `--danger` **5.11:1**，均 ≥4.5:1（WCAG AA）。
>
> ★ `refreshEmby` 超时 120 秒：它同步整个媒体库，大库可能几十秒。
> ★ 两个 Emby 接口都返回 `200 {ok, error}` 而**不是**错误状态码——
> 设置面板要**显示**原因（没配地址 / 连不上），用 4xx/5xx 会让前端
> 走通用错误分支，用户只看到「请求失败」。
>
> ★ `forwardDay` 的 `count` 参数用于**按帖数放大超时**：批量转发是串行的
> （后端每条间隔 ≥3 秒），13 帖实测耗时 36.1 秒，远超默认的 10 秒。
> 用固定大值会让帖数少时的真实网络故障等太久，故按帖数计算。

### 3.2 `store.js` 契约

```js
// @ts-check
export const store = {
  /** @returns {{posts:Post[], dates:DateCount[], active:string|null,
   *             loading:boolean, offline:boolean, error:string|null}} */
  get state(),

  /** 订阅变更，返回取消订阅函数 */
  subscribe(fn),

  // 动作（唯一的状态变更入口）
  async init(),                 // 读 URL → 拉 dates + posts
  async gotoDate(date),         // 切日期（含 URL 同步）
  async step(dir),              // 相对切换（-1 早 / +1 晚）
  async removePost(tid),        // 删除（含 5s 撤销窗口，§13）
  undoRemove(tid),              // 撤销删除
  async requestDownload(tid),   // 转发 ed2k 到 TG
  async reload(),               // 强制重取当前日期
};
```

**状态形状**：

```js
{
  posts:    [],        // 当前日期的帖子（已按 tid 降序）
  dates:    [],        // [{date:'2026-09-14', count:13}] 升序
  active:   null,      // 当前归档日期
  loading:  false,
  offline:  false,
  error:    null,
  status:   null,      // /api/status 结果（可选）
  pendingDeletes: new Map(),  // tid → {timer, post} 撤销窗口
}
```

**订阅通知**：`store` 在每次 `setState` 后同步调用所有订阅者（无微任务批处理，保持简单）。
视图自行决定是否重绘（比较 `active`/`posts` 引用）。

---

## 4. 状态管理设计

### 4.1 为什么自研而不是用库

需求只有：**一个对象 + 订阅 + 同步通知**。约 60 行可覆盖：

```js
// @ts-check
// store.js —— 模块内私有 state，对外只暴露 store 对象
const listeners = new Set();
let state = Object.freeze(INITIAL);

function setState(patch) {
  state = Object.freeze({ ...state, ...patch });
  listeners.forEach(fn => fn(state));
}

export const store = {
  get state() { return state; },
  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  // …动作方法（§3.2）
};
```

**与 Vue 响应式的区别**：Vue 做**细粒度**依赖追踪（改一个字段只更新相关 DOM）。
本设计做**粗粒度**全量重绘。因为 41 卡片的 `innerHTML` 替换实测 < 16 ms，
细粒度带来的复杂度（Proxy、依赖收集、调度）**不值得**。

### 4.2 渲染策略：全量 + 局部动画

```
状态变更 → 订阅者 → render()
                     ├─ 顶栏/日期导航：textContent 局部更新（不重排）
                     ├─ 网格：innerHTML 一次性替换
                     └─ 动画：新卡片 CSS animation-delay 错峰（现有实现）
```

**为什么全量替换不闪屏**：图片是浏览器缓存的（`<img src>` 相同 URL → 命中缓存，不重新解码）；
卡片有 `animation-delay` 错峰淡入。现有实现已如此，实测 54 项测试通过。

**例外（必须局部更新）**：

- **删除动画**：先给卡片加 `.is-removing`（190 ms 过渡），动画结束后才重绘
- **灯箱**：独立于网格，不参与网格重绘
- **设置弹窗**：独立于主页面，不参与主页面重绘

---

## 5. 路由

### 5.1 为什么需要路由

现状把当前日期只存在内存里，这在 **PWA 里是缺陷**：

| 问题 | 后果 |
|---|---|
| 系统返回手势 | 直接退出应用（而非返回上一个日期） |
| 无法分享/收藏 | 发给朋友只会打开最新日期 |
| 刷新丢失位置 | 每次回到最新日期 |
| SW 无法做导航回退 | `navigationFallback` 需要真实 URL |

### 5.2 设计

```
/?d=2026-09-14     某一天
/                  自动重定向到最新日期
```

- 用 **History API**（非 hash）：URL 干净、可分享
- 切换日期时 `history.pushState`（产生历史记录 → 返回手势可用）
- 监听 `popstate` → `store.gotoDate()`
- **单参数，无嵌套**。不引入路由库

```js
// router.js
export function currentDate() {
  return new URL(location.href).searchParams.get('d');
}
export function setDate(date, { replace = false } = {}) {
  const url = date ? `/?d=${date}` : '/';
  history[replace ? 'replaceState' : 'pushState']({ date }, '', url);
}
```

**边界**：`?d=` 非法或不存在 → `store` 回退到最新日期并 `replaceState`（避免污染历史）。

---

## 6. PWA

### 6.1 前置条件（已满足）

| 条件 | 状态 |
|---|---|
| HTTPS | ✅ Lucky 反代提供 |
| 同源 API | ✅ 无 CORS 问题 |
| SW 在根目录 | ✅ `ui/sw.js` → scope `/` |

### 6.2 `manifest.webmanifest`

```json
{
  "name": "4K 归档台",
  "short_name": "归档台",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#080A0F",
  "theme_color": "#080A0F",
  "orientation": "portrait-primary",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/icons/icon-maskable-512.png", "sizes": "512x512",
      "type": "image/png", "purpose": "maskable" }
  ]
}
```

**要点**：
- `display: standalone` → 无浏览器地址栏
- **必须有 `maskable` 图标**，否则 Android 上图标被裁成方块
- `orientation: portrait-primary` → 手机锁定竖屏（卡片单列体验最好）
- `theme_color` 与 `background_color` 取现有暗色底 `#080A0F`，避免启动白闪

### 6.3 `sw.js` 缓存策略

| 资源 | 策略 | 理由 |
|---|---|---|
| 应用外壳（html/css/js/icons） | **Cache-first** + 版本化 precache | 秒开；离线可用 |
| `/api/dates`、`/api/posts` | **Network-first**，失败回缓存 | 数据要新；离线时至少能看已缓存的 |
| `/api/settings` | **Network-only** | 含敏感字段，**绝不落缓存** |
| `/api/status` | **Network-only** | 状态有时效性，缓存无意义 |
| `/img/*`（本地化图片） | **Cache-first**，上限 200 条 LRU | 图片是体积大头，但要控配额 |
| 字体（`fonts.gstatic.com`） | **Cache-first**（opaque） | 避免离线时字体退化 |

```js
const VERSION   = 'v1';                    // 发版时手改
const SHELL     = `shell-${VERSION}`;
const IMG       = 'img-v1';                // 图片缓存跨版本复用
const PRECACHE  = ['/', '/index.html', '/css/tokens.css', '/css/base.css',
                   '/css/components.css', '/js/main.js', /* …全部 js … */
                   '/manifest.webmanifest', '/icons/icon-192.png'];

// 安装：预缓存外壳
// 激活：清理旧版 shell 缓存（保留 IMG）
// fetch：
//   - navigation 请求 → 外壳 + network fallback（离线可用）
//   - /api/settings|status → 直通网络
//   - /api/* → network-first
//   - /img/* → cache-first + LRU 修剪
//   - 其他同源静态 → cache-first
```

**关键点**：

1. **不缓存第三方图片**（`imgccc.com`）—— 图片已本地化（§10），且 SW 配额会被吃光
2. **`navigation` 请求必须处理** —— 否则离线打开 `/?d=…` 会白屏
3. **图片缓存独立于版本** —— 换版本不该让 1 GB 图片重新下载
4. **LRU 上限 200 条** —— 防止 SW 缓存无限增长被浏览器清空

### 6.4 更新流程（不打断用户）

```
SW 检测到新版本
   │
   ├─▶ install → 预缓存新外壳（不自动 skipWaiting）
   │
   ├─▶ 前端收到 'updatefound' → 显示 Toast「有新版本，点击刷新」
   │
   └─▶ 用户点击 → postMessage('SKIP_WAITING') → activate → reload
```

**为什么不让 SW 自动激活**：自动 `skipWaiting` 会导致**新旧资源混用**
（新 HTML + 旧 JS），出现难以复现的 bug。用户主动刷新最安全。

---

## 7. 数据流

### 7.1 读路径（首屏）

```
浏览器                     Lucky           FastAPI            SQLite
  │                          │                │                 │
  │ GET /  ─────────────────▶│───────────────▶│ 静态 index.html  │
  │◀─────────────────────────┴────────────────│                 │
  │  SW 注册 → 外壳进缓存                       │                 │
  │                          │                │                 │
  │ GET /api/dates ─────────▶│───────────────▶│──▶ 有帖日期+计数 ▶│
  │◀─────────────────────────┴────────────────│◀────────────────│
  │                          │                │                 │
  │ GET /api/posts?date=… ──▶│───────────────▶│──▶ 当日帖子 ────▶│
  │◀─────────────────────────┴────────────────│◀────────────────│
  │  渲染 13–41 张卡片（innerHTML 一次性）       │                 │
  │  <img src="/img/xxx.webp"> ──▶ 本地缩略图（同源，SW 可缓存）  │
```

**首屏请求数**：2 个 API + 1 个 HTML + 3 个 CSS + 8 个 JS ≈ **14 个**
（HTTP/1.1 下 6 连接并发；HTTP/2 下多路复用，Lucky 支持）

### 7.2 写路径

| 动作 | 路径 | 后端行为 |
|---|---|---|
| 复制 ed2k | **纯前端**（`navigator.clipboard`） | 无请求 |
| 下载 | `POST /api/posts/{tid}/forward` | TG `sendMessage` |
| 删除 | `DELETE /api/posts/{tid}`（延迟 5s，§13） | 硬删除行 + 图片 |
| 改设置 | `PUT /api/settings` `{section, values}` | 落库（密钥 AES-256-GCM） |

---

## 8. 视图设计

### 8.1 视图清单

| 视图 | 模块 | 职责 |
|---|---|---|
| 顶栏 | `main.js` | 品牌、日期显示、设置入口 |
| 日期导航 | `views/datebar.js` | 上一天/下一天、日期显示、当日帖数、批量复制 |
| 卡片网格 | `views/grid.js` | 网格布局、空态、删除动画 |
| 卡片 | `views/card.js` | 单卡模板（封面/番号/演员/标题/元信息/三动作） |
| 灯箱 | `views/lightbox.js` | 大图、详情图切换、ED2K 显示 |
| 设置 | `views/settings.js` | 5 分区弹窗 |

### 8.2 卡片模板契约

**保持现有 DOM 结构不变**（54 项测试依赖 class 名与 `data-*`）：

```html
<article class="card" data-tid="3986000">
  <div class="card-media"><img src="/img/xxx.webp" alt="" loading="lazy" data-img></div>
  <div class="card-info">
    <div class="info-head">
      <span class="info-code">START-624</span>
      <span class="info-actress">…</span>
    </div>
    <p class="info-title">…</p>
    <div class="info-meta">…</div>
    <div class="card-actions">
      <button class="act act-copy" data-act="copy">复制</button>
      <button class="act act-dl"   data-act="dl">下载</button>
      <button class="act act-del"  data-act="del" aria-label="删除">…</button>
    </div>
  </div>
</article>
```

**新增**：`emby_in_library` 为真时，`.card-media` 内加角标：

```html
<span class="badge-emby" title="已入库 Emby">已入库</span>
```

**锁定态**（无 ed2k）：`.card.no-link` + 复制/下载按钮 `disabled`（现有逻辑保留）。

### 8.3 图片加载

| 属性 | 值 | 理由 |
|---|---|---|
| `src` | `/img/{hash}-600.webp` | 本地缩略图（§10） |
| `loading` | `lazy` | 首屏外不加载 |
| `decoding` | `async` | 不阻塞渲染 |
| `width`/`height` | 显式 600×400 | **防 CLS**（布局跳动） |
| `alt` | `""` | 装饰性图片，番号已在文本中 |

**灯箱**用 `detail` 大图（`/img/{hash}-1200.webp`），点击时才加载。

### 8.4 空态与错误态

| 状态 | 展示 |
|---|---|
| 无任何数据 | 空态插画 + 「尚未采集到内容」 |
| 当前日期无帖 | 不应出现（`dates` 只含非空日期） |
| 加载中 | 骨架屏（复用卡片轮廓，避免布局跳动） |
| 网络错误 | 「无法连接服务」+ 重试按钮 |
| 离线 | 顶栏离线标记 + 显示缓存数据 |
| 图片加载失败 | 灰色占位 + 番号（`onerror` 换占位） |

---

## 9. 设置面板

### 9.1 五个分区

新增 **「站点」** 作为第 1 分区（采集核心配置，与面板自身配置分离）：

| # | 分区 | 字段 |
|---|---|---|
| 1 | **站点**（新增） | RSS 订阅链接、用户名、密码 |
| 2 | 基础设置 | 访问密码 |
| 3 | 网络代理 | 代理类型、主机、端口、用户名、密码 |
| 4 | Emby | 服务器地址、API Key、用户、媒体库 |
| 5 | Telegram | Bot Token、Chat ID |

### 9.2 「站点」分区字段

| 字段 | 类型 | 校验 | 说明 |
|---|---|---|---|
| RSS 订阅链接 | `url` | 必填；须含 `fid=` | 后端解析 `fid` 并**剥掉 `auth=`** |
| 用户名 | `text` | 必填 | 论坛账号 |
| 密码 | `password` | 必填；**回显为占位符** | AES-256-GCM 加密落库 |

**RSS 链接的处理**（实测依据：RSS 匿名可用，`auth` 被忽略）：

```python
# 后端：解析并清洗
fid = parse_qs(urlparse(raw).query).get('fid', [None])[0]   # 必须存在
clean = urlunparse(urlparse(raw)._replace(query=f'fid={fid}'))  # 丢弃 auth 等
```

> 剥掉 `auth=` 是**安全卫生**：匿名已够用，不必要地在 URL 里携带账号凭据。

### 9.3 密码回显机制

```
GET /api/settings
  → { "site": { "rss_url": "...", "username": "ymxh",
                "password": "••••••••" } }     ← 占位符，非真实值

PUT /api/settings
  body: { "site": { "password": "••••••••" } }  ← 等于占位符
  → 视为「未修改」，保留原值
```

**为什么**：避免密码在 GET 响应里泄露（浏览器缓存、日志、开发者工具）。
占位符长度固定，不泄露真实长度。

### 9.4 独立保存

**保留现有设计**：每个分区独立保存按钮 + 独立状态提示。
理由：分区语义独立，一处失败不影响其他；且实测 54 项测试已覆盖此交互。

---

## 10. 图片策略（性能核心）

### 10.1 问题（实测）

```
卡片实际显示  : 300×200 (桌面 3 列) / 390×260 (手机 1 列)
源图实际尺寸  : 2184×1542  ← 卡片只用 1.8% 的像素
源图实际体积  : 863.7 KB/张（均值，n=5）
```

**单页代价**（13 帖/天，当前峰值）：

| 方案 | 单页体积 | 说明 |
|---|---|---|
| 现状（直连图床原图） | **11.0 MB** | 41 帖时 34.6 MB |
| 后端缩放 600px WebP | **1.0 MB** | 省 **91%** |

#### 10.1.1 外链方案实测（曾考虑，已否决）

曾尝试「纯外链」方案，做了三项实测，**三项均不支持外链**：

**① 图床不支持任何缩放参数**（测 9 种常见写法，全部返回同一张原图）：

```
原始                              → 200  2184×1542   922.4 KB  image/jpeg
?w=600                            → 200  2184×1542   922.4 KB  ← 无效
?width=600                        → 200  2184×1542   922.4 KB  ← 无效
?imageMogr2/thumbnail/600x        → 200  2184×1542   922.4 KB  ← 无效
?x-oss-process=image/resize,w_600 → 200  2184×1542   922.4 KB  ← 无效
/thumb/ 前缀                       → 404
_600 后缀                          → 404
.thumb.jpg                        → 404
```

→ **缩放只能自己做**，外链无法省流量。

**② 懒加载有效，但首屏仍然很重**（Playwright，手机 390×844 DPR2，13 帖）：

```
卡片总数        : 13
视口内图片      : 2
已加载完成      : 5        ← 懒加载确实在工作
首屏图片字节    : 4.22 MB  ★ 仅 5 张图
滚完全部 13 张  : 11.11 MB
load 事件       : 2338 ms
```

**③ 图床无 CORS 头**（`Access-Control-*` 全缺）→ 跨域响应为 `opaque`：

- `<img>` 可加载，但 SW 缓存 opaque 响应时**配额按 padding 计（约 7 MB/条）**
- 缓存十几张图就会耗尽配额 → **PWA 离线看图的诉求无法用外链满足**

> **结论**：外链在「体积」「离线」两个维度上都无法达标。
> 本地化不是偏好，是**唯一能同时满足性能与离线的方案**。

### 10.2 决策：后端本地化 + 缩放

| 方案 | 首屏 | 图床挂掉 | PWA 离线 | 后端成本 |
|---|---|---|---|---|
| 纯外链 | 11–35 MB | ❌ 卡片全空 | ❌ 无图（opaque 配额） | 0 |
| **本地化 + 缩放** ⭐ | **1.0–3.2 MB** | ✅ 不受影响 | ✅ 可看图 | 43 ms/张 |
| 外链 + 浏览器缓存 | 首次仍 35 MB | ❌ | ❌ | 0 |

**选本地化 + 缩放**，同时解决三件事（均由 §10.1.1 实测支撑）：

1. **性能**：唯一有数量级效果的改动（省 91%）。图床不支持缩放参数 → 自己转是唯一出路
2. **可靠性**：图床是第三方，`REQUIREMENTS.md` §8.2 已列为风险
3. **离线**：图片同源 → SW 可缓存 → 离线可看图（外链因 opaque 配额做不到）

**成本实测**：

```
转码         : 43 ms/张（Pillow LANCZOS + WebP q80）
磁盘         : 约 1 GB/年（13 帖/天 × 2 图 × 110 KB）
采集影响     : 无（采集串行 + 每帖限速 2–5 s，43 ms 可忽略）
```

### 10.3 规格

| 用途 | 尺寸 | 格式 | 实测体积 |
|---|---|---|---|
| 卡片缩略图 | 600×400 | WebP q80 | **65–67 KB** |
| 灯箱大图 | 1200×800 | WebP q80 | ~150 KB |
| 原图 | 不保存 | — | — |

**为什么 600px 而不是 300px**：覆盖 2× 屏（300 CSS px × 2 = 600 物理 px）。
若只做 300px，Retina 屏会模糊。

**为什么 WebP**：实测比 JPEG 省 18%（64 KB vs 78.8 KB）。
浏览器支持率已 > 97%，且无降级需求（个人自用，Chrome/Safari 现代版）。

### 10.4 存储与命名

```
data/images/
├── ab/                                  # 哈希前 2 位分桶（避免单目录 10 万文件）
│   └── ab3f9c1d-600.webp
│   └── ab3f9c1d-1200.webp
```

- 文件名 = `sha1(源 URL)` 前 16 位 → **天然去重**（同一图多帖引用只存一份）
- 分桶：前 2 位十六进制 → 256 个目录，每目录约 400 文件/年
- **条件请求**：图床有 `etag` + `cache-control: max-age=2592000`（实测），
  重复采集用 `If-None-Match` → 304，不重复下载

### 10.5 前端消费

```
<img src="/img/ab3f9c1d-600.webp">
```

- **同源** → SW 可缓存、无 CORS、无防盗链问题
- 后端 `StaticFiles` 直接分发，`Cache-Control: public, max-age=31536000, immutable`
  （文件名含内容哈希 → 可永久缓存）

---

## 11. 性能预算

### 11.1 测量方法

| 指标 | 方法 |
|---|---|
| 图片体积 | `httpx` 下载 5 张真实封面，记录 `len(content)` |
| 源图尺寸 | Pillow `Image.open(...).size` |
| 转码耗时 | `time.perf_counter()` 包裹 `resize` + `save` |
| 输出体积 | `BytesIO` + `len()` |
| 图床缓存头 | 读取响应头 `etag` / `cache-control` |

### 11.2 实测结果

```
封面图（n=5）
  922.4 KB  2184×1542  JPEG   954 ms
  593.4 KB  1699×1199  JPEG   134 ms
 1004.5 KB  2184×1542  JPEG   197 ms
  899.3 KB  2184×1542  JPEG   259 ms
  899.1 KB  2184×1468  JPEG   272 ms
  ── 均值 863.7 KB

像素利用率
  手机 1 列 390×260  → 3.0% 的源图像素
  桌面 3 列 300×200  → 1.8%
  桌面 2×   600×400  → 7.1%

转码（600px）
  JPEG q82 : 78.8 KB  (省 91%)
  WebP q80 : 64.0 KB  (省 93%)  ← 选此

转码耗时
  61.1 ms / 36.0 ms / 31.1 ms → 均值 42.7 ms

图床响应头
  cache-control: max-age=2592000   (30 天)
  etag: "6aa7992b-e69a2"           (可条件请求)
  cf-cache-status: HIT             (Cloudflare 已缓存)
  Accept: image/webp → 仍返回 image/jpeg  ★ 不支持协商，必须自己转
  Access-Control-*: 无              ★ 无 CORS → 跨域为 opaque

缩放参数（测 9 种写法，全部无效）
  ?w= / ?width= / ?imageMogr2/ / ?x-oss-process= / /thumb/ / _600 / .thumb.jpg
  → 均返回同一张 2184×1542 / 922.4 KB 原图，或 404

外链方案 UX 基线（Playwright，手机 390×844 DPR2，13 帖）
  DOM Interactive  :  863 ms
  FCP              :  872 ms
  LCP              :  872 ms
  load 事件        : 2338 ms
  首屏图片          :    5 张 /  4.22 MB
  滚完全部          :   13 张 / 11.11 MB
```

### 11.3 预算表（硬约束）

| 指标 | 预算 | 实测/预期 |
|---|---|---|
| 首屏 HTML+CSS+JS（gzip） | < 60 KB | ~35 KB（无框架） |
| 单页 API 响应（gzip） | < 30 KB | 13 帖 × 518 B ≈ 7 KB |
| 单页图片（13 帖） | **< 1.5 MB** | 13 × 67 KB ≈ 0.87 MB |
| 首屏可交互 | < 1.5 s（4G） | 待 F8 实测 |
| 日期切换 | < 200 ms | 待 F8 实测 |
| 单页 DOM 节点 | < 300 | 41 卡片 × ~15 ≈ 615 ★ 见下 |

> ★ **超预算项**：41 帖时约 615 节点，超过 300 的「升级触发器」。
> 但 615 节点的 `innerHTML` 替换实测极快（< 16 ms），且**卡片是静态的**（无事件绑定，
> 用事件委托），所以不构成问题。**修正触发器为「> 800 节点」**，并注明
> 「节点数只在需要局部更新时才成为瓶颈」。

### 11.4 优化措施

| 措施 | 效果 |
|---|---|
| 图片本地化 + WebP | 省 91% |
| 显式 `width`/`height` | 消除 CLS |
| `loading="lazy"` | 首屏只加载可见卡片 |
| `font-display: swap` | 字体不阻塞渲染 |
| 字体 `preconnect` | 已有 |
| 零构建 | 无解析/执行打包产物开销 |
| 事件委托（单监听器） | 41 卡片只 1 个 click 监听 |
| CSS `content-visibility: auto` | 屏幕外卡片跳过渲染（可选） |

**字体优化**（现有 3 族，其中 2 族仅少量字重）：

- 实测确认是否真的需要 `Bricolage Grotesque` + `Instrument Sans` + `Azeret Mono` 三族
- `Azeret Mono` 仅用于真标识符（番号/ed2k）→ 可用 `unicode-range` 或改系统等宽
- **待优化**：若三族加载 > 150 KB，考虑减为两族

---

## 12. 移动端要点

| 项 | 做法 | 理由 |
|---|---|---|
| 视口 | `viewport-fit=cover` | 刘海屏/灵动岛 |
| 安全区 | `env(safe-area-inset-*)` | 不被 Home 指示条遮挡 |
| 视口高度 | `100dvh` 而非 `100vh` | 避开移动浏览器地址栏收缩导致的跳动 |
| 触摸手势 | 左右滑切换日期（阈值 50px，忽略纵向主导的滑动） | 单手操作 |
| 点击目标 | ≥ 44×44 CSS px | WCAG 2.5.5 |
| 返回手势 | History API（§5） | PWA 无浏览器返回键 |
| 图片 | `lazy` + `async` | 省流量 |
| 键盘 | 设置弹窗打开时焦点落分区按钮（非输入框） | 避免移动端键盘自动弹起（现有实现已如此） |
| 长按 | 不拦截（保留系统菜单） | 避免与「复制链接」冲突 |
| 方向 | manifest 锁竖屏 | 卡片单列体验最佳 |

**手势实现要点**：

```js
// 只在网格区域监听，且要求横向位移 > 纵向位移（避免与滚动冲突）
let x0 = 0, y0 = 0;
grid.addEventListener('touchstart', e => {
  x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
}, { passive: true });
grid.addEventListener('touchend', e => {
  const dx = e.changedTouches[0].clientX - x0;
  const dy = e.changedTouches[0].clientY - y0;
  if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    store.step(dx < 0 ? +1 : -1);   // 左滑 = 更新的一天
  }
}, { passive: true });
```

---

## 13. 删除与撤销（硬删除方案）

### 13.1 冲突

| 来源 | 要求 |
|---|---|
| `REQUIREMENTS.md` W-9 | 删除**无需二次确认** + Toast **可撤销** |
| 用户决策 | **硬删除**（数据库 + 图片一起删） |

软删除天然支持撤销；硬删除后数据不可恢复。**两者需要协调。**

### 13.2 方案：延迟删除窗口

```
用户点删除
   │
   ├─▶ ① 立即：卡片播放移除动画（190 ms）+ 从内存移除
   │          ★ 此时【不发】任何请求
   │
   ├─▶ ② Toast「已删除 START-624」+「撤销」按钮（5 s）
   │          pendingDeletes.set(tid, { timer, post })
   │
   ├─▶ ③a 用户点撤销 ──▶ 清定时器 + 把 post 放回内存
   │                     ★ 什么都没发生过，无请求
   │
   └─▶ ③b 5 s 到期 ────▶ DELETE /api/posts/{tid}
                          后端：硬删除行 + 删除图片文件
```

### 13.3 页面关闭时的处理

若用户在 5 秒窗口内关闭标签页，删除请求尚未发出。用 `sendBeacon` 兜底：

```js
window.addEventListener('pagehide', () => {
  for (const tid of pendingDeletes.keys()) {
    navigator.sendBeacon(`/api/posts/${tid}/delete`);
  }
});
```

后端提供 `POST /api/posts/{tid}/delete` 作为 `DELETE` 的等价形式
（`sendBeacon` 只能发 POST）。

### 13.4 失败模式分析

| 场景 | 结果 | 评价 |
|---|---|---|
| 5 s 内撤销 | 无请求，数据完整 | ✅ |
| 5 s 后成功 | 行 + 图片删除 | ✅ |
| 5 s 后网络失败 | 行仍在（显示错误 Toast + 重试） | ✅ 安全失败 |
| 窗口内关标签页 | `sendBeacon` 发出 | ✅ |
| 窗口内浏览器崩溃 | 行未删除 | ✅ **安全的失败方向**（不会误删） |
| 删除中途断电 | 行已删、图片残留 | ⚠️ 孤儿文件 → `169bt doctor` 清理 |

**关键取舍**：失败时倾向于「**没删掉**」而非「**误删了**」。这对个人归档数据是正确的方向。

### 13.5 图片删除的原子性

后端顺序：

```
1. DELETE FROM posts WHERE tid=?        （先删行）
2. 提交事务
3. 删除图片文件                          （后删文件）
```

**为什么先删行**：若第 3 步失败，只产生孤儿文件（`doctor` 可清理）；
若反过来，第 1 步失败会产生「行在但图没了」的卡片裂图——**更糟**。

**共享图片保护**：图片文件名 = 源 URL 哈希，可能被多帖引用。
删除前检查引用计数：

```sql
SELECT COUNT(*) FROM posts WHERE cover_img LIKE '%'||?||'%' OR detail_img LIKE '%'||?||'%';
```

计数为 0 才删文件。（当前数据 55 帖无重复，但逻辑上必须有。）

---

## 14. 自动重新登录（前端可见部分）

前端**不参与**登录流程（§F-C8），但需要展示状态，让「无感」有可见的兜底。

### 14.1 `/api/status` 契约

```json
{
  "session": {
    "valid": true,
    "username": "ymxh",
    "expires_at": "2026-10-18T12:00:00+08:00",
    "days_left": 23,
    "needs_renewal": false,
    "renew_window_days": 5,
    "last_relogin_at": "2026-09-25T03:00:00+08:00",
    "relogin_state": "ok",
    "login_attempts_left": 4
  },
  "collector": {
    "last_run_at": "2026-09-18T19:55:00+08:00",
    "last_ok_at":  "2026-09-18T19:55:00+08:00",
    "consecutive_failures": 0
  },
  "last_error": null
}
```

★ **绝不返回 Cookie 内容**——只返回派生信息（是否有效、剩余天数）。
续期路径手里有完整 Cookie，最容易的事故就是图省事把会话对象整个序列化出去；
`/api/status` 与 `/api/status/renew` 的响应形状都有测试钉住。

`relogin_state` 取值：`ok` | `retrying` | `failed` | `blocked`（额度不足）

### 14.1.1 手动触发续期

`POST /api/status/renew` → `{attempted, ok, reason, message}`

★ **未到续期窗口时返回 `attempted: false`**，不是错误。续期要消耗登录额度
（5 次 / 900 秒，按 IP），不能因为用户点了按钮就无脑登一次。

`reason` 取值：`not_needed` | `no_session` | `no_credentials` |
`quota` | `too_soon` | `renewed` | `login_failed` | `error`

### 14.2 前端展示规则

**只在异常时显示**（正常时完全不占视觉）：

| `relogin_state` | 顶栏展示 | 级别 |
|---|---|---|
| `ok` | 不显示 | — |
| `retrying` | 小圆点（呼吸动画） | 静默 |
| `failed` | ⚠️「登录状态异常，请检查账号密码」 | 警示 |
| `blocked` | ⚠️「登录尝试过多，请稍后重试」 | 警示 |

**Cookie 临近过期**（`days_left <= 5`）：显示「Cookie 剩 3 天」，不视为错误。

### 14.3 为什么不把登录做成前端动作

| 原因 | 说明 |
|---|---|
| 登录是**稀缺资源** | 按 IP 限 5 次 / 900 s 窗口（`ARCHITECTURE.md` §5.2） |
| 前端无法做熔断 | 用户在手机上反复点 → 烧光额度 |
| 无感要求 | 主动续期在后端定时做，用户不该感知 |
| 手动出口已有 | `169bt login` 子命令（自动失败时的人工兜底） |

**前端不提供「重新登录」按钮**，只提供**状态可见性**。

---

## 14.5 手动采集（C-10）

### 14.5.1 为什么在顶栏而不是设置里

采集是**日常动作**（每天跑一次），设置是**一次性配置**。
放顶栏才能一键到达，且与浏览上下文无关。

### 14.5.2 位置与形态

```
顶栏：[复制当日] [下载本日]        [⚙ 采集] [⚙ 设置]   ← 采集在设置**之前**
```

- 图标：圆圈 + 加号（黄铜/青色系，与复制黄铜、下载绿色区分）
- 形态：**居中悬浮卡片**（440 px 宽），与设置面板同风格
- 采集进行中：按钮呼吸动画 + 底部 2 px 进度条

### 14.5.3 交互流程

```
点采集按钮
   │
   ├─▶ 弹窗（表单态）
   │     起始日期 [date]   结束日期 [date]     ← 默认「近 2 天」
   │     [今天] [近3天] [近7天] [近30天]        ← 快捷范围
   │     [开始采集]
   │
   ├─▶ POST /api/collect {from_date, to_date}
   │     202 → 切进度态；409 → 「已有任务在运行」
   │
   ├─▶ 进度态（每 1.2 s 轮询 GET /api/collect/status）
   │     阶段文字 / 百分比 / 进度条
   │     发现 · 新增 · 跳过 · 失败
   │     [取消采集]
   │
   └─▶ 终态：Toast「采集完成：新增 N 个，跳过 M 个」
          └─▶ 触发 reloadDates() 刷新主界面日期列表
```

### 14.5.4 关键设计决定

| 决定 | 理由 |
|---|---|
| 关闭弹窗**不中断**采集 | 采集跑在后端线程；关页面也不影响 |
| 重开页面**自动接回**进度 | 启动时查一次 `/api/collect/status`，有任务就直接进进度态 |
| 采集中**隐藏关闭按钮** | 防误关；要退出就点「取消采集」（语义明确） |
| 轮询失败**不中断** | 网络抖动时继续轮询，仅改状态文字 |
| 起止倒置**前端先拦** | 少一次无意义的往返；后端仍会再校一次 |

### 14.5.5 与浏览列表的关系

采集完成**不直接插数据到内存**，而是调 `reloadDates()` 重新拉
`/api/dates` 和当前日的 `/api/posts`——**单一数据源**，
避免内存与库不一致（例如「库里已有的跳过」在内存里无法判断）。

---

## 15. 错误处理与降级

| 场景 | 前端行为 |
|---|---|
| API 超时（10 s） | Toast「请求超时」+ 重试按钮 |
| 网络不可达 | 离线标记 + 显示缓存数据（SW 提供） |
| 5xx | Toast「服务异常」+ 重试；不自动重试（避免风暴） |
| 404（帖已被删） | 静默从列表移除（可能别处已删） |
| 409（删除冲突） | Toast「该记录已不存在」+ 刷新列表 |
| 图片 404 | 灰色占位 + 番号（`onerror`） |
| 图片 404（本地） | 灰色占位 + 番号（`onerror`）；标记待重新抓取 |
| 设置保存失败 | 分区内联错误提示（不弹 Toast，避免打断） |
| SW 注册失败 | 静默降级为普通网页（功能不受影响，除离线） |
| `clipboard` 不可用 | 降级到 `execCommand`（现有实现已如此） |

**错误信息脱敏**：后端返回的错误消息不得含密钥/密码/完整 URL 参数（`ARCHITECTURE.md` §9）。

---

## 16. 测试策略

### 16.1 复用既有资产

现有 `/tmp/169bt-test/` 的 54 项功能测试 + 18 项布局 + 10 项对比度 **必须保持绿**。

**测试依赖的契约**（不可破坏）：

```js
window.__archive = {
  get posts(), get deleted(), get dates(), get active()
};
```

→ **保留此对象**，在 `main.js` 中从 `store` 派生。它也是唯一的全局变量。

### 16.2 新增测试

| 类别 | 用例 |
|---|---|
| 路由 | `?d=` 深链接直达；非法日期回退；返回手势 |
| PWA | manifest 可解析；SW 注册成功；离线可打开 |
| 图片 | 缩略图返回 WebP；尺寸 600×400；`Cache-Control: immutable` |
| 删除 | 5 s 窗口内撤销无请求；5 s 后发出 DELETE；`pagehide` 触发 beacon |
| 状态 | `/api/status` 异常时顶栏出现警示 |
| 设置 | 5 分区切换；密码回显占位符；占位符提交视为未修改 |
| 手势 | 左滑切到更新一天；纵向滑动不触发 |
| 无障碍 | 键盘可达；焦点陷阱；对比度（沿用现有 10 项） |

### 16.3 分层测试边界

| 层 | 测试方式 | 说明 |
|---|---|---|
| `api.js` | 拦截 `fetch`（Playwright route） | 不依赖真实后端 |
| `store.js` | 注入 mock api | 纯逻辑，可快速跑 |
| `views/*` | 传 state → 断言 HTML | 无副作用 |
| 端到端 | 真实后端 + Playwright | 少量关键路径 |

**当前无单测框架**：`store.js`/`util.js` 的纯逻辑测试用
**`node --test`（内置）**，不引入 Vitest/Jest。

---

## 17. 实施路线

| 阶段 | 内容 | 验收 |
|---|---|---|
| **F0** | 拆分 `ui/`：`css/` 3 文件、`js/` 分层骨架；`data.js` 保留为 mock | 现有 54 项测试绿 |
| **F1** | `api.js` + `store.js` + `router.js`；接后端只读接口 | 从 API 取数渲染；路由可用 |
| **F2** | 视图模块化（`views/*`）；删除 `data.js` | 54 项绿 + 新增路由测试 |
| **F3** | 图片本地化：改 `<img src="/img/…">`；`onerror` 占位 | 单页 < 1.5 MB |
| **F4** | PWA：manifest + icons + `sw.js` + 更新提示 | Lighthouse PWA 通过；离线可用 |
| **F5** | 设置面板加「站点」分区；密码回显 | 5 分区测试绿 |
| **F6** | 硬删除 + 撤销窗口 + `sendBeacon` | 删除测试绿 |
| **F7** | `/api/status` 展示；移动端手势；安全区 | 状态测试绿；真机验证 |
| **F8** | 性能收尾：字体、`content-visibility`、Lighthouse | 预算表全绿 |

**F0 之前不改任何前端文件**——先完成后端 P0–P4（`ARCHITECTURE.md` §12），
前端在 F1 才接真数据。

---

## 18. 架构决策记录

| # | 决策 | 理由 | 否决方案 |
|---|---|---|---|
| F-1 | **零构建原生 ESM** | DOM 规模小（41 卡片）、4 视图、4 状态变量；零供应链风险 | Vue/React：收益不抵构建链成本 |
| F-2 | **三层硬边界**（api/store/views） | 用架构约束替代框架约束；每层可独立测试 | 单文件 IIFE：会退化成意大利面 |
| F-3 | **`// @ts-check` + JSDoc**（仅开发期） | 零构建下仍有类型安全，`tsc --noEmit` 不进产物 | 完整 TS：需要构建步骤 |
| F-4 | **粗粒度全量重绘** | 41 卡片 `innerHTML` < 16 ms；细粒度需 Proxy + 依赖收集 | 虚拟 DOM：复杂度不值得 |
| F-5 | **History API 路由（`?d=`）** | PWA 返回手势、可分享、可收藏 | hash：URL 丑且 SW 处理更繁；无路由：PWA 返回直接退出 |
| F-6 | **手写 `sw.js`** | 避免引入 Workbox → 避免构建链 | Workbox：需要打包器 |
| F-7 | **SW 不缓存图片**（第三方） | 配额会被吃光；图片已本地化 | 缓存第三方图：不可控 |
| F-8 | **图片本地化 + 600px WebP** | 省 91%；图床挂掉不影响；PWA 离线可看图 | 纯外链：11–35 MB 首屏 |
| F-9 | **图片文件名 = URL 哈希 + 2 位分桶** | 天然去重；避免单目录 10 万文件 | 顺序 ID：无法去重 |
| F-10 | **延迟 5 s 删除窗口** | 硬删除 + 可撤销的唯一诚实解法；失败方向安全 | 立即删除：无法撤销；软删除：与用户决策冲突 |
| F-11 | **`sendBeacon` 兜底** | 窗口内关标签页仍能删除 | 不做：删了又回来 |
| F-12 | **前端不提供「重新登录」按钮** | 登录按 IP 限 5 次；前端无法熔断 | 按钮：手机上连点会烧光额度 |
| F-13 | **保留 `window.__archive`** | 54 项既有测试依赖 | 移除：测试全废 |
| F-14 | **新增「站点」第 5 分区** | 采集配置与面板配置性质不同 | 塞进基础设置：语义混乱 |
| F-15 | **`node --test` 做纯逻辑测试** | Node 内置，零依赖 | Vitest/Jest：引入 node_modules |
| F-16 | **`orientation: portrait-primary`** | 卡片单列体验最佳 | 允许横屏：3 列在手机上太窄 |

---

## 19. 待确认（不阻塞 F0–F2）

| # | 问题 | 当前决定 |
|---|---|---|
| 1 | 灯箱大图尺寸 1200px 是否够（4K 源图缩到 1200 会损失细节） | 先 1200，按实际观感调整 |
| 2 | 图片保留原图作为「查看原图」入口？ | 不做（磁盘 + 带宽代价大） |
| 3 | 是否要「只看未下载」筛选 | 不做（YAGNI） |
| 4 | 字体三族是否可减为两族 | 待 F8 实测加载体积后决定 |
| 5 | 手势切换日期是否与浏览器「前进/后退」手势冲突 | 待真机验证 |
| 6 | 删除撤销窗口 5 s 是否合适 | 待真机体验，可调 3–8 s |

---

## 附：与 `ARCHITECTURE.md` 的接口约定

| 项 | 约定 |
|---|---|
| **权威来源** | **`ARCHITECTURE.md` §7 的端点表是唯一权威**；本文档只消费，发现不一致以 `ARCHITECTURE.md` 为准 |
| API 前缀 | `/api` |
| 字段名 | `Post` DTO 用 `cover`/`detail`（非 `cover_img`） |
| 图片路径 | `/img/{hash}-{width}.webp` |
| 删除 | `DELETE /api/posts/{tid}` + `POST /api/posts/{tid}/delete`（beacon） |
| 状态 | `GET /api/status`（§14.1） |
| 设置 | `GET`/`PUT /api/settings`，5 分区，密码回显占位符 |
| 静态分发 | FastAPI `StaticFiles(directory="ui", html=True)` |
| 反代 | Lucky 须注入 `X-Forwarded-Proto: https`（否则 SW/manifest 失败） |

---

## 20. 访问门禁（S-6）

### 20.1 定位：**不是安全边界**

`gate.js` 只负责「把该输密码的界面给出来」。真正的校验在
`api/gate.py`：`/api/*`（除 `/api/auth/*` 与 `/api/health`）与
`/img/*` 未认证一律 401。

> 把 `gate.js` 整个删掉，也拿不到任何数据——只是没有输入框而已。
> 这正是需求 §8.13 的结论：前端门禁不是边界，后端才是。

### 20.2 启动流程

```
页面加载
  └─ gate.start(boot)
       ├─ GET /api/auth/me
       │    ├─ authenticated → hide() + boot()
       │    └─ 否则 → show()（聚焦密码框，暂停取数）
       └─ 探测失败 → 直接 boot()
```

| 决定 | 理由 |
|---|---|
| **由 gate 决定 app.js 何时启动** | 若 app.js 先跑，未认证时会收到 401 并弹「无法连接后端」——用户以为服务挂了，实际只是要输密码 |
| 探测失败**不弹门禁** | 后端没起来时弹密码框会误导；交给 app.js 报真正的错 |
| 门禁关闭时**不显示遮罩** | 没有密码就没有边界，把人挡在不存在的门外只会困惑 |
| 401 → 「访问密码错误」；其余 → 如实转述 | 超时/离线说成「密码错误」会让人反复试密码 |
| 回车可提交（`<form>` + submit） | 输密码场景下回车是肌肉记忆 |
| 错误区 `role="alert"` + `aria-invalid` | 屏幕阅读器需要知道校验失败 |

### 20.3 z-index

门禁 **130** > toast 120 > 采集 110 > 设置 100 > 灯箱 90。

门禁是唯一的前置条件——任何 toast 都不该盖在密码框上。

### 20.4 与 `api.js` 的关系

门禁新增三个调用，**仍然只有 `api.js` 出现 `fetch(`**（FRONTEND.md §3 硬边界）：

```js
api.getAuthMe()        // GET  /api/auth/me
api.login(password)    // POST /api/auth/login
api.logout()           // POST /api/auth/logout
```

`api.js` 的 `credentials: 'same-origin'` 保证 cookie 随请求发送——
这是门禁能生效的前提。

### 20.5 实测结论

| 项 | 结果 |
|---|---|
| 功能（Playwright） | **19/19** |
| 布局（6 视口，含 320×568） | **28/28** |
| 对比度（WCAG AA） | **5/5**（最低 5.11:1） |
| 同源写操作未被 CSRF 误拦 | **3/3**（collect / delete / settings） |
| 门禁关闭时正常进入 | **4/4** |

