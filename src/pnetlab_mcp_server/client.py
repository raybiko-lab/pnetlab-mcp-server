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
  * The general mutation route is ``POST /api/labs/session/<object>/<action>`` where
    ``$variables`` is parsed from the JSON body (see api.php). So e.g.
    ``nodes/edit``, ``networks/p2p``, ``interfaces/setquality`` all take JSON bodies
    and behave like the existing ``nodes/add``.
  * Node fields accepted on add/edit flow through ``Node::edit()`` ->
    ``device_<type>::editParams()`` + ``device::editParams()``. For QEMU that is
    image, ram, cpu, qemu_arch, qemu_nic, qemu_options, qemu_version, pci_mode,
    config_script, firstmac, console, delay, serial, ... (all optional; only fields
    present in the body are applied).
  * Link quality / link state are interface-level (not network-level): a p2p link is
    a bridge network (visibility 0) joining two interfaces. ``interfaces/setquality``
    sets loss/delay/jitter/bandwidth on ONE interface; ``interfaces/setSuspendtwo_way``
    suspends both interfaces of a visibility-0 network in one call.
"""

from __future__ import annotations

import json
import re
import select
import socket
import time
from typing import Any

import requests


class PNETLabError(Exception):
    """Raised when the PNETLab API returns a non-success code."""

    def __init__(self, code: int, message: str, endpoint: str = ""):
        self.code = code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{code}] {message} ({endpoint})")


# -- telnet console ---------------------------------------------------------

# Telnet protocol bytes (telnetlib was removed in Python 3.13+, so we negotiate
# IAC by hand). The handshake that makes RouterOS/VPCS/IOS accept input is:
# accept every WILL the server offers (reply DO) and refuse every DO the server
# asks of us (reply WONT).
_IAC = 0xFF
_DONT = 0xFE
_DO = 0xFD
_WONT = 0xFC
_WILL = 0xFB
_SB = 0xFA
_SE = 0xF0

# Matches login/password prompts anywhere in the buffer (devices print them
# slowly after boot, so we cannot anchor to end-of-buffer).
_LOGIN_RE = re.compile(rb"(?:Login|login|User|Username|user)\s*:")
_PASS_RE = re.compile(rb"(?:Password|password|Passcode)\s*:")
# A device at its CLI prompt (no login needed / login complete). The trailing
# whitespace/escape junk after '>' or '#' is handled by checking the tail.
_CLI_PROMPT_RE = re.compile(rb"(?:[>#$]\s*$|\]\s*>\s*$|VPCS\[\d+\]>\s*$)")
# Anything that looks like a prompt we can react to.
_ANY_PROMPT_RE = re.compile(rb"(?:[Ll]ogin:|[Pp]assword:|[>#$]\s*$|\]\s*>\s*$|VPCS\[\d+\]>)")

# Terminal escape sequences (CSI/OSC/etc.) and a CLI prompt tail, used to clean
# console output. RouterOS in particular repaints the prompt with cursor moves.
_ANSI_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b[0-9]*[A-Za-z]")
_PROMPT_TAIL_RE = re.compile(rb"\[[^\]]*\]\s*[>#]\s*$|^[>#]\s*$|VPCS\[\d+\]>\s*$")


class _ConsoleSession:
    """A kept-alive telnet session to one node's console.

    One PNETLab console port serves a single telnet client at a time, so sessions
    are pooled per node_id on the PNETLabClient and reused across tool calls.
    """

    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.logged_in = False

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.setblocking(False)
        self.logged_in = False

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.logged_in = False

    @property
    def alive(self) -> bool:
        return self.sock is not None

    # -- low-level IO -------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise PNETLabError(-1, "console not connected", "console")
        try:
            self.sock.sendall(data)
        except OSError as e:
            self.close()
            raise PNETLabError(-1, f"console send failed: {e}", "console")

    def _read(self, idle: float = 0.35, max_wait: float = 15.0) -> bytes:
        """Read until ``idle`` seconds of silence (or ``max_wait``). Returns clean
        text with IAC sequences stripped and answered."""
        if self.sock is None:
            return b""
        deadline = time.monotonic() + max_wait
        last = time.monotonic()
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            try:
                r, _, _ = select.select([self.sock], [], [], 0.1)
            except (OSError, ValueError):
                break
            if r:
                try:
                    data = self.sock.recv(8192)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break  # peer closed
                chunks.append(self._handle_iac(data))
                last = time.monotonic()
            elif chunks and time.monotonic() - last >= idle:
                break
        return b"".join(chunks)

    def _read_until_prompt(self, max_wait: float = 15.0) -> bytes:
        """Read incrementally until a recognizable prompt appears (login/password/
        CLI), then settle. Devices print their banner slowly, so a single idle-based
        read can return before the login prompt is up -- this keeps reading."""
        end = time.monotonic() + max_wait
        buf = bytearray()
        while time.monotonic() < end:
            chunk = self._read(idle=0.3, max_wait=1.0)
            if chunk:
                buf += chunk
                if _ANY_PROMPT_RE.search(bytes(buf)):
                    buf += self._read(idle=0.2, max_wait=0.5)  # settle
                    break
        return bytes(buf)

    def _handle_iac(self, data: bytes) -> bytes:
        """Strip IAC negotiation bytes, answer them, return the remaining text."""
        out = bytearray()
        resp = bytearray()
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == _IAC and i + 1 < n:
                cmd = data[i + 1]
                if cmd == _IAC:  # escaped 0xFF literal
                    out.append(_IAC)
                    i += 2
                    continue
                if cmd == _SB:  # subnegotiation: skip to IAC SE
                    j = i + 2
                    while j + 1 < n and not (data[j] == _IAC and data[j + 1] == _SE):
                        j += 1
                    i = j + 2
                    continue
                if cmd in (_WILL, _WONT, _DO, _DONT) and i + 2 < n:
                    opt = data[i + 2]
                    if cmd == _WILL:
                        resp += bytes([_IAC, _DO, opt])  # accept what it offers
                    elif cmd == _DO:
                        resp += bytes([_IAC, _WONT, opt])  # we offer nothing
                    elif cmd == _WONT:
                        resp += bytes([_IAC, _DONT, opt])
                    elif cmd == _DONT:
                        resp += bytes([_IAC, _WONT, opt])
                    i += 3
                    continue
                i += 2  # unknown 2-byte cmd, skip
                continue
            out.append(b)
            i += 1
        if resp and self.sock is not None:
            try:
                self.sock.sendall(bytes(resp))
            except OSError:
                pass
        return bytes(out)

    # -- high-level ---------------------------------------------------------
    def login(self, username: str = "admin", password: str = "") -> bytes:
        """Drive an interactive login if the device presents a login prompt.

        Waits for the device to surface a prompt (it can take several seconds
        after boot), then walks Login:/Password: prompts. Only newly-read text is
        inspected after each send, so an already-answered prompt is never
        re-triggered. Idempotent: if no login prompt appears (VPCS, unconfigured
        IOS, or an already-logged-in session) it just collects the banner."""
        if self.sock is None:
            raise PNETLabError(-1, "console not connected", "console")
        all_text = bytearray()
        pending = bytearray(self._read_until_prompt(max_wait=15.0))
        all_text += pending
        for _ in range(5):
            tail = bytes(pending)[-80:]
            if _CLI_PROMPT_RE.search(tail):
                break  # at a CLI prompt, nothing left to log in to
            # Password: is checked first -- it's the expected reply to a username.
            if _PASS_RE.search(pending):
                self._send((password + "\r\n").encode())
                pending = bytearray(self._read_until_prompt(max_wait=6.0))
                all_text += pending
                continue
            if _LOGIN_RE.search(pending):
                self._send((username + "\r\n").encode())
                pending = bytearray(self._read_until_prompt(max_wait=6.0))
                all_text += pending
                continue
            # No recognizable prompt; nudge with a newline and read once more.
            self._send(b"\r\n")
            new = self._read_until_prompt(max_wait=4.0)
            pending += new
            all_text += new
        self.logged_in = True
        return bytes(all_text)

    def send(self, text: str, newline: bool = True) -> None:
        line = "\r\n" if newline else ""
        self._send((text + line).encode(errors="replace"))

    def read(self, idle: float = 0.5, max_wait: float = 15.0) -> str:
        return self._read(idle=idle, max_wait=max_wait).decode("utf-8", "replace")

    def run_command(self, command: str, timeout: float = 15.0, wait_for: str | None = None) -> str:
        """Send a command and read until the stream goes idle (or ``wait_for``
        matches). The echoed command line and trailing prompt repaints are
        stripped from the output."""
        self.send(command)
        if wait_for:
            pat = re.compile(wait_for.encode())
            out = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                out += self._read(idle=0.3, max_wait=2.0)
                if pat.search(bytes(out)):
                    break
            text = bytes(out).decode("utf-8", "replace")
        else:
            text = self.read(idle=0.4, max_wait=timeout)
        return _clean_console_output(text, command)


def _clean_console_output(text: str, command: str = "") -> str:
    """Strip ANSI escapes, the echoed command, trailing prompt repaints, and
    collapse runs of blank lines."""
    raw = text.encode("utf-8", "replace")
    raw = _ANSI_RE.sub(b"", raw)
    lines = raw.decode("utf-8", "replace").splitlines()
    # Drop echoed command lines. Devices echo the command once (or twice --
    # RouterOS redraws the prompt with the command on a second line). Strip up to
    # two leading lines that are the bare command or "<prompt> <command>".
    cmd_echo = command.strip()
    stripped = 0
    while lines and cmd_echo and stripped < 2:
        first = lines[0].strip()
        if first == cmd_echo or (
            first.endswith(cmd_echo) and len(first) < len(cmd_echo) + 60
        ):
            lines.pop(0)
            stripped += 1
        else:
            break
    # Trim trailing prompt-only / blank lines.
    while lines and (not lines[-1].strip() or _PROMPT_TAIL_RE.match(lines[-1].strip().encode())):
        lines.pop()
    # Collapse 2+ consecutive blank lines to one.
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln.strip():
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(ln)
    return "\n".join(out).strip()


class PNETLabClient:
    def __init__(self, host: str, username: str, password: str, timeout: int = 30):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "pnetlab-mcp/0.2",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
            }
        )
        self._authed = False
        self._consoles: dict[int, _ConsoleSession] = {}
        self._template_cache: dict[str, dict] = {}

    # -- auth -----------------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """session.request with one retry on a dropped keep-alive connection.

        Apache closes idle pooled connections without notice, which surfaces as
        ``RemoteDisconnected``; urllib3 does not auto-retry that, so we do it here
        (the second attempt opens a fresh connection)."""
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.request(method, url, **kwargs)
        except requests.exceptions.ConnectionError:
            return self.session.request(method, url, **kwargs)

    def login(self) -> dict:
        """Authenticate and obtain the session cookie used for /api/ calls."""
        # Prime the Laravel session (sets _session cookie).
        try:
            self._request("GET", f"{self.host}/store/public/auth/login/offline")
        except requests.RequestException:
            pass
        r = self._request(
            "POST",
            f"{self.host}/store/public/auth/login/login",
            data=json.dumps({"username": self.username, "password": self.password}),
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
        payload = json.dumps(body) if body is not None else None
        r = self._request(method, url, data=payload)
        if r.status_code in (401, 412):
            # Session may have expired; retry once.
            self._authed = False
            self._ensure_authed()
            r = self._request(method, url, data=payload)
        data = self._json(r, path)
        if isinstance(data, dict):
            code = data.get("code", r.status_code)
            status = data.get("status", "")
            if status == "fail" or (isinstance(code, int) and code >= 400 and code not in (401, 412)):
                raise PNETLabError(code, data.get("message", "unknown error"), path)
        return data

    @property
    def _console_host(self) -> str:
        """The telnet host for console sessions (PNETLab host without scheme)."""
        h = self.host
        for scheme in ("https://", "http://"):
            if h.startswith(scheme):
                h = h[len(scheme):]
                break
        return h.split("/")[0]

    # -- listings -------------------------------------------------------------
    def list_templates(self) -> dict:
        """Return the installed node templates (keyed by template id)."""
        return self._api("GET", "/list/templates/").get("data", {})

    def list_network_types(self) -> dict:
        """Return available network types (bridge, pnet0..pnet9, ovs, ...)."""
        return self._api("GET", "/list/networks").get("data", {})

    def get_template(self, template: str) -> dict:
        """Return a single template's editable options (image list, qemu versions,
        defaults). Source: ``GET /api/list/templates/<template>`` -> data.options.
        """
        data = self._api("GET", f"/list/templates/{template}").get("data", {})
        options = data.get("options", {}) or {}
        image = options.get("image", {}) or {}
        qemu_version = options.get("qemu_version", {}) or {}
        return {
            "template": template,
            "type": (options.get("type", {}) or {}).get("value", ""),
            "images": list((image.get("options") or {}).keys()),
            "default_image": image.get("value", ""),
            "qemu_versions": list((qemu_version.get("options") or {}).keys()),
            "default_qemu_version": qemu_version.get("value", ""),
            "options": options,
        }

    def list_images(self, template: str) -> dict:
        """Return the available disk images for a template (e.g.
        ``mikrotik-7.23.2``) plus the default. Use this to pick a valid ``image``
        string before calling add_node."""
        info = self.get_template(template)
        return {
            "template": info["template"],
            "type": info["type"],
            "images": info["images"],
            "default_image": info["default_image"],
        }

    # Node fields whose defaults are pulled from the template (see
    # _template_defaults). These are the fields a GUI-created node inherits, so
    # applying them makes an API-created node behave identically.
    _TEMPLATE_DEFAULT_FIELDS = (
        "image", "ram", "cpu", "qemu_arch", "qemu_nic", "qemu_options",
        "qemu_version", "console", "icon", "pci_mode", "config_script",
    )

    def _template_defaults(self, template: str) -> dict:
        """Return the template's default field values, extracted from
        ``GET /api/list/templates/<template>`` -> data.options.<field>.value.

        Falsy values (None/""/0/False -- e.g. a missing image reported as False,
        or vpcs' empty console) are skipped so they don't clobber good defaults.
        Cached per template for the client's lifetime."""
        if template in self._template_cache:
            return self._template_cache[template]
        defaults: dict[str, Any] = {}
        try:
            options = self._api("GET", f"/list/templates/{template}").get("data", {}).get("options", {}) or {}
        except PNETLabError:
            self._template_cache[template] = defaults
            return defaults
        for field in self._TEMPLATE_DEFAULT_FIELDS:
            entry = options.get(field)
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:  # truthy only -- skip None/""/0/False
                defaults[field] = val
        self._template_cache[template] = defaults
        return defaults

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
        PNETLab encodes an empty node set as a JSON ``[]`` (list) instead of ``{}``;
        we normalize that to an empty dict so callers can always use ``.get()``."""
        data = self._api("POST", "/labs/session/nodestatus", {}).get("data", {})
        if isinstance(data, list):
            return {}
        return data or {}

    def _node_ids(self) -> list[int]:
        nodes = self.get_topology().get("nodes", {})
        if isinstance(nodes, dict) and nodes:
            return sorted(int(k) for k in nodes.keys())
        return []

    def get_lab(self, compact: bool = False) -> dict:
        """Convenience: info + topology + node status in one call.

        ``compact`` drops cosmetic noise (empty ``style`` dicts, second console
        fields, qemu_options when empty) and thins each node down to the fields an
        agent usually needs (id/name/type/template/image/ram/status/console +
        an ethernet map of {if -> {name, network_id, suspend}})."""
        topo = self.get_topology()
        return {
            "info": self.get_lab_info(),
            "topology": _compact_topology(topo) if compact else _strip_empty_styles(topo),
            "node_status": self.get_node_status(),
        }

    # -- nodes ----------------------------------------------------------------
    # Fields common to add/edit that the API accepts (see device::editParams and
    # device_qemu::editParams). All optional; only those passed are applied.
    _NODE_FIELDS = (
        "left", "top", "ethernet", "config", "icon", "name",
        "image", "ram", "cpu", "console", "console_2nd", "delay", "serial",
        "qemu_arch", "qemu_nic", "qemu_options", "qemu_version", "qemu_arch",
        "firstmac", "uuid", "pci_mode", "cpulimit", "first_nic",
        "inject_as_first_nic", "config_script", "script_timeout",
        "map_port", "map_port_2nd", "TPM", "UEFI", "size", "mtu",
        "backspace", "terminaltype", "username", "password",
    )

    def add_node(
        self,
        type: str,
        template: str,
        name: str,
        left: int = 100,
        top: int = 100,
        ethernet: int = 1,
        config: str = "Unconfigured",
        icon: str | None = None,
        template_defaults: bool = True,
        **fields: Any,
    ) -> dict:
        """Add a node to the open lab sandbox. Returns the new node object.

        By default the template's built-in defaults are auto-applied (image, ram,
        cpu, qemu_arch/qemu_nic/qemu_options/qemu_version, console, icon,
        config_script, ...) -- the same fields a GUI-created node inherits -- so a
        bare ``add_node("qemu", "mikrotik", "R1")`` yields a fully bootable node
        matching the GUI. Pass ``template_defaults=False`` to skip this (then you
        must supply image/ram/etc. yourself). Any field you pass explicitly takes
        precedence over the template default.

        Console resolution (in order): explicit ``console`` arg -> template's
        console -> ``"telnet"`` for qemu/iol/dynamips (needed for status reporting
        and the console tools) -> unset for vpcs/docker.

        Icon resolution (in order): explicit ``icon`` arg -> template's icon (e.g.
        mikrotik -> ``Router.png``) -> ``"Desktop.png"``. Only an explicit ``icon``
        overrides the template; the old hard-coded ``Desktop.png`` default is gone so
        nodes inherit the right icon automatically.
        """
        body: dict[str, Any] = {
            "type": type,
            "template": template,
            "name": name,
            "left": left,
            "top": top,
            "ethernet": ethernet,
            "config": config,
        }
        if icon is not None:
            body["icon"] = icon
        for k, v in fields.items():
            if v is not None and k in self._NODE_FIELDS:
                body[k] = v
        # Auto-fill from the template (explicit caller fields already in body win).
        if template_defaults:
            for field, val in self._template_defaults(template).items():
                if field not in body:
                    body[field] = val
        # Console fallback: QEMU/IOL/dynamips need telnet for status + console.
        if "console" not in body and type in ("qemu", "iol", "dynamips"):
            body["console"] = "telnet"
        # Icon fallback: template had none, or template_defaults disabled.
        if "icon" not in body:
            body["icon"] = "Desktop.png"
        data = self._api("POST", "/labs/session/nodes/add", body)
        return data.get("update", {}).get("nodes", {})

    def update_node(self, node_id: int, **fields: Any) -> dict:
        """Edit an existing node's fields (``PUT``-style via nodes/edit).

        Only the fields you pass are changed (editParams touches present fields
        only). Image/ram/qemu_* changes take effect on the next (re)start, not on a
        running node. Returns the updated node(s)."""
        if not fields:
            raise PNETLabError(-1, "no fields supplied to update_node", "nodes/edit")
        body: dict[str, Any] = {"id": int(node_id)}
        for k, v in fields.items():
            if v is not None and k in self._NODE_FIELDS:
                body[k] = v
        if len(body) <= 1:
            raise PNETLabError(-1, "no recognised fields supplied to update_node", "nodes/edit")
        data = self._api("POST", "/labs/session/nodes/edit", body)
        return data.get("update", {}).get("nodes", {})

    def delete_node(self, node_id: int) -> dict:
        return self._api("POST", "/labs/session/nodes/delete", {"id": node_id})

    def start_node(self, node_id: int | None = None, check: bool = False) -> dict:
        """Start one node, or all nodes when node_id is None (iterates node ids
        - the API's null-id path is unreliable for "all").

        With ``check=True`` (single node only), poll status for a few seconds after
        starting: if it falls back to 0 the node crashed on boot -- usually a
        missing/invalid ``image`` or too little ``ram`` -- and a diagnostic is
        returned instead of a bare success."""
        if node_id is None:
            return {nid: self._api("POST", "/labs/session/nodes/start", {"id": nid}) for nid in self._node_ids()}
        out = self._api("POST", "/labs/session/nodes/start", {"id": node_id})
        if check:
            diag = self._check_boot(node_id)
            if diag:
                out = {"start": out, "warning": diag}
        return out

    def _check_boot(self, node_id: int) -> str | None:
        """Poll node status briefly; return a diagnostic string if it fails to
        reach running. Status 0 is falsy, so we must avoid ``or`` short-circuits
        when reading it."""
        saw_activity = False  # saw status 1 (building) or 2 (running)
        last = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                st = self.get_node_status()
            except Exception:
                return None
            status = st.get(str(node_id), st.get(node_id))
            if status is None:
                continue
            status = int(status)
            last = status
            if status == 2:
                return None  # running
            if status in (1, 2, 3):  # building / running / locked-running
                saw_activity = True
            elif status == 0 and saw_activity:
                return (
                    "node crashed back to stopped after starting -- boot failed. "
                    "Common causes: image not set or invalid (use list_images), "
                    "ram too low, or wrong qemu_arch/qemu_nic. Fix with update_node "
                    "then restart."
                )
        if not saw_activity:
            return (
                "node never reached building/running within 30s (status stayed 0). "
                "Common causes: image not set/invalid, ram too low, wrong "
                "qemu_arch/qemu_nic, or console type not telnet (status is detected "
                "via the console port). Use list_images + update_node then restart."
            )
        return None

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
            "template": node.get("template"),
            "type": node.get("type"),
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
        """Directly connect two node interfaces with a point-to-point link.

        Returns a compact summary (the new network_id + both endpoints) instead of
        the full topology snapshot the raw endpoint echoes."""
        body = {
            "name": name,
            "src_id": src_id,
            "src_if": src_if,
            "dest_id": dest_id,
            "dest_if": dest_if,
        }
        data = self._api("POST", "/labs/session/networks/p2p", body)
        # The new network_id is the one now wired to the src node's interface.
        network_id = None
        nodes = (data.get("update", {}) or {}).get("nodes", {}) if isinstance(data, dict) else {}
        snode = nodes.get(str(src_id)) or nodes.get(src_id)
        if snode:
            eths = snode.get("ethernets", {}) or {}
            eth = eths.get(str(src_if)) or eths.get(src_if)
            if eth:
                network_id = eth.get("network_id")
        return {
            "network_id": network_id,
            "name": name,
            "src": {"node": src_id, "interface": src_if},
            "dst": {"node": dest_id, "interface": dest_if},
            "message": data.get("message") if isinstance(data, dict) else None,
        }

    def delete_network(self, network_id: int) -> dict:
        return self._api("POST", "/labs/session/networks/delete", {"id": network_id})

    def delete_link(self, network_id: int) -> dict:
        """Delete a p2p link / network. Unlinks both attached interfaces first."""
        return self.delete_network(network_id)

    def _network_interfaces(self, network_id: int) -> list[dict]:
        """Return [{node_id, interface_id, name}] for every interface attached to
        ``network_id`` (a p2p link has exactly two)."""
        nodes = self.get_topology().get("nodes", {})
        found: list[dict] = []
        for nid, node in (nodes or {}).items():
            for ifid, eth in (node.get("ethernets", {}) or {}).items():
                if str(eth.get("network_id")) == str(network_id):
                    found.append(
                        {"node_id": int(nid), "interface_id": int(ifid), "name": eth.get("name")}
                    )
        return found

    def set_link_state(self, network_id: int, up: bool) -> dict:
        """Bring a link up or down at the topology layer (interface suspend).

        PNETLab suspends interfaces, not networks: for a visibility-0 p2p link,
        ``interfaces/setSuspendtwo_way`` suspends both endpoints in one call. Only
        affects running nodes (the suspend is applied live; stored for next boot)."""
        ifs = self._network_interfaces(network_id)
        if not ifs:
            raise PNETLabError(404, f"no interfaces attached to network {network_id}", "interfaces/setSuspend")
        first = ifs[0]
        body = {"node_id": first["node_id"], "interface_id": first["interface_id"], "status": 0 if up else 1}
        self._api("POST", "/labs/session/interfaces/setSuspendtwo_way", body)
        return {
            "network_id": network_id,
            "state": "up" if up else "down",
            "interfaces": ifs,
            "status": "ok",
        }

    def set_link_quality(
        self,
        network_id: int,
        loss: float | None = None,
        delay: float | None = None,
        jitter: float | None = None,
        bandwidth: float | None = None,
    ) -> dict:
        """Inject packet loss / delay / jitter / bandwidth limit on a link.

        Quality is per-interface (per-direction), so it is applied to every
        interface attached to the link. Pass ``None`` to leave a dimension unset.
        Units follow EVE-NG: loss=percent, delay=ms, jitter=ms, bandwidth=kbit/s.
        Only affects running nodes."""
        ifs = self._network_interfaces(network_id)
        if not ifs:
            raise PNETLabError(404, f"no interfaces attached to network {network_id}", "interfaces/setquality")
        params = {"loss": loss, "delay": delay, "jitter": jitter, "bandwidth": bandwidth}
        params = {k: v for k, v in params.items() if v is not None}
        if not params:
            raise PNETLabError(-1, "no quality params supplied (loss/delay/jitter/bandwidth)", "interfaces/setquality")
        for i in ifs:
            body = {"node_id": i["node_id"], "interface_id": i["interface_id"], **params}
            self._api("POST", "/labs/session/interfaces/setquality", body)
        return {
            "network_id": network_id,
            "quality": params,
            "interfaces": ifs,
            "status": "ok",
        }

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

    # -- console interaction --------------------------------------------------
    def _console_session(
        self, node_id: int, username: str = "admin", password: str = ""
    ) -> _ConsoleSession:
        """Get (or create+login) the pooled telnet session for a node."""
        sess = self._consoles.get(node_id)
        if sess is not None and sess.alive:
            return sess
        info = self.node_console(node_id)
        port = info.get("port")
        if not port:
            raise PNETLabError(-1, f"node {node_id} has no console port (start it first)", "console")
        sess = _ConsoleSession(self._console_host, int(port))
        sess.connect()
        sess.login(username=username, password=password)
        self._consoles[node_id] = sess
        return sess

    def console_send(self, node_id: int, text: str, newline: bool = True,
                     username: str = "admin", password: str = "") -> dict:
        """Send raw text to a node's console (auto-opens + logs in on first use).
        Use newline=False to send a partial line. Pair with console_read."""
        sess = self._console_session(node_id, username=username, password=password)
        sess.send(text, newline=newline)
        return {"node_id": node_id, "sent": text, "newline": newline}

    def console_read(self, node_id: int, timeout: float = 5.0,
                     username: str = "admin", password: str = "") -> dict:
        """Read pending output from a node's console until it goes idle."""
        sess = self._console_session(node_id, username=username, password=password)
        return {"node_id": node_id, "output": sess.read(idle=0.4, max_wait=timeout)}

    def run_command(self, node_id: int, command: str, timeout: float = 15.0,
                    wait_for: str | None = None, username: str = "admin",
                    password: str = "") -> dict:
        """Run a single CLI command on a node and return its output.

        Handles telnet IAC negotiation and an interactive login if the device
        presents one (default admin/empty password -- pass username/password for
        devices with different creds). The session is pooled and reused across
        calls; call console_close when done. ``wait_for`` is an optional regex the
        output must contain before returning (useful for pings/monitors that print
        a final line)."""
        sess = self._console_session(node_id, username=username, password=password)
        output = sess.run_command(command, timeout=timeout, wait_for=wait_for)
        return {"node_id": node_id, "command": command, "output": output}

    def console_close(self, node_id: int | None = None) -> dict:
        """Close one node's console session, or all of them when node_id is None."""
        closed: list[int] = []
        if node_id is None:
            for nid, sess in list(self._consoles.items()):
                sess.close()
                closed.append(nid)
            self._consoles.clear()
        else:
            sess = self._consoles.pop(node_id, None)
            if sess is not None:
                sess.close()
                closed.append(node_id)
        return {"closed": closed}


# -- topology compaction helpers -------------------------------------------

_STYLE_KEYS = (
    "style", "linkstyle", "color", "label", "linkcfg", "labelpos", "srcpos", "dstpos", "width", "fontsize",
)


def _is_empty_style(obj: Any) -> bool:
    """True if obj is a style dict whose values are all empty strings."""
    if not isinstance(obj, dict):
        return False
    return all(k in _STYLE_KEYS and v in ("", None, 0, "0") for k, v in obj.items()) and bool(obj)


def _strip_empty_styles(topo: dict) -> dict:
    """Drop all-empty ``style`` dicts from every ethernet (cosmetic noise)."""
    nodes = topo.get("nodes", {})
    for node in (nodes or {}).values():
        for eth in (node.get("ethernets", {}) or {}).values():
            if _is_empty_style(eth.get("style")):
                eth.pop("style", None)
    return topo


_COMPACT_NODE_KEEP = (
    "id", "name", "type", "template", "status", "image", "ram", "cpu",
    "console", "port", "url", "qemu_arch", "qemu_nic", "qemu_version",
)


def _compact_node(node: dict) -> dict:
    out = {k: node[k] for k in _COMPACT_NODE_KEEP if node.get(k) not in (None, "", 0)}
    eths = {}
    for ifid, eth in (node.get("ethernets", {}) or {}).items():
        eths[ifid] = {
            "name": eth.get("name"),
            "network_id": eth.get("network_id"),
            "suspend": eth.get("suspend"),
        }
    if eths:
        out["ethernets"] = eths
    return out


def _compact_topology(topo: dict) -> dict:
    """Thin a topology down to names/types/ethernet-mapping for readability."""
    nodes = {
        nid: _compact_node(n) for nid, n in (topo.get("nodes", {}) or {}).items()
    }
    networks = {
        nid: {k: net.get(k) for k in ("id", "name", "type", "count") if net.get(k) is not None}
        for nid, net in (topo.get("networks", {}) or {}).items()
    }
    out: dict[str, Any] = {"nodes": nodes}
    if networks:
        out["networks"] = networks
    return out
