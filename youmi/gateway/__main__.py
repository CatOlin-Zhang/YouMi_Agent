"""
网关服务入口（``python -m youmi.gateway``）

用法::

    python -m youmi.gateway                          # 127.0.0.1:8000
    python -m youmi.gateway --host 0.0.0.0 --port 8080 --workers 4
    YOUMI_AUTH_TOKENS="tok1:admin:ops:t1;tok2:agent::t2" python -m youmi.gateway

说明：
- MasterAgent 按租户懒装配（首次收到该租户任务时初始化），
  装配失败不影响服务启动
- 认证 / 审计沿用全局环境变量约定（``YOUMI_AUTH_*`` / ``YOUMI_AUDIT_LOG``）
- 需要可选依赖：``pip install -e .[gateway]``（fastapi + uvicorn）
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from youmi.gateway.api import create_app
from youmi.gateway.executor import MasterTaskExecutor
from youmi.gateway.service import GatewayService

logger = logging.getLogger("youmi.gateway")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="youmi.gateway",
        description="YouMi 网关服务（FastAPI + asyncio Worker 池）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument(
        "--workers", type=int, default=2,
        help="Worker 池大小（默认 2；单 MasterAgent 实例内串行执行）",
    )
    parser.add_argument(
        "--agent-name", default="master",
        help="MasterAgent 配置目录名（youmi/agents/<name>，默认 master）",
    )
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    return parser.parse_args(argv)


def _build_service(args: argparse.Namespace) -> GatewayService:
    """按参数构建网关服务（MasterAgent 按租户懒装配）"""

    async def factory(tenant: str) -> Any:
        # 延迟导入：--help 等轻量场景不加载完整 Agent 链
        from youmi.coordinator.master import MasterAgent

        master = MasterAgent.from_config_dir(args.agent_name)
        await master.initialize()
        logger.info("网关: 租户 '%s' MasterAgent 已就绪 (%s)", tenant, master.name)
        return master

    executor = MasterTaskExecutor(factory=factory)
    return GatewayService(executor, size=args.workers)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        import uvicorn
    except ImportError:
        print(
            "缺少网关依赖（fastapi / uvicorn），请安装: pip install -e .[gateway]",
            file=sys.stderr,
        )
        return 2

    service = _build_service(args)
    app = create_app(service)
    logger.info(
        "YouMi 网关启动: http://%s:%d (workers=%d, agent=%s)",
        args.host, args.port, args.workers, args.agent_name,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
