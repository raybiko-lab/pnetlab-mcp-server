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
            hint = " (another session is active for this user - log the PNETLab browser out, or use a different account)"
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
    """List installed node templates as structured entries.

    Each entry is ``{template, name, installed}`` where ``template`` is the id
    usable in add_node and ``installed`` is False when the image is missing
    (PNETLab marks these with a ``.missing`` suffix)."""
    try:
        raw = _c().list_templates()
        entries = []
        for tid, label in raw.items():
            label = str(label)
            missing = label.endswith(".missing")
            entries.append(
                {
                    "template": tid,
                    "name": label[: -len(".missing")] if missing else label,
                    "installed": not missing,
                }
            )
        entries.sort(key=lambda e: (not e["installed"], e["template"]))
        return _ok({"count": len(entries), "templates": entries})
    except Exception as e:
        return _err(e)


@mcp.tool()
def list_network_types() -> str:
    """List available network types (bridge, pnet0..pnet9, ovs, ...)."""
    try:
        return _ok(_c().list_network_types())
    except Exception as e:
        return _err(e)


@mcp.tool()
def list_images(template: str) -> str:
    """List the disk images available for a template (e.g. ``mikrotik-7.23.2``)
    plus the default image. Call this before add_node to get a valid ``image``
    string for a QEMU node -- without one the node starts and immediately crashes.

    For non-QEMU types (vpcs/iol/dynamips/docker) this lists whatever images that
    backend offers."""
    try:
        return _ok(_c().list_images(template))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_template(template: str) -> str:
    """Get a template's full editable options: available images, qemu versions,
    and all default field values. Use this when you need more than list_images
    (e.g. the right qemu_arch/qemu_nic/qemu_options for a node)."""
    try:
        return _ok(_c().get_template(template))
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
def get_lab(compact: bool = False) -> str:
    """Get the open lab's info, full topology (nodes/networks/connections) and node
    status. Use this to see what's running and how nodes are wired.

    Pass ``compact=True`` to drop cosmetic noise (empty style dicts, second-console
    fields, empty qemu_options) and thin each node to id/name/type/template/image/
    ram/status/console plus an ethernet map of {if -> {name, network_id, suspend}}.
    Compact mode is recommended for large topologies -- the full payload can be
    80-100KB."""
    try:
        return _ok(_c().get_lab(compact=compact))
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
    image: str | None = None,
    ram: int | None = None,
    cpu: int | None = None,
    left: int = 100,
    top: int = 100,
    ethernet: int = 1,
    config: str = "Unconfigured",
    icon: str = "Desktop.png",
    console: str | None = None,
    qemu_arch: str | None = None,
    qemu_nic: str | None = None,
    qemu_options: str | None = None,
    qemu_version: str | None = None,
    pci_mode: str | None = None,
    config_script: str | None = None,
    firstmac: str | None = None,
    delay: int | None = None,
    serial: int | None = None,
    template_defaults: bool = True,
) -> str:
    """Add a node to the open lab. ``type``/``template`` come from list_templates
    (e.g. type="vpcs" template="vpcs", or type="qemu" template="mikrotik").

    By default (``template_defaults=True``) the template's built-in defaults are
    auto-applied -- image, ram, cpu, qemu_arch/qemu_nic/qemu_options/qemu_version,
    console, icon, config_script -- exactly what a GUI-created node inherits. So a
    bare ``add_node("qemu", "mikrotik", "R1")`` produces a fully bootable node with
    the right image and console; you usually don't need list_images first. Any field
    you pass explicitly overrides the template default. Pass
    ``template_defaults=False`` to supply everything yourself.

    ``console`` defaults to the template's console, falling back to ``telnet`` for
    qemu/iol/dynamips (needed for status reporting and the console tools). Node is
    added stopped; call start_node to boot it."""
    try:
        return _ok(
            _c().add_node(
                type, template, name, left, top, ethernet, config, icon,
                template_defaults=template_defaults,
                image=image, ram=ram, cpu=cpu, console=console, qemu_arch=qemu_arch,
                qemu_nic=qemu_nic, qemu_options=qemu_options, qemu_version=qemu_version,
                pci_mode=pci_mode, config_script=config_script, firstmac=firstmac,
                delay=delay, serial=serial,
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_node(
    node_id: int,
    name: str | None = None,
    image: str | None = None,
    ram: int | None = None,
    cpu: int | None = None,
    left: int | None = None,
    top: int | None = None,
    ethernet: int | None = None,
    config: str | None = None,
    icon: str | None = None,
    console: str | None = None,
    qemu_arch: str | None = None,
    qemu_nic: str | None = None,
    qemu_options: str | None = None,
    qemu_version: str | None = None,
    pci_mode: str | None = None,
    config_script: str | None = None,
    firstmac: str | None = None,
    delay: int | None = None,
    serial: int | None = None,
) -> str:
    """Edit an existing node's fields. Only the fields you pass are changed.

    Use this to fix a node that crashes on boot (e.g. set a missing image), or to
    tweak ram/qemu_nic without deleting and recreating. Image/ram/qemu_* changes
    take effect on the next (re)start, not on a running node. Accepts the same
    field names as add_node."""
    try:
        return _ok(
            _c().update_node(
                node_id,
                name=name, image=image, ram=ram, cpu=cpu, left=left, top=top,
                ethernet=ethernet, config=config, icon=icon, console=console,
                qemu_arch=qemu_arch, qemu_nic=qemu_nic, qemu_options=qemu_options,
                qemu_version=qemu_version, pci_mode=pci_mode,
                config_script=config_script, firstmac=firstmac, delay=delay, serial=serial,
            )
        )
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
    ethernet indices (0 = eth0, 1 = eth1, ...). Returns the new network_id and
    both endpoints (use the network_id with set_link_state/set_link_quality/
    delete_link)."""
    try:
        return _ok(_c().connect_p2p(name, src_id, src_if, dest_id, dest_if))
    except Exception as e:
        return _err(e)


@mcp.tool()
def start_node(node_id: int | None = None, check: bool = False) -> str:
    """Start one node, or every node in the lab if node_id is omitted.

    With ``check=True`` (single node only), poll status for a few seconds after
    starting and report a diagnostic if the node crashes back to stopped (usually
    a missing/invalid image or too little ram)."""
    try:
        return _ok(_c().start_node(node_id, check=check))
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
    """Delete a node from the open lab (stop it first)."""
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
    ``hostname ...`` / ``interface ...`` running-config style. For interactive
    CLIs (e.g. RouterOS) prefer run_command.

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
    can interact with the device CLI. For actual command execution prefer
    run_command / console_send + console_read, which handle the telnet handshake
    and login for you."""
    try:
        return _ok(_c().node_console(node_id))
    except Exception as e:
        return _err(e)


# -- links / fault injection ------------------------------------------------

@mcp.tool()
def delete_link(network_id: int) -> str:
    """Delete a link (p2p network) at the topology layer. Unlinks both attached
    interfaces. Use the network_id returned by connect_nodes (or read from
    get_lab). The node must be stopped or the interfaces will be hot-unplugged."""
    try:
        return _ok(_c().delete_link(network_id))
    except Exception as e:
        return _err(e)


@mcp.tool()
def set_link_state(network_id: int, up: bool) -> str:
    """Bring a link up or down at the topology layer (physical-style fault
    injection). ``up=True`` restores the link, ``up=False`` suspends it. This is
    not the same as disabling an interface inside the device CLI -- the device
    sees its port go down, matching a real cable pull. Only affects running nodes.

    Use the network_id from connect_nodes / get_lab. Current state is readable
    from get_lab (each ethernet has a ``suspend`` field: 1=suspended)."""
    try:
        return _ok(_c().set_link_state(network_id, up))
    except Exception as e:
        return _err(e)


@mcp.tool()
def set_link_quality(
    network_id: int,
    loss: float | None = None,
    delay: float | None = None,
    jitter: float | None = None,
    bandwidth: float | None = None,
) -> str:
    """Inject impairment on a link: packet loss (percent), one-way delay (ms),
    jitter (ms), and/or bandwidth limit (kbit/s). Pass only the dimensions you
    want to set. Applied to both directions of the link. Only affects running
    nodes. Clear an impairment by passing 0 for that dimension."""
    try:
        return _ok(_c().set_link_quality(network_id, loss=loss, delay=delay, jitter=jitter, bandwidth=bandwidth))
    except Exception as e:
        return _err(e)


# -- console interaction ----------------------------------------------------

@mcp.tool()
def run_command(
    node_id: int,
    command: str,
    timeout: float = 15.0,
    wait_for: str | None = None,
    username: str = "admin",
    password: str = "",
) -> str:
    """Run a single CLI command on a running node and return its output.

    Opens a telnet console, handles the IAC handshake, logs in if the device asks
    (default admin/empty password -- pass credentials for devices that differ),
    sends the command, and reads until the output goes idle (or ``wait_for``
    regex matches). The session is pooled and reused across calls; call
    console_close when finished with a node.

    Examples: run_command(1, "/ip address print") on a MikroTik;
    run_command(1, "ping 10.0.0.2 count 4", wait_for="packet-loss") ;
    run_command(2, "show ip interface brief") on IOS;
    run_command(3, "ping 10.0.0.1") on VPCS.

    Gotcha: a single ``?`` for help often does not trigger under telnet; use
    run_command with the full command instead. For long-running monitors, pass
    wait_for to match the final summary line."""
    try:
        return _ok(_c().run_command(node_id, command, timeout=timeout, wait_for=wait_for, username=username, password=password))
    except Exception as e:
        return _err(e)


@mcp.tool()
def console_send(
    node_id: int,
    text: str,
    newline: bool = True,
    username: str = "admin",
    password: str = "",
) -> str:
    """Send raw text to a node's console (auto-opens + logs in on first use).
    Lower-level than run_command: use it for multi-step interactive sessions where
    you need to drive the prompt yourself. Set newline=False to send a partial
    line (e.g. before reading). Read the response with console_read."""
    try:
        return _ok(_c().console_send(node_id, text, newline=newline, username=username, password=password))
    except Exception as e:
        return _err(e)


@mcp.tool()
def console_read(node_id: int, timeout: float = 5.0, username: str = "admin", password: str = "") -> str:
    """Read pending output from a node's console until it goes idle (up to
    ``timeout`` seconds). Pair with console_send for interactive sessions."""
    try:
        return _ok(_c().console_read(node_id, timeout=timeout, username=username, password=password))
    except Exception as e:
        return _err(e)


@mcp.tool()
def console_close(node_id: int | None = None) -> str:
    """Close a node's console session, or all of them when node_id is None. Good
    practice once you are done configuring a node."""
    try:
        return _ok(_c().console_close(node_id))
    except Exception as e:
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
