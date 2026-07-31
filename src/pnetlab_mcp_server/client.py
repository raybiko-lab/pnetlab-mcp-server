"""PNETLab v6 API client.

PNETLab v6 keeps the classic EVE-NG engine under /api/ but moved lab operations to a
session-scoped API (``/api/labs/session/*``) and replaced the classic login with a
Laravel login at ``/store/public/auth/login/login``.

Everything here was reverse-engineered + live-verified against PNETLab 6.0.0-100.

Key gotchas encoded in this client:
  * Login is POST /store/public/auth/login/login with JSON {username, password}
    AND the header ``X-Requested-With: XMLHttpRequest`` (without it the server may
    serve the SPA shell instead of authenticating). It sets ``_session`` + ``token``
    cookies which are reused for every /api/ call.
  * All /api/labs/session/* endpoints require a **JSON** body (form-encoded bodies
    are silently dropped and surface as 40000 "missing required fields").
  * Open a lab with POST /api/labs/session/factory/create {path} where ``path`` has
    NO leading slash (e.g. "mylab.unl"). The server creates/reuses a sandbox file
    named ``labs<path>`` at /opt/unetlab/ and binds the lab session to your cookie.
  * One active session per user account: if a browser is logged in as the same user,
    factory/create returns 20039 "sandbox already exists". Log the browser out first.
  * factory/leave releases the session binding but does NOT delete the sandbox file;
  re-opening the same lab works fine (the sandbox is reused).
"""

from __future__ import annotations

import json
from typing import Any

import requests


class PNETLabError(Exception):
    """Raised when the PNETLab API returns a non-success code."""

    def __init__(self, code: int, message: str, endpoint: str = ""):
        self.code = code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{code}] {message} ({endpoint})")


