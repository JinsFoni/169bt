/* ============================================================
   设置弹窗 — 交互逻辑（纯 UI，不落盘）
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

  /* ---------- 打开 / 关闭 ---------- */

  function openDrawer() {
    lastFocused = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
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

  /* ---------- 保存（仅反馈，不持久化） ---------- */

  var saveTimers = {};

  function flashSaved(name) {
    var state = modal.querySelector('[data-state="' + name + '"]');
    if (!state) return;
    state.textContent = '已保存';
    clearTimeout(saveTimers[name]);
    saveTimers[name] = setTimeout(function () { state.textContent = ''; }, 2000);
  }

  modal.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-save]');
    if (!btn) return;
    flashSaved(btn.dataset.save);
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
