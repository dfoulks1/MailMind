"""
Mailmind MCP Server — main entry point.

Registers all Gmail and RAG tools with the MCP SDK and starts the server
in either stdio (Claude Desktop default) or SSE (HTTP) transport mode.

Usage (stdio, for Claude Desktop):
    python -m mailmind_mcp.server

Usage (SSE / HTTP):
    MCP_TRANSPORT=sse python -m mailmind_mcp.server
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

from .config import get_settings
from .tools import gmail as gmail_tools
from .tools import search as search_tools

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.mcp.log_level, logging.INFO),
    format="%(message)s",
    stream=sys.stderr,
)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.mcp.log_level, logging.INFO)
    ),
)
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Maps tool_name → (callable, input_schema_dict)
TOOL_REGISTRY: dict[str, tuple[Any, dict[str, Any]]] = {
    # Gmail tools
    "search_gmail": (
        gmail_tools.search_gmail,
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": 'Gmail search query, e.g. "from:boss@example.com is:unread"',
                },
                "max_results": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of results (1–500).",
                },
                "page_token": {
                    "type": "string",
                    "description": "Pagination token from a previous search.",
                },
            },
            "required": ["query"],
        },
    ),
    "get_email": (
        gmail_tools.get_email,
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
                "include_body": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include decoded plain-text body.",
                },
            },
            "required": ["message_id"],
        },
    ),
    "get_email_headers": (
        gmail_tools.get_email_headers,
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
            },
            "required": ["message_id"],
        },
    ),
    "list_labels": (
        gmail_tools.list_labels,
        {"type": "object", "properties": {}, "required": []},
    ),
    "create_label": (
        gmail_tools.create_label,
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name for the new label."},
            },
            "required": ["name"],
        },
    ),
    "add_label": (
        gmail_tools.add_label,
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "label_id": {
                    "type": "string",
                    "description": "Gmail label ID. Use list_labels to find IDs.",
                },
            },
            "required": ["message_id", "label_id"],
        },
    ),
    "remove_label": (
        gmail_tools.remove_label,
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "label_id": {"type": "string"},
            },
            "required": ["message_id", "label_id"],
        },
    ),
    "mark_read": (
        gmail_tools.mark_read,
        {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    ),
    "mark_unread": (
        gmail_tools.mark_unread,
        {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    ),
    "trash_email": (
        gmail_tools.trash_email,
        {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    ),
    "delete_email": (
        gmail_tools.delete_email,
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to permanently delete.",
                },
            },
            "required": ["message_id"],
        },
    ),
    "ingest_emails": (
        gmail_tools.ingest_emails,
        {
            "type": "object",
            "properties": {
                "max_emails": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max emails to ingest in this batch.",
                },
            },
            "required": [],
        },
    ),
    # Search / RAG tools
    "search_emails": (
        search_tools.search_emails,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword search terms."},
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum results to return.",
                },
            },
            "required": ["query"],
        },
    ),
    "summarize_email": (
        search_tools.summarize_email,
        {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Look up this Gmail ID in the local cache.",
                },
                "body": {
                    "type": "string",
                    "description": "Raw email body text (alternative to message_id).",
                },
                "subject": {"type": "string", "description": "Optional subject line."},
            },
            "required": [],
        },
    ),
    "ask_emails": (
        search_tools.ask_emails,
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to answer."},
                "search_query": {
                    "type": "string",
                    "description": "Optional cache search override.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "Emails to use as context.",
                },
            },
            "required": ["question"],
        },
    ),
    "refresh_rag": (
        search_tools.refresh_rag,
        {"type": "object", "properties": {}, "required": []},
    ),
    "cache_stats": (
        search_tools.cache_stats,
        {"type": "object", "properties": {}, "required": []},
    ),
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("mailmind-mcp")


@server.list_tools()
async def handle_list_tools(request: ListToolsRequest) -> ListToolsResult:
    """Return all registered tools with their schemas."""
    tools = [
        Tool(
            name=name,
            description=(fn.__doc__ or "").strip().splitlines()[0],
            inputSchema=schema,
        )
        for name, (fn, schema) in TOOL_REGISTRY.items()
    ]
    return ListToolsResult(tools=tools)


@server.call_tool()
async def handle_call_tool(request: CallToolRequest) -> CallToolResult:
    """Dispatch a tool call and return the JSON-encoded result."""
    name = request.params.name
    args: dict[str, Any] = request.params.arguments or {}

    if name not in TOOL_REGISTRY:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Unknown tool: {name}")],
            isError=True,
        )

    fn, _ = TOOL_REGISTRY[name]
    try:
        result = fn(**args)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, default=str, indent=2))],
            isError=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_error", tool=name, error=str(exc))
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": str(exc)}))],
            isError=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run_stdio() -> None:
    """Run the server over stdio transport (Claude Desktop / MCP CLI)."""
    log.info("mailmind_mcp_starting", transport="stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mailmind-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


async def _run_sse() -> None:
    """Run the server over SSE/HTTP transport."""
    from mcp.server.sse import SseServerTransport  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.routing import Mount, Route  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415

    transport = SseServerTransport("/messages")

    async def handle_sse(scope: Any, receive: Any, send: Any) -> None:
        async with transport.connect_sse(scope, receive, send) as streams:
            await server.run(
                streams[0],
                streams[1],
                InitializationOptions(
                    server_name="mailmind-mcp",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=None,
                        experimental_capabilities={},
                    ),
                ),
            )

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages", app=transport.handle_post_message),
        ]
    )
    log.info(
        "mailmind_mcp_starting",
        transport="sse",
        host=settings.mcp.host,
        port=settings.mcp.port,
    )
    await uvicorn.Server(
        uvicorn.Config(
            starlette_app,
            host=settings.mcp.host,
            port=settings.mcp.port,
            log_level=settings.mcp.log_level.lower(),
        )
    ).serve()


def main() -> None:
    """Select transport from config and start the server."""
    import asyncio

    transport = settings.mcp.transport
    if transport == "sse":
        asyncio.run(_run_sse())
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
