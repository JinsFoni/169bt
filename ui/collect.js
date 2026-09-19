/**
 * collect.js —— 手动采集弹窗 + 后台执行。
 *
 * 职责边界：这是 UI 编排，**不是** api.js 的第二出口（FRONTEND.md §3）。
 * 采集是用户显式触发的独立动作，与浏览状态无关，因此不复用 app.js 的
 * 渲染管线；但它必须通过 window.api 访问网络，不得自己 fetch。
 *
 * 后端语义（ARCHITECTURE.md §7）：
 *   POST /api/collect              → 202 {job}
 *   GET  /api/collect/status       → {job|null, recent[]}
 *   POST /api/collect/jobs/{id}/cancel
 *   POST /api/collect/poll         → 202 {accepted, feed_url}（C-1 立即检查新帖）
 *
 * ★ 后台模式（转后台）：任务本就在后端守护线程 + DB 里跑，前端「转后台」
 *   只是**关掉弹窗但不停轮询**——工具栏采集按钮接管状态展示（is-running
 *   呼吸 + 按钮底部细进度条 #collectBar），完成时 toast 通知并复位。
 *   状态机 view：'form'（表单）→ 'progress'（弹窗内进度）→ 'background'
 *   （弹窗关闭、轮询继续）；终态在弹窗开着时就地展示，重开弹窗则回表单
 *   （备注带一句上次结果），后台终态走 toast。无论哪种视图，同一时刻
 *   **只有一个轮询循环**（refresh ⇄ poll 心跳）。
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var modal     = $('collectModal');
  var openBtn   = $('collectBtn');
  var closeBtn  = $('collectClose');
  var formView  = $('collectForm');
  var progView  = $('collectProgress');
  var fromInput = $('collectFrom');
  var toInput   = $('collectTo');
  var startBtn  = $('collectStart');
  var pollBtn   = $('collectPoll');
  var cancelBtn = $('collectCancel');
  var bgBtn     = $('collectBackground');
  var bar       = $('collectBar');
  var note      = $('collectNote');

  var statusText = $('collectStatusText');
  var percentEl  = $('collectPercent');
  var track      = $('collectTrack');
  var fill       = $('collectFill');
  var totalEl    = $('collectTotal');
  var gotEl      = $('collectCollected');
  var skipEl     = $('collectSkipped');
  var failEl     = $('collectFailed');
  var progNote   = $('collectProgressNote');

  var POLL_MS   = 1200;
  var pollTimer = null;
  var jobId     = null;
  var view      = 'form';   // 'form' | 'progress' | 'background'
  var lastFocus = null;

  /* ---------- 工具 ---------- */

  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function isoOf(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function today() { return isoOf(new Date()); }

  function daysAgo(n) {
    var d = new Date();
    d.setDate(d.getDate() - n);
    return isoOf(d);
  }

  /** 复用 app.js 的 toast（全局函数，由 app.js 挂载）。 */
  function toast(msg, kind, opts) {
    if (typeof window.toast === 'function') window.toast(msg, kind, opts);
  }

  /* ---------- 视图切换 ---------- */

  /**
   * 进入「进行中」状态（不负责弹窗显隐——弹窗内进度与后台共用）。
   * ★ 先 stopPoll：所有入口都可能带着一个还在跑的旧循环进来，
   *   不掐掉就会双循环、双倍请求。
   */
  function adopt(job) {
    stopPoll();
    jobId = job.id;
    view = 'progress';
    formView.hidden = true;
    progView.hidden = false;
    startBtn.hidden = true;
    // ★ 「检查新帖」也要藏：采集中再点必然 409（并发度恒为 1），
    //   留着一个注定失败的按钮只会让人以为坏了。
    pollBtn.hidden = true;
    cancelBtn.hidden = false;
    closeBtn.hidden = true;          // 采集中不允许误关（可取消/转后台）
    bgBtn.hidden = false;
    openBtn.classList.add('is-running');
    bar.hidden = false;
    paint(job);
  }

  /** 弹窗内进入进度态（adopt + 打开弹窗）。 */
  function enterProgress(job) {
    adopt(job);
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  /**
   * 页面加载时静默接续进行中的任务：不弹窗、不抢焦点，
   * 工具栏按钮呼吸 + 细进度条接管展示（后台态）。
   */
  function restoreBackground(job) {
    adopt(job);
    view = 'background';
    poll();
  }

  /** 转后台：关弹窗，轮询继续。 */
  function toBackground() {
    if (view !== 'progress' || !jobId) return;
    view = 'background';
    modal.hidden = true;
    document.body.style.overflow = '';
    if (!pollTimer) poll();          // 兜底：正常路径轮询本就在跑
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  /* ---------- 打开 / 关闭 ---------- */

  /** 表单态（也是一切终态之后的复位基准）。 */
  function showForm() {
    stopPoll();
    jobId = null;
    view = 'form';
    formView.hidden = false;
    progView.hidden = true;
    startBtn.hidden = false;
    pollBtn.hidden = false;
    cancelBtn.hidden = true;
    closeBtn.hidden = false;
    bgBtn.hidden = true;
    openBtn.classList.remove('is-running');
    bar.hidden = true;
    bar.style.width = '0';
  }

  function open() {
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';

    // 后台 → 恢复进度视图：轮询循环还活着，不要动它
    if (view === 'background') {
      view = 'progress';
      return;
    }

    if (!fromInput.value) {
      fromInput.value = daysAgo(2);
      toInput.value = today();
    }
    // 回到表单（重开弹窗 = 想开新一轮），再按后端实况调整：
    // 有任务→进度态；无任务→留在表单，备注带一句上次结果
    showForm();
    window.api.getCollectStatus().then(function (s) {
      if (modal.hidden) return;      // 用户在请求途中又关掉了
      var j = s && s.job;
      if (j) { enterProgress(j); poll(); return; }
      var recent = (s && s.recent) || [];
      if (recent.length) {
        var last = recent[0];
        note.textContent = '上次采集：' + (last.message || PHASE_TEXT[last.phase] || '已结束');
      }
    }).catch(function () { /* 后端不可达时仍允许填表，提交时报错 */ });

    fromInput.focus();
  }

  function close() {
    if (view === 'progress' && jobId) return;   // 采集中不允许误关（防御）
    modal.hidden = true;
    document.body.style.overflow = '';
    stopPoll();
    view = 'form';                   // 下次打开重新同步
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  var PHASE_TEXT = {
    listing:  '正在翻列表页…',
    fetching: '正在抓取帖子详情…',
    done:     '采集完成',
    failed:   '采集失败',
    cancelled:'已取消'
  };

  function paint(job) {
    var pct = Math.max(0, Math.min(100, job.percent || 0));
    percentEl.textContent = pct + '%';
    fill.style.width = pct + '%';
    bar.style.width = pct + '%';
    track.setAttribute('aria-valuenow', String(pct));

    var phase = PHASE_TEXT[job.phase] || '正在采集…';
    statusText.textContent = job.status === 'running' ? phase : (job.message || phase);

    totalEl.textContent = job.total;
    gotEl.textContent   = job.collected;
    skipEl.textContent  = job.skipped;
    failEl.textContent  = job.failed;
    gotEl.className  = job.collected ? 'is-ok' : 'is-muted';
    skipEl.className = 'is-muted';
    failEl.className = job.failed ? 'is-err' : 'is-muted';

    progNote.textContent = job.message || (
      job.status === 'running' && job.current_tid
        ? '当前帖子 tid=' + job.current_tid
        : '—'
    );
  }

  /**
   * 任务终态收尾。分两条路：
   *   后台 —— toast 通知 + reloadDates + 全面复位（工具栏交还）；
   *   弹窗 —— 维持原行为：就地展示终态，toast + reloadDates。
   */
  function finish(job) {
    stopPoll();
    paint(job);

    if (view === 'background') {
      if (job.status === 'done') {
        toast('采集完成：新增 ' + job.collected + ' 个，跳过 ' + job.skipped + ' 个', 'ok',
              { duration: 4200 });
        // 让主界面刷新出新日期
        if (typeof window.reloadDates === 'function') window.reloadDates();
      } else if (job.status === 'failed') {
        toast('采集失败：' + (job.message || '未知错误'), 'err', { duration: 5000 });
      } else if (job.status === 'cancelled') {
        toast('采集已取消', 'info');
      }
      showForm();                    // jobId 复位、工具栏呼吸/细条全部交还
      return;
    }

    // 弹窗内：展示终态（startBtn 回来，方便直接开下一轮）
    closeBtn.hidden = false;
    cancelBtn.hidden = true;
    pollBtn.hidden = false;
    startBtn.hidden = false;
    bgBtn.hidden = true;
    openBtn.classList.remove('is-running');
    bar.hidden = true;
    bar.style.width = '0';
    statusText.textContent = job.message || PHASE_TEXT[job.phase] || '已结束';
    fill.style.width = '100%';
    jobId = null;

    if (job.status === 'done') {
      toast('采集完成：新增 ' + job.collected + ' 个，跳过 ' + job.skipped + ' 个', 'ok',
            { duration: 4200 });
      if (typeof window.reloadDates === 'function') window.reloadDates();
    } else if (job.status === 'failed') {
      toast('采集失败：' + (job.message || '未知错误'), 'err', { duration: 5000 });
    }
  }

  /* ---------- 轮询：单循环心跳 ---------- */

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  /** 取一次状态并分发；任务仍在跑就续上下一次心跳。 */
  function refresh() {
    window.api.getCollectStatus().then(function (s) {
      var j = s && s.job;
      if (j) {
        if (view === 'background') paint(j);      // 后台：只刷工具栏
        else if (j.id !== jobId) enterProgress(j); // 换了任务（旧的去而复返）
        else paint(j);
        poll();
        return;
      }
      // job 没了：从 recent 里找回本次任务的终态
      var recent = (s && s.recent) || [];
      var done = jobId && recent.filter(function (x) { return x.id === jobId; })[0];
      if (done) { finish(done); return; }
      if (jobId) { showForm(); }     // 任务凭空消失（后端重启等）：退回表单
    }).catch(function (e) {
      if (view !== 'background') statusText.textContent = '状态查询失败：' + e.message;
      poll();                        // 网络抖动不该中断轮询
    });
  }

  function poll() {
    stopPoll();
    pollTimer = setTimeout(refresh, POLL_MS);
  }

  /* ---------- 动作 ---------- */

  function start() {
    var from = fromInput.value;
    var to = toInput.value;

    if (!from || !to) {
      note.textContent = '请选择起始和结束日期。';
      note.style.color = 'var(--danger)';
      return;
    }
    if (from > to) {
      note.textContent = '起始日期不能晚于结束日期。';
      note.style.color = 'var(--danger)';
      return;
    }
    note.style.color = '';
    note.textContent = '采集为串行限速（每帖间隔 2–5 秒），数百帖可能耗时较久。';

    startBtn.disabled = true;
    window.api.startCollect(from, to).then(function (res) {
      startBtn.disabled = false;
      enterProgress(res.job);
      poll();
    }).catch(function (e) {
      startBtn.disabled = false;
      if (e.code === 'collect_running') {
        note.textContent = '已有采集任务在运行，请等待其完成或取消。';
        note.style.color = 'var(--danger)';
        window.api.getCollectStatus().then(function (s) {
          if (s && s.job) { enterProgress(s.job); poll(); }
        }).catch(function () {});
        return;
      }
      note.textContent = e.message;
      note.style.color = 'var(--danger)';
      toast('无法开始采集：' + e.message, 'err', { duration: 4600 });
    });
  }

  function cancel() {
    if (!jobId) return;
    cancelBtn.disabled = true;
    window.api.cancelCollect(jobId).then(function () {
      cancelBtn.disabled = false;
      statusText.textContent = '正在取消…（当前帖子抓完后停止）';
    }).catch(function (e) {
      cancelBtn.disabled = false;
      toast('取消失败：' + e.message, 'err');
    });
  }

  /**
   * C-1：立即跑一轮 RSS 轮询（不必等 5 分钟的定时器）。
   *
   * ★ 不需要填日期：RSS 只回最近 20 条，轮询器自己算归档日期。
   * ★ 通道被占时后端**同步**返回 409 —— 此时引导用户去看进度，
   *   而不是干等一个不会出现的 202。
   */
  function pollNow() {
    pollBtn.disabled = true;
    window.api.pollNow().then(function () {
      pollBtn.disabled = false;
      toast('已开始检查新帖', 'ok');
      // 有新帖就会建任务，跟着进度条走
      window.api.getCollectStatus().then(function (s) {
        if (s && s.job) { enterProgress(s.job); poll(); }
      }).catch(function () {});
    }).catch(function (e) {
      pollBtn.disabled = false;
      if (e.code === 'collect_running') {
        toast('已有采集任务在运行，请等待其完成或取消。', 'err', { duration: 4600 });
        window.api.getCollectStatus().then(function (s) {
          if (s && s.job) { enterProgress(s.job); poll(); }
        }).catch(function () {});
        return;
      }
      toast('检查新帖失败：' + e.message, 'err', { duration: 4600 });
    });
  }

  /* ---------- 事件 ---------- */

  openBtn.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  startBtn.addEventListener('click', start);
  pollBtn.addEventListener('click', pollNow);
  cancelBtn.addEventListener('click', cancel);
  bgBtn.addEventListener('click', toBackground);

  modal.addEventListener('click', function (e) {
    if (!e.target.hasAttribute('data-close')) return;
    if (view === 'progress' && jobId) toBackground();   // 采集中点遮罩 = 转后台
    else close();
  });

  // 快捷范围
  modal.querySelectorAll('[data-quick]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var n = +btn.dataset.quick;
      toInput.value = today();
      fromInput.value = n === 1 ? today() : daysAgo(n - 1);
    });
  });

  document.addEventListener('keydown', function (e) {
    if (modal.hidden) return;
    if (e.key !== 'Escape') return;
    if (view === 'progress' && jobId) toBackground();
    else close();
  });

  /* ---------- 启动：恢复进行中的任务 ---------- */

  /*
   * ★ 必须经门禁放行后再取数（S-6）。
   *
   * 之前这里是无条件 IIFE，页面一加载就 GET /api/collect/status。门禁
   * 开启时尚未登录 → 401，控制台报红；用户看到的是「坏了」，而实际只是
   * 还没输密码。与 app.js 一致，改走 gate.onReady。
   *
   * ★ 有任务在跑时**静默转后台**（不弹窗）：用户刷新页面不代表想被
   *   弹窗糊脸，工具栏按钮的呼吸 + 细进度条足以说明「还在跑」。
   */
  function restoreRunning() {
    window.api.getCollectStatus().then(function (s) {
      if (s && s.job) restoreBackground(s.job);
    }).catch(function () { /* 后端未起：静默，用户点按钮时才提示 */ });
  }

  if (window.gate && window.gate.onReady) {
    window.gate.onReady(restoreRunning);
  } else {
    restoreRunning();
  }

  window.__collect = {
    open: open,
    get running() { return jobId !== null; }
  };
})();
