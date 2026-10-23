#!/usr/bin/env python3
"""安全地把麦当劳 MCP Token 写入 MiniMax Code 的 mcp.json。

Token 通过 getpass 读取：不会回显到屏幕，也不会进入 shell 历史或进程参数。
默认写入 ~/.minimax/mcp.json 的 mcd-mcp 条目 Authorization 头。

用法：
    python3 scripts/fill_mcd_token.py            # 交互输入并写入
    python3 scripts/fill_mcd_token.py --dry-run  # 只打印将要写入的内容，不落盘
    python3 scripts/fill_mcd_token.py --path /custom/mcp.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

SERVER_NAME = "mcd-mcp"
PLACEHOLDER = "REPLACE_WITH_YOUR_MCD_MCP_TOKEN"
DEFAULT_PATH = Path.home() / ".minimax" / "mcp.json"


def read_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到配置文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"配置文件不是合法 JSON：{exc}") from exc


def build_entry(token: str) -> dict:
    return {
        "type": "streamable-http",
        "url": "https://mcp.mcd.cn",
        "headers": {"Authorization": f"Bearer {token}"},
        "enabled": True,
        "description": "麦当劳中国 MCP Server：菜单/营养/门店/价格/优惠券/积分/点餐等 33 个业务工具",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="安全写入麦当劳 MCP Token")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="mcp.json 路径")
    parser.add_argument("--dry-run", action="store_true", help="只演示不写盘")
    args = parser.parse_args()

    path = Path(args.path).expanduser()
    config = read_config(path)

    servers = config.setdefault("mcpServers", {})
    current = servers.get(SERVER_NAME, {})
    if current.get("url") != "https://mcp.mcd.cn":
        print(f"提示：现有 {SERVER_NAME} 条目的 url 非官方地址，将被覆盖为 https://mcp.mcd.cn")

    token = getpass.getpass("请输入麦当劳 MCP Token（输入不回显）: ").strip()
    if not token:
        print("未输入 Token，已取消。", file=sys.stderr)
        return 1
    if token.startswith("Bearer "):
        token = token[len("Bearer ") :].strip()

    servers[SERVER_NAME] = build_entry(token)

    if args.dry_run:
        preview = json.loads(json.dumps(config))
        preview["mcpServers"][SERVER_NAME]["headers"]["Authorization"] = "Bearer ***已隐藏***"
        print("--- dry-run，将写入以下内容（Token 已隐藏）---")
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"✓ 已写入 {path}")
    print(f"✓ 旧配置备份在 {backup}")
    print("→ 重新开启一个会话，MCP 工具才会重新握手加载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
