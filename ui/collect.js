/**
 * collect.js —— 手动采集弹窗。
 *
 * 职责边界：这是 UI 编排，**不是** api.js 的第二出口（FRONTEND.md §3）。
 * 采集是用户显式触发的独立动作，与浏览状态无关，因此不复用 app.js 的
 * 渲染管线；但它必须通过 window.api 访问网络，不得自己 fetch。
 *
 * 后端语义（ARCHITECTURE.md §7）：
 *   POST /api/collect              → 202 {job}
 *   GET  /api/collect/status       → {job|null, recent[]}
 *   POST /api/collect/jobs/{id}/cancel
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
  var cancelBtn = $('collectCancel');
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

  /* ---------- 打开 / 关闭 ---------- */

  function open() {
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';

    if (!fromInput.value) {
      fromInput.value = daysAgo(2);
      toInput.value = today();
    }
    // 打开时若已有任务在跑，直接进入进度态
    window.api.getCollectStatus().then(function (s) {
      if (s && s.job) enterProgress(s.job);
    }).catch(function () { /* 后端不可达时仍允许填表，提交时报错 */ });

    fromInput.focus();
  }

  function close() {
    modal.hidden = true;
    document.body.style.overflow = '';
    stopPoll();
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  /* ---------- 视图切换 ---------- */

  function enterProgress(job) {
    jobId = job.id;
    formView.hidden = true;
    progView.hidden = false;
    startBtn.hidden = true;
    cancelBtn.hidden = false;
    closeBtn.hidden = true;          // 采集中不允许误关（可取消）
    openBtn.classList.add('is-running');
    bar.hidden = false;
    paint(job);
  }

  function enterForm() {
    jobId = null;
    formView.hidden = false;
    progView.hidden = true;
    startBtn.hidden = false;
    cancelBtn.hidden = true;
    closeBtn.hidden = false;
    openBtn.classList.remove('is-running');
    bar.hidden = true;
    bar.style.width = '0';
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

  function finish(job) {
    stopPoll();
    closeBtn.hidden = false;
    cancelBtn.hidden = true;
    openBtn.classList.remove('is-running');
    paint(job);
    statusText.textContent = job.message || PHASE_TEXT[job.phase] || '已结束';
    bar.style.width = '100%';

    if (job.status === 'done') {
      toast('采集完成：新增 ' + job.collected + ' 个，跳过 ' + job.skipped + ' 个', 'ok',
            { duration: 4200 });
      // 让主界面刷新出新日期
      if (typeof window.reloadDates === 'function') window.reloadDates();
    } else if (job.status === 'failed') {
      toast('采集失败：' + (job.message || '未知错误'), 'err', { duration: 5000 });
    }
  }

  /* ---------- 轮询 ---------- */

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function poll() {
    stopPoll();
    pollTimer = setTimeout(function () {
      window.api.getCollectStatus().then(function (s) {
        var job = s && s.job;
        if (!job) {
          // 任务已结束：从 recent 里取回终态
          var recent = (s && s.recent) || [];
          var done = recent.filter(function (j) { return j.id === jobId; })[0];
          if (done) finish(done);
          else { enterForm(); }
          return;
        }
        paint(job);
        poll();
      }).catch(function (e) {
        statusText.textContent = '状态查询失败：' + e.message;
        poll();                                  // 网络抖动不该中断轮询
      });
    }, POLL_MS);
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

  /* ---------- 事件 ---------- */

  openBtn.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  startBtn.addEventListener('click', start);
  cancelBtn.addEventListener('click', cancel);

  modal.addEventListener('click', function (e) {
    if (e.target.hasAttribute('data-close') && !closeBtn.hidden) close();
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
    if (e.key === 'Escape' && !closeBtn.hidden) close();
  });

  /* ---------- 启动：恢复进行中的任务 ---------- */

  window.api.getCollectStatus().then(function (s) {
    if (s && s.job) {
      enterProgress(s.job);
      poll();
    }
  }).catch(function () { /* 后端未起：静默，用户点按钮时才提示 */ });

  window.__collect = {
    open: open,
    get running() { return jobId !== null; }
  };
})();
