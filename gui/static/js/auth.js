// 认证工具：token 存取 / REST 请求头 / WebSocket 查询参数。
//
// token 来源优先级：
// 1. URL 查询参数 ?token=xxx（首次打开），随后写入 localStorage 并清理地址栏
// 2. localStorage 中已保存的 token
// 服务端未启用认证时 token 为空，所有请求照常（零摩擦）。

const TOKEN_KEY = "youmi_token";

/** 解析并缓存 token（首次通过 ?token= 打开时自动保存并清理地址栏） */
export function resolveToken() {
  let params;
  try {
    params = new URLSearchParams(location.search);
  } catch {
    return "";
  }
  const urlToken = params.get("token");
  if (urlToken) {
    try {
      localStorage.setItem(TOKEN_KEY, urlToken);
    } catch {
      /* 隐私模式下 localStorage 不可用，忽略 */
    }
    // 从地址栏移除 token，避免出现在历史记录 / 截图 / 日志中
    params.delete("token");
    const qs = params.toString();
    history.replaceState(
      null,
      "",
      location.pathname + (qs ? `?${qs}` : "") + location.hash
    );
    return urlToken;
  }
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

/** REST 请求头：有 token 时附加 Authorization: Bearer */
export function authHeaders(extra = {}) {
  const token = resolveToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra };
}

/** WebSocket 查询串：有 token 时返回 ?token=xxx */
export function tokenQuery() {
  const token = resolveToken();
  return token ? `?token=${encodeURIComponent(token)}` : "";
}
