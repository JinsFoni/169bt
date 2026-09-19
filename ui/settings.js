/* ============================================================
   设置弹窗 — 交互逻辑

   按分区读写（ARCHITECTURE.md ADR-18）：
     GET /api/settings            → {site:{…}, basic:{…}, …}
     PUT /api/settings {section, values}

   密钥字段后端只回占位符（••••••••）；原样回传即表示「未修改」。
   ============================================================ */

(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var modal    = $('settingsModal');
  var openBtn   = $('settingsBtn');
  var closeBtn  = $('settingsClose');
  var navBtns   = Array.prototype.slice.call(modal.querySelectorAll('.dnav'));
  var panes     = Array.prototype.slice.call(modal.querySelectorAll('.pane'));

  var lastFocused = null;

  function toast(msg, kind, opts) {
    if (typeof window.toast === 'function') window.toast(msg, kind, opts);
  }

  /* ---------- 打开 / 关闭 ---------- */

  function openDrawer() {
    lastFocused = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
    // 每次打开都重新拉：面板关闭时未保存的改动本就作废，
    // 重新拉取才能反映后端真实状态（比如密钥的占位符）。
    load();
    // 焦点落在当前选中分区，而非第一个输入框（避免移动端键盘弹起）
    var current = navBtns.filter(function (b) { return b.getAttribute('aria-selected') === 'true'; })[0];
    (current || navBtns[0]).focus();
  }

  function closeDrawer() {
    modal.hidden = true;
    document.body.style.overflow = '';
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  openBtn.addEventListener('click', openDrawer);
  closeBtn.addEventListener('click', closeDrawer);

  modal.addEventListener('click', function (e) {
    if (e.target.hasAttribute('data-close')) closeDrawer();
  });

  /* ---------- 分区切换 ---------- */

  function selectPane(name, focusNav) {
    navBtns.forEach(function (b) {
      var on = b.dataset.pane === name;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      if (on && focusNav) b.focus();
    });
    panes.forEach(function (p) {
      p.hidden = p.dataset.pane !== name;
    });
  }

  navBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { selectPane(btn.dataset.pane, false); });

    // 侧边栏内的方向键：上/下移动焦点并切换，符合 tablist 惯例
    btn.addEventListener('keydown', function (e) {
      var i = navBtns.indexOf(btn);
      var next = null;
      if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = navBtns[(i + 1) % navBtns.length];
      if (e.key === 'ArrowUp'   || e.key === 'ArrowLeft')  next = navBtns[(i - 1 + navBtns.length) % navBtns.length];
      if (e.key === 'Home') next = navBtns[0];
      if (e.key === 'End')  next = navBtns[navBtns.length - 1];
      if (!next) return;
      e.preventDefault();
      // 阻止冒泡到 document，否则 app.js 的 ←/→ 会在背后翻归档日期
      e.stopPropagation();
      selectPane(next.dataset.pane, true);
    });
  });

  /* ---------- 密码显隐 ---------- */

  modal.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-reveal]');
    if (!btn) return;
    var input = $(btn.dataset.reveal);
    if (!input) return;
    var show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.setAttribute('aria-label', (show ? '隐藏' : '显示') + '内容');
  });

  /* ---------- 字段映射：分区 → [字段名, 输入框 id] ---------- */

  var FIELDS = {
    site:  [['rss_url', 'siteRss'], ['username', 'siteUser'], ['password', 'sitePass']],
    basic: [['password', 'setPassword']],
    proxy: [['type', 'proxyType'], ['host', 'proxyHost'], ['port', 'proxyPort'],
            ['username', 'proxyUser'], ['password', 'proxyPass']],
    emby:  [['url', 'embyUrl'], ['api_key', 'embyKey'],
            ['username', 'embyUser'], ['library', 'embyLib']],
    tg:    [['token', 'tgToken'], ['chat_id', 'tgChat']]
  };

  /* 与后端 config.SECRET_FIELDS 对齐：这些字段后端只回占位符 */
  var SECRET_FIELDS = ['password', 'token', 'apikey', 'api_key', 'secret'];
  var PLACEHOLDER = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';

  function isSecret(key) { return SECRET_FIELDS.indexOf(key) >= 0; }

  /* ---------- 读取：拉设置并填充 ---------- */

  function fill(data) {
    Object.keys(FIELDS).forEach(function (section) {
      var values = (data && data[section]) || {};
      FIELDS[section].forEach(function (pair) {
        var el = $(pair[1]);
        // 不覆盖用户正在编辑的字段（响应慢时可能已经开打）
        if (el && document.activeElement !== el) el.value = values[pair[0]] || '';
      });
    });
  }

  function load() {
    return window.api.getSettings().then(function (data) {
      fill(data);
      // ★ 媒体库下拉框的选项**不来自设置**，来自 Emby 的库列表。
      //   每次打开面板都重拉：用户可能刚在 Emby 里建了新库。
      loadLibraries(data && data.emby ? data.emby.library : '');
    }).catch(function (e) {
      toast('无法读取设置：' + e.message, 'err', { duration: 5000 });
    });
  }

  /*
   * 拉 Emby 媒体库列表填进下拉框。
   *
   * ★ 失败静默：Emby 没配或连不上时，下拉框退化为只有一个「全部媒体库」，
   *   不弹错误。用户可能只是还没填地址——此时报错是噪声（E-7 同样的思路）。
   * ★ 但**已保存的库 id 必须保留为选中项**：列表拉不到时若把 value 清空，
   *   用户一保存就把已配好的库弄丢了。
   */
  function loadLibraries(savedId) {
    var sel = $('embyLib');
    if (!sel) return;

    window.api.getEmbyLibraries().then(function (res) {
      var keep = savedId || sel.value || '';
      var opts = ['<option value="">全部媒体库</option>'];
      ((res && res.libraries) || []).forEach(function (lib) {
        opts.push('<option value="' + escAttr(lib.id) + '">' +
                  escHtml(lib.name) + '</option>');
      });

      // 已保存的 id 不在列表里（Emby 里删了/还没加载）也要保留，
      // 否则保存一次就静默丢掉配置。
      var known = ((res && res.libraries) || []).some(function (l) {
        return String(l.id) === String(keep);
      });
      if (keep && !known) {
        opts.push('<option value="' + escAttr(keep) + '">' +
                  escHtml(keep) + '（当前）</option>');
      }

      sel.innerHTML = opts.join('');
      sel.value = keep;
      renderLibHint(res);
    }).catch(function () { /* 静默，见上 */ });
  }

  /* 媒体库下拉框下面的提示行。

     说清楚列表是**按用户名过滤过的**还是**全部库**——否则用户会以为
     Emby 里少了几个库（或多了几个）。source 由后端给：
       "user" → 已按该用户权限过滤
       "all"  → 管理员视角（没配用户名 / 用户名找不到）
     warning 非空时用警示色。 */
  function renderLibHint(res) {
    var hint = $('embyLibHint');
    if (!hint) return;

    var src = res && res.source;
    var who = (res && res.filtered_by) || '';
    hint.classList.remove('is-warn');

    if (src === 'user') {
      hint.textContent = '仅显示 Emby 用户「' + who + '」能访问的媒体库。';
    } else if (res && res.warning) {
      hint.textContent = res.warning;
      hint.classList.add('is-warn');
    } else {
      hint.textContent = '留空 = 全部媒体库。填了用户名则只显示该用户能访问的库。';
    }
  }

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
               "'": '&#39;' }[c];
    });
  }

  function escAttr(s) { return escHtml(s); }

  /* ---------- 保存：按分区提交 ---------- */

  var saveTimers = {};

  function setState(name, text, kind) {
    var state = modal.querySelector('[data-state="' + name + '"]');
    if (!state) return;
    state.textContent = text;
    state.className = 'save-state' + (kind ? ' is-' + kind : '');
    clearTimeout(saveTimers[name]);
    if (kind !== 'err') {
      saveTimers[name] = setTimeout(function () {
        state.textContent = '';
        state.className = 'save-state';
      }, 2600);
    }
  }

  function collect(section) {
    var values = {};
    FIELDS[section].forEach(function (pair) {
      var el = $(pair[1]);
      if (el) values[pair[0]] = el.value;
    });
    return values;
  }

  function save(section, btn) {
    btn.disabled = true;
    setState(section, '保存中…');

    window.api.saveSettings(section, collect(section)).then(function (res) {
      btn.disabled = false;
      var saved = (res && res.saved) || [];
      var skipped = (res && res.skipped) || [];

      // ★ 已落库的密钥字段立即换回占位符：明文不该继续留在 DOM 里，
      //   否则再保存一次会把明文当「新值」重复提交。
      FIELDS[section].forEach(function (pair) {
        if (isSecret(pair[0]) && saved.indexOf(pair[0]) >= 0) {
          $(pair[1]).value = PLACEHOLDER;
        }
      });

      setState(section, skipped.length ? '已保存（密钥未改）' : '已保存', 'ok');

      // ★ 改完 Emby 用户名/地址后，媒体库列表与提示行必须**当场**重拉。
      //   不重拉的话，用户改完用户名点保存，看到的还是按**旧**用户过滤的
      //   列表——会以为过滤坏了（或以为改没生效），只好关面板重开。
      if (section === 'emby') {
        loadLibraries($('embyLib') ? $('embyLib').value : '');
      }
    }).catch(function (e) {
      btn.disabled = false;
      setState(section, e.message, 'err');
      toast('保存失败：' + e.message, 'err', { duration: 5000 });
    });
  }

  modal.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-save]');
    if (!btn) return;
    save(btn.dataset.save, btn);
  });

  /* ---------- 测试连接 ---------- */

  /*
   * 发一条测试消息到 Telegram。
   *
   * ★ 必须先保存再测：后端每次都从**数据库**读凭据，而用户刚输入的值
   *   还在 DOM 里。不先保存的话，测的是**旧凭据**——用户改了错 token、
   *   点测试、收到「测试成功」（实际用的是旧的好 token），然后一头雾水。
   *   这条路径就是「先保存，再测」的强制顺序。
   */
  function testTg(btn) {
    btn.disabled = true;
    setState('tg', '正在发送测试消息…');

    var saved = collect('tg');
    var isPlaceholder = saved.token === PLACEHOLDER;

    // 没改 token（还是占位符）就不必重存，直接测已有配置
    var prep = isPlaceholder
      ? Promise.resolve(null)
      : window.api.saveSettings('tg', saved);

    prep.then(function () {
      return window.api.testTelegram();
    }).then(function (res) {
      btn.disabled = false;
      if (res && res.ok) {
        setState('tg', '测试成功，请查看 Telegram', 'ok');
        toast('测试消息已发送，请查看 Telegram', 'ok', { duration: 3600 });
      } else {
        var detail = (res && res.detail) || '未知原因';
        setState('tg', detail, 'err');
        toast('测试失败：' + detail, 'err', { duration: 6000 });
      }
    }).catch(function (e) {
      btn.disabled = false;
      setState('tg', e.message, 'err');
      toast('测试失败：' + e.message, 'err', { duration: 6000 });
    });
  }

  /*
   * 触发一次 Emby 同步（E-6 的手动刷新）。
   *
   * ★ 必须先保存再刷：后端从**数据库**读 Emby 地址与 Key，用户刚输入的值
   *   还在 DOM 里。不先保存的话，刷新用的是**旧配置**——用户填好新地址、
   *   点刷新、得到「未配置」，一头雾水。与 testTg 同一个坑。
   */
  function testEmby(btn) {
    btn.disabled = true;
    setState('emby', '正在同步…');

    var saved = collect('emby');
    var keyIsPlaceholder = saved.api_key === PLACEHOLDER;

    var prep = keyIsPlaceholder
      ? Promise.resolve(null)
      : window.api.saveSettings('emby', saved);

    prep.then(function () {
      return window.api.refreshEmby();
    }).then(function (res) {
      btn.disabled = false;
      if (res && res.ok) {
        var msg = '已检查 ' + res.checked + ' 帖，命中 ' + res.in_library + ' 帖';
        setState('emby', msg, 'ok');
        toast('入库状态已刷新：' + msg, 'ok', { duration: 3600 });
        // 卡片标记变了，重新拉一次列表（app.js 暴露的是 window.__archive）
        var a = window.__archive;
        if (a && a.reload) a.reload();
      } else {
        var detail = (res && res.error) || '未知原因';
        setState('emby', detail, 'err');
        toast('同步失败：' + detail, 'err', { duration: 6000 });
      }
      // 顺手刷新库列表（地址改了以后库可能不同）
      loadLibraries($('embyLib') ? $('embyLib').value : '');
    }).catch(function (e) {
      btn.disabled = false;
      setState('emby', e.message, 'err');
      toast('同步失败：' + e.message, 'err', { duration: 6000 });
    });
  }

  modal.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-test]');
    if (!btn) return;
    if (btn.dataset.test === 'tg') testTg(btn);
    if (btn.dataset.test === 'emby') testEmby(btn);
  });

  /* ---------- 拦截 app.js 的方向键翻页 ---------- */

  // app.js 把 ←/→ 翻页监听在 document 冒泡阶段，弹窗打开时必须拦住。
  // 用捕获阶段（实测可阻断同节点的冒泡监听），且不 preventDefault，
  // 所以输入框内的光标移动不受影响。
  // 例外：焦点在侧边栏时放行，由 tablist 自己处理方向键切换（它会再阻断冒泡）。
  document.addEventListener('keydown', function (e) {
    if (modal.hidden) return;

    if (e.key === 'Escape') {
      e.stopPropagation();
      closeDrawer();
      return;
    }

    if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') &&
        !(e.target.closest && e.target.closest('.settings-nav'))) {
      e.stopPropagation();
    }
  }, true);

  /* ---------- 焦点陷阱 ---------- */

  function focusables() {
    return Array.prototype.slice
      .call(modal.querySelectorAll('button, input, select, [href], [tabindex]:not([tabindex="-1"])'))
      .filter(function (el) {
        // 排除禁用、不可见，以及侧边栏中 roving tabindex 的非选中项
        return !el.disabled && el.offsetParent !== null && el.getAttribute('tabindex') !== '-1';
      });
  }

  modal.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var list = focusables();
    if (!list.length) return;
    var first = list[0];
    var last  = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

})();
