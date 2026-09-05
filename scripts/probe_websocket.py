"""Find out why the X-Plane WebSocket will not connect.

`create_connection` reports "timed out" for several unrelated causes: nothing
listening, the wrong address family, a path the server ignores, or a handshake
the server never answers. This performs the handshake by hand and prints
whatever actually comes back, which distinguishes them.

    .venv\\Scripts\\python.exe scripts\\probe_websocket.py

X-Plane must be running.
"""

from __future__ import annotations

import base64
import os
import socket
import sys
from urllib.parse import urlparse

HOST_CANDIDATES = ("localhost", "127.0.0.1", "::1")
PATH_CANDIDATES = ("/api/v3", "/api/v2", "/api/v1", "/api")
PORT = int(os.environ.get("XP_WEB_PORT", "8086"))
READ_TIMEOUT = 5.0


def show_resolution(host: str) -> list[tuple]:
    try:
        infos = socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        print(f"  {host:12s} -> DNS failure: {exc}")
        return []
    seen = []
    for family, _type, _proto, _canon, addr in infos:
        name = {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}.get(family, str(family))
        if (family, addr) not in seen:
            seen.append((family, addr))
            print(f"  {host:12s} -> {name:4s} {addr[0]}")
    return infos


def tcp_probe(host: str) -> bool:
    try:
        with socket.create_connection((host, PORT), timeout=2.0):
            print(f"  {host:12s} -> TCP connect OK")
            return True
    except OSError as exc:
        print(f"  {host:12s} -> TCP connect failed: {exc}")
        return False


def handshake(host: str, path: str) -> str:
    """Send a real RFC 6455 upgrade and report the server's answer."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: http://localhost\r\n"
        "\r\n"
    )
    try:
        sock = socket.create_connection((host, PORT), timeout=2.0)
    except OSError as exc:
        return f"connect failed: {exc}"
    try:
        sock.settimeout(READ_TIMEOUT)
        sock.sendall(request.encode())
        chunks = b""
        while b"\r\n\r\n" not in chunks:
            block = sock.recv(4096)
            if not block:
                return "server closed the connection without replying"
            chunks += block
            if len(chunks) > 65536:
                break
        head = chunks.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.splitlines()
        status = lines[0] if lines else "(empty)"
        interesting = [
            line for line in lines[1:]
            if line.split(":", 1)[0].strip().lower()
            in {"upgrade", "connection", "sec-websocket-accept", "content-type", "content-length"}
        ]
        detail = "; ".join(interesting)
        return f"{status}" + (f"  [{detail}]" if detail else "")
    except socket.timeout:
        return f"no reply within {READ_TIMEOUT:.0f}s (connected, but the server never answered)"
    except OSError as exc:
        return f"error: {exc}"
    finally:
        sock.close()


def main() -> int:
    print(f"Probing the X-Plane web server on port {PORT}\n")

    proxy = {k: v for k, v in os.environ.items()
             if k.lower() in {"http_proxy", "https_proxy", "ws_proxy", "no_proxy", "all_proxy"}}
    if proxy:
        print("Proxy environment variables are set. websocket-client honours these,")
        print("which can silently redirect or stall a localhost connection:")
        for k, v in proxy.items():
            print(f"  {k}={v}")
        print()

    print("Name resolution:")
    for host in ("localhost",):
        show_resolution(host)
    print()

    print("TCP reachability:")
    reachable = [h for h in HOST_CANDIDATES if tcp_probe(h)]
    print()

    if not reachable:
        print("Nothing is listening. Is X-Plane running, and is the web server enabled?")
        return 1

    if "localhost" in reachable and "127.0.0.1" in reachable:
        pass
    elif "127.0.0.1" in reachable and "localhost" not in reachable:
        print("NOTE: 127.0.0.1 is reachable but 'localhost' is not.")
        print("      X-Plane binds IPv4 loopback only; set publish.base_url to")
        print("      http://127.0.0.1:8086 rather than http://localhost:8086.\n")

    print("WebSocket handshake (raw, so the real HTTP response is visible):")
    for host in reachable:
        for path in PATH_CANDIDATES:
            print(f"  ws://{host}:{PORT}{path}")
            print(f"      {handshake(host, path)}")
    print()

    try:
        from websocket import create_connection
    except ImportError:
        print("websocket-client is not installed; skipping its own attempt.")
        return 0

    print("websocket-client:")
    for host in reachable:
        url = f"ws://{host}:{PORT}/api/v3"
        try:
            conn = create_connection(url, timeout=5.0)
            conn.close()
            print(f"  {url} -> connected")
        except Exception as exc:  # noqa: BLE001
            print(f"  {url} -> {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
