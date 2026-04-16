from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
import uvicorn
import threading
from fastmcp import FastMCP
import httpx
import os
import ssl
from typing import Optional

mcp = FastMCP("go-proxmox")

# In-memory state for connection configuration
_connection_config: dict = {}
_mock_interceptors_active: bool = False
_mock_version: str = ""


@mcp.tool()
async def connect_proxmox(
    uri: str,
    username: str,
    password: str,
    tls_insecure: bool = False
) -> dict:
    """
    Initialize and configure a connection to a Proxmox VE server.
    Use this first to establish the client with the server URI and credentials
    before performing any operations. Supports Proxmox VE versions 6.x through 9.x.
    """
    global _connection_config

    # Store connection config
    _connection_config = {
        "uri": uri,
        "username": username,
        "tls_insecure": tls_insecure
    }

    # Build the API ticket endpoint
    ticket_url = f"{uri.rstrip('/')}/api2/json/access/ticket"

    try:
        async with httpx.AsyncClient(verify=not tls_insecure, timeout=30.0) as client:
            response = await client.post(
                ticket_url,
                data={
                    "username": username,
                    "password": password
                }
            )

            if response.status_code == 200:
                data = response.json()
                ticket = data.get("data", {}).get("ticket", "")
                csrf_token = data.get("data", {}).get("CSRFPreventionToken", "")

                # Store auth tokens
                _connection_config["ticket"] = ticket
                _connection_config["csrf_token"] = csrf_token

                return {
                    "status": "connected",
                    "uri": uri,
                    "username": username,
                    "tls_insecure": tls_insecure,
                    "authenticated": True,
                    "message": f"Successfully connected to Proxmox VE at {uri}"
                }
            else:
                return {
                    "status": "error",
                    "uri": uri,
                    "username": username,
                    "authenticated": False,
                    "http_status": response.status_code,
                    "message": f"Authentication failed with HTTP {response.status_code}"
                }
    except httpx.ConnectError as e:
        _connection_config = {
            "uri": uri,
            "username": username,
            "tls_insecure": tls_insecure,
            "configured": True
        }
        return {
            "status": "configured",
            "uri": uri,
            "username": username,
            "tls_insecure": tls_insecure,
            "authenticated": False,
            "message": f"Connection configured but server unreachable: {str(e)}. Configuration saved for mock/test use."
        }
    except Exception as e:
        return {
            "status": "error",
            "uri": uri,
            "username": username,
            "authenticated": False,
            "message": f"Failed to connect: {str(e)}"
        }


@mcp.tool()
async def enable_mock_interceptors(
    uri: str,
    version: str = "9x"
) -> dict:
    """
    Enable HTTP mock interceptors for testing against a specific Proxmox VE version
    without a real server. Use this in test/development environments to simulate API
    responses. Supports versions 6.x, 7.x, 8.x, and 9.x.
    """
    global _mock_interceptors_active, _mock_version, _connection_config

    valid_versions = ["6x", "7x", "8x", "9x"]
    if version not in valid_versions:
        return {
            "status": "error",
            "message": f"Invalid version '{version}'. Valid values are: {', '.join(valid_versions)}"
        }

    _mock_interceptors_active = True
    _mock_version = version
    _connection_config["uri"] = uri
    _connection_config["mock"] = True

    version_map = {
        "6x": "Proxmox VE 6.x",
        "7x": "Proxmox VE 7.x",
        "8x": "Proxmox VE 8.x",
        "9x": "Proxmox VE 9.x"
    }

    return {
        "status": "enabled",
        "uri": uri,
        "version": version,
        "version_label": version_map[version],
        "mock_active": True,
        "message": f"Mock interceptors enabled for {version_map[version]} at {uri}. API calls will be simulated."
    }


@mcp.tool()
async def disable_mock_interceptors() -> dict:
    """
    Disable and remove all active HTTP mock interceptors. Use this after testing
    is complete to restore normal HTTP behavior and allow real API calls to proceed.
    """
    global _mock_interceptors_active, _mock_version

    was_active = _mock_interceptors_active
    previous_version = _mock_version

    _mock_interceptors_active = False
    _mock_version = ""

    if _connection_config.get("mock"):
        _connection_config.pop("mock", None)

    if was_active:
        return {
            "status": "disabled",
            "mock_active": False,
            "previous_version": previous_version,
            "message": f"Mock interceptors for version '{previous_version}' have been disabled. Real HTTP requests will now proceed normally."
        }
    else:
        return {
            "status": "already_disabled",
            "mock_active": False,
            "message": "No active mock interceptors were found. Nothing to disable."
        }


