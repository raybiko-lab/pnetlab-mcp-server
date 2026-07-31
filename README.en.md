# pnetlab-mcp-server

[![中文](https://img.shields.io/badge/README-中文-lightgrey.svg)](README.md) [![English](https://img.shields.io/badge/README-English-blue.svg)](README.en.md)

An [MCP](https://modelcontextprotocol.io) server that gives Claude (and other LLM
agents) programmatic control over **PNETLab v6** network labs - create topologies,
add nodes, wire them, push configs, boot them and read back state, all through
natural language.

It mirrors the tool surface of [`axiom-works-ai/eveng-mcp-server`](https://github.com/axiom-works-ai/eveng-mcp-server)
but talks to PNETLab **v6's** session-scoped API instead of the classic EVE-NG API
(which v6 removed).

> **⚠️ Generation & testing note**  
> The code in this project is entirely AI-generated. Basic functionality has been live-tested, but it has not gone through comprehensive coverage testing. You're encouraged to have an agent run its own validation pass against your PNETLab environment before relying on it. Issues welcome.

## Why this exists

PNETLab v6 (6.0.0+) is a Laravel rewrite that kept the old EVE-NG engine under
`/api/` but:

- **Deleted** the classic login (`/api/auth/login`), `/api/status`, `/api/labs/`,
  `/api/folders/`, `/api/users/`.
- **Moved** lab operations to a session-scoped API: `/api/labs/session/*`.
- **Replaced** login with a Laravel endpoint: `POST /store/public/auth/login/login`.

So `eveng-mcp-server` (which speaks the classic API) does **not** work against v6.
This server was reverse-engineered and live-verified against PNETLab 6.0.0-100.

## Install

```bash
pip install -e .
```

Provides the `pnetlab-mcp-server` command.

## Configure

Set env vars (the server logs in lazily on first tool call):

| Variable | Example | Purpose |
|---|---|---|
| `PNETLAB_HOST` | `http://192.168.231.128` | PNETLab v6 URL |
| `PNETLAB_USERNAME` | `mcp` | Account the agent works as (use a **dedicated** account) |
| `PNETLAB_PASSWORD` | `pnet` | Worker password |
| `PNETLAB_VIEWER_USERNAME` | `admin` | (optional) Account that watches in the browser |
| `PNETLAB_VIEWER_PASSWORD` | `pnet` | Viewer password |

**Why a dedicated worker account?** v6 allows one active lab session per user
account. If the agent and your browser both use `admin`, `open_lab` fails with
`20039 "sandbox already exists"`. Give the agent its own account (e.g. `mcp`,
admin role) so your browser is free.

**Watching live in the browser.** When `PNETLAB_VIEWER_*` is set, `open_lab`
automatically joins the viewer account to the agent's lab session - so the two
share one live topology. After the agent opens a lab, just log into the PNETLab
web UI as the viewer account and open the lab (or go to `/legacy/topology`):
you'll see the agent's topology, and refresh to see its changes. `join_viewer`
re-joins if you opened the browser first; `close_lab` also leaves the viewer.

### Claude Code (`.claude.json`)

```json
{
  "mcpServers": {
    "pnetlab": {
      "command": "pnetlab-mcp-server",
      "env": {
        "PNETLAB_HOST": "http://192.168.231.128",
        "PNETLAB_USERNAME": "mcp",
        "PNETLAB_PASSWORD": "pnet",
        "PNETLAB_VIEWER_USERNAME": "admin",
        "PNETLAB_VIEWER_PASSWORD": "pnet"
      }
    }
  }
}
```

Restart Claude Code after adding it.

## Tools

| Tool | What it does |
|---|---|
| `list_templates` | List installed node templates (keys are template ids usable in add_node) |
| `list_network_types` | List network types (bridge, pnet0..pnet9, ovs) |
| `open_lab(path)` | Open a lab by filename, e.g. `2pc_1sw.unl` (no leading slash); auto-joins viewer |
| `close_lab()` | Leave the current lab session (also leaves the viewer) |
| `join_viewer()` | (Re)join the viewer account to the agent's session so you can watch in the browser |
| `get_lab()` | Info + full topology (nodes/networks/connections) + node status |
| `get_node_status()` | Per-node running status (0=stopped, 1=building, 2=running) |
| `add_node(type, template, name, ...)` | Add a node; returns id + console port |
| `connect_nodes(src_id, src_if, dest_id, dest_if)` | P2P link between two node interfaces |
| `start_node(node_id?)` | Start one node, or all if id omitted |
| `stop_node(node_id?)` | Stop one node, or all if id omitted |
| `delete_node(node_id)` | Delete a node (stop it first) |
| `push_config(node_id, config)` | Push startup config (applied on next boot) |
| `node_console(node_id)` | Get a node's telnet/SSH host:port for CLI access |

## Important gotchas (v6-specific)

1. **One session per user.** v6 allows one active lab session per account. The
   agent must use a dedicated account (e.g. `mcp`), not the one you browse with.
   Configure `PNETLAB_VIEWER_*` so `open_lab` auto-joins your browsing account to
   the agent's session - then both share one live topology (see Configure above).
2. **All `/api/labs/session/*` calls use JSON bodies.** Form-encoded bodies are
   silently dropped and show up as `40000 "missing required fields"`.
3. **`open_lab` path has no leading slash** - `"2pc_1sw.unl"`, not `"/2pc_1sw.unl"`.
4. `open_lab` creates/reuses a sandbox file (`labs<name>.unl`) on the server; the
   sandbox is empty on first open. `close_lab` releases the session binding but
   keeps the sandbox file (reopening is fine).
5. To delete a node you must `stop_node` it first.
6. `start_node()`/`stop_node()` with no id iterate the lab's node ids (the API's
   null-id "all" path is unreliable).

## Architecture

```
LLM agent  ──MCP/stdio──►  pnetlab-mcp-server  ──HTTP/JSON──►  PNETLab v6
                              (this repo)                         /store/public/auth/login/login  (login)
                                                                  /api/labs/session/*             (lab ops)
                                                                  /api/list/templates|networks    (listings)
```

`client.py` is the verified v6 API client; `server.py` wraps it as MCP tools.
