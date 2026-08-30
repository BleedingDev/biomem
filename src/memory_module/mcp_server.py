"""Offline stdio MCP adapter for the running biomem daemon.

The adapter is intentionally stateless.  It owns no ``TextMemory`` instance
and never opens a ``.bdbm`` file; all tools use the loopback HTTP v1 service.
"""

import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

from . import __version__
from .local_daemon_client import DaemonError, LocalDaemonClient


Layer = Literal["both", "stm", "ltm"]
GraphLayer = Literal["stm", "ltm"]


def create_server(client: Optional[LocalDaemonClient] = None):
    """Build the six-tool MCP server, optionally against a test client."""

    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp_types import ToolAnnotations
        from pydantic import Field
        from typing import Annotated
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging smoke covers this path
        raise RuntimeError(
            "The biomem MCP adapter requires the 'mcp' package (version 2.1.1)."
        ) from exc

    daemon = client or LocalDaemonClient()
    process_session_id = f"mcp:{uuid.uuid4()}"

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {"daemon_client": daemon}
        finally:
            await daemon.aclose()

    server = MCPServer(
        name="biomem",
        title="biomem local memory",
        description="Offline adapter to the running local biomem daemon.",
        instructions=(
            "Use biomem_store only for durable facts the user intends to retain. "
            "All memory data remains on the local biomem daemon."
        ),
        version=str(__version__),
        lifespan=lifespan,
        log_level="ERROR",
    )

    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    store_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )

    async def invoke(command: str, **arguments: Any) -> dict[str, Any]:
        try:
            return await daemon.command(command, **arguments)
        except DaemonError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="biomem_status",
        title="biomem status",
        description="Check the identity, version, readiness, and status of the local biomem daemon.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def biomem_status() -> dict[str, Any]:
        try:
            health = await daemon.health()
        except DaemonError as exc:
            raise ToolError(str(exc)) from None
        return health.payload

    @server.tool(
        name="biomem_store",
        title="Store a biomem record",
        description="Store one durable key/value record in the shared local biomem state.",
        annotations=store_annotations,
        structured_output=True,
    )
    async def biomem_store(
        key: Annotated[str, Field(min_length=1, max_length=16384)],
        value: Annotated[str, Field(min_length=1, max_length=32768)],
    ) -> dict[str, Any]:
        return await invoke(
            "store_record",
            key=key,
            value=value,
            provenance={
                "source_class": "mcp",
                "origin": "local-mcp-stdio",
                "session_id": process_session_id,
            },
        )

    @server.tool(
        name="biomem_retrieve",
        title="Retrieve associated biomem records",
        description="Recall the strongest local memory associations for a query.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def biomem_retrieve(
        query: Annotated[str, Field(min_length=1, max_length=16384)],
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        return await invoke(
            "retrieve", query=query, top_k=top_k, session_id=process_session_id
        )

    @server.tool(
        name="biomem_search",
        title="Search biomem records",
        description="Search local records by semantic similarity without changing memory state.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def biomem_search(
        query: Annotated[str, Field(min_length=1, max_length=16384)],
        top_k: Annotated[int, Field(ge=1, le=50)] = 10,
        layer: Layer = "both",
    ) -> dict[str, Any]:
        return await invoke("search", query=query, top_k=top_k, layer=layer)

    @server.tool(
        name="biomem_list",
        title="List biomem records",
        description="List a bounded page of stable records from local memory.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def biomem_list(
        layer: Layer = "both",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: Annotated[Optional[str], Field(max_length=512)] = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"layer": layer, "limit": limit}
        if cursor is not None:
            arguments["cursor"] = cursor
        return await invoke("list_memories", **arguments)

    @server.tool(
        name="biomem_graph",
        title="Get the biomem graph",
        description="Return a bounded semantic graph projection from one local memory layer.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def biomem_graph(
        layer: GraphLayer = "ltm",
        threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6,
        max_nodes: Annotated[int, Field(ge=1, le=250)] = 100,
    ) -> dict[str, Any]:
        return await invoke(
            "get_memory_graph",
            layer=layer,
            threshold=threshold,
            max_nodes=max_nodes,
        )

    # MCPServer derives argument models from function signatures. Make the
    # contract closed explicitly: Pydantic's default is to ignore unknown
    # fields, which would contradict the advertised additionalProperties=false.
    for tool in server._tool_manager.list_tools():
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)

    return server


def main() -> None:
    """Run the adapter over stdio; stdout is reserved for MCP frames."""

    try:
        create_server().run(transport="stdio")
    except (RuntimeError, ValueError) as exc:
        print(f"biomem MCP startup error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
