// 启动入口：连接 WebSocket，加载数据，并把服务端事件转发给 ui.js 处理。
import { connectWS } from "./ws.js";
import { initUI, loadAgents, loadSessions, handleEvent } from "./ui.js";

function boot() {
  initUI();
  connectWS();
  loadAgents();
  loadSessions();

  window.addEventListener("gui-event", (e) => {
    try {
      handleEvent(e.detail);
    } catch (err) {
      console.error("[gui] 事件处理出错", err);
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
