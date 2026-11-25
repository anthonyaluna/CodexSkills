# Figma MCP config reference

Use this snippet to register the Figma MCP server in `~/.codex/config.toml` as a streamable HTTP server with bearer auth pulled from your env.

```toml
[mcp_servers.figma]
url = "https://mcp.figma.com/mcp"
bearer_token_env_var = "FIGMA_OAUTH_TOKEN"
http_headers = { "X-Figma-Region" = "us-east-1" }
```

## Notes and options
- The bearer token must be available as `FIGMA_OAUTH_TOKEN` in the environment that launches Codex.
- Keep the region header aligned with your Figma region. If your org uses another region, update `X-Figma-Region` consistently.
- OAuth on streamable HTTP requires the RMCP client: set `[features].rmcp_client = true` (or `experimental_use_rmcp_client = true` on older builds) at the top level of `config.toml`.
- Optional per-server timeouts: `startup_timeout_sec` (default 10) and `tool_timeout_sec` (default 60) can be set inside `[mcp_servers.figma]` if needed.

## Env var setup (if missing)
- Supply `FIGMA_OAUTH_TOKEN` through your secret manager to the shell or IDE that starts Codex.
- Run `test "${FIGMA_OAUTH_TOKEN:+set}" = set` in that environment. Exit status 0 means the token is present.

## Setup + verification checklist
- Add the snippet above to `~/.codex/config.toml` under `[mcp_servers.figma]`, and enable `[features].rmcp_client = true` (or `experimental_use_rmcp_client = true` on older releases).
- Restart Codex (CLI/IDE) after updating config and env vars.
- Ask Codex to list Figma tools or run a simple call to confirm the server is reachable.

## Troubleshooting
- If Codex cannot pick up the token, restart it from the environment that received `FIGMA_OAUTH_TOKEN`.
- OAuth errors: Verify `rmcp_client` is enabled and the bearer token is valid. Tokens copied from Figma should not include surrounding quotes.
- Network/headers: Keep the `X-Figma-Region` header; if your org uses another region, update the header consistently across config and requests.

## Usage reminders
- The server is link-based: copy the Figma frame or layer link, then ask the MCP client to implement that URL. The client will extract the node ID from the link (it does not browse the page).
- If output feels generic, restate the project-specific rules from the main skill and ensure you follow the required flow (get_design_context → get_metadata if needed → get_screenshot).
