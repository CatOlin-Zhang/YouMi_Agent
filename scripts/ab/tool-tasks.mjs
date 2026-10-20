// A/B 任务集（工具发现场景）：验证「海量工具下的按需加载」
//
// 设计约束：
// - 每个任务在 120 个 mock 工具里对应唯一的正确工具
// - 正确工具周围刻意安排 2-3 个近义干扰项（试算 vs 真做、查询 vs 变更）
// - 断言只看实际调用了哪个工具，不看模型怎么描述，避免文案差异干扰判定
// - prompt 不给工具名，只给业务意图，这样 A（全量）与 B（检索）面临同样的判断难度
export default [
  {
    id: 't1-refund',
    title: '发起订单退款',
    prompt: '客户投诉被重复扣款。请为订单 A123 发起 199 元退款，退款原因是重复支付。',
    expect: 'billing_refund',
    distractors: ['order_refund_preview', 'billing_refund_query', 'billing_chargeback'],
  },
  {
    id: 't2-stock',
    title: '查询可售库存',
    prompt: '华东一号仓里 SKU 为 SKU-8891 的商品现在还能卖几件？我需要一个准确的数字。',
    expect: 'inventory_check',
    distractors: ['inventory_batch_query', 'inventory_snapshot', 'inventory_age_report'],
  },
  {
    id: 't3-track',
    title: '查询物流轨迹',
    prompt: '客户问包裹到哪了。运单号 SF7788990011，请查一下最新的物流节点。',
    expect: 'logistics_track',
    distractors: ['logistics_signature_query', 'logistics_exception_report'],
  },
  {
    id: 't4-assign',
    title: '转派工单',
    prompt: '工单 T-2024-0917 现在挂在我名下，请把它转派给二线的张工（工号 E10086）。',
    expect: 'ticket_assign',
    distractors: ['ticket_escalate', 'ticket_merge', 'ticket_reply'],
  },
  {
    id: 't5-grant',
    title: '授予员工角色',
    prompt: '新同事李明（工号 E20345）需要财务复核权限，审批单号 AP-7788，请给他开通。',
    expect: 'identity_role_grant',
    distractors: ['identity_role_revoke', 'identity_permission_check', 'identity_user_lookup'],
  },
  {
    id: 't6-cohort',
    title: '生成留存报表',
    prompt: '我要看按注册月份分组的客户后续留存情况，2026-01-01 到 2026-06-30 的数据。',
    expect: 'report_cohort',
    distractors: ['report_funnel', 'report_custom_query', 'report_sales_daily'],
  },
  {
    id: 't7-blacklist',
    title: '加入风控黑名单',
    prompt: '商户 M-5521 确认是欺诈团伙，请把它拉进风控黑名单，永久生效。',
    expect: 'risk_blacklist_add',
    distractors: ['risk_blacklist_query', 'risk_rule_toggle', 'risk_manual_review'],
  },
  {
    id: 't8-sms',
    title: '发送短信通知',
    prompt: '给 13800138000 这个手机号发一条到货提醒短信，用模板 TPL-ARRIVAL。',
    expect: 'notify_sms_send',
    distractors: ['notify_email_send', 'notify_push_send', 'notify_batch_send'],
  },
]
