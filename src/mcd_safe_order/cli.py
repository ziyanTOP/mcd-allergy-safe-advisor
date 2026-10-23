"""命令行入口。

用法示例：
    python -m mcd_safe_order probe
    python -m mcd_safe_order menu --store 12345
    python -m mcd_safe_order advise --allergens peanut,milk --avoid 牛肉 --strict
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .advisor import McDonaldAdvisor
from .knowledge import AllergenProfile, KnowledgeBase
from .mcp_client import McpError, create_client
from .report import render_json, render_markdown, render_text


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.replace("，", ",").split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-safe-order",
        description="麦麦安心点单官 · 基于麦当劳 MCP 的过敏原安全点单顾问",
    )
    parser.add_argument("--token", help="MCP Token（默认读环境变量 MCD_MCP_TOKEN）")
    parser.add_argument("--url", help="MCP 地址（默认 https://mcp.mcd.cn）")
    parser.add_argument("--matrix", help="过敏原知识库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="列出 MCP 暴露的工具与参数")
    p_probe.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_stores = sub.add_parser("stores", help="查询门店，获取 storeCode / beCode")
    p_stores.add_argument("--city", help="城市名")
    p_stores.add_argument("--keyword", help="关键词，如商圈名")
    p_stores.add_argument("--search-type", type=int, default=1, choices=[1, 2], help="1=收藏餐厅 2=按位置")
    p_stores.add_argument("--be-type", type=int, default=1, choices=[1, 5], help="1=到店自提 5=得来速")
    p_stores.add_argument("--json", action="store_true")

    p_menu = sub.add_parser("menu", help="拉取门店菜单并归一化")
    p_menu.add_argument("--store", required=True, help="门店编码 storeCode")
    p_menu.add_argument("--order-type", type=int, default=1, choices=[1, 2])
    p_menu.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6])
    p_menu.add_argument("--be-code", help="业务编码，得来速/外送场景必传")
    p_menu.add_argument("--json", action="store_true")

    p_nut = sub.add_parser("nutrition", help="拉取营养数据")
    p_nut.add_argument("--json", action="store_true")

    p_adv = sub.add_parser("advise", help="生成过敏原安全方案")
    p_adv.add_argument("--allergens", help="过敏原，逗号分隔，如 peanut,milk,egg 或 花生,乳制品")
    p_adv.add_argument("--avoid", help="忌口，逗号分隔，如 牛肉,香菜")
    p_adv.add_argument("--strict", action="store_true", help="严格模式：交叉接触也判为不可选")
    p_adv.add_argument("--store", help="门店编码 storeCode")
    p_adv.add_argument("--order-type", type=int, default=1, choices=[1, 2])
    p_adv.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6])
    p_adv.add_argument("--be-code", help="业务编码，得来速/外送场景必传")
    p_adv.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    p_adv.add_argument("--no-nutrition", action="store_true", help="跳过营养数据合并")
    p_adv.add_argument("--detail-limit", type=int, help="只对前 N 个餐品拉取明细（调试用）")
    p_adv.add_argument("--offline", action="store_true", help="不连 MCP，用本地样例菜单演示")

    return parser


DEMO_MENU = [
    {"name": "巨无霸", "category": "汉堡", "price": "25", "tags": ["含芝麻面包", "芝士", "千岛酱"]},
    {"name": "麦辣鸡腿汉堡", "category": "汉堡", "price": "21", "tags": ["面包", "沙拉酱"]},
    {"name": "薯条", "category": "小食", "price": "9", "tags": ["中薯", "番茄酱"]},
    {"name": "麦乐鸡", "category": "小食", "price": "13", "tags": ["裹粉", "甜辣酱"]},
    {"name": "麦香鱼", "category": "汉堡", "price": "23", "tags": ["鱼", "芝士", "面包"]},
    {"name": "圆筒", "category": "甜品", "price": "7", "tags": ["牛奶"]},
    {"name": "美式咖啡", "category": "饮品", "price": "10", "tags": ["无奶"]},
]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kb = KnowledgeBase.load(args.matrix)

    if args.cmd == "advise" and args.offline:
        from .knowledge import build_order_plan

        profile = AllergenProfile(
            allergens=_split_csv(args.allergens),
            avoid=_split_csv(args.avoid),
            strict=args.strict,
        )
        plan = build_order_plan(DEMO_MENU, profile, kb)
        plan["store_code"] = args.store
        plan["offline_demo"] = True
        if args.format == "json":
            print(render_json(plan))
        elif args.format == "markdown":
            print(render_markdown(plan))
        else:
            print(render_text(plan))
        return 0

    try:
        client = create_client(token=args.token, url=args.url)
        client.initialize()
    except McpError as exc:
        print(f"❌ 无法连接麦当劳 MCP：{exc}", file=sys.stderr)
        print("   提示：设置环境变量 MCD_MCP_TOKEN，或使用 `advise --offline` 先跑通逻辑。", file=sys.stderr)
        return 2

    advisor = McDonaldAdvisor(client, kb)

    try:
        if args.cmd == "probe":
            tools = advisor.probe()
            if args.json:
                print(json.dumps(tools, ensure_ascii=False, indent=2))
            else:
                print(f"麦当劳 MCP 共暴露 {len(tools)} 个工具：")
                for t in tools:
                    req = "、".join(t["required"]) or "-"
                    print(f"  · {t['name']}  必填参数: {req}")
                    if t.get("schema_keys"):
                        print(f"      参数: {', '.join(t['schema_keys'])}")
        elif args.cmd == "stores":
            stores = advisor.fetch_stores(
                city=args.city,
                keyword=args.keyword,
                search_type=args.search_type,
                be_type=args.be_type,
            )
            if args.json:
                print(json.dumps(stores, ensure_ascii=False, indent=2))
            else:
                print(f"找到 {len(stores)} 家门店：")
                for s in stores:
                    print(f"  · {s.get('name') or '(未命名)':<20} {s.get('address','')[:28]}")
                    print(f"      storeCode={s.get('storeCode')}  beCode={s.get('beCode') or '-'}")
        elif args.cmd == "menu":
            items = advisor.fetch_menu(
                store_code=args.store,
                order_type=args.order_type,
                be_type=args.be_type,
                be_code=args.be_code,
            )
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            else:
                print(f"拉取到 {len(items)} 个餐品：")
                for it in items:
                    tags = ("  [" + "、".join(it.get("tags", [])[:3]) + "]") if it.get("tags") else ""
                    print(f"  · {it['name']:<16} {it.get('category',''):<6} {it.get('price','')}{tags}")
        elif args.cmd == "nutrition":
            rows = advisor.fetch_nutrition()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"拉取到 {len(rows)} 条营养数据")
                for r in rows[:20]:
                    print(f"  · {r['name']}")
        elif args.cmd == "advise":
            profile = AllergenProfile(
                allergens=_split_csv(args.allergens),
                avoid=_split_csv(args.avoid),
                strict=args.strict,
            )
            plan = advisor.advise(
                profile,
                store_code=args.store,
                order_type=args.order_type,
                be_type=args.be_type,
                be_code=args.be_code,
                with_nutrition=not args.no_nutrition,
                detail_limit=args.detail_limit,
            )
            if args.format == "json":
                print(render_json(plan))
            elif args.format == "markdown":
                print(render_markdown(plan))
            else:
                print(render_text(plan))
    except McpError as exc:
        print(f"❌ MCP 调用失败：{exc}", file=sys.stderr)
        return 3
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
