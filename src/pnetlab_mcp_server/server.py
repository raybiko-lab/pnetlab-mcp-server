"""MCP server exposing PNETLab v6 lab control to LLM agents.

Mirrors the tool surface of axiom-works-ai/eveng-mcp-server but talks to
PNETLab v6's session-scoped API (see client.py). The server keeps one
authenticated session alive for its lifetime, so a lab opened with
``open_lab`` stays bound for subsequent ``add_node`` / ``start_node`` /
``get_lab`` calls until ``close_lab`` is called.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastmcp import FastMCP

from .client import PNETLabClient, PNETLabError

mcp = FastMCP("pnetlab")

_client: PNETLabClient | None = None


def _c() -> PNETLabClient:
    """Lazily build (and login) the singleton client from env vars."""
    global _client
    if _client is None:
        host = os.environ.get("PNETLAB_HOST") or os.environ.get("EVENG_HOST")
        username = os.environ.get("PNETLAB_USERNAME") or os.environ.get("EVENG_USERNAME")
        password = os.environ.get("PNETLAB_PASSWORD") or os.environ.get("EVENG_PASSWORD")
        if not host or not username or not password:
            raise PNETLabError(
                -1,
                "missing config: set PNETLAB_HOST, PNETLAB_USERNAME, PNETLAB_PASSWORD env vars",
                "config",
            )
        _client = PNETLabClient(host, username, password)
    _client._ensure_authed()
    return _client


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _err(e: Exception) -> str:
    if isinstance(e, PNETLabError):
        hint = ""
        if e.code == 20039:
            hint = " (another session is active for this user — log the PNETLab browser out, or use a different account)"
        return f"PNETLab error {e.code}: {e.message}{hint}"
    return f"error: {e}"


_viewer_client: PNETLabClient | None = None


def _viewer() -> PNETLabClient | None:
    """Build the singleton viewer client (the account that watches in the browser).

    Returns None if PNETLAB_VIEWER_USERNAME / PNETLAB_VIEWER_PASSWORD are unset.
    """
    global _viewer_client
    u = os.environ.get("PNETLAB_VIEWER_USERNAME")
    p = os.environ.get("PNETLAB_VIEWER_PASSWORD")
    if not u or not p:
        return None
    if _viewer_client is None:
        host = os.environ.get("PNETLAB_HOST") or os.environ.get("EVENG_HOST")
        _viewer_client = PNETLabClient(host, u, p)
    return _viewer_client


def _join_viewer(session_id: int) -> dict:
    """Join the viewer account to the given lab session so its browser sees the
    live topology. Leaves the viewer's prior session first."""
    v = _viewer()
    if v is None:
        return {"skipped": "PNETLAB_VIEWER_USERNAME/PASSWORD not set"}
    v._ensure_authed()
    try:
        v.close_lab()  # clear any prior session binding
    except Exception:
        pass
    return v.join_session(session_id)


# -- listings ---------------------------------------------------------------

@mcp.tool()
def list_templates() -> str:
    """List installed node templates (keys are template ids usable in add_node)."""
    try:
        return _ok(_c().list_templates())
    except Exception as e:
        return _err(e)


@mcp.tool()
def list_network_types() -> str:
    """List available network types (bridge, pnet0..pnet9, ovs, ...)."""
    try:
        return _ok(_c().list_network_types())
    except Exception as e:
        return _err(e)


# -- lab session ------------------------------------------------------------

