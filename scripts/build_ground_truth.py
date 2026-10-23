#!/usr/bin/env python3
"""从麦当劳 MCP 抽取真实底库，生成 data/mcp_observed_vocabulary.json。

只读操作：不写入任何配置，仅产出知识库文件。
"""
import json, urllib.request, urllib.error, re, sys
from pathlib import Path

ROOT = Path("/Users/agiuser/Playground/261024/mcd-allergy-safe-advisor")
cfg = json.loads((Path.home()/".minimax"/"mcp.json").read_text(encoding="utf-8"))
srv = cfg["mcpServers"]["mcd-mcp"]
token = srv["headers"]["Authorization"].split("Bearer",1)[-1].strip()
URL = srv["url"]; _id=[0]

def rpc(method, params=None):
    _id[0]+=1
    body={"jsonrpc":"2.0","id":_id[0],"method":method}
    if params is not None: body["params"]=params
    req=urllib.request.Request(URL,data=json.dumps(body).encode(),method="POST")
    for k,v in [("Content-Type","application/json"),("Accept","application/json, text/event-stream"),("Authorization",f"Bearer {token}")]:
        req.add_header(k,v)
    try:
        with urllib.request.urlopen(req,timeout=60) as r: raw=r.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    p=None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try: p=json.loads(line[5:].strip()); break
            except Exception: pass
    if p is None:
        try: p=json.loads(raw)
        except Exception: p={"_raw":raw[:600]}
    return p

_DEC = json.JSONDecoder()

def call(name,args):
    res=rpc("tools/call",{"name":name,"arguments":args})
    raw="\n".join(c.get("text","") for c in res.get("result",{}).get("content",[]) if c.get("type")=="text")
    m=re.search(r"## Original Response\s*(\{.*)",raw,re.S)
    if m:
        try:                       # raw_decode：JSON 之后可能还有尾随文本
            obj,_=_DEC.raw_decode(m.group(1))
            return obj
        except Exception: pass
    try: return json.loads(raw)
    except Exception: return {"_raw":raw[:800]}

rpc("initialize",{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"builder","version":"1"}})
rpc("notifications/initialized")

STORE = "1950526"   # 麦当劳北京新东安二号餐厅

# 1) 营养表（真实品名 + 营养数值）
nut = call("list-nutrition-foods", {})
raw = nut.get("data") if isinstance(nut, dict) else ""
m = re.search(r"\[(\d+)\]\{([^}]*)\}:\n(.*)", raw or "", re.S)
nutrition = []
if m:
    cols = [c.strip() for c in m.group(2).split(",")]
    for line in m.group(3).strip().splitlines():
        parts = line.split(",")
        if len(parts) != len(cols): continue
        nutrition.append(dict(zip(cols, parts)))
print(f"营养表条目: {len(nutrition)}")

# 2) 菜单结构
meals = call("query-meals", {"storeCode":STORE,"orderType":1,"beType":1})
cats = meals.get("data",{}).get("categories",[]) if isinstance(meals,dict) else []
menu = []
for c in cats:
    items=[]
    for m2 in (c.get("meals") or []):
        items.append({"code":m2.get("code"), "tags":m2.get("tags") or []})
    menu.append({"category": (c.get("name") or "").replace("\n",""), "count":len(items), "items":items})
print(f"菜单分类: {len(menu)}，商品合计: {sum(c['count'] for c in menu)}")

# 3) 特制项词表（下钻若干代表性单品）
MOD_SAMPLES = ["9900000888","9900016075","9900008751","515946","9900008778","9900008700"]
component_vocab = set(); modification_vocab = {}   # valueName -> {code, selectedKey, unselectedKey}
for code in MOD_SAMPLES:
    d = call("query-meal-detail", {"storeCode":STORE,"orderType":1,"beType":1,"code":code})
    data = d.get("data") if isinstance(d,dict) else None
    if not isinstance(data,dict): continue
    for rd in (data.get("rounds") or []):
        for ch in (rd.get("choices") or []):
            if ch.get("name"): component_vocab.add(ch["name"])
            for it in ((ch.get("modification") or {}).get("items") or []):
                for v in (it.get("values") or []):
                    n=v.get("name")
                    if n:
                        modification_vocab.setdefault(n,{
                            "code": v.get("code"), "selectedKey": v.get("selectedKey"),
                            "unselectedKey": v.get("unselectedKey")})
    for it in ((data.get("modification") or {}).get("items") or []):
        for v in (it.get("values") or []):
            n=v.get("name")
            if n:
                modification_vocab.setdefault(n,{
                    "code": v.get("code"), "selectedKey": v.get("selectedKey"),
                    "unselectedKey": v.get("unselectedKey")})
print(f"配料词表: {len(component_vocab)}  特制项词表: {len(modification_vocab)}")

# 4) 过敏原信号词 → 命中真实配料名的映射
SIGNALS = {
 "milk":    ["吉士","芝士","奶","乳","奶油","黄油","麦旋风","新地","玉米杯","巧克力","冰浓浓","热浓浓"],
 "egg":     ["蛋","酱"],
 "wheat":   ["面包","麦旋风","奥利奥","麦满分","派","麦香鱼","鸡块","脆薯饼","扭扭薯","V翅","鸡球","饼干"],
 "soy":     ["巨无霸酱","酱","豆腐","豆"],
 "sesame":  ["芝麻"],
 "fish":    ["鱼"],
 "peanut":  ["花生"],
 "tree_nut":["坚果","马苏里拉"],
 "shellfish":["虾","蟹"],
}
hit = {}
for al, kws in SIGNALS.items():
    m2 = sorted({c for c in component_vocab for kw in kws if kw in c})
    if m2: hit[al]=m2
for al, m2 in hit.items():
    print(f"  {al:10} → {len(m2)} 个配料命中")

out = {
  "_generated_at": "2026-10-23",
  "_source": "麦当劳中国 MCP Server https://mcp.mcd.cn（真实调用抓取）",
  "_store_sampled": {"storeCode":STORE,"storeName":"麦当劳北京新东安二号餐厅"},
  "_note": "本文件为 MCP 实测抓取的客观底库，不含推断成分。过敏原判定需结合 data/allergen_matrix.json 的知识层。",
  "menu_categories": menu,
  "component_vocabulary": sorted(component_vocab),
  "modification_vocabulary": modification_vocab,
  "allergen_signal_hits": hit,
  "nutrition_table": nutrition,
}
(ROOT/"data"/"mcp_observed_vocabulary.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n✓ 已写入 {ROOT/'data'/'mcp_observed_vocabulary.json'}")
