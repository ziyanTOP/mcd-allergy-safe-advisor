"""过敏原知识库与风险判定引擎。

设计原则：
1. 实时 MCP 数据（菜单标签、餐品描述）优先于静态知识库，二者取**并集**（保守策略）。
2. 静态知识库只覆盖中国大陆市场常见餐品，每条都带 source / confidence。
3. 引擎本身零第三方依赖，保证离线可跑、可单测。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_MATRIX_PATH = Path(__file__).resolve().parents[2] / "data" / "allergen_matrix.json"

RISK_SAFE = "safe"
RISK_CONDITIONAL = "conditional"
RISK_CAUTION = "caution"
RISK_UNSAFE = "unsafe"

RISK_ORDER = {RISK_SAFE: 0, RISK_CONDITIONAL: 1, RISK_CAUTION: 2, RISK_UNSAFE: 3}

RISK_LABELS = {
    RISK_SAFE: "安心可选",
    RISK_CONDITIONAL: "去除配料后可选",
    RISK_CAUTION: "谨慎，建议到店确认",
    RISK_UNSAFE: "不建议选择",
}

# 各类别会接触到的共享设备 -> 该设备涉及的过敏原
_CATEGORY_SHARED_EQUIPMENT = {
    "汉堡": ("grill", "toaster"),
    "小食": ("fryer",),
    "早餐": ("grill", "toaster"),
    "套餐": ("grill", "toaster", "fryer"),
    "甜品": ("toaster",),
    "饮品": (),
}

_EQUIPMENT_LABELS = {
    "fryer": "共用炸锅",
    "toaster": "共用烤面包机",
    "grill": "共用烤架",
}

# 忌口同义词：菜单里往往不直接写"牛肉"，而是写"厚牛堡""安格斯"
AVOID_SYNONYMS: dict[str, tuple[str, ...]] = {
    "牛肉": ("牛肉", "牛堡", "厚牛", "安格斯", "牛柳", "牛排", "肥牛"),
    "猪肉": ("猪肉", "猪柳", "培根", "烟肉", "火腿扒", "猪柳蛋"),
    "鸡肉": ("鸡肉", "鸡腿", "鸡翅", "鸡块", "鸡排", "麦辣鸡", "麦乐鸡", "龙焰鸡", "鸡堡", "板烧鸡"),
    "羊肉": ("羊肉", "羊堡", "羊肉卷"),
    "鱼": ("鱼", "鳕鱼"),
    "虾": ("虾",),
    "贝": ("贝",),
    "辣": ("辣", "辣椒"),
    "香菜": ("香菜",),
    "葱": ("葱",),
}


def avoid_variants(term: str) -> list[str]:
    """把忌口词扩展成可能出现在菜单里的写法。"""
    base = normalize(term)
    if not base:
        return []
    out = {base}
    for key, words in AVOID_SYNONYMS.items():
        if key in term or term in key:
            out.update(normalize(w) for w in words)
    return [w for w in out if w]


# 植物奶：含"奶"字但不含乳制品，不能因关键词误判为乳过敏
DAIRY_FREE_MILK_TERMS = (
    "燕麦奶", "植物奶", "杏仁奶", "椰奶", "米奶", "豆奶", "坚果奶", "咖啡伴侣",
)


def normalize(text: str) -> str:
    """归一化餐品名，便于精确/模糊匹配。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"[\s　·・\-_()（）\[\]【】,，.。/\\]+", "", text)


@dataclass
class AllergenProfile:
    """用户画像：过敏原、忌口与严格程度。"""

    allergens: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    strict: bool = False
    max_energy_kcal: float | None = None
    max_sodium_mg: float | None = None
    budget: float | None = None

    def normalized_allergens(self) -> set[str]:
        return {a.strip().lower() for a in self.allergens if a and a.strip()}


@dataclass
class Hit:
    allergen: str
    label: str
    source: str
    removable: bool = False
    mitigation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allergen": self.allergen,
            "label": self.label,
            "source": self.source,
            "removable": self.removable,
            "mitigation": self.mitigation,
        }


