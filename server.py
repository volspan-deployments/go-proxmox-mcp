from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
import uvicorn
import threading
from fastmcp import FastMCP
import httpx
import os
from typing import Optional, List, Dict, Any

mcp = FastMCP("go-proxmox")

# In-memory state for the connection configuration
_connection_state: Dict[str, Any] = {
    "uri": None,
    "token_id": None,
    "token_secret": None,
    "mock_mode": False,
    "mock_version": None,
}

# Mock data store for simulated responses
_mock_responses: Dict[str, Any] = {}


def _get_headers() -> Dict[str, str]:
    """Build authentication headers from current connection state."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    token_id = _connection_state.get("token_id")
    token_secret = _connection_state.get("token_secret")
    if token_id and token_secret:
        headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
    return headers


def _get_base_url() -> Optional[str]:
    """Return the base API URL."""
    uri = _connection_state.get("uri")
    if not uri:
        return None
    return uri.rstrip("/") + "/api2/json"


def _build_mock_data(version: str, uri: str) -> Dict[str, Any]:
    """Build mock response data for a given PVE version."""
    return {
        "version": version,
        "uri": uri,
        "/nodes": {
            "data": [
                {
                    "node": "pve",
                    "status": "online",
                    "cpu": 0.05,
                    "maxcpu": 4,
                    "mem": 2147483648,
                    "maxmem": 8589934592,
                    "disk": 10737418240,
                    "maxdisk": 107374182400,
                    "uptime": 86400,
                    "type": "node",
                    "id": "node/pve",
                }
            ]
        },
        "/version": {
            "data": {
                "version": version.replace("x", ".0"),
                "release": version,
                "repoid": "mock",
            }
        },
        "/cluster/status": {
            "data": [
                {
                    "type": "cluster",
                    "name": "mock-cluster",
                    "nodes": 1,
                    "quorate": 1,
                    "version": 2,
                    "id": "cluster",
                }
            ]
        },
    }


@mcp.tool()
async def connect_proxmox(
    uri: str,
    token_id: Optional[str] = None,
    token_secret: Optional[str] = None,
) -> dict:
    """
    Initialize and configure a connection to a Proxmox VE server.
    Use this first to set up the client with the target Proxmox host URI
    before performing any operations. This establishes the base configuration
    used by all subsequent API calls.
    """
    _connection_state["uri"] = uri.rstrip("/")
    _connection_state["token_id"] = token_id
    _connection_state["token_secret"] = token_secret

    result: Dict[str, Any] = {
        "status": "configured",
        "uri": _connection_state["uri"],
        "auth_method": "api_token" if (token_id and token_secret) else "none",
        "message": f"Connection configured for {uri}",
    }

    # Try to verify connectivity if not in mock mode
    if not _connection_state.get("mock_mode"):
        base_url = _get_base_url()
        if base_url:
            try:
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    response = await client.get(
                        f"{base_url}/version",
                        headers=_get_headers(),
                    )
                    if response.status_code == 200:
                        data = response.json()
                        result["connection_test"] = "success"
                        result["server_version"] = data.get("data", {})
                    else:
                        result["connection_test"] = "failed"
                        result["http_status"] = response.status_code
            except Exception as e:
                result["connection_test"] = "error"
                result["error"] = str(e)
    else:
        result["connection_test"] = "skipped (mock mode active)"

    return result


@mcp.tool()
async def open_terminal_session(
    node: str,
    vm_id: Optional[int] = None,
    vm_type: str = "qemu",
) -> dict:
    """
    Open a WebSocket terminal (shell) session to a Proxmox VE node or VM.
    Use this when you need interactive terminal access to a node or virtual
    machine via the Proxmox terminal WebSocket endpoint.
    """
    uri = _connection_state.get("uri")
    if not uri:
        return {
            "status": "error",
            "message": "Not connected. Please call connect_proxmox first.",
        }

    if vm_id is not None:
        # VM-level terminal
        if vm_type == "lxc":
            api_path = f"/nodes/{node}/lxc/{vm_id}/terminal"
            ws_path = f"/nodes/{node}/lxc/{vm_id}/vncwebsocket"
        else:
            api_path = f"/nodes/{node}/qemu/{vm_id}/terminal"
            ws_path = f"/nodes/{node}/qemu/{vm_id}/vncwebsocket"
        target = f"VM {vm_id} ({vm_type}) on node {node}"
    else:
        # Node-level shell
        api_path = f"/nodes/{node}/termproxy"
        ws_path = f"/nodes/{node}/vncwebsocket"
        target = f"node {node}"

    base_url = _get_base_url()
    result: Dict[str, Any] = {
        "status": "initiated",
        "target": target,
        "node": node,
        "vm_id": vm_id,
        "vm_type": vm_type,
        "api_path": api_path,
        "websocket_path": ws_path,
        "websocket_url": f"{uri}{ws_path}",
    }

    if _connection_state.get("mock_mode"):
        result["ticket"] = "mock-terminal-ticket-abc123"
        result["port"] = 5900
        result["upid"] = "UPID:pve:mock:terminal:mock"
        result["message"] = f"Mock terminal session for {target}"
        return result

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{base_url}{api_path}",
                headers=_get_headers(),
            )
            if response.status_code in (200, 201):
                data = response.json()
                result["ticket"] = data.get("data", {}).get("ticket")
                result["port"] = data.get("data", {}).get("port")
                result["upid"] = data.get("data", {}).get("upid")
                result["message"] = f"Terminal session opened for {target}"
            else:
                result["status"] = "error"
                result["http_status"] = response.status_code
                result["message"] = response.text
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


@mcp.tool()
async def open_vnc_session(
    node: str,
    vm_id: int,
    vm_type: str = "qemu",
) -> dict:
    """
    Open a VNC WebSocket connection to a Proxmox VE virtual machine for
    graphical console access. Use this when you need to access the graphical
    display of a VM running on Proxmox.
    """
    uri = _connection_state.get("uri")
    if not uri:
        return {
            "status": "error",
            "message": "Not connected. Please call connect_proxmox first.",
        }

    if vm_type == "lxc":
        api_path = f"/nodes/{node}/lxc/{vm_id}/vncproxy"
        ws_path = f"/nodes/{node}/lxc/{vm_id}/vncwebsocket"
    else:
        api_path = f"/nodes/{node}/qemu/{vm_id}/vncproxy"
        ws_path = f"/nodes/{node}/qemu/{vm_id}/vncwebsocket"

    base_url = _get_base_url()
    result: Dict[str, Any] = {
        "status": "initiated",
        "node": node,
        "vm_id": vm_id,
        "vm_type": vm_type,
        "api_path": api_path,
        "websocket_path": ws_path,
        "websocket_url": f"{uri}{ws_path}",
    }

    if _connection_state.get("mock_mode"):
        result["ticket"] = "mock-vnc-ticket-xyz789"
        result["port"] = 5901
        result["cert"] = "mock-cert-fingerprint"
        result["message"] = f"Mock VNC session for VM {vm_id} ({vm_type}) on node {node}"
        return result

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{base_url}{api_path}",
                headers=_get_headers(),
                json={"websocket": 1},
            )
            if response.status_code in (200, 201):
                data = response.json()
                vnc_data = data.get("data", {})
                result["ticket"] = vnc_data.get("ticket")
                result["port"] = vnc_data.get("port")
                result["cert"] = vnc_data.get("cert")
                result["upid"] = vnc_data.get("upid")
                # Build full websocket URL with ticket
                ticket = vnc_data.get("ticket", "")
                port = vnc_data.get("port", 5900)
                result["vnc_websocket_url"] = (
                    f"{uri}{ws_path}?port={port}&vncticket={ticket}"
                )
                result["message"] = f"VNC session opened for VM {vm_id} on node {node}"
            else:
                result["status"] = "error"
                result["http_status"] = response.status_code
                result["message"] = response.text
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


@mcp.tool()
async def get_vnc_ticket(
    node: str,
    vm_id: int,
    vm_type: str = "qemu",
) -> dict:
    """
    Retrieve a VNC ticket for a Proxmox VM to use with a VNC client for
    authentication. Use this before establishing a VNC connection when a
    short-lived authentication ticket is required.
    """
    uri = _connection_state.get("uri")
    if not uri:
        return {
            "status": "error",
            "message": "Not connected. Please call connect_proxmox first.",
        }

    if vm_type == "lxc":
        api_path = f"/nodes/{node}/lxc/{vm_id}/vncproxy"
    else:
        api_path = f"/nodes/{node}/qemu/{vm_id}/vncproxy"

    base_url = _get_base_url()
    result: Dict[str, Any] = {
        "status": "pending",
        "node": node,
        "vm_id": vm_id,
        "vm_type": vm_type,
        "api_path": api_path,
    }

    if _connection_state.get("mock_mode"):
        result["status"] = "success"
        result["ticket"] = "PVE:mock@pam:mock-ticket-token"
        result["port"] = 5900
        result["cert"] = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
        result["message"] = f"Mock VNC ticket for VM {vm_id} ({vm_type}) on node {node}"
        result["usage"] = {
            "vnc_host": uri.replace("https://", "").replace("http://", "").split(":")[0],
            "vnc_port": 5900,
            "vnc_password": result["ticket"],
        }
        return result

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{base_url}{api_path}",
                headers=_get_headers(),
                json={"websocket": 0},
            )
            if response.status_code in (200, 201):
                data = response.json()
                vnc_data = data.get("data", {})
                ticket = vnc_data.get("ticket", "")
                port = vnc_data.get("port", 5900)
                host = uri.replace("https://", "").replace("http://", "").split(":")[0]
                result["status"] = "success"
                result["ticket"] = ticket
                result["port"] = port
                result["cert"] = vnc_data.get("cert")
                result["message"] = f"VNC ticket obtained for VM {vm_id} on node {node}"
                result["usage"] = {
                    "vnc_host": host,
                    "vnc_port": port,
                    "vnc_password": ticket,
                    "hint": "Use this ticket as the VNC password. Tickets are short-lived.",
                }
            else:
                result["status"] = "error"
                result["http_status"] = response.status_code
                result["message"] = response.text
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


@mcp.tool()
async def enable_mock_mode(
    uri: str,
    pve_version: str = "9x",
) -> dict:
    """
    Enable HTTP mock interception for testing against a simulated Proxmox VE
    environment without a real server. Use this during development or testing
    to load mock responses for a specific Proxmox VE version. Useful for
    unit tests and CI pipelines.
    """
    valid_versions = ["6x", "7x", "8x", "9x"]
    if pve_version not in valid_versions:
        return {
            "status": "error",
            "message": f"Invalid pve_version '{pve_version}'. Must be one of: {valid_versions}",
        }

    _connection_state["uri"] = uri.rstrip("/")
    _connection_state["mock_mode"] = True
    _connection_state["mock_version"] = pve_version
    _mock_responses.clear()
    _mock_responses.update(_build_mock_data(pve_version, uri))

    return {
        "status": "enabled",
        "mock_mode": True,
        "pve_version": pve_version,
        "uri": uri,
        "message": f"Mock mode enabled for Proxmox VE {pve_version} at {uri}",
        "available_mock_endpoints": list(_mock_responses.keys()),
        "note": "All API calls will return simulated responses. No real server is contacted.",
    }


@mcp.tool()
async def disable_mock_mode() -> dict:
    """
    Disable all active HTTP mock interceptors and restore real HTTP communication.
    Use this after finishing tests that used mock mode to ensure subsequent API
    calls go to the real Proxmox server.
    """
    was_active = _connection_state.get("mock_mode", False)
    prev_version = _connection_state.get("mock_version")

    _connection_state["mock_mode"] = False
    _connection_state["mock_version"] = None
    _mock_responses.clear()

    return {
        "status": "disabled",
        "mock_mode": False,
        "was_active": was_active,
        "previous_version": prev_version,
        "message": "Mock mode disabled. Subsequent API calls will target the real Proxmox server.",
    }


@mcp.tool()
async def query_proxmox_api(
    method: str,
    endpoint: str,
    params: Optional[List[Dict[str, str]]] = None,
) -> dict:
    """
    Execute a raw API call against the Proxmox VE /api2/json REST API.
    Use this for any Proxmox operation not covered by dedicated tools, such as
    querying nodes, VMs, storage, tasks, or cluster status. Supports GET, POST,
    PUT, and DELETE methods.
    """
    uri = _connection_state.get("uri")
    if not uri:
        return {
            "status": "error",
            "message": "Not connected. Please call connect_proxmox first.",
        }

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        return {
            "status": "error",
            "message": f"Invalid HTTP method '{method}'. Must be GET, POST, PUT, or DELETE.",
        }

    # Normalize endpoint
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    # Convert params list to dict
    params_dict: Dict[str, str] = {}
    if params:
        for item in params:
            if isinstance(item, dict) and "key" in item and "value" in item:
                params_dict[str(item["key"])] = str(item["value"])

    # Handle mock mode
    if _connection_state.get("mock_mode"):
        mock_key = endpoint
        if mock_key in _mock_responses:
            return {
                "status": "success",
                "method": method,
                "endpoint": endpoint,
                "mock_mode": True,
                "data": _mock_responses[mock_key],
            }
        else:
            return {
                "status": "success",
                "method": method,
                "endpoint": endpoint,
                "mock_mode": True,
                "data": {"data": [], "message": f"Mock response for {endpoint} (no specific mock data)"},
            }

    base_url = _get_base_url()
    full_url = f"{base_url}{endpoint}"

    result: Dict[str, Any] = {
        "method": method,
        "endpoint": endpoint,
        "url": full_url,
        "params": params_dict,
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            headers = _get_headers()

            if method == "GET":
                response = await client.get(
                    full_url,
                    headers=headers,
                    params=params_dict if params_dict else None,
                )
            elif method == "POST":
                response = await client.post(
                    full_url,
                    headers=headers,
                    json=params_dict if params_dict else None,
                )
            elif method == "PUT":
                response = await client.put(
                    full_url,
                    headers=headers,
                    json=params_dict if params_dict else None,
                )
            elif method == "DELETE":
                response = await client.delete(
                    full_url,
                    headers=headers,
                    params=params_dict if params_dict else None,
                )

            result["http_status"] = response.status_code
            result["headers"] = dict(response.headers)

            try:
                result["data"] = response.json()
                result["status"] = "success" if response.status_code < 400 else "error"
            except Exception:
                result["data"] = response.text
                result["status"] = "success" if response.status_code < 400 else "error"

    except httpx.ConnectError as e:
        result["status"] = "error"
        result["error"] = f"Connection error: {str(e)}"
    except httpx.TimeoutException as e:
        result["status"] = "error"
        result["error"] = f"Request timed out: {str(e)}"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result




_SERVER_SLUG = "go-proxmox"

def _track(tool_name: str, ua: str = ""):
    try:
        import urllib.request, json as _json
        data = _json.dumps({"slug": _SERVER_SLUG, "event": "tool_call", "tool": tool_name, "user_agent": ua}).encode()
        req = urllib.request.Request("https://www.volspan.dev/api/analytics/event", data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass

async def health(request):
    return JSONResponse({"status": "ok", "server": mcp.name})

async def tools(request):
    registered = await mcp.list_tools()
    tool_list = [{"name": t.name, "description": t.description or ""} for t in registered]
    return JSONResponse({"tools": tool_list, "count": len(tool_list)})

sse_app = mcp.http_app(transport="sse")

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/tools", tools),
        Mount("/", sse_app),
    ],
    lifespan=sse_app.lifespan,
)
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
