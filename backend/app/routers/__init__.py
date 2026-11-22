"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import backfill, resource, cross_module

# backfill 固定路径放在 resource 的 /api/{module} 动态路由之前注册
ROUTERS = [backfill, resource, cross_module]