@mcp.tool()
async def open_terminal_session(
    node: str,
    vmid: int,
    vm_type: str = "qemu"
) -> dict:
    """
    Open a WebSocket-based terminal (xterm) session to a Proxmox node or VM.
    Use this when you need interactive shell access to a virtual machine or
    container over a secure WebSocket connection.
    """
    if not _connection_config.get("uri"):
        return {
            "status": "error",
            "message": "No Proxmox connection configured. Call connect_proxmox first."
        }

    uri = _connection_config["uri"].rstrip("/")
    ticket = _connection_config.get("ticket", "")
    csrf_token = _connection_config.get("csrf_token", "")
    tls_insecure = _connection_config.get("tls_insecure", False)

    if vm_type not in ["qemu", "lxc"]:
        return {
            "status": "error",
            "message": f"Invalid vm_type '{vm_type}'. Must be 'qemu' or 'lxc'."
        }

    # Build the terminal proxy URL
    if vm_type == "qemu":
        term_url = f"{uri}/api2/json/nodes/{node}/qemu/{vmid}/terminal-proxy"
    else:
        term_url = f"{uri}/api2/json/nodes/{node}/lxc/{vmid}/terminal-proxy"

    # If mock mode is active, return simulated response
    if _mock_interceptors_active:
        ws_url = f"wss://{uri.replace('https://', '').replace('http://', '')}/term?node={node}&vmid={vmid}&vmtype={vm_type}"
        return {
            "status": "success",
            "mock": True,
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "websocket_url": ws_url,
            "proxy_url": term_url,
            "ticket": "MOCK_TERMINAL_TICKET",
            "port": 5900,
            "message": f"Mock terminal session opened for {vm_type} VM {vmid} on node {node}"
        }

    if not ticket:
        return {
            "status": "error",
            "message": "Not authenticated. Please call connect_proxmox with valid credentials first."
        }

    try:
        async with httpx.AsyncClient(verify=not tls_insecure, timeout=30.0) as client:
            response = await client.post(
                term_url,
                headers={
                    "CSRFPreventionToken": csrf_token,
                    "Cookie": f"PVEAuthCookie={ticket}"
                }
            )

            if response.status_code == 200:
                data = response.json().get("data", {})
                port = data.get("port", 5900)
                term_ticket = data.get("ticket", "")
                upid = data.get("upid", "")

                # Build WebSocket URL
                ws_host = uri.replace("https://", "").replace("http://", "")
                ws_url = f"wss://{ws_host}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket?port={port}&vncticket={term_ticket}"

                return {
                    "status": "success",
                    "node": node,
                    "vmid": vmid,
                    "vm_type": vm_type,
                    "websocket_url": ws_url,
                    "proxy_url": term_url,
                    "ticket": term_ticket,
                    "port": port,
                    "upid": upid,
                    "message": f"Terminal session opened for {vm_type} VM {vmid} on node {node}. Connect via WebSocket: {ws_url}"
                }
            else:
                return {
                    "status": "error",
                    "http_status": response.status_code,
                    "message": f"Failed to open terminal session: HTTP {response.status_code} - {response.text}"
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error opening terminal session: {str(e)}"
        }


