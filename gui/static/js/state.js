// 全局客户端状态（单一数据源，渲染函数都从这里读取）。
export const Store = {
  ws: null,
  sessions: [],
  contacts: [],          // 已配置的 Agent（youmi/agents 下）
  activeSessionId: null,
  messages: [],          // 当前会话的消息记录（按时间线性排列）
  members: [],           // 当前会话的成员卡片
  openMsgs: {},          // msg_id -> { seg, contentEl, rec, turn, kind }
  activeTurn: null,      // 当前进行中的轮次（用户一条消息 + Agent 大卡片）
  masterId: null,        // 主 Agent 的 id（用于固定蓝色）
  workflowSteps: [],     // 当前工作流步骤（来自 tracker）
  workflowComplete: false, // 工作流是否已全部完成
  toolsList: [],          // MCP 工具列表（来自 tool_list 事件）
  toolStats: {},          // MCP 统计信息
};

// 客户端头像配色（与服务器一致地按 id 哈希，保证稳定）
const PALETTE = [
  "#2f7cf6", "#e8590c", "#2b8a3e", "#9c36b5", "#c2255c",
  "#0b7285", "#5f3dc4", "#d6336c", "#364fc7", "#5c940d",
];
const colorCache = {};

export function colorForAgent(agentId, name, explicitColor) {
  if (explicitColor) return explicitColor;
  if (agentId === "__user__") return "#07c160";
  if (agentId && agentId === Store.masterId) return "#2f7cf6";
  if (colorCache[agentId]) return colorCache[agentId];
  let h = 0;
  const s = String(agentId);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return (colorCache[agentId] = PALETTE[h % PALETTE.length]);
}
