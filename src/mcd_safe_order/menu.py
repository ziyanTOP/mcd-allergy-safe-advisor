"""解析麦当劳 MCP 返回的菜单结构。

MCP 的真实返回结构（2026-10 实测）：

    query-meals
      data.categories[] : { name: <分类名>, meals: [{ code, tags[] }] }
      data.meals        : { <code>: { currentPrice, originalPrice, ... } }
      data.frequent     : 常点

    query-meal-detail
      data.name         : 餐品名
      data.rounds[]     : 套餐子项 { name, quantity, minQuantity, choices[] }
      data.modification.items[].values[] : 特制配料 { name, minQuantity, price, ... }
      data.supportModify: 是否支持特制

    list-nutrition-foods
      data              : 带类型头的 CSV 字符串 "[N]{f1,f2,...}:\n  v,v,v"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

# 套餐子项/配料里代表"酱料"的后缀，用于识别可去除的酱料
SAUCE_HINTS = ("酱", "汁", "蘸", "调味")
# 明确表示已排除的写法
EXCLUDED_PATTERN = re.compile(r"[（(]\s*(不含|不要|无)\s*[^）)]{0,6}[）)]")

# 这些不是具体餐品，而是"让用户自己挑"的选品器/组合入口
SELECTOR_PATTERNS = ("随心选", "随心配", "单品精选", "任选", "自选", "专区", "组合任选")

# 限时/限定标签：这些餐品会随活动结束下架，结论有时效性
TIME_LIMITED_TAG_PATTERNS = ("限时", "上新", "限定", "麦金卡", "新品", "尝鲜", "回归")

# 非食品 / 周边：玩具、赠品、卡券等，不参与过敏原分级
NON_FOOD_PATTERNS = (
    "玩具", "玩偶", "捶", "挠", "钥匙扣", "徽章", "礼品卡", "周边",
    "系列玩具", "手提袋", "背包", "卡包", "公仔", "拼图",
)


def is_time_limited(tags: Sequence[str]) -> bool:
    """判断餐品是否带限时/限定标签（结论有时效性）。"""
    return any(
        any(p in str(tag) for p in TIME_LIMITED_TAG_PATTERNS) for tag in tags or []
    )


def is_non_food(name: str) -> bool:
    """判断是否为玩具/周边等非食品项。"""
    n = str(name or "")
    return any(p in n for p in NON_FOOD_PATTERNS)


def looks_like_selector(name: str) -> bool:
    """判断是否为"选品器"而非具体餐品。"""
    return any(p in name for p in SELECTOR_PATTERNS)


@dataclass
class MenuItem:
    """一个可下单的餐品（含实时价格、成分构成与可去除配料）。"""

    code: str
    name: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    price: str = ""
    original_price: str = ""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    choice_codes: list[str] = field(default_factory=list)
    required_components: list[str] = field(default_factory=list)
    removable_components: list[str] = field(default_factory=list)
    optional_components: list[str] = field(default_factory=list)
    removable: list[str] = field(default_factory=list)
    support_modify: bool = False
    image: str = ""
    is_selector: bool = False

    @property
    def components(self) -> list[str]:
        return list(self.required_components) + list(self.optional_components)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "tags": list(self.tags),
            "price": self.price,
            "original_price": self.original_price,
            "required_components": list(self.required_components),
            "removable_components": list(self.removable_components),
            "optional_components": list(self.optional_components),
            "components": self.components,
            "removable": list(self.removable),
            "support_modify": self.support_modify,
            "choice_codes": list(self.choice_codes),
            "is_selector": self.is_selector,
            "rounds": self.rounds,
            "image": self.image,
        }


def parse_query_meals(payload: Any) -> list[MenuItem]:
    """解析 query-meals：分类 → 编码 → 价格。

    实测同一餐品会出现在多个分类下（如"人气热卖"与"鸡肉汉堡/卷"都有），
    因此按 code 去重，并合并它们的分类与标签。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    price_map = data.get("meals") if isinstance(data.get("meals"), dict) else {}

    items: dict[str, MenuItem] = {}
    order: list[str] = []
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        category = str(cat.get("name") or "").strip()
        for meal in cat.get("meals") or []:
            if not isinstance(meal, dict):
                continue
            code = str(meal.get("code") or "").strip()
            if not code:
                continue
            meta = price_map.get(code) if isinstance(price_map, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            tags = [str(t).strip() for t in (meal.get("tags") or []) if isinstance(t, (str, int, float))]

            item = items.get(code)
            if item is None:
                item = MenuItem(
                    code=code,
                    category=category,
                    tags=list(tags),
                    price=str(meta.get("currentPrice") or ""),
                    original_price=str(meta.get("originalPrice") or ""),
                )
                items[code] = item
                order.append(code)
            else:
                if category and category not in item.category.split(" / "):
                    item.category = f"{item.category} / {category}" if item.category else category
                for tag in tags:
                    if tag not in item.tags:
                        item.tags.append(tag)
                if not item.price and meta.get("currentPrice"):
                    item.price = str(meta.get("currentPrice"))
                    item.original_price = str(meta.get("originalPrice") or "")
    return [items[c] for c in order]


def parse_meal_detail(payload: Any) -> dict[str, Any]:
    """解析 query-meal-detail：名称、套餐子项、特制配料。

    关键语义区分（实测自麦当劳 MCP）：
      - rounds / choices 是餐品**必含**成分
      - modification.values 是**特制选项**：
          minQuantity >= 1  → 必选（含该过敏原，且无法去除）
          minQuantity == 0  → 可去除（可以要求"不加"）
                             selectedQuantity >= 1 表示默认已加，但仍可去掉
                             selectedQuantity == 0 表示默认不加，属可选加料
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    rounds = data.get("rounds") or []
    required: list[str] = []
    removable_components: list[str] = []
    optional: list[str] = []
    removable: list[str] = []
    choice_codes: list[str] = []

    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        round_name = str(rnd.get("name") or "")
        if round_name:
            required.append(round_name)
        # 整个子项可被移除（例如酱料单列为一个 round 且允许数量降到 0）
        if rnd.get("minQuantity") == 0 and round_name:
            removable.append(round_name)
        for choice in rnd.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            cname = str(choice.get("name") or "")
            if not cname:
                continue
            ccode = str(choice.get("code") or "").strip()
            if ccode:
                choice_codes.append(ccode)
            required.append(cname)
            if EXCLUDED_PATTERN.search(cname):
                # 名称里已声明"不含酱"，说明默认已排除
                required.append(f"{cname}（默认已排除酱料）")
            if choice.get("supportModify") is False and any(h in cname for h in SAUCE_HINTS):
                removable.append(f"{cname}（该子项不可特制）")

    modification = data.get("modification") or {}
    for group in modification.get("items") or []:
        if not isinstance(group, dict):
            continue
        for val in group.get("values") or []:
            if not isinstance(val, dict):
                continue
            vname = str(val.get("name") or "")
            if not vname:
                continue
            min_q = val.get("minQuantity")
            sel_q = val.get("selectedQuantity")
            if isinstance(min_q, (int, float)) and min_q >= 1:
                # 必选配料，无法去除
                required.append(vname)
            elif isinstance(sel_q, (int, float)) and sel_q >= 1:
                # 默认已加，但可以去掉：算"可去除"，不算硬命中
                removable_components.append(vname)
                removable.append(f"不加{vname}")
            else:
                # 默认不加：纯可选加料
                optional.append(vname)
                removable.append(f"不加{vname}（默认不加）")

    return {
        "name": str(data.get("name") or ""),
        "rounds": rounds,
        "choice_codes": choice_codes,
        "required_components": required,
        "removable_components": removable_components,
        "optional_components": optional,
        "components": required + removable_components + optional,
        "removable": removable,
        "support_modify": bool(data.get("supportModify")),
        "image": str(data.get("image") or ""),
    }


def parse_nutrition(payload: Any) -> list[dict[str, Any]]:
    """解析 list-nutrition-foods 的 CSV 字符串。

    形如：
        [160]{productName,energyKcal,protein,...}:
          猪柳麦满分,null,308,16,16,24,781,213
          ...
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if not isinstance(data, str) or not data.strip():
        return []

    header = re.match(r"^\s*\[\d+\]\s*\{([^}]*)\}\s*:\s*\n?", data)
    if not header:
        return []
    fields = [f.strip() for f in header.group(1).split(",")]
    rows: list[dict[str, Any]] = []
    for line in data[header.end():].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < len(fields):
            parts += [""] * (len(fields) - len(parts))
        row: dict[str, Any] = {}
        for key, value in zip(fields, parts):
            value = value.strip()
            if value in ("", "null", "None"):
                row[key] = None
                continue
            try:
                row[key] = float(value) if "." in value else int(value)
            except ValueError:
                row[key] = value
        rows.append(row)
    return rows


def nutrition_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按餐品名建立营养索引（归一化去空格）。"""
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("productName")
        if not name:
            continue
        index[str(name).strip()] = row
    return index


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
