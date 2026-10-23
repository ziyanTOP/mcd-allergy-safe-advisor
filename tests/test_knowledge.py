"""针对**真实麦当劳 MCP 响应**的解析与判定测试。

fixtures/ 下的三个 JSON 是 2026-10-23 从 https://mcp.mcd.cn 实采的响应原文，
用于回归测试解析逻辑，任何对返回结构的假设变更都会在这里暴露。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_safe_order.knowledge import (  # noqa: E402
    RISK_CAUTION,
    RISK_CONDITIONAL,
    RISK_SAFE,
    RISK_UNSAFE,
    AllergenProfile,
    KnowledgeBase,
    assess_item,
    build_order_plan,
    resolve_allergen_keys,
)
from mcd_safe_order.menu import (  # noqa: E402
    nutrition_index,
    parse_meal_detail,
    parse_nutrition,
    parse_query_meals,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KB = KnowledgeBase.load(ROOT / "data" / "allergen_matrix.json")


def _load(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestKnowledgeBase(unittest.TestCase):
    def test_matrix_loads(self):
        self.assertGreater(len(KB.items), 20)
        self.assertEqual(KB.lookup("薯条").get("allergens"), [])
        self.assertTrue(KB.disclaimer)

    def test_alias_lookup(self):
        self.assertEqual(KB.lookup("Big Mac")["name"], "巨无霸")
        self.assertEqual(KB.lookup("french fries")["name"], "薯条")

    def test_chinese_allergen_resolution(self):
        self.assertEqual(resolve_allergen_keys(["花生"], KB), {"peanut"})
        self.assertEqual(resolve_allergen_keys(["乳制品"], KB), {"milk"})
        self.assertEqual(resolve_allergen_keys(["gluten"], KB), {"wheat"})
        self.assertEqual(resolve_allergen_keys(["芝麻"], KB), {"sesame"})

    def test_unknown_allergen_is_preserved(self):
        self.assertEqual(resolve_allergen_keys(["青霉素"], KB), {"青霉素"})

    def test_sauce_component_lookup(self):
        comp = KB.lookup_component("甜酸酱")
        self.assertIsNotNone(comp)
        self.assertIn("soy", comp.get("allergens", []))


class TestParseQueryMeals(unittest.TestCase):
    """解析真实 query-meals 响应。"""

    def setUp(self):
        self.items = parse_query_meals(_load("query-meals.json"))

    def test_parses_all_categories(self):
        self.assertGreater(len(self.items), 60)
        self.assertTrue(all(i.code for i in self.items))

    def test_price_is_joined_from_meals_map(self):
        by_code = {i.code: i for i in self.items}
        # fixtures 里 currentPrice=15.9 / originalPrice=24 的餐品
        item = by_code.get("521952")
        self.assertIsNotNone(item)
        self.assertEqual(item.price, "15.9")
        self.assertEqual(item.original_price, "24")

    def test_category_and_tags_are_kept(self):
        dragon = next(i for i in self.items if i.code == "521952")
        self.assertTrue(dragon.category)
        self.assertIn("上新", dragon.tags)

    def test_no_duplicate_codes(self):
        codes = [i.code for i in self.items]
        self.assertEqual(len(codes), len(set(codes)))


class TestParseMealDetail(unittest.TestCase):
    """解析真实 query-meal-detail 响应。"""

    def test_parses_combo_item(self):
        detail = parse_meal_detail(_load("query-meal-detail.json"))
        self.assertEqual(detail["name"], "“我就喜欢”鸡薯双全盒")
        self.assertTrue(detail["components"])
        self.assertIn("5块麦乐鸡", " ".join(detail["components"]))

    def test_parses_single_burger_with_modification(self):
        payload = {
            "data": {
                "code": "521952",
                "name": "龙焰鸡腿堡",
                "rounds": [],
                "modification": {
                    "items": [
                        {
                            "maxValues": 2,
                            "minValues": 0,
                            "values": [
                                {"code": "120549", "price": 0, "name": "辣椒黄油风味酱",
                                 "minQuantity": 1, "maxQuantity": 1},
                                {"code": "120512", "price": 0, "name": "切块生菜",
                                 "minQuantity": 0, "maxQuantity": 1},
                            ],
                        }
                    ]
                },
                "supportModify": True,
            }
        }
        detail = parse_meal_detail(payload)
        self.assertEqual(detail["name"], "龙焰鸡腿堡")
        self.assertIn("辣椒黄油风味酱", detail["components"])
        # minQuantity=0 的配料应被识别为可去除
        self.assertTrue(any("切块生菜" in r for r in detail["removable"]))
        self.assertTrue(detail["support_modify"])


class TestParseNutrition(unittest.TestCase):
    def test_parses_real_nutrition_payload(self):
        rows = parse_nutrition(_load("list-nutrition-foods.json"))
        self.assertGreater(len(rows), 50)
        big = next(r for r in rows if r.get("productName") == "巨无霸")
        self.assertEqual(big["energyKcal"], 513)
        self.assertEqual(big["sodium"], 961)

    def test_nutrition_index(self):
        rows = parse_nutrition(_load("list-nutrition-foods.json"))
        idx = nutrition_index(rows)
        self.assertIn("巨无霸", idx)
        self.assertIn("麦香鱼", idx)


class TestAssessItem(unittest.TestCase):
    def test_bigmac_with_peanut_is_caution(self):
        a = assess_item({"name": "巨无霸"}, AllergenProfile(allergens=["花生"]), KB)
        self.assertEqual(a.risk, RISK_CAUTION)
        self.assertEqual([h.allergen for h in a.may_contain], ["peanut"])

    def test_bigmac_with_milk_is_conditional(self):
        a = assess_item({"name": "巨无霸"}, AllergenProfile(allergens=["乳制品"]), KB)
        self.assertEqual(a.risk, RISK_CONDITIONAL)
        self.assertTrue(a.hits[0].removable)
        self.assertIn("去芝士", a.hits[0].mitigation)

    def test_bigmac_with_egg_is_conditional(self):
        a = assess_item({"name": "巨无霸"}, AllergenProfile(allergens=["蛋"]), KB)
        self.assertEqual(a.risk, RISK_CONDITIONAL)
        self.assertIn("千岛酱", a.hits[0].mitigation)

    def test_fries_with_wheat_is_caution_only(self):
        a = assess_item({"name": "薯条"}, AllergenProfile(allergens=["wheat"]), KB)
        self.assertEqual(a.risk, RISK_CAUTION)
        self.assertEqual([h.allergen for h in a.cross_contact], ["wheat"])

    def test_strict_mode_upgrades_cross_contact_to_unsafe(self):
        a = assess_item(
            {"name": "薯条"}, AllergenProfile(allergens=["wheat"], strict=True), KB
        )
        self.assertEqual(a.risk, RISK_UNSAFE)

    def test_live_tags_are_detected(self):
        a = assess_item(
            {"name": "神秘新品", "tags": ["含乳制品", "含花生"]},
            AllergenProfile(allergens=["乳制品"]),
            KB,
        )
        self.assertEqual(a.risk, RISK_UNSAFE)
        self.assertEqual(a.hits[0].allergen, "milk")
        self.assertEqual(a.hits[0].source, "live_mcp")

    def test_component_sauce_is_resolved(self):
        # 组件"甜酸酱"经酱料表解析出大豆
        a = assess_item(
            {"name": "神秘新品", "components": ["甜酸酱"]},
            AllergenProfile(allergens=["大豆"]),
            KB,
        )
        self.assertEqual(a.risk, RISK_UNSAFE)
        self.assertEqual(a.hits[0].allergen, "soy")

    def test_live_removable_action_downgrades_to_conditional(self):
        # MCP 给出"不加辣椒黄油风味酱"，其中含乳制品
        a = assess_item(
            {
                "name": "龙焰鸡腿堡",
                "components": ["辣椒黄油风味酱", "切块生菜"],
                "removable": ["不加切块生菜", "不加辣椒黄油风味酱"],
            },
            AllergenProfile(allergens=["乳制品"]),
            KB,
        )
        self.assertEqual(a.risk, RISK_CONDITIONAL)
        self.assertIn("黄油", a.hits[0].mitigation)

    def test_avoid_term_marks_unsafe(self):
        a = assess_item(
            {"name": "安格斯牛肉堡"}, AllergenProfile(avoid=["牛肉"]), KB
        )
        self.assertEqual(a.risk, RISK_UNSAFE)
        self.assertTrue(any("忌口" in r for r in a.reasons))

    def test_avoid_term_matches_menu_naming(self):
        # 菜单里不一定直接写"牛肉"
        for name in ("培根安格斯厚牛堡", "安格斯MAX厚牛堡", "双层安格斯牛堡"):
            a = assess_item({"name": name}, AllergenProfile(avoid=["牛肉"]), KB)
            self.assertEqual(a.risk, RISK_UNSAFE, name)

    def test_avoid_chicken_matches_poultry(self):
        a = assess_item({"name": "龙焰鸡腿堡"}, AllergenProfile(avoid=["鸡肉"]), KB)
        self.assertEqual(a.risk, RISK_UNSAFE)

    def test_avoid_beef_does_not_hit_milk_drink(self):
        # "纯牛奶"含"牛"，但不是牛肉，不能误伤
        a = assess_item({"name": "纯牛奶(盒装)"}, AllergenProfile(avoid=["牛肉"]), KB)
        self.assertNotEqual(a.risk, RISK_UNSAFE)

    def test_no_allergen_is_safe(self):
        a = assess_item({"name": "美式咖啡"}, AllergenProfile(allergens=["花生"]), KB)
        self.assertEqual(a.risk, RISK_SAFE)

    def test_plant_milk_is_not_dairy(self):
        # 燕麦奶含"奶"字但不含乳制品，不能误判
        self.assertNotIn("milk", KB.parse_allergen_keywords("热燕麦奶中杯"))
        self.assertNotIn("milk", KB.parse_allergen_keywords("植物奶"))
        self.assertIn("milk", KB.parse_allergen_keywords("热奶茶"))
        self.assertIn("milk", KB.parse_allergen_keywords("麦咖啡冰奶铁"))

    def test_removable_component_is_soft_hit(self):
        # 默认已加但可去掉 → 降级为"可去除后可选"，且给出去险动作
        a = assess_item(
            {
                "name": "小杯鲜萃咖啡",
                "required_components": [],
                "removable_components": ["奶油球"],
                "optional_components": ["白砂糖"],
                "removable": ["不加奶油球", "不加白砂糖（默认不加）"],
            },
            AllergenProfile(allergens=["乳制品"]),
            KB,
        )
        self.assertEqual(a.risk, RISK_CONDITIONAL)
        self.assertIn("奶油球", a.hits[0].mitigation)
        self.assertTrue(any("可要求不加" in r for r in a.reasons))

    def test_required_component_is_hard_hit(self):
        a = assess_item(
            {
                "name": "龙焰鸡腿堡",
                "required_components": ["辣椒黄油风味酱"],
                "removable": [],
            },
            AllergenProfile(allergens=["乳制品"]),
            KB,
        )
        self.assertEqual(a.risk, RISK_UNSAFE)
        self.assertFalse(a.hits[0].removable)


class TestBuildOrderPlan(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"name": "巨无霸", "category": "汉堡"},
            {"name": "薯条", "category": "小食"},
            {"name": "麦香鱼", "category": "汉堡"},
            {"name": "圆筒", "category": "甜品"},
        ]

    def test_plan_counts_and_ordering(self):
        plan = build_order_plan(self.items, AllergenProfile(allergens=["花生"]), KB)
        self.assertEqual(plan["summary"]["total"], 4)
        order = {"safe": 0, "conditional": 1, "caution": 2, "unsafe": 3}
        risks = [a["risk"] for a in plan["assessments"]]
        self.assertEqual(risks, sorted(risks, key=lambda r: order[r]))

    def test_combo_note_when_mixed(self):
        plan = build_order_plan(self.items, AllergenProfile(allergens=["乳制品"]), KB)
        self.assertTrue(plan["combo_note"])

    def test_profile_reflects_resolved_allergens(self):
        plan = build_order_plan(self.items, AllergenProfile(allergens=["乳制品"]), KB)
        self.assertEqual(plan["profile"]["allergens"], ["milk"])


class TestObservedVocabulary(unittest.TestCase):
    """MCP 实测底库作为"观察提示"接入。"""

    def test_observed_hits_are_advisory_not_hard(self):
        # "月下相思扭扭薯" 在实测底库里被观测到含小麦，但名字里没有小麦关键词
        a = assess_item({"name": "月下相思扭扭薯"}, AllergenProfile(allergens=["wheat"]), KB)
        observed = [h for h in a.may_contain if h.source == "mcp_observed"]
        self.assertTrue(observed, "应产生一条 MCP 实测观察提示")
        # 观察提示只降为"需确认"，不应直接判为不可选
        self.assertNotEqual(a.risk, RISK_UNSAFE)
        self.assertEqual(a.risk, RISK_CAUTION)

    def test_name_with_allergen_keyword_is_still_hard_hit(self):
        # 名字里直接含"芝士" → 硬命中，不受观察层影响
        a = assess_item({"name": "龙焰芝士棒鸡腿堡"}, AllergenProfile(allergens=["乳制品"]), KB)
        self.assertEqual(a.risk, RISK_UNSAFE)

    def test_observed_not_applicable_to_other_items(self):
        a = assess_item({"name": "可乐"}, AllergenProfile(allergens=["乳制品"]), KB)
        self.assertFalse([h for h in a.may_contain if h.source == "mcp_observed"])


class TestEndToEndOnRealMenu(unittest.TestCase):
    """用真实门店菜单跑完整分级。"""

    def test_real_menu_can_be_graded(self):
        items = parse_query_meals(_load("query-meals.json"))
        rows = [it.to_dict() for it in items]
        plan = build_order_plan(rows, AllergenProfile(allergens=["乳制品", "花生"]), KB)
        self.assertEqual(plan["summary"]["total"], len(rows))
        # 至少要能分出不同风险等级，说明分级在真实数据上有效
        self.assertGreater(plan["summary"]["total"], 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
