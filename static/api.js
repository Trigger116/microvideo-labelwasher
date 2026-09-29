/* api.js —— 后端 fetch 封装：统一错误格式、JSON、超时 */
"use strict";

const Api = (() => {
  async function req(method, url, body, opts = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || 30000);
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
    } catch (e) {
      clearTimeout(timer);
      throw new Error("网络错误：无法连接本地服务 " + (e.name === "AbortError" ? "(超时)" : ""));
    }
    clearTimeout(timer);
    if (resp.status === 204) return null;
    let data = null;
    try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
    if (!resp.ok) {
      const err = data && data.error ? data.error : {};
      const e = new Error(err.message || `请求失败 HTTP ${resp.status}`);
      e.code = err.code || `HTTP_${resp.status}`;
      e.status = resp.status;
      throw e;
    }
    return data;
  }
  return {
    get: (url, opts) => req("GET", url, undefined, opts),
    post: (url, body, opts) => req("POST", url, body, opts),
    put: (url, body, opts) => req("PUT", url, body, opts),
    del: (url, opts) => req("DELETE", url, undefined, opts),
  };
})();