@mcp.tool()
def open_lab(path: str) -> str:
    """Open a lab for editing/running. `path` is the lab file name with NO leading
    slash, e.g. "2pc_1sw.unl". Creates/reuses the lab's sandbox and binds it to this
    session. Must be called before add_node / start_node / get_lab / etc.

    If PNETLAB_VIEWER_USERNAME is configured, the viewer account (e.g. your admin)
    is auto-joined to this session so you can watch the live topology in the browser
    at the PNETLab web UI while the agent works."""
    try:
        path = path.lstrip("/")
        res = _c().open_lab(path)
        out: dict = {"opened": path, "result": res}
        try:
            sid = _c().get_current_session()
            if sid is not None:
                out["session_id"] = sid
                out["viewer_joined"] = _join_viewer(sid)
                out["watch_in_browser"] = (
                    f"{_c().host}/legacy/topology - log in as the viewer account "
                    f"({os.environ.get('PNETLAB_VIEWER_USERNAME', '?')}) to see the live topology"
                )
        except Exception as ve:
            out["viewer_join_error"] = _err(ve)
        return _ok(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def join_viewer() -> str:
    """(Re)join the configured viewer account to the agent's current lab session.
    Call this if you logged into the browser as the viewer before the agent opened
    the lab, or to refresh the viewer's binding. Lets you watch the live topology
    in the PNETLab web UI."""
    try:
        sid = _c().get_current_session()
        if sid is None:
            return "no lab session is open; call open_lab first"
        return _ok(
            {
                "session_id": sid,
                "viewer_join": _join_viewer(sid),
                "watch_in_browser": f"{_c().host}/legacy/topology",
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def close_lab() -> str:
    """Leave the current lab session (releases the session binding). Also leaves
    the viewer account so its browser stops tracking this session."""
    try:
        out: dict = {"closed": _c().close_lab()}
        try:
            v = _viewer()
            if v is not None:
                v._ensure_authed()
                out["viewer_left"] = v.close_lab()
        except Exception as ve:
            out["viewer_leave_error"] = _err(ve)
        return _ok(out)
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_lab() -> str:
    """Get the open lab's info, full topology (nodes/networks/connections) and node
    status. Use this to see what's running and how nodes are wired."""
    try:
        return _ok(_c().get_lab())
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_node_status() -> str:
    """Get per-node running status (0=stopped, 1=building/booting, 2=running)."""
    try:
        return _ok(_c().get_node_status())
    except Exception as e:
        return _err(e)


# -- nodes ------------------------------------------------------------------

@mcp.tool()
def add_node(
    type: str,
    template: str,
    name: str,
    left: int = 100,
    top: int = 100,
    ethernet: int = 1,
    config: str = "Unconfigured",
    icon: str = "Desktop.png",
) -> str:
    """Add a node to the open lab. `type`/`template` come from list_templates
    (e.g. type="vpcs" template="vpcs", or type="qemu" template="vios").
    Returns the new node including its console port/url. Node is added stopped;
    call start_node to boot it."""
    try:
        return _ok(_c().add_node(type, template, name, left, top, ethernet, config, icon))
    except Exception as e:
        return _err(e)


@mcp.tool()
def connect_nodes(
    src_id: int,
    src_if: int,
    dest_id: int,
    dest_if: int,
    name: str = "p2p",
) -> str:
    """Connect two node interfaces with a point-to-point link.
    src_id/dest_id are node ids (from get_lab); src_if/dest_if are 0-based
    ethernet indices (0 = eth0, 1 = eth1, ...)."""
    try:
        return _ok(_c().connect_p2p(name, src_id, src_if, dest_id, dest_if))
    except Exception as e:
        return _err(e)


@mcp.tool()
def start_node(node_id: int | None = None) -> str:
    """Start one node, or every node in the lab if node_id is omitted."""
    try:
        return _ok(_c().start_node(node_id))
    except Exception as e:
        return _err(e)


@mcp.tool()
def stop_node(node_id: int | None = None) -> str:
    """Stop one node, or every node in the lab if node_id is omitted."""
    try:
        return _ok(_c().stop_node(node_id))
    except Exception as e:
        return _err(e)


@mcp.tool()
def delete_node(node_id: int) -> str:
    """Delete a node from the open lab."""
    try:
        return _ok(_c().delete_node(node_id))
    except Exception as e:
        return _err(e)


@mcp.tool()
def push_config(node_id: int, config: str) -> str:
    """Push a startup configuration to a node (applied on next boot).

    Stores the config text and enables it for boot injection. For this to take
    effect the node must be (re)started after the push - the config is injected
    during boot, not into a running node.

    Config format is device-specific. For VPCS use plain VPCS commands, one per
    line, e.g. ``ip 192.168.1.1 24``. For IOS/IOL routers use the normal
    ``hostname ...`` / ``interface ...`` running-config style.

    Note: this tool also flips the node's config flag so PNETLab actually loads
    the stored config on boot (without that flag the stored config is silently
    ignored - a known PNETLab configs/edit quirk).
    """
    try:
        return _ok(_c().push_config(node_id, config))
    except Exception as e:
        return _err(e)


@mcp.tool()
def node_console(node_id: int) -> str:
    """Get a node's console connection info (telnet/SSH host:port) so the agent
    can interact with the device CLI."""
    try:
        return _ok(_c().node_console(node_id))
    except Exception as e:
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
