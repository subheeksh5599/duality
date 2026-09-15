"""Shared fixtures.

Two rules this suite holds to, because they are what make it evidence rather than
decoration:

  * no fixtures are invented. The chain reads hit the deployment recorded in
    `deployments/`, and the HTTP tests drive the real service object.
  * when configuration is absent the suite SKIPS the live checks and says so. A
    skipped live check is honest; a mocked one that passes is not.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


@pytest.fixture(scope="session")
def env() -> dict:
    import keeperhub_release as K

    e = K.load_env()
    if not e.get("RPC_URL"):
        pytest.skip("no RPC_URL configured: live checks are skipped rather than faked")
    return e


@pytest.fixture(scope="session")
def svc(env):
    import duality_service as S

    S.STATE = S.State(env)
    return S


@pytest.fixture(scope="session")
def base_url(svc):
    server = ThreadingHTTPServer(("127.0.0.1", 0), svc.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="session")
def http(base_url):
    def call(path: str, method: str = "GET", timeout: int = 120):
        req = urllib.request.Request(base_url + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                try:
                    return resp.status, json.loads(body or "{}")
                except ValueError:
                    return resp.status, {"raw": body[:400]}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body[:200]}

    return call


@pytest.fixture(scope="session")
def chain(env):
    import keeperhub_release as K

    return K.Chain(env)


def raw_request(host_port: tuple[str, int], path: str) -> str:
    """Send a path urllib would normalise away, so the allowlist is actually tested."""
    host, port = host_port
    sock = socket.create_connection((host, port), timeout=30)
    with sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        return sock.recv(65536).decode(errors="replace").split("\r\n", 1)[0]
