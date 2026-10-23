"""麦麦安心点单官 · 麦当劳过敏原安全顾问。"""

from .knowledge import (
    AllergenProfile,
    Assessment,
    KnowledgeBase,
    assess_item,
    build_order_plan,
)
from .mcp_client import McdMcpClient, McpError, create_client

__all__ = [
    "AllergenProfile",
    "Assessment",
    "KnowledgeBase",
    "McdMcpClient",
    "McpError",
    "assess_item",
    "build_order_plan",
    "create_client",
]

__version__ = "1.0.0"
