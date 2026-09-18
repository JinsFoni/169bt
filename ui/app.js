/* ============================================================
   4K 归档台 — 交互逻辑
   ============================================================ */

(function () {
  'use strict';

  /* ---------- 状态 ---------- */

  var posts = [];            // 当前归档日的帖子（按需从后端取）
  var dateList = [];         // 有帖的日期（升序：左旧右新）
  var counts = {};           // date → 帖子数
  var activeDate = null;     // 当前归档日期
  var deleted = [];          // 删除栈，支持撤销
  var pendingDelete = null;  // {tid, timer, entry} 等待 5 s 撤销窗口
  var loading = false;

  /* ---------- DOM ---------- */

  var $ = function (id) { return document.getElementById(id); };

  var grid        = $('grid');
  var empty       = $('empty');
  var dateValue   = $('dateValue');
  var dateMeta    = $('dateMeta');
  var prevDay     = $('prevDay');
  var nextDay     = $('nextDay');
  var copyDay     = $('copyDay');
  var dlDay       = $('dlDay');
  var lightbox    = $('lightbox');
  var lightboxBody= $('lightboxBody');
  var lightboxClose = $('lightboxClose');
  var lbCode      = $('lbCode');
  var lbActress   = $('lbActress');
  var toastStack  = $('toastStack');

  var lastFocused = null;

  // TG 是否已配置（需求 T-4）。来自 /api/status，每次刷新日期时重取。
  // 未配置时「下载」按钮禁用 + 提示去设置页，而不是发一个注定 400 的请求。
  var tgConfigured = false;

  // ★ 两个独立的忙碌标志，不能共用一个：
  //   - 单帖转发只该锁住那张卡片的按钮
  //   - 批量转发只该锁住顶栏按钮
  //   共用一个会让「转发一张卡片」把整个顶栏锁死。
  //   而且**每个标志变化后必须重新 render()**——否则按钮会永远停在
  //   禁用态（render 只在别处被调用时才会重算）。
  var tgSendingOne = false;   // 单帖转发中
  var tgSendingDay = false;   // 批量转发中

  /* ---------- 工具 ---------- */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* 2026-09-14 → 9月14日 周一 */
  function humanDate(iso) {
    var p = iso.split('-');
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    var w = ['周日','周一','周二','周三','周四','周五','周六'][d.getDay()];
    return (+p[1]) + '月' + (+p[2]) + '日 ' + w;
  }

  /* 2026-09-14 → 2026/09/14 */
  function slashDate(iso) {
    return iso ? iso.replace(/-/g, '/') : '—';
  }

  /* ed2k 文件字节数 → 估算时长（4K HEVC ≈ 20 Mb/s 码率） */
  function durationOf(p) {
    var size = null;
    if (p.duration) return p.duration;                    // 数据自带时长则直接用
    var m = (p.ed2k || '').match(/\|(\d+)\|/);
    if (m) size = +m[1];
    if (!size) return '';
    var min = Math.round(size / (20e6 / 8) / 60);         // 字节 / (B/s) / 60
    if (min < 60) return min + '分钟';
    return Math.floor(min / 60) + '小时' + (min % 60) + '分';
  }

  /* 标题去掉 [4K] 等画质标签与番号（番号即 data.code，大小写不敏感） */
  function cleanTitle(p) {
    var t = String(p.title || '');
    t = t.replace(/^\s*\[[^\]]*[4kK][^\]]*\]\s*/, '');    // 开头 [4K] 标签
    if (p.code) {
      var re = new RegExp('^\\s*' + p.code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*', 'i');
      t = t.replace(re, '');
    }
    return t.trim() || String(p.title || '');
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy') ? resolve() : reject(); }
      catch (e) { reject(e); }
      finally { document.body.removeChild(ta); }
    });
  }

  /* ---------- Toast ---------- */

  function toast(msg, kind, opts) {
    kind = kind || 'info';
    opts = opts || {};

    var el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.innerHTML =
      '<span class="toast-icon"></span>' +
      '<span class="toast-msg">' + esc(msg) + '</span>' +
      (opts.actionLabel ? '<button class="toast-undo" type="button"></button>' : '');

    if (opts.actionLabel) {
      var b = el.querySelector('.toast-undo');
      b.textContent = opts.actionLabel;
      b.addEventListener('click', function () {
        if (opts.onAction) opts.onAction();
        dismiss();
      });
    }

    toastStack.appendChild(el);

    var timer = setTimeout(dismiss, opts.duration || 3000);
    function dismiss() {
      clearTimeout(timer);
      if (!el.parentNode) return;
      el.classList.add('is-out');
      setTimeout(function () { el.remove(); }, 200);
    }
    return dismiss;
  }

  /* ---------- 归档日期 ---------- */

  /* 日期列表来自后端（`/api/dates` → [{date, count}]，后端按降序返回） */
  function loadDates() {
    return window.api.getDates().then(function (rows) {
      rows = rows || [];
      dateList = rows.map(function (r) { return r.date; }).sort();
      counts = {};
      rows.forEach(function (r) { counts[r.date] = r.count; });
      return dateList;
    });
  }

  function countOf(date) { return counts[date] || 0; }

  /*
   * 取 TG 配置状态（需求 T-4）。
   *
   * ★ 失败时**保持当前值不变**，不降为 false：
   *   /api/status 拉不到（网络抖动、后端重启）不代表用户没配 TG，
   *   把按钮锁上会让用户莫名其妙——尤其正在批量转发时。
   *   宁可多发一个会报错的请求，也不要无缘无故禁用。
   */
  function loadStatus() {
    return window.api.getStatus().then(function (s) {
      tgConfigured = !!(s && s.telegram && s.telegram.configured);
      render();
    }).catch(function () { /* 保持原值 */ });
  }

  function navTo(date) {
    if (!date || date === activeDate) return;
    activeDate = date;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    loadPosts(date);
  }

  function step(dir) {
    var i = dateList.indexOf(activeDate);
    if (i < 0) return;
    var next = dateList[i + dir];          // -1 = 更早，+1 = 更晚
    if (next) navTo(next);
  }

  /* 取某日帖子并渲染。竞态保护：期间用户切了日期就丢弃这次结果 */
  function loadPosts(date) {
    loading = true;
    render();
    return window.api.getPosts(date).then(function (list) {
      if (activeDate !== date) return;     // 已切走，丢弃
      loading = false;
      posts = list || [];
      render();
    }).catch(function (e) {
      if (activeDate !== date) return;
      loading = false;
      posts = [];
      render();
      toast('加载失败：' + e.message, 'err', { duration: 4600 });
    });
  }

  /* ---------- 渲染：卡片 ---------- */

  function cardHTML(p, idx) {
    var locked = !p.ed2k;
    var dur = durationOf(p);
    var title = cleanTitle(p);

    var media = p.cover
      ? '<img src="' + esc(p.cover) + '" alt="" loading="lazy" data-img>'
      : '';

    // E-2/E-3：已入库在**图片区右上角**显示标记；未入库**什么都不显示**。
    // 只标「有」不标「无」——每张卡片都挂个「未入库」是纯视觉噪声。
    if (p.emby_in_library) {
      media += '<span class="lib-badge" title="已在 Emby 媒体库中">' +
                 '<svg viewBox="0 0 20 20" aria-hidden="true">' +
                 '<path d="M4.5 10.5 8 14l7.5-8"/></svg>已入库</span>';
    }

    var durHTML = dur
      ? '<span class="meta-item"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.25"/><path d="M10 6v4.2l2.8 1.7"/></svg>' + esc(dur) + '</span>'
      : '';
    var dateHTML = p.release_date
      ? '<span class="meta-item"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3.5" y="4.5" width="13" height="12" rx="2"/><path d="M3.5 8.5h13M7 3v3M13 3v3"/></svg>' + esc(slashDate(p.release_date)) + '</span>'
      : '';
    var sizeHTML = p.size
      ? '<span class="meta-item"><svg viewBox="0 0 20 20" aria-hidden="true"><ellipse cx="10" cy="5.5" rx="6" ry="2.5"/><path d="M4 5.5v9c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5v-9M4 10c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5"/></svg>' + esc(p.size) + '</span>'
      : '';
    var actressHTML = p.actress
      ? '<span class="info-actress"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="6.5" r="3.25"/><path d="M4 16.5c.8-3 3.2-4.5 6-4.5s5.2 1.5 6 4.5"/></svg>' + esc(p.actress) + '</span>'
      : '';

    return '' +
      '<article class="card' + (locked ? ' no-link' : '') + '" data-tid="' + p.tid + '"' +
      ' style="animation-delay:' + Math.min(idx * 26, 340) + 'ms">' +

        '<div class="card-media">' + media + '</div>' +

        '<div class="card-info">' +
          '<div class="info-head">' +
            '<span class="info-code">' + esc(p.code) + '</span>' +
            actressHTML +
          '</div>' +
          '<p class="info-title">' + esc(title) + '</p>' +

          '<div class="info-meta">' +
            durHTML + dateHTML + sizeHTML +
          '</div>' +

          '<div class="card-actions">' +
            '<button class="act act-copy" type="button" data-act="copy"' +
              (locked ? ' disabled title="尚未解锁 ED2K 链接"' : ' title="复制 ED2K 链接"') + '>' +
              '<svg viewBox="0 0 20 20"><path d="M7 3.5h7.5A1.5 1.5 0 0 1 16 5v7.5M4 6.5h7.5A1.5 1.5 0 0 1 13 8v7.5A1.5 1.5 0 0 1 11.5 17H4a1.5 1.5 0 0 1-1.5-1.5V8A1.5 1.5 0 0 1 4 6.5Z"/></svg>' +
              '复制</button>' +
            '<button class="act act-dl" type="button" data-act="dl"' +
              (locked
                ? ' disabled title="尚未解锁 ED2K 链接"'
                : (tgSendingOne
                    ? ' disabled title="正在转发…"'
                    : (p.tg_sent_at
                        ? ' title="已转发到 Telegram"'
                        : (tgConfigured
                            ? ' title="转发到 Telegram，由 Bot 侧下载"'
                            : ' title="尚未配置 Telegram，请先到设置页填写"')))) + '>' +
              '<svg viewBox="0 0 20 20"><path d="M10 3.5v9m0 0 3.5-3.5M10 12.5 6.5 9M4 16.5h12"/></svg>' +
              (tgSendingOne ? '发送中' : (p.tg_sent_at ? '已发' : '下载')) + '</button>' +
            '<button class="act act-del" type="button" data-act="del" title="删除这条记录" aria-label="删除">' +
              '<svg viewBox="0 0 20 20"><path d="M4 6.5h12M8.5 6.5V5a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1.5M6 6.5l.7 8.4a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9l.7-8.4"/></svg>' +
            '</button>' +
          '</div>' +
        '</div>' +

      '</article>';
  }

  /* ---------- 渲染：整体 ---------- */

  function render() {
    if (!activeDate && dateList.length) activeDate = dateList[dateList.length - 1];

    var list = posts;
    var total = activeDate ? countOf(activeDate) : 0;

    /* 顶栏 */
    dateValue.textContent = activeDate ? slashDate(activeDate) : '—';
    dateMeta.textContent  = activeDate ? (humanDate(activeDate).split(' ')[1] + ' · ' + total + ' 帖') : '—';

    // 上一页 = 更早（◀），下一页 = 更晚（▶）
    var i = dateList.indexOf(activeDate);
    prevDay.disabled = i <= 0;
    nextDay.disabled = i < 0 || i >= dateList.length - 1;

    var copyable = list.filter(function (p) { return p.ed2k; }).length;
    copyDay.disabled = copyable === 0;
    copyDay.title = copyable
      ? '复制当日全部 ED2K 链接 · ' + copyable + ' 条'
      : '当日没有可复制的 ED2K 链接';

    /* 顶栏「下载本日」（T-2）。三个独立条件：转发中 / 没链接 / 没配 TG。 */
    if (tgSendingDay) {
      dlDay.disabled = true;
      dlDay.title = '正在转发…';
    } else if (copyable === 0) {
      dlDay.disabled = true;
      dlDay.title = '当日没有可转发的 ED2K 链接';
    } else if (!tgConfigured) {
      dlDay.disabled = true;
      dlDay.title = '尚未配置 Telegram，请先到设置页填写 Bot Token 与 Chat ID';
    } else {
      dlDay.disabled = false;
      dlDay.title = '转发当日全部 ED2K 链接到 Telegram · ' + copyable + ' 条';
    }

    /* 卡片 */
    if (loading) {
      grid.innerHTML = '';
      grid.hidden = true;
      empty.hidden = false;
      empty.querySelector('.empty-title').textContent = '正在加载…';
      empty.querySelector('.empty-hint').textContent = '';
    } else if (!list.length) {
      grid.innerHTML = '';
      grid.hidden = true;
      empty.hidden = false;
      empty.querySelector('.empty-title').textContent = '这一天没有帖子';
      empty.querySelector('.empty-hint').textContent =
        dateList.length ? '用上方的 ◀ ▶ 按钮切换到其他归档日。'
                        : '点顶栏的采集按钮，把帖子抓回来。';
    } else {
      empty.hidden = true;
      grid.hidden = false;
      grid.innerHTML = list.map(cardHTML).join('');

      // 图片淡入
      grid.querySelectorAll('img[data-img]').forEach(function (img) {
        if (img.complete) img.classList.add('is-loaded');
        else img.addEventListener('load', function () { img.classList.add('is-loaded'); });
        img.addEventListener('error', function () {
          img.classList.add('is-loaded');
          img.style.opacity = '.25';
        });
      });
    }
  }

  /* ---------- 删除 / 撤销（FRONTEND.md §13 延迟删除窗口） ----------

     ① 点删除 → 卡片动画 + 从内存移除，**此时不发请求**
     ② Toast 5 s 撤销窗口
     ③a 撤销 → 什么都没发生过
     ③b 到期 → DELETE /api/posts/{tid}
     ④  窗口内关页 → sendBeacon 兜底
  */

  var DELETE_WINDOW_MS = 5000;

  function removePost(tid, animate) {
    var idx = -1;
    for (var i = 0; i < posts.length; i++) {
      if (posts[i].tid === tid) { idx = i; break; }
    }
    if (idx < 0) return;
    if (pendingDelete && pendingDelete.tid === tid) return;   // 防重复点击

    var removed = posts[idx];
    var pos = idx;

    var card = grid.querySelector('.card[data-tid="' + tid + '"]');
    var finish = function () {
      posts.splice(pos, 1);
      counts[activeDate] = Math.max(0, (counts[activeDate] || 1) - 1);

      // 当前日期已空 → 自动跳到最近的相邻日期
      if (countOf(activeDate) === 0) {
        var j = dateList.indexOf(activeDate);
        if (j >= 0) dateList.splice(j, 1);
        var next = dateList[dateList.length - 1];
        if (next && next !== activeDate) {
          activeDate = next;
          render();
          loadPosts(next);
          return;
        }
      }
      if (!dateList.length) activeDate = null;
      render();
    };

    if (card && animate !== false) {
      card.classList.add('is-removing');
      setTimeout(finish, 190);
    } else {
      finish();
    }

    // ★ 5 s 内不发任何请求
    var timer = setTimeout(function () {
      pendingDelete = null;
      window.api.deletePost(tid).then(function () {
        toast('已删除 ' + (removed.code || tid), 'ok', { duration: 2000 });
      }).catch(function (e) {
        toast('删除失败：' + e.message, 'err', { duration: 4600 });
        // 安全失败方向：没删掉 → 把卡片放回来
        posts.splice(Math.min(pos, posts.length), 0, removed);
        counts[activeDate] = (counts[activeDate] || 0) + 1;
        render();
      });
    }, DELETE_WINDOW_MS);

    pendingDelete = { tid: tid, timer: timer, post: removed, index: pos };

    toast('已删除 ' + (removed.code || tid), 'info', {
      actionLabel: '撤销',
      duration: DELETE_WINDOW_MS,
      onAction: undoDelete
    });
  }

  function undoDelete() {
    if (!pendingDelete) return;
    clearTimeout(pendingDelete.timer);
    var entry = pendingDelete;
    pendingDelete = null;

    posts.splice(Math.min(entry.index, posts.length), 0, entry.post);
    counts[activeDate] = (counts[activeDate] || 0) + 1;
    if (dateList.indexOf(activeDate) < 0 && activeDate) {
      dateList.push(activeDate);
      dateList.sort();
    }
    render();
    toast('已恢复 ' + (entry.post.code || entry.post.tid), 'ok', { duration: 2200 });
  }

  /* ③④ 页面关闭时把窗口内的删除发出去（FRONTEND.md §13.3） */
  window.addEventListener('pagehide', function () {
    if (!pendingDelete) return;
    var tid = pendingDelete.tid;
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/posts/' + tid + '/delete');
    } else {
      window.api.deletePost(tid).catch(function () {});
    }
  });

  /* ---------- 复制 ---------- */

  function copyOne(p) {
    if (!p.ed2k) {
      toast(p.code + ' 尚未解锁 ED2K 链接', 'err');
      return;
    }
    copyText(p.ed2k).then(function () {
      toast('已复制 ' + p.code, 'ok', { duration: 2000 });
    }).catch(function () {
      toast('复制失败，请手动选择链接', 'err');
    });
  }

  function downloadOne(p) {
    if (!p.ed2k) {
      toast(p.code + ' 尚未解锁 ED2K 链接', 'err');
      return;
    }
    if (!tgConfigured) {
      toast('尚未配置 Telegram，请先到设置页填写 Bot Token 与 Chat ID', 'err',
            { duration: 4000 });
      return;
    }
    if (tgSendingOne) return;
    tgSendingOne = true;
    render();

    api.forward(p.tid).then(function () {
      toast('已转发 ' + p.code + ' 到 Telegram', 'ok', { duration: 2400 });
      p.tg_sent_at = new Date().toISOString();
    }).catch(function (e) {
      toast(tgMessage(e, p.code), 'err', { duration: 4000 });
    }).then(function () {
      tgSendingOne = false;
      render();            // ★ 必须重算：否则按钮停在禁用态
    });
  }

  /**
   * 把 TG 错误翻译成用户看得懂的一句话。
   *
   * ★ 后端返回的 code 是给程序看的，message 是给用户看的——直接用 message
   *   就够了。特殊分支只处理**需要补充信息**的两种情况：
   *   - 已转发过：不是错误，是「不必重复操作」，语气要温和
   *   - 限流：必须告诉用户等多久，message 里带的是秒数
   */
  function tgMessage(e, code) {
    if (e && e.code === 'tg_already_sent') {
      return code + ' 之前已转发过，无需重复发送';
    }
    if (e && e.code === 'tg_rate_limited') {
      var wait = e.detail && e.detail.retry_after;
      return 'Telegram 限流' + (wait ? '，请等待 ' + wait + ' 秒后重试' : '，请稍后重试');
    }
    if (e && e.code === 'tg_not_configured') {
      tgConfigured = false;
      render();
      return '尚未配置 Telegram，请先到设置页填写';
    }
    return (e && e.message) || '转发失败';
  }

  /** 顶栏「下载本日」——批量转发当日全部 ed2k（需求 T-2）。 */
  function downloadDayAll() {
    var links = posts.filter(function (p) { return p.ed2k; });
    if (!links.length) {
      toast('这一天没有可转发的 ED2K 链接', 'err');
      return;
    }
    if (!tgConfigured) {
      toast('尚未配置 Telegram，请先到设置页填写 Bot Token 与 Chat ID', 'err',
            { duration: 4000 });
      return;
    }
    if (tgSendingDay) return;

    // ★ 后端串行发送、每条间隔 ≥3 秒（TG 限流），十几帖要几十秒。
    //   按钮必须进入忙碌态，否则用户会以为没反应而反复点击。
    tgSendingDay = true;
    render();
    var n = links.length;
    toast('正在转发 ' + n + ' 条到 Telegram…', 'ok', { duration: 60000 });

    api.forwardDay(activeDate, n).then(function (r) {
      var parts = [];
      if (r.sent)    parts.push('成功 ' + r.sent);
      if (r.skipped) parts.push('已发过 ' + r.skipped);
      if (r.failed)  parts.push('失败 ' + r.failed);
      var kind = r.failed ? 'err' : 'ok';
      toast('转发完成：' + parts.join('，'), kind, { duration: 5000 });
      if (r.failed && r.errors && r.errors.length) {
        console.warn('转发失败明细', r.errors);
      }
      return refresh();
    }).catch(function (e) {
      toast(tgMessage(e, '当日'), 'err', { duration: 4500 });
    }).then(function () {
      tgSendingDay = false;
      render();
    });
  }

  function copyDayAll() {
    var links = posts
      .map(function (p) { return p.ed2k; })
      .filter(Boolean);

    if (!links.length) {
      toast('这一天没有可复制的 ED2K 链接', 'err');
      return;
    }
    copyText(links.join('\n')).then(function () {
      toast('已复制 ' + links.length + ' 条 ED2K 链接', 'ok', { duration: 2600 });
    }).catch(function () {
      toast('复制失败，请手动选择链接', 'err');
    });
  }

  /* ---------- 大图 ---------- */

  function openLightbox(p) {
    lastFocused = document.activeElement;
    lbCode.textContent = p.code;
    lbActress.textContent = p.actress;

    var figs = [];
    if (p.cover)  figs.push({ src: p.cover,  cap: '封面图' });
    if (p.detail) figs.push({ src: p.detail, cap: '详情图' });

    lightboxBody.innerHTML = figs.length
      ? figs.map(function (f) {
          return '<figure><img src="' + esc(f.src) + '" alt="">' +
                 '<figcaption>' + esc(f.cap) + '</figcaption></figure>';
        }).join('')
      : '<p class="empty-hint" style="padding:40px;text-align:center">这条记录没有图片</p>';

    lightbox.hidden = false;
    document.body.style.overflow = 'hidden';
    lightboxClose.focus();
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxBody.innerHTML = '';
    document.body.style.overflow = '';
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  /* ---------- 事件 ---------- */

  grid.addEventListener('click', function (e) {
    var card = e.target.closest('.card');
    if (!card) return;
    var tid = +card.dataset.tid;
    var p = posts.filter(function (x) { return x.tid === tid; })[0];
    if (!p) return;

    var actBtn = e.target.closest('[data-act]');
    if (actBtn) {
      e.stopPropagation();
      if (actBtn.disabled) return;
      if (actBtn.dataset.act === 'copy') copyOne(p);
      if (actBtn.dataset.act === 'dl')   downloadOne(p);
      if (actBtn.dataset.act === 'del')  removePost(tid);
      return;
    }
    openLightbox(p);
  });

  prevDay.addEventListener('click', function () { step(-1); });
  nextDay.addEventListener('click', function () { step(1); });
  copyDay.addEventListener('click', copyDayAll);
  dlDay.addEventListener('click', downloadDayAll);
  lightboxClose.addEventListener('click', closeLightbox);

  lightbox.addEventListener('click', function (e) {
    if (e.target.hasAttribute('data-close')) closeLightbox();
  });

  document.addEventListener('keydown', function (e) {
    if (!lightbox.hidden) {
      if (e.key === 'Escape') closeLightbox();
      return;
    }
    if (e.target.matches('input, textarea')) return;
    if (e.key === 'ArrowLeft')  step(-1);
    if (e.key === 'ArrowRight') step(1);
  });

  /* ---------- 启动 ---------- */

  /* 从后端拉日期列表，默认停在最新一天 */
  function boot() {
    return loadDates().then(function () {
      loadStatus();                        // 不阻塞首屏：TG 状态晚一点到没关系
      activeDate = dateList[dateList.length - 1] || null;
      if (!activeDate) { render(); return; }
      return loadPosts(activeDate);
    }).catch(function (e) {
      loading = false;
      render();
      toast('无法连接后端：' + e.message, 'err', { duration: 6000 });
    });
  }

  /* 采集完成后由 collect.js 调用，刷新日期列表 */
  window.reloadDates = function () {
    loadStatus();
    return loadDates().then(function () {
      if (!activeDate) activeDate = dateList[dateList.length - 1] || null;
      if (activeDate) return loadPosts(activeDate);
    }).catch(function () {});
  };

  /* app.js 的 toast 供 collect.js 复用（全局单例只有这一个 UI 出口） */
  window.toast = toast;

  /* ★ 经门禁启动（S-6）：未认证时先让用户输密码，认证后再取数。
     门禁未启用时 gate.start 会立即回调，行为与直接 boot() 一致。 */
  if (window.gate && window.gate.start) {
    window.gate.start(boot);
  } else {
    boot();
  }

  // 调试入口
  window.__archive = {
    get posts()   { return posts; },
    get deleted() { return deleted; },
    get dates()   { return dateList; },
    get active()  { return activeDate; },
    reload: boot
  };
})();
