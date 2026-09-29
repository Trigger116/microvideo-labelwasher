/* image_store.js —— 图片加载与缓存：单张驻留 + 预取下一张 */
"use strict";

const ImageStore = (() => {
  let wsId = null;
  let current = null;        // {img_id, name, bitmap, width, height}
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

  async function load(imgId, name) {
    if (current && current.img_id === imgId && current.bitmap) return current;
    const bmp = await fetchBitmap(imgId);
    if (current) release(current.bitmap);
    const { width, height } = bmpSize(bmp);
    current = { img_id: imgId, name, bitmap: bmp, width, height };
    return current;
  }

  function prefetch(imgId) {
    if (prefetchCtrl) prefetchCtrl.abort();
    if (!imgId || (current && current.img_id === imgId)) return;
    const ctrl = new AbortController();
    prefetchCtrl = ctrl;
    fetch(`/api/workspaces/${wsId}/images/${imgId}/file`, { signal: ctrl.signal })
      .then(r => r.ok ? r.blob() : Promise.reject(new Error("skip")))
      .then(blob => createImageBitmap(blob))
      .then(bmp => {
        // 仅当用户尚未切到别的图时缓存
        if (!current || current.img_id !== imgId) {
          const cache = prefetched;
          release(cache.bitmap);
          prefetched = { img_id: imgId, bitmap: bmp };
        } else {
          release(bmp);
        }
      })
      .catch(() => { /* 忽略预取失败 */ });
  }

  let prefetched = null;
  function takePrefetched(imgId) {
    if (prefetched && prefetched.img_id === imgId) {
      const p = prefetched;
      prefetched = null;
      return p;
    }
    return null;
  }

  function clear() {
    if (prefetchCtrl) prefetchCtrl.abort();
    if (current) release(current.bitmap);
    if (prefetched) release(prefetched.bitmap);
    current = null; prefetched = null;
  }

  return {
    setWorkspace(id) { wsId = id; clear(); },
    get current() { return current; },
    async load(imgId, name) {
      const p = takePrefetched(imgId);
      if (p) {
        if (current) release(current.bitmap);
        const { width, height } = bmpSize(p.bitmap);
        current = { img_id: imgId, name, bitmap: p.bitmap, width, height };
        return current;
      }
      return load(imgId, name);
    },
    prefetch,
    clear,
  };
})();
