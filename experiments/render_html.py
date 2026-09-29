"""
HTML 对比图渲染 (render_html)

把 `paper_comparison` 的方案画像渲染为**自包含** HTML（内联 CSS + 内联 SVG，
不依赖任何 CDN / 外链），本地双击即可在浏览器查看，便于快速迭代对比效果。

三张图 + 一张表：
1. 定位散点图（复用抽象粒度 × 学习/自进化闭环）
2. 定量收益条形图（成本 / 延迟节省，标注 benchmark 口径差异）
3. 机制对比矩阵表（8 个定性维度）
"""

from __future__ import annotations

from experiments.paper_comparison import (
    METHODS,
    QUALITATIVE_DIMENSIONS,
    MethodProfile,
)

# 配色（浅色主题）
BG = "#ffffff"
CARD = "#f7f8fa"
BORDER = "#e5e7eb"
TEXT = "#1a1d21"
MUTED = "#6b7280"
OURS = "#e11d48"       # 高亮本项目
OTHER = "#94a3b8"      # 其他方案


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# 图 1：定位散点图
# ---------------------------------------------------------------------------

def _scatter_svg() -> str:
    W, H = 640, 440
    ml, mr, mt, mb = 70, 30, 40, 70
    px = lambda x: ml + (W - ml - mr) * x / 100.0
    py = lambda y: (H - mb) - (H - mt - mb) * y / 100.0

    parts: list[str] = []
    parts.append(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
                 f'font-family="-apple-system, Segoe UI, Microsoft YaHei, sans-serif">')

    # 背景 + 四象限淡色分区
    parts.append(f'<rect x="{ml}" y="{mt}" width="{W-ml-mr}" height="{H-mt-mb}" '
                 f'fill="#fafbfc" stroke="{BORDER}"/>')
    parts.append(f'<line x1="{ml}" y1="{py(50)}" x2="{W-mr}" y2="{py(50)}" '
                 f'stroke="{BORDER}" stroke-dasharray="4 4"/>')
    parts.append(f'<line x1="{px(50)}" y1="{mt}" x2="{px(50)}" y2="{H-mb}" '
                 f'stroke="{BORDER}" stroke-dasharray="4 4"/>')

    # 轴
    parts.append(f'<line x1="{ml}" y1="{H-mb}" x2="{W-mr}" y2="{H-mb}" stroke="{TEXT}"/>')
    parts.append(f'<line x1="{ml}" y1="{H-mb}" x2="{ml}" y2="{mt}" stroke="{TEXT}"/>')

    # 轴刻度 + 标签
    for v in (0, 25, 50, 75, 100):
        parts.append(f'<text x="{px(v)}" y="{H-mb+20}" font-size="11" fill="{MUTED}" '
                     f'text-anchor="middle">{v}</text>')
        parts.append(f'<text x="{ml-12}" y="{py(v)+4}" font-size="11" fill="{MUTED}" '
                     f'text-anchor="end">{v}</text>')

    parts.append(f'<text x="{(ml+W-mr)/2}" y="{H-12}" font-size="12" fill="{TEXT}" '
                 f'text-anchor="middle">复用抽象粒度 →（动作序列 → 多 Agent 骨架）</text>')
    parts.append(
        f'<text x="18" y="{(mt+H-mb)/2}" font-size="12" fill="{TEXT}" '
        f'text-anchor="middle" transform="rotate(-90 18 {(mt+H-mb)/2})">'
        f'学习 / 自进化闭环 →</text>'
    )

    # 数据点
    for m in METHODS:
        cx, cy = px(m.x_granularity), py(m.y_evolution)
        color = OURS if m.is_ours else OTHER
        r = 9 if m.is_ours else 6
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{color}" '
                     f'fill-opacity="0.9" stroke="#fff" stroke-width="2">'
                     f'<title>{_esc(m.cite)}：{_esc(m.one_line)}</title></circle>')
        weight = "700" if m.is_ours else "500"
        label_color = OURS if m.is_ours else TEXT
        # 名称标签（YouMi 下方，其余上方或侧方，避免重叠简单处理）
        parts.append(
            f'<text x="{cx:.1f}" y="{cy - r - 6:.1f}" font-size="12" font-weight="{weight}" '
            f'fill="{label_color}" text-anchor="middle">{_esc(m.name)}</text>'
        )

    # 图例
    parts.append(
        f'<text x="{W-mr}" y="{mt+12}" font-size="11" fill="{MUTED}" text-anchor="end">'
        f'● 红色 = YouMi Agent（本框架）</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 图 2：定量收益条形图（水平）
# ---------------------------------------------------------------------------

def _h_bar_svg(title: str, items: list[tuple[str, float, bool]], note: str) -> str:
    """items: (标签, 百分比值, 是否本项目)"""
    W, H = 360, 60 + 46 * len(items)
    ml, mr, mt, mb = 130, 30, 30, 30
    maxv = max(v for _, v, _ in items) * 1.15
    bw = lambda v: (W - ml - mr) * v / maxv

    parts: list[str] = []
    parts.append(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
                 f'font-family="-apple-system, Segoe UI, Microsoft YaHei, sans-serif">')
    parts.append(f'<text x="{ml}" y="18" font-size="13" font-weight="700" fill="{TEXT}">{_esc(title)}</text>')

    row_h = 46
    for i, (label, val, is_ours) in enumerate(items):
        y0 = mt + 20 + i * row_h
        color = OURS if is_ours else OTHER
        parts.append(f'<text x="{ml-8}" y="{y0+14}" font-size="12" fill="{TEXT}" '
                     f'text-anchor="end">{_esc(label)}</text>')
        parts.append(f'<rect x="{ml}" y="{y0}" width="{bw(val):.1f}" height="26" rx="4" '
                     f'fill="{color}" fill-opacity="0.9">'
                     f'<title>{_esc(label)}: {val:.1f}%</title></rect>')
        parts.append(f'<text x="{ml+bw(val)+6:.1f}" y="{y0+18}" font-size="12" '
                     f'font-weight="600" fill="{TEXT}">{val:.1f}%</text>')
    parts.append(f'<text x="{ml}" y="{H-6}" font-size="10" fill="{MUTED}">{_esc(note)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _quant_section() -> str:
    ours = next(m for m in METHODS if m.is_ours)

    cost_items = [
        (m.name, m.cost_savings_pct, m.is_ours)
        for m in METHODS if m.cost_savings_pct is not None
    ]
    lat_items = [
        (m.name, m.latency_savings_pct, m.is_ours)
        for m in METHODS if m.latency_savings_pct is not None
    ]

    cost_svg = _h_bar_svg(
        "成本节省（相对不复用）",
        cost_items,
        "口径：YouMi=合成基准(mock)，APC=真实应用；不可直接比较",
    )
    lat_svg = _h_bar_svg(
        "延迟节省（相对不复用）",
        lat_items,
        "口径：YouMi=合成基准(mock)，APC/AgentReuse=真实数据；不可直接比较",
    )
    return cost_svg, lat_svg


# ---------------------------------------------------------------------------
# 图 3：机制对比矩阵表
# ---------------------------------------------------------------------------

def _matrix_table() -> str:
    rows: list[str] = []
    headers = ["维度"] + [m.name for m in METHODS]
    rows.append("<tr>" + "".join(
        f'<th class="dim">{_esc(h)}</th>' if i == 0 else
        f'<th class="{"ours" if METHODS[i-1].is_ours else ""}">{_esc(h)}</th>'
        for i, h in enumerate(headers)
    ) + "</tr>")

    for dim_cn, field in QUALITATIVE_DIMENSIONS:
        tds = [f'<td class="dim">{_esc(dim_cn)}</td>']
        for m in METHODS:
            val = getattr(m, field)
            cls = "ours" if m.is_ours else ""
            tds.append(f'<td class="{cls}">{_esc(val)}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


# ---------------------------------------------------------------------------
# 完整 HTML
# ---------------------------------------------------------------------------

def render_html() -> str:
    ours = next(m for m in METHODS if m.is_ours)
    cost_svg, lat_svg = _quant_section()

    # 引用说明
    refs = [
        m for m in METHODS
    ]
    ref_lines = "".join(
        f'<li><b>{_esc(m.name)}</b> — {_esc(m.venue)} {m.year} · {_esc(m.org)}：'
        f'{_esc(m.one_line)}</li>' for m in refs
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>YouMi Agent × 前沿论文对比</title>
<style>
  :root {{
    --bg: {BG}; --card: {CARD}; --border: {BORDER};
    --text: {TEXT}; --muted: {MUTED}; --ours: {OURS};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 14px; margin: 0 0 20px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 24px; margin-bottom: 24px;
  }}
  .card h2 {{ font-size: 17px; margin: 0 0 12px; }}
  .warn {{
    background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
    border-radius: 10px; padding: 12px 16px; font-size: 13px; margin-bottom: 24px;
  }}
  .kv {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
  .kv .item {{ background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }}
  .kv .item .k {{ font-size: 12px; color: var(--muted); }}
  .kv .item .v {{ font-size: 16px; font-weight: 700; margin-top: 2px; }}
  .kv .item .v.ours {{ color: var(--ours); }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 800px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  svg {{ max-width: 100%; height: auto; display: block; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; background: #fff; }}
  th, td {{ border: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f1f5f9; font-size: 12.5px; }}
  th.ours, td.ours {{ background: #fff1f2; }}
  td.dim, th.dim {{ font-weight: 700; color: var(--muted); white-space: nowrap; width: 92px; }}
  .refs {{ font-size: 12.5px; color: var(--muted); }}
  .refs li {{ margin-bottom: 4px; }}
  .foot {{ font-size: 12px; color: var(--muted); margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>YouMi Agent × 前沿论文对比</h1>
  <p class="sub">计划复用 / 记忆复用方向的机制与收益对比 · 生成时间 2026-09-03</p>

  <div class="warn">
    ⚠️ <b>口径说明</b>：YouMi 的量化数字来自<b>合成基准 + 确定性 mock</b>；
    APC / AgentReuse 来自各自<b>真实 benchmark</b>。三者口径不同、不可直接比较，
    下方条形图仅作<b>量级参考</b>。定位散点图与机制矩阵为<b>定性对比</b>。
  </div>

  <div class="card">
    <h2>YouMi Agent 一句话定位</h2>
    <div class="kv">
      <div class="item"><div class="k">复用对象</div><div class="v">{_esc(ours.reuse_target)}</div></div>
      <div class="item"><div class="k">自进化</div><div class="v ours">{_esc(ours.self_evolving)}</div></div>
      <div class="item"><div class="k">成本优化</div><div class="v">{_esc(ours.cost_opt)}</div></div>
      <div class="item"><div class="k">差异化</div><div class="v">{_esc(ours.one_line)}</div></div>
    </div>
  </div>

  <div class="card">
    <h2>① 定位图：复用粒度 × 学习闭环</h2>
    {_scatter_svg()}
  </div>

  <div class="card">
    <h2>② 定量收益对比（量级参考，口径见页首警告）</h2>
    <div class="grid2">
      <div>{cost_svg}</div>
      <div>{lat_svg}</div>
    </div>
  </div>

  <div class="card">
    <h2>③ 机制对比矩阵</h2>
    {_matrix_table()}
  </div>

  <div class="card">
    <h2>方案清单</h2>
    <ol class="refs">{ref_lines}</ol>
  </div>

  <p class="foot">由 experiments/render_html.py 生成 · 数据见 experiments/paper_comparison.py</p>
</div>
</body>
</html>"""
    return html


def write_html(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html())


__all__ = ["render_html", "write_html"]
