#!/usr/bin/env python
"""A/B 实验装置：可切换「全量注册」与「按需激活」两种工具暴露策略的 MCP server。

同一个进程、同一份工具目录，靠 `--eager` 开关模拟两种架构：

- `--eager`（A 组）：初始化后 `tools/list` 一次性吐回全部工具。
  等价于传统 MCP server 把整个工具库摊在上下文里。
- 默认（B 组）：初始只暴露 2 个元工具；`youmi_tool_search` 命中后把
  目标工具并入激活集，再推 `notifications/tools/list_changed`，
  客户端重新拉取时才会看到它们。工具集只增不减，避免反复抖动。

DSH 侧的行为（已核实 dsh-mcp-client/lib/index.js）：

    602  listChanged: { tools: { onChanged: () => refreshTools() } }
    632  refreshTools() → enqueueSync() → listTools({ cacheMode: "refresh" })
    150  for (const dispose of previous.values()) dispose()      // 先全量注销
    153  ctx.tools.register(definition)                            // 再全量注册

即 `tools/list` 返回什么，客户端的工具集就精确等于什么。

用法::

    python scripts/ab/mock_tool_server.py --eager          # A 组
    python scripts/ab/mock_tool_server.py                  # B 组
    python scripts/ab/mock_tool_server.py --catalog-size 40
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

PROTOCOL_VERSION = "2024-11-05"

# 12 个业务域 × 每域若干工具 = 默认 120 个，schema 刻意写得和真实工具一样啰嗦
DOMAINS: dict[str, list[tuple[str, str]]] = {
    'crm': [
        ('customer_lookup', '按手机号/邮箱/客户 ID 查询客户档案与最近 30 天互动'),
        ('customer_create', '创建客户档案，需提供姓名、联系方式、归属销售'),
        ('customer_merge', '合并两个重复客户档案，保留主档案的全部历史记录'),
        ('customer_tag_add', '为客户打标签，标签必须在运营后台已注册'),
        ('customer_timeline', '拉取客户的全部行为时间线，支持按事件类型过滤'),
        ('customer_score_refresh', '重算客户价值评分，返回新评分与分级变化'),
        ('customer_owner_transfer', '转移客户归属销售，需原销售与新销售同时确认'),
        ('customer_consent_query', '查询客户在各渠道的营销授权状态'),
        ('customer_blacklist_check', '检查客户是否命中风控黑名单'),
        ('customer_export', '导出客户列表为 CSV，单次上限 5000 行'),
    ],
    'order': [
        ('order_query', '按订单号或客户 ID 查询订单详情与履约状态'),
        ('order_cancel', '取消未发货订单，已发货订单需走退货流程'),
        ('order_split', '把一个订单拆成多个子订单，按仓库或商品类型拆分'),
        ('order_merge', '合并同一客户的多个待发货订单'),
        ('order_address_update', '修改未发货订单的收货地址'),
        ('order_note_append', '在订单上追加备注，不触发通知'),
        ('order_status_history', '查询订单状态流转历史与操作人'),
        ('order_refund_preview', '试算订单可退款金额，不实际发起退款'),
        ('order_invoice_request', '为订单申请发票，需提供抬头与税号'),
        ('order_export', '按时间范围导出订单明细'),
    ],
    'billing': [
        ('billing_refund', '发起退款，需指定订单号、金额与退款原因'),
        ('billing_refund_query', '查询退款单的处理进度与到账时间'),
        ('billing_chargeback', '登记拒付工单并冻结对应结算批次'),
        ('billing_invoice_issue', '开具正式发票并推送至客户邮箱'),
        ('billing_invoice_void', '作废已开具发票，需说明作废原因'),
        ('billing_statement', '生成账单对账单，支持按月或按结算周期'),
        ('billing_reconcile', '执行支付渠道对账，返回差异明细'),
        ('billing_plan_change', '变更客户订阅套餐，立即生效或下周期生效'),
        ('billing_coupon_apply', '为订单应用优惠券，校验适用性与叠加规则'),
        ('billing_ledger_query', '查询客户资金流水明细'),
    ],
    'inventory': [
        ('inventory_check', '查询指定 SKU 在指定仓库的可售库存，返回可卖数量'),
        ('inventory_reserve', '为订单预占库存，超时自动释放'),
        ('inventory_release', '释放已预占但未发货的库存'),
        ('inventory_adjust', '人工调整库存，需填写调整原因与凭证号'),
        ('inventory_transfer', '在仓库之间调拨库存，生成调拨单'),
        ('inventory_batch_query', '批量查询多个 SKU 的库存水位'),
        ('inventory_age_report', '生成库龄报表，识别超 90 天滞销'),
        ('inventory_cycle_count', '发起循环盘点任务并指派仓管员'),
        ('inventory_alert_rule', '配置库存预警规则与通知对象'),
        ('inventory_snapshot', '导出某一时刻的库存快照'),
    ],
    'logistics': [
        ('logistics_track', '按运单号查询物流轨迹与最新节点'),
        ('logistics_create_waybill', '为订单创建运单并预约上门取件'),
        ('logistics_intercept', '拦截在途包裹，成功后原路退回'),
        ('logistics_reschedule', '修改配送时间窗'),
        ('logistics_fee_estimate', '预估运费，支持多承运商比价'),
        ('logistics_carrier_list', '列出可用承运商及其覆盖区域'),
        ('logistics_exception_report', '上报物流异常（破损/丢件/超时）'),
        ('logistics_claim', '发起物流理赔，需附凭证照片'),
        ('logistics_route_plan', '为批量订单规划最优配送路线'),
        ('logistics_signature_query', '查询签收人信息与签收凭证'),
    ],
    'ticket': [
        ('ticket_create', '创建工单，需指定分类、优先级与处理人'),
        ('ticket_assign', '转派工单给其他处理人或队列'),
        ('ticket_reply', '回复工单客户，支持模板与附件'),
        ('ticket_close', '关闭工单并填写解决方案'),
        ('ticket_escalate', '升级工单至二线支持'),
        ('ticket_sla_query', '查询工单的 SLA 剩余时间与超时风险'),
        ('ticket_merge', '合并同一客户重复提交的工单'),
        ('ticket_template_list', '列出可用回复模板'),
        ('ticket_satisfaction', '发起工单满意度回访'),
        ('ticket_export', '按条件导出工单列表'),
    ],
    'identity': [
        ('identity_user_lookup', '按工号或邮箱查询内部员工账号'),
        ('identity_role_grant', '授予员工角色并开通对应权限，需审批单号'),
        ('identity_role_revoke', '回收角色权限'),
        ('identity_permission_check', '校验某人对某资源的操作权限'),
        ('identity_session_revoke', '强制下线指定用户的所有会话'),
        ('identity_mfa_reset', '重置用户多因子认证绑定'),
        ('identity_api_key_rotate', '轮换服务账号的 API Key'),
        ('identity_audit_log', '查询权限变更审计日志'),
        ('identity_group_manage', '维护用户组及其成员'),
        ('identity_sso_config', '查看或更新单点登录配置'),
    ],
    'report': [
        ('report_sales_daily', '生成销售日报，支持按区域或品类拆分'),
        ('report_funnel', '生成转化漏斗报表，指定时间窗口'),
        ('report_cohort', '生成客户留存队列报表，按注册月份分组查看后续留存'),
        ('report_gmv_breakdown', '拆解 GMV 构成与同比环比'),
        ('report_stock_turnover', '生成库存周转率报表'),
        ('report_refund_rate', '统计退款率并按原因归因'),
        ('report_custom_query', '执行自定义报表查询，需提供指标与维度'),
        ('report_schedule', '配置报表定时推送'),
        ('report_export', '导出报表为 Excel 或 CSV'),
        ('report_dashboard_share', '生成看板分享链接并设置有效期'),
    ],
    'notify': [
        ('notify_sms_send', '向手机号发送短信通知，需模板编号与变量'),
        ('notify_email_send', '发送邮件通知，支持 HTML 正文'),
        ('notify_push_send', '发送 App 推送，支持定向人群'),
        ('notify_template_create', '创建通知模板并提交审核'),
        ('notify_template_list', '列出可用通知模板'),
        ('notify_suppress', '设置通知免打扰规则'),
        ('notify_bounce_query', '查询通知退信与失败原因'),
        ('notify_channel_test', '测试通道连通性'),
        ('notify_batch_send', '批量发送通知，单次上限 10000 条'),
        ('notify_delivery_report', '生成通知送达率报表'),
    ],
    'approval': [
        ('approval_submit', '提交审批单，需指定审批流与金额'),
        ('approval_query', '查询审批单状态'),
        ('approval_approve', '通过审批单'),
        ('approval_reject', '驳回审批单并填写理由'),
        ('approval_withdraw', '撤回自己提交的审批单'),
        ('approval_flow_list', '列出可用审批流定义'),
        ('approval_delegate', '设置审批代理人及生效时间'),
        ('approval_remind', '催办审批单'),
        ('approval_history', '查询审批历史记录'),
        ('approval_rule_update', '更新审批金额阈值规则'),
    ],
    'risk': [
        ('risk_order_score', '对订单做实时风险评分'),
        ('risk_customer_grade', '查询客户风险等级与命中规则'),
        ('risk_rule_list', '列出当前生效的风控规则'),
        ('risk_rule_toggle', '启停某条风控规则'),
        ('risk_manual_review', '将订单转人工审核队列'),
        ('risk_blacklist_add', '将主体（客户或商户）加入黑名单'),
        ('risk_blacklist_query', '查询黑名单命中情况'),
        ('risk_case_create', '创建风控案件'),
        ('risk_case_close', '结案并填写处置结论'),
        ('risk_alert_subscribe', '订阅风控告警'),
    ],
    'search': [
        ('search_products', '按自然语言检索商品，支持属性过滤'),
        ('search_documents', '检索知识库文档'),
        ('search_similar_cases', '检索相似历史工单案例'),
        ('search_suggest', '给出搜索词补全建议'),
        ('search_index_rebuild', '重建指定索引'),
        ('search_synonym_add', '添加同义词规则'),
        ('search_ranking_tune', '调整排序权重'),
        ('search_query_log', '查询搜索日志与零结果词'),
        ('search_abtest_config', '配置搜索 A/B 实验'),
        ('search_health', '检查搜索集群健康状态'),
    ],
}

# 每个业务工具的参数骨架：让 schema 有真实的体积，而不是一行空对象
PARAMS: dict[str, list[tuple[str, str, str]]] = {
    'crm': [('customer_id', 'string', '客户 ID，与手机号二选一'), ('phone', 'string', '手机号'), ('email', 'string', '邮箱')],
    'order': [('order_no', 'string', '订单号'), ('customer_id', 'string', '客户 ID'), ('reason', 'string', '操作原因')],
    'billing': [('order_no', 'string', '订单号'), ('amount', 'number', '金额，单位元'), ('reason', 'string', '退款/变更原因')],
    'inventory': [('sku', 'string', '商品 SKU'), ('warehouse', 'string', '仓库编码'), ('quantity', 'integer', '数量')],
    'logistics': [('tracking_no', 'string', '运单号'), ('carrier', 'string', '承运商编码'), ('window', 'string', '时间窗')],
    'ticket': [('ticket_id', 'string', '工单 ID'), ('priority', 'string', '优先级 low/medium/high'), ('assignee', 'string', '处理人')],
    'identity': [('user', 'string', '工号或邮箱'), ('role', 'string', '角色编码'), ('ticket_no', 'string', '审批单号')],
    'report': [('start', 'string', '起始日期 YYYY-MM-DD'), ('end', 'string', '结束日期'), ('dimension', 'string', '拆分维度')],
    'notify': [('template', 'string', '模板编号'), ('targets', 'string', '目标人群，逗号分隔'), ('variables', 'string', '模板变量 JSON')],
    'approval': [('form_id', 'string', '审批单 ID'), ('flow', 'string', '审批流编码'), ('comment', 'string', '审批意见')],
    'risk': [('subject', 'string', '主体 ID（订单/客户）'), ('rule_id', 'string', '规则 ID'), ('level', 'string', '风险等级')],
    'search': [('query', 'string', '检索词'), ('index', 'string', '索引名'), ('filters', 'string', '过滤条件 JSON')],
}

META_TOOLS = [
    {
        'name': 'youmi_tool_search',
        'description': '按自然语言描述检索工具库，返回最匹配的少量工具。这是获取业务工具的唯一入口。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '当前任务的自然语言描述'},
                'top_k': {'type': 'integer', 'description': '返回候选数量，默认 3'},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'youmi_tool_get',
        'description': '读取单个工具的完整定义与参数说明。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': '工具名称'},
            },
            'required': ['name'],
        },
    },
]


def build_catalog(limit: int) -> list[dict[str, Any]]:
    """按域展开业务工具，每域最多取到 limit 总量。"""
    catalog: list[dict[str, Any]] = []
    for domain, entries in DOMAINS.items():
        for raw_name, description in entries:
            if len(catalog) >= limit:
                return catalog
            properties = {
                key: {'type': kind, 'description': text}
                for key, kind, text in PARAMS[domain]
            }
            # 域内名字已带域前缀的不重复拼接（billing_refund → billing_refund）
            tool_name = raw_name if raw_name.startswith(f'{domain}_') else f'{domain}_{raw_name}'
            catalog.append({
                'name': tool_name,
                'description': f'[{domain}] {description}。调用前请确认业务主体与操作原因，非法参数会被服务端拒绝。',
                'inputSchema': {
                    'type': 'object',
                    'properties': properties,
                    'required': [next(iter(properties))],
                    'additionalProperties': False,
                },
                '_domain': domain,
            })
    return catalog


class MockServer:
    """最小 MCP server：只实现 tools/list 与 tools/call。"""

    def __init__(self, catalog: list[dict[str, Any]], eager: bool) -> None:
        self.catalog = catalog
        self.by_name = {tool['name']: tool for tool in catalog}
        self.eager = eager
        self.activated: set[str] = set()
        self.pending_notice = False

    def list_tools(self) -> list[dict[str, Any]]:
        if self.eager:
            return self.catalog
        visible = list(META_TOOLS)
        visible.extend(self.by_name[name] for name in sorted(self.activated) if name in self.by_name)
        return visible

    def _public(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in tool.items() if not key.startswith('_')}

    def _search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """关键词打分：中文二元组 + 英文词，按 IDF 压低高频噪声，工具名命中额外计权。

        用 TF 直接求和时，「客户」「商品」这类出现在几十个工具里的词会把真正
        目标挤下去（实测 8 个任务有一半正确工具不在第一位）。IDF 让只有少数
        工具共享的词占主导，排序才和人的直觉一致。
        """
        chars = [ch for ch in query if '\u4e00' <= ch <= '\u9fff']
        grams = {''.join(chars[i:i + 2]) for i in range(max(len(chars) - 1, 0))}
        words = {word for word in query.lower().replace('-', ' ').split() if word}
        terms = grams | words
        if not terms:
            return []

        haystacks = [
            (f"{tool['name']} {tool['description']}".lower(), tool['name'].lower())
            for tool in self.catalog
        ]
        total = len(haystacks)
        df = {term: sum(1 for hay, _ in haystacks if term in hay) for term in terms}

        scored: list[tuple[float, dict[str, Any]]] = []
        for (hay, name), tool in zip(haystacks, self.catalog):
            score = 0.0
            for term in terms:
                if term not in hay:
                    continue
                idf = math.log((total + 1) / (df[term] + 1)) + 1.0
                # 命中工具名比命中描述更能说明这就是目标工具
                score += idf * (2.5 if term in name else 1.0)
            if score > 0:
                scored.append((score, tool))
        scored.sort(key=lambda item: -item[0])
        return [tool for _, tool in scored[:max(1, top_k)]]

    def _business_result(self, tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        """给出确定性的业务回执，让任务一次就能完成。

        早期这里只回 {'ok': true}，结果模型拿不到任何业务字段，
        会误以为工具没生效，转头用 pwsh/grep 去文件系统里找答案：
        t2 因此跑到 22 步、其中 20 次是宿主工具，把 A/B 差异完全污染成
        了「模型瞎摸索」的成本。回执必须带业务字段，模型才会收工。
        """
        domain = tool['_domain']
        receipt = {
            'billing': lambda: {'refund_id': 'RF-20261020-0001', 'order_no': arguments.get('order_no'),
                                'amount': arguments.get('amount'), 'status': 'submitted'},
            'inventory': lambda: {'sku': arguments.get('sku'), 'warehouse': arguments.get('warehouse'),
                                  'available': 428, 'reserved': 62},
            'logistics': lambda: {'tracking_no': arguments.get('tracking_no'),
                                  'latest_node': '派送中（派件员 王师傅）', 'at': '2026-10-20 09:12'},
            'ticket': lambda: {'ticket_id': arguments.get('ticket_id'), 'assignee': arguments.get('assignee'),
                               'status': 'assigned'},
            'identity': lambda: {'user': arguments.get('user'), 'role': arguments.get('role'),
                                 'status': 'granted', 'audit_id': 'AUD-20261020-77'},
            'report': lambda: {'report_id': 'RPT-20261020-01', 'rows': 12,
                               'url': 'https://bi.internal/reports/RPT-20261020-01'},
            'risk': lambda: {'subject': arguments.get('subject'), 'listed': True, 'expires': 'never'},
            'notify': lambda: {'message_id': 'MSG-20261020-88', 'targets': arguments.get('targets'),
                               'status': 'queued'},
            'approval': lambda: {'form_id': arguments.get('form_id'), 'status': 'approved'},
            'order': lambda: {'order_no': arguments.get('order_no'), 'status': 'updated'},
            'crm': lambda: {'status': 'updated'},
            'search': lambda: {'total': 7},
        }.get(domain, lambda: {'status': 'ok'})
        return {'ok': True, 'tool': tool['name'], 'result': receipt()}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == 'youmi_tool_search':
            query = str(arguments.get('query', ''))
            top_k = int(arguments.get('top_k') or 3)
            hits = self._search(query, top_k)
            # 关键动作：把命中工具并入激活集，随后推送 list_changed
            for tool in hits:
                if tool['name'] not in self.activated:
                    self.activated.add(tool['name'])
                    self.pending_notice = True
            payload = [
                {'name': tool['name'], 'description': tool['description']}
                for tool in hits
            ]
            return _text_result(json.dumps(
                {'hits': payload, 'activated': sorted(self.activated)},
                ensure_ascii=False,
            ))

        if name == 'youmi_tool_get':
            tool = self.by_name.get(str(arguments.get('name', '')))
            if tool is None:
                return _text_result(json.dumps({'error': f"未知工具 {arguments.get('name')}"}, ensure_ascii=False), True)
            self.activated.add(tool['name'])
            self.pending_notice = True
            return _text_result(json.dumps(self._public(tool), ensure_ascii=False))

        # eager 模式下全部工具本就可用；lazy 模式必须先经检索激活
        if name in self.by_name and (self.eager or name in self.activated):
            return _text_result(json.dumps(
                self._business_result(self.by_name[name], arguments),
                ensure_ascii=False,
            ))

        return _text_result(json.dumps({'error': f'工具不可用：{name}'}, ensure_ascii=False), True)


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


def write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def notify_tools_changed() -> None:
    write({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
    # DSH 自己的 info 日志不进 stderr，这里补一条打点供 A/B 驱动统计注入次数
    print('tools/list_changed', file=sys.stderr, flush=True)


def selftest(server: MockServer) -> int:
    """不走 stdio，直接验证激活逻辑是否符合预期。"""
    before = server.list_tools()
    print(f'初始 tools/list = {len(before)} 个: {[t["name"] for t in before]}')

    query = '帮客户发起退款，订单 A123，金额 199'
    hits = server._search(query, 3)
    print(f'search("{query}") 命中 {len(hits)}: {[t["name"] for t in hits]}')

    server.call('youmi_tool_search', {'query': query, 'top_k': 3})
    print(f'pending_notice = {server.pending_notice}')

    after = server.list_tools()
    print(f'激活后 tools/list = {len(after)} 个: {[t["name"] for t in after]}')

    target = hits[0]['name'] if hits else ''
    executed = server.call(target, {'order_no': 'A123', 'amount': 199})
    print(f'执行 {target} → {executed["content"][0]["text"]}')
    return 0 if hits and len(after) > len(before) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='按需激活型 MCP server（A/B 实验装置）')
    parser.add_argument('--eager', action='store_true', help='A 组：一次性暴露全部工具')
    parser.add_argument('--catalog-size', type=int, default=120, help='业务工具总量')
    parser.add_argument('--selftest', action='store_true', help='不起 stdio，直接自检激活逻辑')
    args = parser.parse_args()

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8')

    catalog = build_catalog(args.catalog_size)
    server = MockServer(catalog, args.eager)
    if args.selftest:
        return selftest(server)
    print(
        f'mock_tool_server 启动: 目录={len(catalog)} 模式={"eager" if args.eager else "lazy"}',
        file=sys.stderr,
        flush=True,
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get('method', '')
        req_id = request.get('id')
        params = request.get('params') or {}

        if method == 'initialize':
            write({
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {
                    'protocolVersion': PROTOCOL_VERSION,
                    'capabilities': {'tools': {'listChanged': True}},
                    'serverInfo': {'name': 'mock-tools', 'version': '0.1.0'},
                },
            })
            continue

        if method in ('notifications/initialized', 'notifications/cancelled'):
            continue

        if method == 'tools/list':
            write({
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {'tools': [server._public(tool) for tool in server.list_tools()]},
            })
            continue

        if method == 'tools/call':
            result = server.call(params.get('name', ''), params.get('arguments') or {})
            # 响应必须先于通知落盘，客户端才会在下一次 list 时看到新工具
            write({'jsonrpc': '2.0', 'id': req_id, 'result': result})
            if server.pending_notice:
                server.pending_notice = False
                notify_tools_changed()
            continue

        if method == 'ping':
            write({'jsonrpc': '2.0', 'id': req_id, 'result': {}})
            continue

        if req_id is not None:
            write({
                'jsonrpc': '2.0',
                'id': req_id,
                'error': {'code': -32601, 'message': f'未知方法: {method}'},
            })

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
