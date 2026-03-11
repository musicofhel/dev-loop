# ADR-008: Python/uv Replaces TypeScript for Glue Code

## Status
Accepted

## Context
The original design specified TypeScript for MCP servers and glue code (`src/mcp/*/server.ts`). This requires:
- Node.js runtime + npm for dependency management
- TypeScript compilation step
- `@modelcontextprotocol/sdk` npm package

Python with uv offers:
- `fastmcp` — mature Python MCP framework with decorator-based tool definitions
- `uv` — fast dependency management, lockfile, `uv run` for scripts
- OpenTelemetry Python SDK is well-maintained
- Simpler subprocess integration for calling `br`, `git`, shell tools
- No compilation step

## Decision
Use Python (>=3.12) managed by uv for all glue code. MCP servers use `fastmcp`. Package layout: `src/devloop/<layer>/`.

## Consequences

### Positive
- Single `uv sync` installs everything (no npm + pip split)
- `fastmcp` decorator syntax is more concise than TypeScript MCP SDK
- Python subprocess calls to `br`, `git`, `dmux` are natural
- OTel Python SDK has first-class support
- `pyproject.toml` is the single source of truth for dependencies
- `uv run` replaces `npx` / `ts-node`

### Negative
- Python type checking is opt-in (not as strict as TypeScript)
- fastmcp is newer than the TypeScript MCP SDK
- Some team members may prefer TypeScript

### Mitigation
- Use `ruff` for linting + formatting (fast, strict)
- Type hints on all public functions
- `pyright` available as additional type checker if needed

## Package Layout
```
src/devloop/
├── __init__.py
├── cli.py                 # Entry point (just delegates)
├── intake/
│   ├── __init__.py
│   └── beads_poller.py    # Poll br ready --json
├── orchestration/
│   └── __init__.py
├── runtime/
│   └── __init__.py
├── gates/
│   └── __init__.py
├── observability/
│   └── __init__.py
└── feedback/
    └── __init__.py
```
