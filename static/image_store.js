/* image_store.js —— 图片加载与缓存：3 张 LRU + 预取下一张（v1.3.0）
 *
 * cache Map（插入序即 LRU 序）：命中移末尾、新图入末尾、超容量逐出最旧。
 * current = 最近使用（Map 末尾）。预取与当前图解码并行发起。
 */
"use strict";

const ImageStore = (() => {
  const CACHE_SIZE = 3;
  let wsId = null;
  let current = null;        // {img_id, name, bitmap, width, height}
  const cache = new Map();   // img_id → {img_id, name, bitmap, width, height}
  let inflight = new Map();  // img_id → Promise（去重并发加载）
  let prefetchCtrl = null;   // 预取中的 AbortController

  async function fetchBitmap(imgId) {
    const resp = await fetch(`/api/workspaces/${wsId}/images/${imgId}/file`);
    if (!resp.ok) throw new Error(`图片加载失败 HTTP ${resp.status}`);
    const blob = await resp.blob();
    let bmp;
    try {
      bmp = await createImageBitmap(blob);
    } catch (e) {
      // 降级：Image 元素
      bmp = await new Promise((resolve, reject) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => resolve({ isImg: true, img, url });
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("图片解码失败")); };
        img.src = url;
      });
    }
    return bmp;
  }

  function bmpSize(bmp) {
    if (bmp.isImg) return { width: bmp.img.naturalWidth, height: bmp.img.naturalHeight };
    return { width: bmp.width, height: bmp.height };
  }

  function release(bmp) {
    if (bmp && !bmp.isImg) bmp.close();
    if (bmp && bmp.isImg) URL.revokeObjectURL(bmp.url);
  }

  function evict() {
    while (cache.size > CACHE_SIZE) {
      const oldest = cache.keys().next().value;
      const e = cache.get(oldest);
      cache.delete(oldest);
      release(e.bitmap);
    }
  }

  async function load(imgId, name) {
    if (cache.has(imgId)) {
      const hit = cache.get(imgId);
      cache.delete(imgId); cache.set(imgId, hit);   // LRU 移到末尾
      current = hit;
      return hit;
    }
    let p = inflight.get(imgId);
    if (!p) {
      p = fetchBitmap(imgId).then(bmp => {
        const { width, height } = bmpSize(bmp);
        const rec = { img_id: imgId, name, bitmap: bmp, width, height };
        return rec;
      }).finally(() => inflight.delete(imgId));
      inflight.set(imgId, p);
    }
    const rec = await p;
    // 等加载期间可能已切走又切回：仍放入缓存（容量内）
    cache.set(imgId, rec);
    evict();
    current = rec;
    return rec;
  }

  function prefetch(imgId) {
    if (prefetchCtrl) prefetchCtrl.abort();
    if (!imgId || cache.has(imgId) || inflight.has(imgId)) return;
    const ctrl = new AbortController();
    prefetchCtrl = ctrl;
    fetch(`/api/workspaces/${wsId}/images/${imgId}/file`, { signal: ctrl.signal })
      .then(r => r.ok ? r.blob() : Promise.reject(new Error("skip")))
      .then(blob => createImageBitmap(blob))
      .then(bmp => {
        const { width, height } = bmpSize(bmp);
        // 解码期间用户已切到该图 → 直接装进缓存；否则按预取缓存
        cache.set(imgId, { img_id: imgId, name: "", bitmap: bmp, width, height });
        evict();
      })
      .catch(() => { /* 忽略预取失败（含 abort） */ });
  }

  function clear() {
    if (prefetchCtrl) prefetchCtrl.abort();
    for (const rec of cache.values()) release(rec.bitmap);
    cache.clear(); inflight.clear();
    current = null;
  }

  return {
    setWorkspace(id) { wsId = id; clear(); },
    get current() { return current; },
    load, prefetch, clear,
  };
})();