@mcp.tool()
async def open_vnc_session(
    node: str,
    vmid: int,
    vm_type: str = "qemu"
) -> dict:
    """
    Open a WebSocket-based VNC graphical console session to a Proxmox VM.
    Use this when you need graphical desktop access to a virtual machine.
    Returns a VNC proxy WebSocket connection.
    """
    if not _connection_config.get("uri"):
        return {
            "status": "error",
            "message": "No Proxmox connection configured. Call connect_proxmox first."
        }

    uri = _connection_config["uri"].rstrip("/")
    ticket = _connection_config.get("ticket", "")
    csrf_token = _connection_config.get("csrf_token", "")
    tls_insecure = _connection_config.get("tls_insecure", False)

    if vm_type not in ["qemu", "lxc"]:
        return {
            "status": "error",
            "message": f"Invalid vm_type '{vm_type}'. Must be 'qemu' or 'lxc'."
        }

    # Build the VNC proxy URL
    if vm_type == "qemu":
        vnc_url = f"{uri}/api2/json/nodes/{node}/qemu/{vmid}/vncproxy"
    else:
        vnc_url = f"{uri}/api2/json/nodes/{node}/lxc/{vmid}/vncproxy"

    # If mock mode is active, return simulated response
    if _mock_interceptors_active:
        ws_host = uri.replace("https://", "").replace("http://", "")
        ws_url = f"wss://{ws_host}/vnc?node={node}&vmid={vmid}&vmtype={vm_type}"
        return {
            "status": "success",
            "mock": True,
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "websocket_url": ws_url,
            "proxy_url": vnc_url,
            "ticket": "MOCK_VNC_TICKET",
            "port": 5900,
            "cert": "MOCK_CERT",
            "message": f"Mock VNC session opened for {vm_type} VM {vmid} on node {node}"
        }

    if not ticket:
        return {
            "status": "error",
            "message": "Not authenticated. Please call connect_proxmox with valid credentials first."
        }

    try:
        async with httpx.AsyncClient(verify=not tls_insecure, timeout=30.0) as client:
            response = await client.post(
                vnc_url,
                headers={
                    "CSRFPreventionToken": csrf_token,
                    "Cookie": f"PVEAuthCookie={ticket}"
                },
                data={"websocket": 1}
            )

            if response.status_code == 200:
                data = response.json().get("data", {})
                port = data.get("port", 5900)
                vnc_ticket = data.get("ticket", "")
                cert = data.get("cert", "")
                upid = data.get("upid", "")

                ws_host = uri.replace("https://", "").replace("http://", "")
                ws_url = f"wss://{ws_host}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket?port={port}&vncticket={vnc_ticket}"

                return {
                    "status": "success",
                    "node": node,
                    "vmid": vmid,
                    "vm_type": vm_type,
                    "websocket_url": ws_url,
                    "proxy_url": vnc_url,
                    "ticket": vnc_ticket,
                    "port": port,
                    "cert": cert,
                    "upid": upid,
                    "message": f"VNC session opened for {vm_type} VM {vmid} on node {node}. Connect via WebSocket: {ws_url}"
                }
            else:
                return {
                    "status": "error",
                    "http_status": response.status_code,
                    "message": f"Failed to open VNC session: HTTP {response.status_code} - {response.text}"
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error opening VNC session: {str(e)}"
        }


@mcp.tool()
async def get_vnc_ticket(
    node: str,
    vmid: int,
    vm_type: str = "qemu"
) -> dict:
    """
    Retrieve a one-time VNC authentication ticket for a Proxmox VM.
    Use this before establishing a VNC connection to obtain the temporary ticket
    required for authentication with the VNC proxy.
    """
    if not _connection_config.get("uri"):
        return {
            "status": "error",
            "message": "No Proxmox connection configured. Call connect_proxmox first."
        }

    uri = _connection_config["uri"].rstrip("/")
    ticket = _connection_config.get("ticket", "")
    csrf_token = _connection_config.get("csrf_token", "")
    tls_insecure = _connection_config.get("tls_insecure", False)

    if vm_type not in ["qemu", "lxc"]:
        return {
            "status": "error",
            "message": f"Invalid vm_type '{vm_type}'. Must be 'qemu' or 'lxc'."
        }

    # If mock mode is active, return simulated ticket
    if _mock_interceptors_active:
        return {
            "status": "success",
            "mock": True,
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "ticket": f"MOCK_VNC_TICKET_{node}_{vmid}",
            "port": 5900,
            "cert": "MOCK_CERTIFICATE_DATA",
            "message": f"Mock VNC ticket generated for {vm_type} VM {vmid} on node {node}"
        }

    if not ticket:
        return {
            "status": "error",
            "message": "Not authenticated. Please call connect_proxmox with valid credentials first."
        }

    # Build the VNC proxy URL to get a ticket
    if vm_type == "qemu":
        vnc_url = f"{uri}/api2/json/nodes/{node}/qemu/{vmid}/vncproxy"
    else:
        vnc_url = f"{uri}/api2/json/nodes/{node}/lxc/{vmid}/vncproxy"

    try:
        async with httpx.AsyncClient(verify=not tls_insecure, timeout=30.0) as client:
            response = await client.post(
                vnc_url,
                headers={
                    "CSRFPreventionToken": csrf_token,
                    "Cookie": f"PVEAuthCookie={ticket}"
                },
                data={"websocket": 1, "generate-password": 1}
            )

            if response.status_code == 200:
                data = response.json().get("data", {})
                vnc_ticket = data.get("ticket", "")
                port = data.get("port", 5900)
                cert = data.get("cert", "")

                return {
                    "status": "success",
                    "node": node,
                    "vmid": vmid,
                    "vm_type": vm_type,
                    "ticket": vnc_ticket,
                    "port": port,
                    "cert": cert,
                    "message": f"VNC ticket retrieved for {vm_type} VM {vmid} on node {node}. Use this ticket to authenticate your VNC connection."
                }
            else:
                return {
                    "status": "error",
                    "http_status": response.status_code,
                    "message": f"Failed to get VNC ticket: HTTP {response.status_code} - {response.text}"
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error retrieving VNC ticket: {str(e)}"
        }


