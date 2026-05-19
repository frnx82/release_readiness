import asyncio
import json
import httpx

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

CONFLUENCE_MCP_URL = "https://abc.com/mcp/confluence/"
CONFLUENCE_EMAIL = "<user_email>"
CONFLUENCE_PAT = "<user_pat>"


def build_httpx_client_factory(auth_headers: dict[str, str]):
    def factory(**kwargs):
        kwargs.pop("verify", None)
        incoming = kwargs.pop("headers", {}) or {}
        merged = {**incoming, **auth_headers}
        # For production, prefer verify=True with proper cert chain
        return httpx.AsyncClient(headers=merged, verify=False, **kwargs)
    return factory


def extract_tool_payload(result):
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
    return {}


async def call_tool(mcp: Client, tool_name: str, args: dict):
    raw = await mcp.call_tool(tool_name, args)
    return extract_tool_payload(raw)


async def main():
    headers = {
        "Authorization": f"Bearer {CONFLUENCE_PAT}",
        "X-Confluence-User-Email": CONFLUENCE_EMAIL,
    }

    transport = StreamableHttpTransport(
        CONFLUENCE_MCP_URL,
        httpx_client_factory=build_httpx_client_factory(headers),
    )

    async with Client(transport) as mcp:
        try:
            tools = await mcp.list_tools()
            tool_names = [t.name for t in tools]
            print("Available tools:", tool_names)

            if "confluence_validate_auth" not in tool_names:
                raise RuntimeError("confluence_validate_auth tool not found on this MCP endpoint.")

            auth_result = await call_tool(mcp, "confluence_validate_auth", {})
            if not auth_result.get("success"):
                raise RuntimeError(f"Auth failed: {auth_result.get('message')}")
        except Exception as exc:
            raise RuntimeError(
                "Failed to connect/authenticate with Confluence MCP server. "
                "Check URL, headers, email/PAT, and TLS/proxy settings."
            ) from exc

        # ── Search Examples ──────────────────────────────────────────────

        # 1. Search ALL spaces (original)
        print("\n═══ 1. Search all spaces for 'runbook' ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": 'type=page AND title ~ "runbook"', "limit": 10},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 2. Search within a SPECIFIC SPACE
        #    Replace "DEV" with your actual space key (e.g., "DEVOPS", "PLATFORM", "SRE")
        SPACE_KEY = "DEV"
        print(f"\n═══ 2. Search space '{SPACE_KEY}' for 'runbook' ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": f'type=page AND space="{SPACE_KEY}" AND title ~ "runbook"', "limit": 10},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 3. List ALL pages in a specific space
        print(f"\n═══ 3. List all pages in space '{SPACE_KEY}' ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": f'type=page AND space="{SPACE_KEY}" ORDER BY lastModified DESC', "limit": 20},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 4. Search by LABEL within a space
        print(f"\n═══ 4. Pages with label 'deployment' in space '{SPACE_KEY}' ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": f'type=page AND space="{SPACE_KEY}" AND label="deployment"', "limit": 10},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 5. Recently modified pages in a space (last 7 days)
        print(f"\n═══ 5. Recently modified pages in space '{SPACE_KEY}' ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": f'type=page AND space="{SPACE_KEY}" AND lastModified >= now("-7d") ORDER BY lastModified DESC', "limit": 10},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 6. Full-text search across MULTIPLE spaces
        print("\n═══ 6. Search across multiple spaces ═══")
        try:
            result = await call_tool(
                mcp,
                "confluence_search",
                {"cql": 'type=page AND (space="DEV" OR space="SRE" OR space="PLATFORM") AND text ~ "kubernetes"', "limit": 10},
            )
            print(json.dumps(result, indent=2)[:500])
        except Exception as exc:
            print(f"  Error: {exc}")

        # 7. Search by ancestor (pages under a specific parent page)
        # Uncomment and replace PARENT_PAGE_ID with the actual page ID
        # print("\n═══ 7. Pages under a parent page ═══")
        # result = await call_tool(
        #     mcp,
        #     "confluence_search",
        #     {"cql": 'type=page AND ancestor=123456789', "limit": 10},
        # )


if __name__ == "__main__":
    asyncio.run(main())  # use `await main()` if you are using Jupyter notebook etc.