"""Official MCP adapter for the protocol-neutral ReviewService."""
from __future__ import annotations

import argparse
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from code_review_agent.service_core import ReviewService, create_review_service_from_env


def create_mcp(
    service: ReviewService,
    *,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    mcp = FastMCP(
        "code-review-agent",
        instructions=(
            "Queue code reviews only for repositories registered by the operator. "
            "Review tools are asynchronous: poll get_review_status."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=transport_security,
    )

    @mcp.tool()
    def review_diff(repository: str, diff: str) -> dict[str, Any]:
        """Queue a unified diff review against a registered owner/repo alias."""
        return service.submit_diff(repository, diff)

    @mcp.tool()
    def review_pr(repository: str, pull_request: str) -> dict[str, Any]:
        """Queue a GitHub pull request review against a registered repository."""
        job, duplicate = service.submit_pr(repository, pull_request)
        return {**job, "duplicate": duplicate}

    @mcp.tool()
    def get_review_status(review_id: str) -> dict[str, Any]:
        """Return current state and, when terminal, review result or error code."""
        return service.get(review_id)

    @mcp.resource("crag://reviews/{review_id}", mime_type="application/json")
    def review_resource(review_id: str) -> str:
        """Read a review job as service-schema JSON."""
        return json.dumps(service.get(review_id), ensure_ascii=False)

    @mcp.resource("crag://traces/{review_id}", mime_type="application/x-ndjson")
    def trace_resource(review_id: str) -> str:
        """Read the canonical, serialization-redacted JSONL trace for a terminal job."""
        return service.get_trace(review_id)

    @mcp.prompt(title="Review repository change")
    def review_change(
        repository: str,
        change: str,
        focus: str = "correctness",
    ) -> str:
        """Guide a client to queue and poll a review without widening authority."""
        return (
            f"Review {change!r} in registered repository {repository!r}, focusing on "
            f"{focus!r}. Use review_pr for a PR number or exact GitHub PR URL; "
            "otherwise use review_diff with a unified diff. Poll get_review_status "
            "until succeeded or failed. Do not request unregistered paths, commands, "
            "credentials, posting, approval, or repository mutation."
        )

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run code-review-agent as an MCP stdio server")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="MCP transport (HTTP is mounted by crag-service)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    build_parser().parse_args(argv)
    service = create_review_service_from_env()
    try:
        create_mcp(service).run(transport="stdio")
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
