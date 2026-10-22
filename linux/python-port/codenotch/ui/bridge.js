/* Stands in for Tauri's window.__TAURI__ inside WebKitGTK, so the Windows port's pages run
   unchanged: invoke() posts to the Python host through a WebKit script message handler and is
   answered by id; listen() is fed by the host calling __cn_emit. Injected at document start. */
(function () {
  if (window.__TAURI__) return;
  let seq = 0;
  const pending = new Map();
  const listeners = new Map();
  function post(msg) {
    window.webkit.messageHandlers.codenotch.postMessage(JSON.stringify(msg));
  }
  function invoke(cmd, args) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      try {
        post({ id, cmd, args: args || {} });
      } catch (e) {
        pending.delete(id);
        reject(e);
      }
    });
  }
  window.__cn_reply = function (id, ok, payload) {
    const p = pending.get(id);
    if (!p) return;
    pending.delete(id);
    if (ok) p.resolve(payload);
    else p.reject(new Error(String(payload)));
  };
  window.__cn_emit = function (event, payload) {
    const set = listeners.get(event);
    if (!set) return;
    for (const fn of Array.from(set)) {
      try { fn({ event, payload }); } catch (e) { console.error(e); }
    }
  };
  function listen(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return Promise.resolve(() => { const s = listeners.get(event); if (s) s.delete(fn); });
  }
  window.__TAURI__ = {
    core: { invoke },
    event: { listen },
    window: { getCurrentWindow: () => ({ close: () => invoke('__close_window') }) },
    app: { getVersion: () => invoke('__app_version') },
  };
  // Tauri's drag regions: only a press on the element that carries the attribute itself, so a
  // button sitting inside the title bar stays a button.
  document.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const t = e.target;
    if (t && t.hasAttribute && t.hasAttribute('data-tauri-drag-region')) {
      invoke('__start_window_drag', { x: e.screenX, y: e.screenY }).catch(() => {});
    }
  }, true);
})();
