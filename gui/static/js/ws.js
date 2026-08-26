// WebSocket 客户端封装：自动重连 + 把消息以 CustomEvent 派发出去。
import { Store } from "./state.js";

export function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  const ws = new WebSocket(url);
  Store.ws = ws;

  ws.onopen = () => console.log("[gui] WS 已连接");
  ws.onclose = () => {
    console.warn("[gui] WS 断开，2s 后重连");
    setTimeout(connectWS, 2000);
  };
  ws.onmessage = (e) => {
    let ev;
    try {
      ev = JSON.parse(e.data);
    } catch {
      return;
    }
    window.dispatchEvent(new CustomEvent("gui-event", { detail: ev }));
  };
  return ws;
}

export function sendCommand(obj) {
  if (Store.ws && Store.ws.readyState === WebSocket.OPEN) {
    Store.ws.send(JSON.stringify(obj));
  } else {
    console.warn("[gui] WS 未就绪，命令丢弃", obj);
  }
}