@mcp.tool()
async def start_console_server(
    proxmox_uri: str,
    port: int = 8523,
    cert_file: str = "server.crt",
    key_file: str = "server.key"
) -> dict:
    """
    Start the local HTTPS proxy server that bridges WebSocket terminal and VNC connections
    between a browser client and the Proxmox API. Use this to set up the term-and-vnc
    example server with TLS, required before opening terminal or VNC sessions via the web interface.
    """
    # Validate the proxmox_uri
    if not proxmox_uri:
        proxmox_uri = os.environ.get("PROXMOX_URI", "")
        if not proxmox_uri:
            return {
                "status": "error",
                "message": "proxmox_uri is required. Provide it directly or set the PROXMOX_URI environment variable."
            }

    # Check if port is valid
    if not (1 <= port <= 65535):
        return {
            "status": "error",
            "message": f"Invalid port {port}. Must be between 1 and 65535."
        }

    # Check cert and key files existence
    cert_exists = os.path.isfile(cert_file)
    key_exists = os.path.isfile(key_file)

    server_config = {
        "status": "configured",
        "proxmox_uri": proxmox_uri,
        "server_port": port,
        "cert_file": cert_file,
        "key_file": key_file,
        "cert_file_found": cert_exists,
        "key_file_found": key_exists,
        "routes": [
            {"method": "GET", "path": "/", "description": "Health check - returns hello world"},
            {"method": "GET", "path": "/term", "description": "WebSocket terminal (xterm) handler"},
            {"method": "GET", "path": "/vnc", "description": "WebSocket VNC console handler"},
            {"method": "GET", "path": "/vnc-ticket", "description": "VNC ticket retrieval endpoint"}
        ],
        "server_url": f"https://localhost:{port}",
        "term_endpoint": f"https://localhost:{port}/term",
        "vnc_endpoint": f"https://localhost:{port}/vnc",
        "vnc_ticket_endpoint": f"https://localhost:{port}/vnc-ticket"
    }

    if not cert_exists or not key_exists:
        missing = []
        if not cert_exists:
            missing.append(f"cert file '{cert_file}'")
        if not key_exists:
            missing.append(f"key file '{key_file}'")
        server_config["status"] = "warning"
        server_config["message"] = (
            f"Console server configuration ready for Proxmox at {proxmox_uri} on port {port}, "
            f"but TLS files are missing: {', '.join(missing)}. "
            f"Generate them with: openssl req -x509 -newkey rsa:4096 -keyout {key_file} -out {cert_file} -days 365 -nodes"
        )
    else:
        server_config["message"] = (
            f"Console proxy server is configured to run on port {port} with TLS, "
            f"proxying to Proxmox VE at {proxmox_uri}. "
            f"The server bridges browser WebSocket connections to the Proxmox API for terminal and VNC sessions."
        )

    return server_config




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

mcp_app = mcp.http_app(transport="streamable-http")

class _FixAcceptHeader:
    """Ensure Accept header includes both types FastMCP requires."""
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            accept = headers.get(b"accept", b"").decode()
            if "text/event-stream" not in accept:
                new_headers = [(k, v) for k, v in scope["headers"] if k != b"accept"]
                new_headers.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope, headers=new_headers)
        await self.app(scope, receive, send)

app = _FixAcceptHeader(Starlette(
    routes=[
        Route("/health", health),
        Route("/tools", tools),
        Mount("/", mcp_app),
    ],
    lifespan=mcp_app.lifespan,
))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
