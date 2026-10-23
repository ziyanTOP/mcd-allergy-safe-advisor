"""报告渲染：把风险分级结果转成人类可读的 Markdown / 终端文本。"""

from __future__ import annotations

import json
from typing import Any

RISK_ICON = {
    "safe": "✅",
    "conditional": "🟡",
    "caution": "🟠",
    "unsafe": "⛔",
}


def _fmt_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    parts = []
    for h in hits:
        tag = ""
        if h.get("removable") and h.get("mitigation"):
            tag = f"（可去除：{h['mitigation']}）"
        elif h.get("source") == "knowledge_base":
            tag = "（静态知识库，建议到店核实）"
        parts.append(f"{h['label']}{tag}")
    return "、".join(parts)


def render_markdown(plan: dict[str, Any]) -> str:
    s = plan.get("summary", {})
    lines: list[str] = []
    lines.append("# 麦麦安心点单官 · 过敏原安全方案")
    lines.append("")
    _profile = plan.get("profile", {})
    _allergens = "、".join(_profile.get("allergens", [])) or "无"
    _avoid = "、".join(_profile.get("avoid", [])) or "无"
    _strict = "是" if _profile.get("strict") else "否"
    lines.append(f"过敏原：{_allergens} ｜ 忌口：{_avoid} ｜ 严格模式：{_strict}")
    lines.append("")
    lines.append(
        f"共评估 **{s.get('total', 0)}** 项："
        f"✅ 安心 {s.get('safe', 0)} ｜ 🟡 可去除后可选 {s.get('conditional', 0)} ｜ "
        f"🟠 需确认 {s.get('caution', 0)} ｜ ⛔ 不建议 {s.get('unsafe', 0)}"
    )
    lines.append("")

    if plan.get("combo_note"):
        lines.append(f"> ⚠️ {plan['combo_note']}")
        lines.append("")

    rec = plan.get("recommend", [])
    if rec:
        lines.append("## ✅ 推荐选择")
        lines.append("")
        for a in rec:
            lines.append(f"### {RISK_ICON.get(a['risk'],'')} {a['item']} — {a['risk_label']}")
            for r in a.get("reasons", []):
                lines.append(f"- {r}")
            if a.get("hits"):
                lines.append(f"- 处理建议：{_fmt_hits(a['hits'])}")
            if a.get("removable_actions"):
                lines.append(f"- MCP 可用特制：{'、'.join(a['removable_actions'][:5])}")
            if a.get("nutrition"):
                nut = a["nutrition"]
                vals = [f"{k}={v}" for k, v in nut.items() if k not in ("name", "raw", "category", "code", "tags") and v]
                if vals:
                    lines.append(f"- 营养：{'，'.join(vals)}")
            lines.append("")
    else:
        lines.append("## ✅ 推荐选择")
        lines.append("")
        lines.append("_当前菜单中没有任何通过安全筛查的餐品，建议换店或改为非堂食方案。_")
        lines.append("")

    rev = plan.get("review", [])
    if rev:
        lines.append("## ⛔ 需要人工确认 / 不建议选择")
        lines.append("")
        for a in rev:
            lines.append(f"### {RISK_ICON.get(a['risk'],'')} {a['item']} — {a['risk_label']}")
            for r in a.get("reasons", []):
                lines.append(f"- {r}")
            if a.get("may_contain"):
                lines.append(f"- 可能含有：{_fmt_hits(a['may_contain'])}")
            if a.get("cross_contact"):
                lines.append(f"- 交叉接触：{_fmt_hits(a['cross_contact'])}")
            lines.append("")

    pending = plan.get("pending_knowledge") or []
    if pending:
        lines.append("## 📌 知识库待补充（新品自动入队）")
        lines.append("")
        lines.append("以下成分在知识库中没有收录，无法确认过敏原，"
                     "相关餐品已自动降级为「需确认」而不是判为安全：")
        lines.append("")
        for row in pending[:15]:
            lines.append(f"- `{row['component']}`（被 {row['count']} 个餐品引用）")
        lines.append("")
        lines.append("补录方式：编辑 `data/allergen_matrix.json` 的 `items` / `sauces`，"
                     "或重新运行 `scripts/build_ground_truth.py` 刷新实测底库。")
        lines.append("")

    lines.append("## 到店话术")
    lines.append("")
    lines.append("> 我对<u>花生</u>过敏（严重程度：过敏性休克级）。请确认这个餐品全程没有接触花生、"
                 "花生油和共用油锅，并帮忙去掉<u>酱料</u>。如果不满足，我换一个。")
    lines.append("")
    lines.append("## 免责声明")
    lines.append("")
    lines.append(plan.get("disclaimer", ""))
    return "\n".join(lines)


def render_text(plan: dict[str, Any]) -> str:
    s = plan.get("summary", {})
    out: list[str] = []
    out.append("麦麦安心点单官 · 过敏原安全方案")
    out.append(
        f"过敏原: {'、'.join(plan.get('profile', {}).get('allergens', [])) or '无'} | "
        f"忌口: {'、'.join(plan.get('profile', {}).get('avoid', [])) or '无'} | "
        f"严格模式: {'是' if plan.get('profile', {}).get('strict') else '否'}"
    )
    out.append(
        f"评估 {s.get('total',0)} 项 | 安心 {s.get('safe',0)} | 可去除 {s.get('conditional',0)} | "
        f"需确认 {s.get('caution',0)} | 不建议 {s.get('unsafe',0)}"
    )
    out.append("")
    out.append("[推荐]")
    for a in plan.get("recommend", []):
        out.append(f"  {RISK_ICON.get(a['risk'],'')} {a['item']} — {a['risk_label']}")
        for r in a.get("reasons", []):
            out.append(f"      · {r}")
    if plan.get("combo_note"):
        out.append("")
        out.append(f"  ⚠️ {plan['combo_note']}")
    pending = plan.get("pending_knowledge") or []
    if pending:
        out.append("")
        out.append(f"[知识库待补充 {len(pending)} 项]（相关餐品已降级为需确认）")
        for row in pending[:10]:
            out.append(f"  · {row['component']}  被 {row['count']} 个餐品引用")
    out.append("")
    out.append("[需确认/不建议]")
    for a in plan.get("review", []):
        out.append(f"  {RISK_ICON.get(a['risk'],'')} {a['item']} — {a['risk_label']}")
        for r in a.get("reasons", []):
            out.append(f"      · {r}")
    return "\n".join(out)


def render_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2)
