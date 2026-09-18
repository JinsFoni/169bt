/**
 * gate.js —— 访问门禁界面（S-6）。
 *
 * ★ **这不是安全边界。** 真正的校验在后端（`api/gate.py`）：所有
 *   `/api/*`（除 `/api/auth/*`）与 `/img/*` 都要求有效会话，未认证
 *   一律 401。这里的遮罩只是「把该输密码的界面给出来」——把 JS 删掉
 *   也拿不到任何数据。
 *
 * 流程：
 *   1. 启动时问 `/api/auth/me`
 *   2. 未认证 → 显示遮罩、暂停 app.js 的取数
 *   3. 认证成功 → 隐藏遮罩、放行 app.js 启动
 *
 * ★ 为什么由 gate.js 控制 app.js 何时启动，而不是各自独立跑：
 *   若 app.js 先启动，它会在未认证时收到 401 并弹「无法连接后端」
 *   的错误 toast——用户看到的是「后端挂了」，而实际只是要输密码。
 */
(function (global) {
  'use strict';

  var overlay, form, input, submit, errorEl;
  var onUnlock = null;

  function show() {
    if (!overlay) return;
    overlay.hidden = false;
    // 让密码框立刻可输入：门禁界面唯一能做的事就是输密码
    if (input) {
      try { input.focus(); } catch (e) { /* 非浏览器环境 */ }
    }
  }

  function hide() {
    if (overlay) overlay.hidden = true;
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ''; }
    if (input) input.value = '';
  }

  function fail(message) {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.hidden = false;
    if (input) {
      input.setAttribute('aria-invalid', 'true');
      try { input.select(); } catch (e) { /* ignore */ }
    }
  }

  function clearError() {
    if (!errorEl) return;
    errorEl.hidden = true;
    errorEl.textContent = '';
    if (input) input.removeAttribute('aria-invalid');
  }

  function busy(state) {
    if (!submit) return;
    submit.disabled = state;
    submit.textContent = state ? '校验中…' : '进入';
  }

  function submitPassword(event) {
    if (event) event.preventDefault();
    if (!input) return;

    var password = input.value;
    if (!password) {
      fail('请输入访问密码');
      return;
    }

    clearError();
    busy(true);

    global.api.login(password).then(function () {
      busy(false);
      hide();
      if (onUnlock) onUnlock();
    }).catch(function (e) {
      busy(false);
      // 401 = 密码错；其余（超时/离线）如实转述，别一律说「密码错误」
      if (e && e.status === 401) {
        fail('访问密码错误');
      } else {
        fail(e && e.message ? e.message : '登录失败');
      }
    });
  }

  /**
   * 启动门禁检查。
   * @param {function(): void} unlocked 已认证（或门禁未启用）时调用
   */
  function start(unlocked) {
    onUnlock = unlocked;
    overlay = document.getElementById('gate');
    form = document.getElementById('gateForm');
    input = document.getElementById('gatePass');
    submit = document.getElementById('gateSubmit');
    errorEl = document.getElementById('gateError');

    if (form) form.addEventListener('submit', submitPassword);

    global.api.getAuthMe().then(function (state) {
      if (state && state.authenticated) {
        hide();
        if (onUnlock) onUnlock();
      } else {
        show();
      }
    }).catch(function () {
      // ★ 探测失败（后端没起来）不要弹门禁——那会让人以为要输密码，
      //   而实际问题是服务没运行。放行让 app.js 去报真正的错。
      if (onUnlock) onUnlock();
    });
  }

  global.gate = { start: start, show: show, hide: hide };
})(window);
