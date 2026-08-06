"""
工具定义与注册表

ToolDefinition — 工具声明 (name / description / parameters schema)
ToolRegistry   — 工具注册表，管理工具定义 + 绑定执行函数

用法::

    from youmi.core.tool import ToolDefinition, ToolRegistry

    # 定义工具
    def get_weather(city: str, unit: str = "celsius") -> str:
        return f"{city} 的天气是 25°{unit[0].upper()}"

    tool = ToolDefinition.from_function(
        func=get_weather,
        description="获取指定城市的天气",
    )

    # 注册
    registry = ToolRegistry()
    registry.register(tool, handler=get_weather)

    # 生成 OpenAI tools 格式
    tools_schema = registry.to_openai_tools()

    # 执行
    result = await registry.execute("get_weather", {"city": "北京"})
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Callable, Awaitable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

class ToolParameter(BaseModel):
    """工具参数描述"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    enum: list[str] | None = None
    default: Any = None


class ToolDefinition(BaseModel):
    """工具声明 — 描述一个可被 LLM 调用的工具

    包含名称、描述、参数 schema，用于:
    1. 生成 OpenAI function calling 的 tools 格式
    2. 在 ToolRegistry 中查找和校验
    """

    name: str = Field(description="工具名称 (唯一标识)")
    description: str = Field(default="", description="工具功能描述")
    parameters: list[ToolParameter] = Field(default_factory=list)

    def to_openai_function_schema(self) -> dict[str, Any]:
        """生成 OpenAI function calling 的 tool 定义"""
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    @classmethod
    def from_function(
        cls,
        func: Callable,
        name: str = "",
        description: str = "",
        param_descriptions: dict[str, str] | None = None,
    ) -> "ToolDefinition":
        """从 Python 函数自动生成 ToolDefinition

        自动提取函数名、docstring、参数类型和默认值。

        Args:
            func: Python 函数 (同步或异步)
            name: 工具名称 (默认使用函数名)
            description: 工具描述 (默认使用 docstring 第一行)
            param_descriptions: 参数描述映射 {参数名: 描述}
        """
        sig = inspect.signature(func)
        tool_name = name or func.__name__

        # 提取描述: 优先使用传入，否则取 docstring 第一行
        if not description:
            doc = inspect.getdoc(func) or ""
            description = doc.split("\n")[0].strip()

        param_descs = param_descriptions or {}
        parameters: list[ToolParameter] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            # 类型映射
            annotation = param.annotation
            py_type = "string"
            if annotation is not inspect.Parameter.empty:
                py_type = _python_type_to_json(annotation)

            # 必填/可选
            has_default = param.default is not inspect.Parameter.empty
            is_required = not has_default

            parameters.append(ToolParameter(
                name=param_name,
                type=py_type,
                description=param_descs.get(param_name, ""),
                required=is_required,
                default=param.default if has_default else None,
            ))

        return cls(
            name=tool_name,
            description=description,
            parameters=parameters,
        )


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

# handler 类型: 同步函数或异步函数
ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]


class ToolRegistry:
    """工具注册表

    管理工具定义 (ToolDefinition) 和执行函数 (handler) 的映射。
    提供:
    - register() — 注册工具
    - execute() — 执行工具调用
    - to_openai_tools() — 生成 OpenAI tools schema 列表
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    @property
    def tool_names(self) -> list[str]:
        return list(self._definitions.keys())

    def register(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
    ) -> None:
        """注册一个工具

        Args:
            definition: 工具定义
            handler: 执行函数 (同步或异步)
        """
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler
        logger.debug("Registered tool: %s", definition.name)

    def register_function(
        self,
        func: ToolHandler,
        name: str = "",
        description: str = "",
        param_descriptions: dict[str, str] | None = None,
    ) -> None:
        """快捷注册: 从函数自动生成定义并注册

        等价于::

            defn = ToolDefinition.from_function(func, name, description)
            registry.register(defn, handler=func)
        """
        defn = ToolDefinition.from_function(
            func,
            name=name,
            description=description,
            param_descriptions=param_descriptions,
        )
        self.register(defn, handler=func)

    def unregister(self, name: str) -> None:
        """注销工具"""
        self._definitions.pop(name, None)
        self._handlers.pop(name, None)

    def get_definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """生成 OpenAI tools 格式的 schema 列表"""
        return [
            defn.to_openai_function_schema()
            for defn in self._definitions.values()
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行工具调用

        Args:
            name: 工具名称
            arguments: 参数 (已解析的 dict)

        Returns:
            工具执行结果

        Raises:
            KeyError: 工具未注册
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"工具 '{name}' 未注册")

        logger.debug("Executing tool '%s' with args: %s", name, arguments)

        if asyncio.iscoroutinefunction(handler):
            result = await handler(**arguments)
        else:
            result = handler(**arguments)

        return result

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, name: str) -> bool:
        return name in self._definitions

    def __repr__(self) -> str:
        return f"<ToolRegistry tools={self.tool_names}>"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _python_type_to_json(annotation: Any) -> str:
    """Python 类型注解 → JSON Schema 类型"""
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    # 处理 typing 泛型 (list[str] → "array")
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        return type_map.get(origin, "string")
    return type_map.get(annotation, "string")
