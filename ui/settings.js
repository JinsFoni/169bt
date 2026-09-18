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
    return window.api.getSettings().then(fill).catch(function (e) {
      toast('无法读取设置：' + e.message, 'err', { duration: 5000 });
    });
  }

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
