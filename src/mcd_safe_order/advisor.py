"""编排层：调用麦当劳 MCP 并产出过敏原安全方案。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .knowledge import AllergenProfile, KnowledgeBase, build_order_plan
from .mcp_client import McdMcpClient
from .menu import (
    MenuItem,
    is_non_food,
    is_time_limited,
    looks_like_selector,
    nutrition_index,
    parse_meal_detail,
    parse_nutrition,
    parse_query_meals,
)

STORE_CODE_KEYS = ("storeCode", "store_code", "shopCode")
BE_CODE_KEYS = ("beCode", "be_code")
STORE_NAME_KEYS = ("storeName", "store_name", "shopName", "name", "restaurantName")
ADDRESS_KEYS = ("address", "addr", "addressDetail", "detailAddress")

CACHE_VERSION = "v4"  # 解析结构变更时递增，自动隔离旧缓存

DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "MCD_SAFE_ORDER_CACHE",
        str(Path.home() / ".cache" / f"mcd-safe-order-{CACHE_VERSION}"),
    )
)


def _first_str(d: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def _extract_stores(payload: Any) -> list[dict[str, Any]]:
    """从 query-nearby-stores 返回体里提取门店列表（含 storeCode / beCode）。"""
    stores: dict[str, dict[str, Any]] = {}

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            code = _first_str(node, STORE_CODE_KEYS)
            if code:
                key = code.strip()
                if key not in stores:
                    stores[key] = {
                        "storeCode": key,
                        "beCode": _first_str(node, BE_CODE_KEYS),
                        "name": _first_str(node, STORE_NAME_KEYS),
                        "address": _first_str(node, ADDRESS_KEYS),
                    }
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    _walk(payload)
    return list(stores.values())


class DetailCache:
    """query-meal-detail 的本地缓存（门店菜单变化不频繁，缓存能省大量调用）。"""

    def __init__(self, cache_dir: Path | None = None, ttl: int = 6 * 3600) -> None:
        self.dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.ttl = ttl
        self.hits = 0
        self.misses = 0

    def _path(self, store_code: str, code: str) -> Path:
        key = hashlib.sha1(f"{store_code}:{code}".encode()).hexdigest()[:16]
        return self.dir / f"{key}.json"

    def get(self, store_code: str, code: str) -> dict[str, Any] | None:
        path = self._path(store_code, code)
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                return None
            with open(path, "r", encoding="utf-8") as fh:
                self.hits += 1
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, store_code: str, code: str, value: dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(store_code, code)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False)
        except OSError:
            pass


class McDonaldAdvisor:
    """把麦当劳 MCP 的实时数据与过敏原知识库组合成可执行建议。"""

    def __init__(
        self,
        client: McdMcpClient,
        kb: KnowledgeBase | None = None,
        cache: DetailCache | None = None,
    ) -> None:
        self.client = client
        self.kb = kb or KnowledgeBase.load()
        self.cache = cache if cache is not None else DetailCache()

    # -- MCP 取数 --------------------------------------------------------
    def probe(self) -> list[dict[str, Any]]:
        """列出 MCP 暴露的全部工具及其参数。"""
        tools = self.client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description[:200],
                "required": t.required,
                "schema_keys": sorted((t.input_schema or {}).get("properties", {}).keys()),
            }
            for t in tools
        ]

    def fetch_stores(
        self,
        city: str | None = None,
        keyword: str | None = None,
        search_type: int = 1,
        be_type: int = 1,
    ) -> list[dict[str, Any]]:
        """查询门店，返回含 storeCode / beCode 的门店列表。"""
        args: dict[str, Any] = {"beType": be_type, "searchType": search_type}
        if city:
            args["city"] = city
        if keyword:
            args["keyword"] = keyword
        payload = self.client.call_tool("query-nearby-stores", args).as_json()
        return _extract_stores(payload)

    def fetch_menu(
        self,
        store_code: str,
        order_type: int = 1,
        be_type: int = 1,
        be_code: str | None = None,
        reservation_date: str | None = None,
        with_details: bool = True,
        detail_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """调用 query-meals + query-meal-detail 拉取门店菜单。

        query-meals 只返回分类、餐品编码与价格表；餐品名、套餐组成与可去除配料
        来自 query-meal-detail，因此必须二次调用。
        """
        args: dict[str, Any] = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
        }
        if be_code:
            args["beCode"] = be_code
        if reservation_date:
            args["reservationDate"] = reservation_date

        payload = self.client.call_tool("query-meals", args).as_json()
        items = parse_query_meals(payload)

        if with_details:
            targets = items if detail_limit is None else items[:detail_limit]
            for item in targets:
                detail = self._detail_for(item.code, store_code, order_type, be_type, be_code)
                if detail:
                    item.name = detail.get("name") or item.name
                    item.rounds = detail.get("rounds") or []
                    item.choice_codes = detail.get("choice_codes") or []
                    item.required_components = detail.get("required_components") or []
                    item.removable_components = detail.get("removable_components") or []
                    item.optional_components = detail.get("optional_components") or []
                    item.removable = detail.get("removable") or []
                    item.support_modify = detail.get("support_modify", False)
                    item.image = detail.get("image") or ""

            # 套餐子项经常引用"未在分类里单列"的餐品（编码可拿到），
            # 顺着 code 再拉一次 detail，把它们纳入同一张菜单参与解析。
            main_codes = {it.code for it in items}
            referenced: list[str] = []
            for it in items:
                for code in it.choice_codes:
                    if code and code not in main_codes and code not in referenced:
                        referenced.append(code)

            for code in referenced:
                if detail_limit is not None and len(items) >= detail_limit:
                    break
                detail = self._detail_for(code, store_code, order_type, be_type, be_code)
                if not detail or not detail.get("name"):
                    continue
                items.append(
                    MenuItem(
                        code=code,
                        name=detail.get("name", ""),
                        category="套餐子项",
                        rounds=detail.get("rounds") or [],
                        choice_codes=detail.get("choice_codes") or [],
                        required_components=detail.get("required_components") or [],
                        removable_components=detail.get("removable_components") or [],
                        optional_components=detail.get("optional_components") or [],
                        removable=detail.get("removable") or [],
                        support_modify=detail.get("support_modify", False),
                        image=detail.get("image") or "",
                    )
                )

        result = [it.to_dict() for it in items]
        for it in result:
            matrix = self.kb.lookup(it["name"])
            if matrix and not it.get("category"):
                it["category"] = matrix.get("category", "")
            it["is_selector"] = looks_like_selector(it.get("name", ""))
            it["is_non_food"] = is_non_food(it.get("name", ""))
            it["time_limited"] = is_time_limited(it.get("tags", []))
        return result

    def _detail_for(
        self,
        code: str,
        store_code: str,
        order_type: int,
        be_type: int,
        be_code: str | None,
    ) -> dict[str, Any]:
        cached = self.cache.get(store_code, code)
        if cached is not None:
            return cached
        try:
            raw = self.client.call_tool(
                "query-meal-detail",
                {
                    "storeCode": store_code,
                    "orderType": order_type,
                    "beType": be_type,
                    "code": code,
                    **({"beCode": be_code} if be_code else {}),
                },
            ).as_json()
        except Exception:
            return {}
        detail = parse_meal_detail(raw)
        self.cache.set(store_code, code, detail)
        return detail

    def fetch_nutrition(self) -> list[dict[str, Any]]:
        """调用 list-nutrition-foods 获取营养数据。"""
        payload = self.client.call_tool("list-nutrition-foods", {}).as_json()
        return parse_nutrition(payload)

    def calculate_price(
        self,
        store_code: str,
        items: list[dict[str, Any]],
        order_type: int = 1,
        be_type: int = 1,
        be_code: str | None = None,
    ) -> Any:
        """调用 calculate-price 核对价格。"""
        args: dict[str, Any] = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": items,
        }
        if be_code:
            args["beCode"] = be_code
        return self.client.call_tool("calculate-price", args).as_json()

    # -- 组合 ------------------------------------------------------------
    def advise(
        self,
        profile: AllergenProfile,
        store_code: str,
        order_type: int = 1,
        be_type: int = 1,
        be_code: str | None = None,
        items: list[dict[str, Any]] | None = None,
        with_nutrition: bool = True,
        detail_limit: int | None = None,
    ) -> dict[str, Any]:
        """核心流程：取菜单 → 合并营养 → 风险分级 → 出方案。"""
        if items is None:
            items = self.fetch_menu(
                store_code=store_code,
                order_type=order_type,
                be_type=be_type,
                be_code=be_code,
                detail_limit=detail_limit,
            )

        nutrition: dict[str, dict[str, Any]] = {}
        if with_nutrition:
            try:
                nutrition = nutrition_index(self.fetch_nutrition())
            except Exception:
                nutrition = {}

        enriched: list[dict[str, Any]] = []
        for it in items:
            row = dict(it)
            nut = nutrition.get(str(it.get("name", "")).strip())
            if nut:
                row["nutrition"] = {
                    k: v for k, v in nut.items() if k != "nutritionDescription"
                }
            enriched.append(row)

        plan = build_order_plan(
            enriched,
            profile,
            self.kb,
            menu_provenance={
                "source": "mcd_mcp",
                "tool": "query-meals + query-meal-detail",
                "store_code": store_code,
                "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "menu_item_count": len(items),
                "note": "本方案只覆盖该门店此刻在售的餐品；静态知识库仅提供过敏原语义，不提供可点菜品。",
            },
        )
        plan["store_code"] = store_code
        plan["menu_count"] = len(items)
        plan["nutrition_merged"] = len(nutrition)
        return plan
