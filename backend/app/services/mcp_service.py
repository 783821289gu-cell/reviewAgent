from dataclasses import dataclass
from inspect import Parameter, Signature
from ipaddress import ip_address

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from config import Settings
from tools.adapters import invoke_validated_tool
from tools.contracts import tool_contracts
from tools.registry import tool_registry


MCP_EXCLUDED_TOOL_NAMES = {
    "parse_document",
    "write_memory",
    "generate_report",
}


@dataclass(frozen=True)
class McpRuntime:
    server: FastMCP
    application: object


class LocalOnlyApplication:
    def __init__(self, application):
        self._application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _is_loopback_client(scope.get("client")):
            response = JSONResponse(
                {"detail": "MCP endpoint is available only from the local host."},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self._application(scope, receive, send)


def create_mcp_runtime(settings: Settings) -> McpRuntime | None:
    if not settings.mcp_enabled:
        return None
    if settings.host != "127.0.0.1":
        raise ValueError(
            "REVIEW_AGENT_MCP_ENABLED requires REVIEW_AGENT_HOST=127.0.0.1"
        )

    server = FastMCP(
        name="contract-review-agent",
        host="127.0.0.1",
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )
    for tool_name in tool_registry:
        if tool_name in MCP_EXCLUDED_TOOL_NAMES:
            continue
        server.add_tool(
            _mcp_callable(tool_name),
            name=tool_name,
            description=tool_contracts[tool_name].description,
            structured_output=True,
        )
    return McpRuntime(
        server=server,
        application=LocalOnlyApplication(server.streamable_http_app()),
    )


def exposed_mcp_tool_names() -> tuple[str, ...]:
    return tuple(
        tool_name
        for tool_name in tool_registry
        if tool_name not in MCP_EXCLUDED_TOOL_NAMES
    )


def _mcp_callable(tool_name: str):
    input_model = tool_contracts[tool_name].input_model
    output_model = tool_contracts[tool_name].output_model

    def call_tool(**kwargs):
        return invoke_validated_tool(tool_name, kwargs)

    call_tool.__name__ = tool_name
    call_tool.__doc__ = tool_contracts[tool_name].description
    call_tool.__annotations__ = {
        field_name: field.annotation
        for field_name, field in input_model.model_fields.items()
    }
    call_tool.__annotations__["return"] = output_model
    call_tool.__signature__ = Signature(
        parameters=[
            Parameter(
                field_name,
                kind=Parameter.KEYWORD_ONLY,
                default=(
                    Parameter.empty
                    if field.is_required()
                    else field.get_default(call_default_factory=True)
                ),
                annotation=field.annotation,
            )
            for field_name, field in input_model.model_fields.items()
        ],
        return_annotation=output_model,
    )
    return call_tool


def _is_loopback_client(client) -> bool:
    if not client or not client[0]:
        return False
    try:
        return ip_address(str(client[0])).is_loopback
    except ValueError:
        return False