class PNETLabClient:
    def __init__(self, host: str, username: str, password: str, timeout: int = 30):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "pnetlab-mcp/0.1",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
            }
        )
        self._authed = False

    # -- auth -----------------------------------------------------------------
    def login(self) -> dict:
        """Authenticate and obtain the session cookie used for /api/ calls."""
        # Prime the Laravel session (sets _session cookie).
        try:
            self.session.get(f"{self.host}/store/public/auth/login/offline", timeout=self.timeout)
        except requests.RequestException:
            pass
        r = self.session.post(
            f"{self.host}/store/public/auth/login/login",
            data=json.dumps({"username": self.username, "password": self.password}),
            timeout=self.timeout,
        )
        data = self._json(r, "/store/public/auth/login/login")
        if not (isinstance(data, dict) and data.get("result") is True):
            raise PNETLabError(401, f"login failed: {r.text[:200]}", "auth/login")
        self._authed = True
        return data

    def _ensure_authed(self) -> None:
        if not self._authed:
            self.login()

    # -- low-level ------------------------------------------------------------
    def _json(self, r: requests.Response, endpoint: str) -> Any:
        ctype = r.headers.get("Content-Type", "").lower()
        if "json" not in ctype and not r.text.lstrip().startswith(("{", "[")):
            raise PNETLabError(
                r.status_code,
                f"unexpected non-JSON response ({ctype}): {r.text[:160]}",
                endpoint,
            )
        try:
            return r.json()
        except ValueError:
            raise PNETLabError(r.status_code, f"invalid JSON: {r.text[:160]}", endpoint)

    def _api(self, method: str, path: str, body: dict | None = None) -> Any:
        """Call a /api/ endpoint with a JSON body and return the parsed payload.

        Raises PNETLabError on HTTP error codes or EVE-NG ``status == "fail"``.
        Re-logs in once on 401/412 (session expired).
        """
        self._ensure_authed()
        url = f"{self.host}/api/{path.lstrip('/')}"
        r = self.session.request(
            method, url, data=json.dumps(body) if body is not None else None, timeout=self.timeout
        )
        if r.status_code in (401, 412):
            # Session may have expired; retry once.
            self._authed = False
            self._ensure_authed()
            r = self.session.request(
                method, url, data=json.dumps(body) if body is not None else None, timeout=self.timeout
            )
        data = self._json(r, path)
        if isinstance(data, dict):
            code = data.get("code", r.status_code)
            status = data.get("status", "")
            if status == "fail" or (isinstance(code, int) and code >= 400 and code not in (401, 412)):
                raise PNETLabError(code, data.get("message", "unknown error"), path)
        return data

    # -- listings -------------------------------------------------------------
    def list_templates(self) -> dict:
        """Return the installed node templates (keyed by template id)."""
        return self._api("GET", "/list/templates/").get("data", {})

    def list_network_types(self) -> dict:
        """Return available network types (bridge, pnet0..pnet9, ovs, ...)."""
        return self._api("GET", "/list/networks").get("data", {})

    # -- lab session lifecycle ------------------------------------------------
    def open_lab(self, path: str) -> dict:
        """Open (or reopen) a lab by path, e.g. ``2pc_1sw.unl`` (no leading slash)."""
        return self._api("POST", "/labs/session/factory/create", {"path": path})

    def close_lab(self) -> dict:
        """Leave the current lab session (releases the session binding)."""
        return self._api("POST", "/labs/session/factory/leave", {})

    def get_current_session(self) -> int | None:
        """Return the current user's lab session id (from /api/auth data.lab), or None."""
        data = self._api("GET", "/auth").get("data", {}) or {}
        lab = data.get("lab")
        return int(lab) if lab not in (None, "") else None

    def join_session(self, lab_session_id: int) -> dict:
        """Join another user's lab session by id (shared live topology)."""
        return self._api("POST", "/labs/session/factory/join", {"lab_session": int(lab_session_id)})

    # -- read topology --------------------------------------------------------
    def get_topology(self) -> dict:
        """Return nodes, networks, connections and text objects of the open lab."""
        return self._api("GET", "/labs/session/topology").get("data", {})

    def get_lab_info(self) -> dict:
        """Return lab metadata (name, id, author, description, ...)."""
        return self._api("GET", "/labs/session/info").get("data", {})

    def get_node_status(self) -> dict:
        """Return per-node running status (0=stopped,1=building,2=running,...).

        Note: this endpoint is POST-only and authenticates via the ``token`` cookie
        set at login; a GET falls through to a different route and returns nothing.
        """
        return self._api("POST", "/labs/session/nodestatus", {}).get("data", {})

    def _node_ids(self) -> list[int]:
        nodes = self.get_topology().get("nodes", {})
        if isinstance(nodes, dict) and nodes:
            return sorted(int(k) for k in nodes.keys())
        return []

    def get_lab(self) -> dict:
        """Convenience: info + topology + node status in one call."""
        return {
            "info": self.get_lab_info(),
            "topology": self.get_topology(),
            "node_status": self.get_node_status(),
        }

    # -- nodes ----------------------------------------------------------------
    def add_node(
        self,
        type: str,
        template: str,
        name: str,
        left: int = 100,
        top: int = 100,
        ethernet: int = 1,
        config: str = "Unconfigured",
        icon: str = "Desktop.png",
        ram: int | None = None,
        image: str | None = None,
        console: str | None = None,
    ) -> dict:
        """Add a node to the open lab sandbox. Returns the new node object."""
        body: dict[str, Any] = {
            "type": type,
            "template": template,
            "name": name,
            "left": left,
            "top": top,
            "ethernet": ethernet,
            "config": config,
            "icon": icon,
        }
        if ram is not None:
            body["ram"] = ram
        if image is not None:
            body["image"] = image
        if console is not None:
            body["console"] = console
        data = self._api("POST", "/labs/session/nodes/add", body)
        return data.get("update", {}).get("nodes", {})

    def delete_node(self, node_id: int) -> dict:
        return self._api("POST", "/labs/session/nodes/delete", {"id": node_id})

    def start_node(self, node_id: int | None = None) -> dict:
        """Start one node, or all nodes when node_id is None (iterates node ids
        - the API's null-id path is unreliable for "all")."""
        if node_id is None:
            return {nid: self._api("POST", "/labs/session/nodes/start", {"id": nid}) for nid in self._node_ids()}
        return self._api("POST", "/labs/session/nodes/start", {"id": node_id})

    def stop_node(self, node_id: int | None = None) -> dict:
        """Stop one node, or all nodes when node_id is None (iterates node ids)."""
        if node_id is None:
            return {nid: self._api("POST", "/labs/session/nodes/stop", {"id": nid}) for nid in self._node_ids()}
        return self._api("POST", "/labs/session/nodes/stop", {"id": node_id})

    def wipe_node(self, node_id: int) -> dict:
        return self._api("POST", "/labs/session/nodes/wipe", {"id": node_id})

    def node_console(self, node_id: int) -> dict:
        """Return the console port/url for a node. The ``nodes/port`` endpoint only
        *sets* a port, so we read the console info from the live topology instead."""
        nodes = self.get_topology().get("nodes", {})
        node = nodes.get(str(node_id)) or nodes.get(node_id)
        if not node:
            raise PNETLabError(404, f"node {node_id} not found in topology", "nodes/port")
        return {
            "id": node.get("id"),
            "name": node.get("name"),
            "port": node.get("port"),
            "url": node.get("url"),
            "console": node.get("console"),
            "status": node.get("status"),
        }

    # -- networks / connectivity ---------------------------------------------
    def add_network(
        self, name: str, type: str = "bridge", left: int = 400, top: int = 300
    ) -> dict:
        """Add a shared network (switch/cloud) that nodes can attach to."""
        body = {
            "name": name,
            "type": type,
            "left": left,
            "top": top,
            "visibility": 1,
            "postfix": 0,
        }
        return self._api("POST", "/labs/session/networks/add", body)

    def connect_p2p(
        self, name: str, src_id: int, src_if: int, dest_id: int, dest_if: int
    ) -> dict:
        """Directly connect two node interfaces with a point-to-point link."""
        body = {
            "name": name,
            "src_id": src_id,
            "src_if": src_if,
            "dest_id": dest_id,
            "dest_if": dest_if,
        }
        return self._api("POST", "/labs/session/networks/p2p", body)

    def delete_network(self, network_id: int) -> dict:
        return self._api("POST", "/labs/session/networks/delete", {"id": network_id})

    # -- configuration --------------------------------------------------------
    def push_config(self, node_id: int, config: str) -> dict:
        """Push a startup configuration to a node (applied on next boot).

        The config text goes in the ``data`` field (apiEditLabConfig ->
        setNodeConfigData(id, data)).

        PNETLab only injects the stored config on boot if the node's ``config``
        flag is ``"1"``: ``device::prepare()`` writes ``config_data`` to the
        node's ``startup-config`` file (which e.g. VPCS then copies to
        ``startup.vpc``) only inside ``if ($this->config == "1")``. But
        apiEditLabConfig merely stores the data and leaves the flag as
        ``"Unconfigured"`` - so the pushed config is silently ignored on boot
        (observed: VPCS boots with ``0.0.0.0/0`` despite "Executing the startup
        file"). We therefore also flip the flag to ``"1"`` via ``nodes/edit``
        (-> editNode -> Node::edit -> editParams, which only touches fields that
        are present) whenever a non-empty config is pushed. An empty config
        leaves the flag untouched (avoids reviving a stale startup-config).
        """
        saved = self._api("POST", "/labs/session/configs/edit", {"id": node_id, "data": config})
        out: dict[str, Any] = {"config_saved": saved}
        if config.strip():
            out["config_enabled"] = self._api(
                "POST", "/labs/session/nodes/edit", {"id": node_id, "config": "1"}
            )
        return out
