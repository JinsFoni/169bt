/**
 * api.js —— **唯一**知道 HTTP 的地方（FRONTEND.md §3.1）。
 *
 * 三条硬规则：
 *   1. views 不得出现 fetch
 *   2. store 是唯一调用 api 的模块
 *   3. api 是唯一出现 fetch( 的模块
 *
 * 所有失败都归一化成 ApiError { status, code, message }，
 * 调用方只处理这一种错误类型。
 */
(function (global) {
  'use strict';

  var TIMEOUT = 10000;

  /**
   * @param {number} status
   * @param {string} code
   * @param {string} message
   */
  function ApiError(status, code, message, detail) {
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.message = message;
    // ★ 保留后端附带的额外字段（如 TG 限流的 retry_after）。
    //   丢掉它们会让前端只能干巴巴说「失败了」，用户不知道等多久。
    this.detail = detail || null;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /**
   * 统一请求：超时、错误归一化、JSON 解析。
   * @param {string} method
   * @param {string} path
   * @param {object=} body
   * @returns {Promise<any>}
   */
  function request(method, path, body) {
    var controller = typeof AbortController !== 'undefined'
      ? new AbortController() : null;
    var timer = null;

    if (controller) {
      timer = setTimeout(function () { controller.abort(); }, TIMEOUT);
    }

    var init = {
      method: method,
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin'
    };
    if (controller) init.signal = controller.signal;
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    return fetch(path, init).then(function (res) {
      if (timer) clearTimeout(timer);
      if (res.status === 204) return null;

      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (res.ok) return data;

        // 后端统一错误形状：{"error":{"code","message"}}
        var err = data && data.error ? data.error : {};
        throw new ApiError(
          res.status,
          err.code || 'http_' + res.status,
          err.message || ('请求失败（HTTP ' + res.status + '）'),
          err
        );
      });
    }).catch(function (e) {
      if (timer) clearTimeout(timer);
      if (e instanceof ApiError) throw e;
      if (e && e.name === 'AbortError') {
        throw new ApiError(0, 'timeout', '请求超时，请检查后端是否在运行');
      }
      // fetch 抛 TypeError = 网络不可达（FRONTEND.md §3.1 离线降级）
      throw new ApiError(0, 'offline', '无法连接后端服务');
    });
  }

  var api = {
    request: request,

    getHealth: function () { return request('GET', '/api/health'); },

    // ---- 访问门禁（S-6）
    getAuthMe: function () { return request('GET', '/api/auth/me'); },
    login: function (password) {
      return request('POST', '/api/auth/login', { password: password });
    },
    logout: function () { return request('POST', '/api/auth/logout'); },

    getDates: function () { return request('GET', '/api/dates'); },
    getPosts: function (date) {
      return request('GET', '/api/posts?date=' + encodeURIComponent(date));
    },
    getStatus: function () { return request('GET', '/api/status'); },

    getSettings: function () { return request('GET', '/api/settings'); },
    saveSettings: function (section, values) {
      return request('PUT', '/api/settings', { section: section, values: values });
    },

    // ---- 手动采集（新增）
    startCollect: function (fromDate, toDate) {
      return request('POST', '/api/collect',
        { from_date: fromDate, to_date: toDate });
    },
    getCollectStatus: function () {
      return request('GET', '/api/collect/status');
    },
    cancelCollect: function (jobId) {
      return request('POST', '/api/collect/jobs/' + jobId + '/cancel');
    },

    deletePost: function (tid) {
      return request('DELETE', '/api/posts/' + tid);
    },

    // TG 转发（T-1/T-2）。★ 不是文件下载——是把 ed2k 发到 Bot，
    // 由 Bot 侧的 aria2/qBittorrent 接手。
    forward: function (tid) {
      return request('POST', '/api/posts/' + tid + '/forward');
    },
    forwardDay: function (date) {
      return request('POST', '/api/archive/forward?date=' + encodeURIComponent(date));
    },
    testTelegram: function () {
      return request('POST', '/api/settings/test/telegram');
    }
  };

  global.ApiError = ApiError;
  global.api = api;
})(window);