@dataclass
class Assessment:
    item: str
    category: str
    risk: str
    hits: list[Hit] = field(default_factory=list)
    may_contain: list[Hit] = field(default_factory=list)
    cross_contact: list[Hit] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    removable_actions: list[str] = field(default_factory=list)
    optional_additives: list[str] = field(default_factory=list)
    is_selector: bool = False
    confidence: str = "unknown"
    data_source: str = "matrix"

    @property
    def risk_label(self) -> str:
        return RISK_LABELS.get(self.risk, self.risk)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "category": self.category,
            "risk": self.risk,
            "risk_label": self.risk_label,
            "hits": [h.to_dict() for h in self.hits],
            "may_contain": [h.to_dict() for h in self.may_contain],
            "cross_contact": [h.to_dict() for h in self.cross_contact],
            "reasons": self.reasons,
            "removable_actions": list(self.removable_actions),
            "optional_additives": list(self.optional_additives),
            "is_selector": self.is_selector,
            "confidence": self.confidence,
            "data_source": self.data_source,
        }


class KnowledgeBase:
    """静态过敏原知识库。"""

    def __init__(self, payload: dict[str, Any], observed: dict[str, Any] | None = None) -> None:
        self.payload = payload
        self.observed_payload = observed or {}
        self.allergens: dict[str, dict[str, Any]] = {
            a["key"]: a for a in payload.get("allergens", [])
        }
        self.items: list[dict[str, Any]] = list(payload.get("items", []))
        self.sauces: list[dict[str, Any]] = list(payload.get("sauces", []))
        self.cross_contact_policy: dict[str, Any] = payload.get("cross_contact_policy", {})
        self.disclaimer: str = payload.get("disclaimer", "")
        self._index: dict[str, dict[str, Any]] = {}
        for item in self.items:
            for name in [item.get("name", "")] + list(item.get("aliases", [])):
                key = normalize(name)
                if key:
                    self._index[key] = item

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        observed_path: str | Path | None = None,
    ) -> "KnowledgeBase":
        target = Path(path) if path else DEFAULT_MATRIX_PATH
        with open(target, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        obs_target = Path(observed_path) if observed_path else target.with_name(
            "mcp_observed_vocabulary.json"
        )
        observed: dict[str, Any] = {}
        if obs_target.exists():
            try:
                with open(obs_target, "r", encoding="utf-8") as fh:
                    observed = json.load(fh)
            except (OSError, json.JSONDecodeError):
                observed = {}
        return cls(payload, observed)

    # -- 实测底库 ---------------------------------------------------------
    @property
    def observed_allergens(self) -> dict[str, set[str]]:
        """MCP 实测反推出的"餐品 → 过敏原"提示集。

        来源是 scripts/build_ground_truth.py 对真实 MCP 响应的抓取与关键词分析，
        因此它属于**观察到的提示**而非官方结论，在引擎中按 may_contain 处理，
        不会直接判为不可选。
        """
        cached = getattr(self, "_observed_allergens", None)
        if cached is not None:
            return cached
        hits = self.observed_payload.get("allergen_signal_hits") or {}
        table: dict[str, set[str]] = {}
        for allergen, names in hits.items():
            for name in names or []:
                table.setdefault(str(name).strip(), set()).add(str(allergen))
        self._observed_allergens = table
        return table

    def observed_for(self, name: str) -> set[str]:
        key = name.strip()
        return set(self.observed_allergens.get(key, set()))

    # -- 查询 -------------------------------------------------------------
    def lookup(self, name: str) -> dict[str, Any] | None:
        key = normalize(name)
        if not key:
            return None
        if key in self._index:
            return self._index[key]
        for alias_key, item in self._index.items():
            if alias_key and (alias_key in key or key in alias_key):
                return item
        return None

    def lookup_component(self, name: str) -> dict[str, Any] | None:
        """按名称查组件（酱料/子项/配料），命中返回过敏原信息。"""
        key = normalize(name)
        if not key:
            return None
        for entry in self.sauces:
            names = [entry.get("name", "")] + list(entry.get("aliases", []))
            for n in names:
                nk = normalize(n)
                if nk and (nk == key or nk in key or key in nk):
                    return entry
        for item in self.items:
            names = [item.get("name", "")] + list(item.get("aliases", []))
            for n in names:
                nk = normalize(n)
                if nk and (nk == key or nk in key or key in nk):
                    return item
        return None

    def allergen_keywords(self, key: str) -> list[str]:
        meta = self.allergens.get(key) or {}
        return list(meta.get("keywords", []))

    def allergen_label(self, key: str) -> str:
        meta = self.allergens.get(key)
        return meta["label"] if meta else key

    def shared_equipment_allergens(self, category: str) -> set[str]:
        policy = self.cross_contact_policy
        out: set[str] = set()
        for equip in _CATEGORY_SHARED_EQUIPMENT.get(category, ()):  # type: ignore[arg-type]
            out |= set(policy.get(equip, []))
        return out

    def shared_equipment_labels(self, category: str) -> list[str]:
        return [
            _EQUIPMENT_LABELS.get(e, e)
            for e in _CATEGORY_SHARED_EQUIPMENT.get(category, ())  # type: ignore[arg-type]
        ]

    # -- 实时标签解析 -----------------------------------------------------
    def parse_allergen_keywords(self, text: str) -> set[str]:
        """从 MCP 返回的标签/描述中解析过敏原关键词。

        特例：植物奶（燕麦奶/植物奶/杏仁奶/椰奶等）虽然含"奶"字，但不含乳制品，
        因此命中"奶"时若文本中出现植物奶词，会跳过，避免误判。
        """
        if not text:
            return set()
        low = unicodedata.normalize("NFKC", str(text)).lower()
        plant_milk = any(term in low for term in DAIRY_FREE_MILK_TERMS)
        found: set[str] = set()
        for key, meta in self.allergens.items():
            for kw in meta.get("keywords", []):
                if kw.lower() not in low:
                    continue
                if key == "milk" and kw == "奶" and plant_milk:
                    continue
                found.add(key)
                break
        return found


def _iter_texts(item: dict[str, Any]) -> Iterable[str]:
    for field_name in ("name", "description", "desc", "tag", "tags", "label", "labels", "remark"):
        value = item.get(field_name)
        if isinstance(value, str) and value.strip():
            yield value
        elif isinstance(value, (list, tuple)):
            for v in value:
                if isinstance(v, str) and v.strip():
                    yield v
                elif isinstance(v, dict):
                    for v2 in v.values():
                        if isinstance(v2, str) and v2.strip():
                            yield v2


def _soft_action(soft_names: list[str], key: str, kb: KnowledgeBase) -> str:
    """给"可去除配料"找一个对应的去险动作描述。"""
    for name in soft_names:
        if any(kw and kw in name for kw in kb.allergen_keywords(key)):
            return f"不加{name}"
    return "不加该配料"


def resolve_allergen_keys(values: Iterable[str], kb: KnowledgeBase) -> set[str]:
    """把用户输入（中文或英文）解析成知识库里的标准过敏原 key。

    例：'花生' -> peanut，'乳制品'/'奶酪' -> milk，'gluten' -> wheat。
    无法识别的输入原样保留，保证它不会被误判为安全。
    """
    resolved: set[str] = set()
    for raw in values:
        low = normalize(raw)
        if not low:
            continue
        if low in kb.allergens:
            resolved.add(low)
            continue
        matched: str | None = None
        for key, meta in kb.allergens.items():
            for kw in meta.get("keywords", []):
                nk = normalize(kw)
                if nk and (nk == low or nk in low or low in nk):
                    matched = key
                    break
            if matched:
                break
        resolved.add(matched if matched else low)
    return resolved


def assess_item(
    item: dict[str, Any],
    profile: AllergenProfile,
    kb: KnowledgeBase,
) -> Assessment:
    """对单个餐品做过敏原风险分级。"""
    name = str(item.get("name") or item.get("item_name") or item.get("meal_name") or "未知餐品")
    category = str(item.get("category") or item.get("type") or "")
    matrix = kb.lookup(name) or {}
    if not category:
        category = str(matrix.get("category") or "")

    user_allergens = resolve_allergen_keys(profile.allergens, kb)
    live_tags = [str(t) for t in (item.get("tags") or [])]
    required = [str(c) for c in (item.get("required_components") or [])]
    soft = [str(c) for c in (item.get("removable_components") or [])]
    optional = [str(c) for c in (item.get("optional_components") or [])]
    if not required and not soft and not optional:
        # 兼容只有扁平 components 字段的输入
        required = [str(c) for c in (item.get("components") or [])]
    live_actions = [str(a) for a in (item.get("removable") or [])]
    is_selector = bool(item.get("is_selector"))

    # 第一层：名称 / 标签 / 必含成分里的过敏原关键词
    live_text = " ".join([name] + live_tags + required)
    live_allergens = kb.parse_allergen_keywords(live_text)

    # 第二层：酱料与子项按名称查库（例如"甜酸酱"→ 含大豆）
    comp_allergens: set[str] = set()
    comp_may_contain: set[str] = set()
    for comp in required:
        entry = kb.lookup_component(comp)
        if not entry:
            continue
        comp_allergens |= set(entry.get("allergens", []))
        comp_may_contain |= set(entry.get("may_contain", []))

    # 第三层：默认已加但可去除的配料（命中只降级，不算硬伤）
    soft_allergens: set[str] = set()
    soft_names: list[str] = []
    for comp in soft:
        entry = kb.lookup_component(comp)
        found = kb.parse_allergen_keywords(comp)
        if entry:
            found |= set(entry.get("allergens", []))
        if found:
            soft_allergens |= found
            soft_names.append(comp)

    # 第四层：默认不���的可选加料，仅作提示
    optional_allergens: set[str] = set()
    optional_names: list[str] = []
    for comp in optional:
        entry = kb.lookup_component(comp)
        found = kb.parse_allergen_keywords(comp)
        if entry:
            found |= set(entry.get("allergens", []))
        if found:
            optional_allergens |= found
            optional_names.append(comp)

    matrix_allergens = set(matrix.get("allergens", []))
    matrix_may_contain = set(matrix.get("may_contain", []))
    removable_map: dict[str, str] = dict(matrix.get("removable", {}))

    sources: set[str] = set()
    if live_allergens or comp_allergens:
        sources.add("live_mcp")
    if matrix_allergens:
        sources.add("knowledge_base")
    data_source = "+".join(sorted(sources)) or "none"

    allergen_union = live_allergens | comp_allergens | matrix_allergens
    soft_union = soft_allergens

    hits: list[Hit] = []
    for key in sorted(user_allergens & (allergen_union | soft_union)):
        hard = key in allergen_union
        # 优先用 MCP 给出的真实可去除动作（"不加辣椒黄油风味酱"），
        # 其次用静态知识库里的去除方案。
        mitigation = ""
        for action in live_actions:
            if any(kw and kw in action for kw in kb.allergen_keywords(key)):
                mitigation = action
                break
        if not mitigation and key in removable_map:
            mitigation = removable_map[key]
        in_live = key in live_allergens or key in comp_allergens or key in soft_allergens
        in_matrix = key in matrix_allergens
        if in_live and in_matrix:
            source = "matrix+live"
        elif in_live:
            source = "live_mcp"
        else:
            source = "knowledge_base"
        hits.append(
            Hit(
                allergen=key,
                label=kb.allergen_label(key),
                source=source,
                removable=(not hard) or bool(mitigation),
                mitigation=mitigation if hard else (mitigation or f"可要求{_soft_action(soft_names, key, kb)}"),
            )
        )

    reasons: list[str] = []

    # MCP 实测底库给出的观察提示（不等于官方结论，按 may_contain 处理）
    observed_hits = kb.observed_for(name)
    observed_user = user_allergens & observed_hits

    may_contain: list[Hit] = []
    for key in sorted(user_allergens & (matrix_may_contain | comp_may_contain)):
        may_contain.append(
            Hit(
                allergen=key,
                label=kb.allergen_label(key),
                source="knowledge_base" if key in matrix_may_contain else "live_mcp",
                removable=False,
                mitigation="",
            )
        )

    for key in sorted(observed_user):
        may_contain.append(
            Hit(
                allergen=key,
                label=kb.allergen_label(key),
                source="mcp_observed",
                removable=False,
                mitigation="",
            )
        )
        reasons.append(f"MCP 实测观测到疑似含{kb.allergen_label(key)}（观察提示，需到店核实）")

    cross_contact: list[Hit] = []
    cross_sources = set(matrix.get("cross_contact", [])) | kb.shared_equipment_allergens(category)
    for key in sorted(user_allergens & cross_sources):
        cross_contact.append(
            Hit(
                allergen=key,
                label=kb.allergen_label(key),
                source="cross_contact",
                removable=False,
                mitigation="",
            )
        )

    if hits:
        hard_labels = [h.label for h in hits if h.allergen in allergen_union]
        soft_labels = [h.label for h in hits if h.allergen not in allergen_union]
        if hard_labels:
            reasons.append("含" + "、".join(hard_labels))
        if soft_labels:
            reasons.append("默认含" + "、".join(soft_labels) + "（可要求不加）")
    if may_contain:
        reasons.append("可能含有" + "、".join(h.label for h in may_contain))
    if optional_names:
        matched_opt = sorted(optional_allergens & user_allergens)
        if matched_opt:
            reasons.append(
                "可选加料含"
                + "、".join(kb.allergen_label(k) for k in matched_opt)
                + f"（{'、'.join(optional_names[:3])}）——点单时不要加"
            )
    if cross_contact:
        equip = kb.shared_equipment_labels(category)
        reasons.append(
            "存在交叉接触风险：" + "、".join(h.label for h in cross_contact)
            + ("（" + "、".join(equip) + "）" if equip else "")
        )

    # 忌口文本匹配
    excluded = False
    avoid_hit = ""
    low_name = normalize(name)
    low_tags = " ".join(normalize(t) for t in live_tags)
    low_text = low_name + " " + low_tags
    for term in profile.avoid:
        for variant in avoid_variants(term):
            if variant and variant in low_text:
                excluded = True
                avoid_hit = term
                break
        if excluded:
            break
    if excluded:
        reasons.append(f"命中忌口：{avoid_hit}")

    # 风险定级
    if excluded or any(not h.removable for h in hits):
        risk = RISK_UNSAFE
    elif hits:
        risk = RISK_CONDITIONAL
    elif profile.strict and cross_contact:
        risk = RISK_UNSAFE
    elif may_contain or cross_contact:
        risk = RISK_CAUTION
    else:
        risk = RISK_SAFE

    if risk == RISK_SAFE:
        reasons.append("未命中任何过敏原或忌口")

    return Assessment(
        item=name,
        category=category,
        risk=risk,
        hits=hits,
        may_contain=may_contain,
        cross_contact=cross_contact,
        reasons=reasons,
        removable_actions=live_actions,
        optional_additives=optional_names,
        is_selector=is_selector,
        confidence=str(matrix.get("confidence", "unknown")) if matrix else "unknown",
        data_source=data_source,
    )


def build_order_plan(
    items: Sequence[dict[str, Any]],
    profile: AllergenProfile,
    kb: KnowledgeBase,
) -> dict[str, Any]:
    """对整单做分级，并给出可执行建议。"""
    assessments = [assess_item(it, profile, kb) for it in items]
    assessments.sort(key=lambda a: (RISK_ORDER[a.risk], a.category, a.item))

    selectors = [a for a in assessments if a.is_selector]
    real = [a for a in assessments if not a.is_selector]
    safe = [a for a in real if a.risk == RISK_SAFE]
    conditional = [a for a in real if a.risk == RISK_CONDITIONAL]
    caution = [a for a in real if a.risk == RISK_CAUTION]
    unsafe = [a for a in real if a.risk == RISK_UNSAFE]

    # 组合级冲突：只要订单里既有"打算吃的"（安心/可去除）又有"带风险"的，
    # 前者就可能被共用设备污染，必须提示。
    intended = safe + conditional
    risky = unsafe + caution
    if intended and risky:
        combo_note = (
            "订单内同时包含可食用的餐品与含过敏原/存在交叉接触风险的餐品，"
            "取餐台、炸锅与操作台共用会带来交叉接触，建议分开取餐、分单存放，"
            "或把高风险餐品直接取消。"
        )
    else:
        combo_note = ""

    return {
        "profile": {
            "allergens": sorted(resolve_allergen_keys(profile.allergens, kb)),
            "avoid": list(profile.avoid),
            "strict": profile.strict,
        },
        "summary": {
            "total": len(assessments),
            "safe": len(safe),
            "conditional": len(conditional),
            "caution": len(caution),
            "unsafe": len(unsafe),
            "selectors": len(selectors),
        },
        "selectors": [a.to_dict() for a in selectors],
        "recommend": [a.to_dict() for a in safe + conditional],
        "review": [a.to_dict() for a in caution + unsafe],
        "combo_note": combo_note,
        "disclaimer": kb.disclaimer,
        "assessments": [a.to_dict() for a in assessments],
    }
