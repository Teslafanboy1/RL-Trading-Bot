"""One-time interactive Robinhood MCP OAuth login for the dashboard.

    python3 -m dashboard.rh_login          # opens the browser, saves tokens
    python3 -m dashboard.rh_login --check  # verify the saved token still works
    python3 -m dashboard.rh_login --clear  # forget the saved token
"""

import sys

from . import mcp_client


def main():
    if "--clear" in sys.argv:
        mcp_client.clear_tokens()
        print("Saved Robinhood MCP tokens cleared.")
        return
    if "--check" in sys.argv:
        try:
            s = mcp_client.MCPSession()
            s.initialize()
            print("✓ token valid — MCP session initialized.")
        except Exception as e:
            print(f"✗ direct MCP unavailable: {e}")
            print("  (The dashboard will fall back to the claude -p broker path.)")
            sys.exit(1)
        return
    try:
        mcp_client.login()
    except mcp_client.MCPError as e:
        print(f"\n✗ Direct OAuth to the Robinhood MCP failed: {e}")
        print("  This can happen if Robinhood restricts third-party OAuth clients.")
        print("  The dashboard automatically falls back to the claude -p broker "
              "path (proven, slower) — nothing else to configure.")
        sys.exit(1)


if __name__ == "__main__":
    main()
