#!/usr/bin/env python3
"""git-over-pycurl: a local HTTP proxy that tunnels Git traffic through a
corporate proxy using pycurl (libcurl).

The tool listens on 127.0.0.1 and speaks the normal HTTP proxy protocol that
Git already understands.  HTTPS requests (CONNECT) become raw byte tunnels
that libcurl establishes through the upstream proxy, so TLS stays end-to-end
between Git and the remote server and native git commands work unchanged.

Commands:
    run        start the local proxy (default)
    install    point git at the local proxy
    uninstall  remove git proxy settings
    test       run connectivity checks
    status     show effective configuration
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    import pycurl
except ImportError:
    pycurl = None

APP_NAME = "git-over-pycurl"
VERSION = "2.0.0"
CONFIG_PATH = Path.home() / ".gitproxy.json"
LOG = logging.getLogger("gitproxy")

DEFAULT_CONFIG = {
    "upstream_proxy": "http://rnt-proxy.rn-t.ru:3128",
    "listen_host": "127.0.0.1",
    "listen_port": 3129,
    "proxy_user": "",
    "connect_timeout": 30,
    "idle_timeout": 600,
    "verify_upstream": False,
}

REASONS = {
    400: "Bad Request",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
}


class TunnelError(Exception):
    pass


class ProtocolError(Exception):
    pass


def _prog() -> str:
    return Path(sys.argv[0]).name


def build_proxy_url(config: dict) -> str:
    proxy = str(config.get("upstream_proxy", "")).strip()
    if "://" not in proxy:
        proxy = "http://" + proxy
    user = str(config.get("proxy_user") or "")
    if user:
        parts = urlsplit(proxy)
        host = parts.hostname or parts.netloc
        netloc = f"{host}:{parts.port}" if parts.port else host
        proxy = f"{parts.scheme or 'http'}://{quote(user, safe=':')}@{netloc}"
    return proxy


def split_host_port(value: str, default_port: int):
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None, None
        host = value[1:end]
        rest = value[end + 1:]
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return None, None
        return host, default_port
    if ":" in value:
        host, _, port_text = value.rpartition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return value, default_port
    return value, default_port


def _safe_close(curl) -> None:
    try:
        curl.close()
    except Exception:
        pass


def _dup_curl_socket(curl):
    raw = None
    for name in ("ACTIVESOCKET", "LASTSOCKET"):
        const = getattr(pycurl, name, None)
        if const is None:
            continue
        try:
            raw = curl.getinfo(const)
        except Exception:
            raw = None
        if isinstance(raw, socket.socket) or (isinstance(raw, int) and raw >= 0):
            break
        raw = None
    if isinstance(raw, socket.socket):
        return raw.dup()
    if isinstance(raw, int) and raw >= 0:
        wrapper = None
        try:
            wrapper = socket.socket(fileno=raw)
            duplicate = wrapper.dup()
        except OSError:
            return None
        finally:
            if wrapper is not None:
                try:
                    wrapper.detach()
                except Exception:
                    pass
        return duplicate
    return None


class Tunnel:
    """Owns a pycurl handle plus the raw connected socket it established."""

    def __init__(self, curl, sock):
        self._curl = curl
        self.sock = sock

    def sendall(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        _safe_close(self._curl)


def open_tunnel(config: dict, host: str, port: int) -> Tunnel:
    """Open a raw TCP tunnel to host:port through the upstream proxy."""
    if pycurl is None:
        raise TunnelError("pycurl is not installed (pip install pycurl)")

    proxy_url = build_proxy_url(config)
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    scheme = urlsplit(proxy_url).scheme or "http"
    timeout = int(config.get("connect_timeout", 30))

    curl = pycurl.Curl()
    try:
        curl.setopt(pycurl.URL, f"https://{host}:{port}/")
        curl.setopt(pycurl.PROXY, proxy_url)
        curl.setopt(pycurl.PROXYAUTH, pycurl.HTTPAUTH_ANY)
        curl.setopt(pycurl.HTTPPROXYTUNNEL, 1)
        curl.setopt(pycurl.CONNECT_ONLY, 1)
        curl.setopt(pycurl.NOPROGRESS, 1)
        curl.setopt(pycurl.CONNECTTIMEOUT, timeout)
        curl.setopt(pycurl.TIMEOUT, timeout)
        curl.setopt(pycurl.SSL_VERIFYPEER, 0)
        curl.setopt(pycurl.SSL_VERIFYHOST, 0)

        proxy_type = getattr(pycurl, "PROXYTYPE_HTTP", None)
        if scheme == "https":
            proxy_type = getattr(pycurl, "PROXYTYPE_HTTPS", proxy_type)
        if proxy_type is not None:
            curl.setopt(pycurl.PROXYTYPE, proxy_type)

        curl.perform()

        sock = _dup_curl_socket(curl)
        if sock is None:
            raise TunnelError("pycurl did not expose the tunnel socket; upgrade pycurl")
        sock.settimeout(None)
        return Tunnel(curl, sock)
    except pycurl.error as exc:
        _safe_close(curl)
        raise TunnelError(str(exc)) from exc
    except Exception:
        _safe_close(curl)
        raise


class BufferedSocket:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()

    def readline(self, limit: int = 65536) -> bytes:
        while b"\n" not in self.buffer:
            if len(self.buffer) > limit:
                raise ProtocolError("header line too long")
            chunk = self.sock.recv(4096)
            if not chunk:
                if not self.buffer:
                    return b""
                break
            self.buffer.extend(chunk)
        index = self.buffer.find(b"\n")
        if index == -1:
            line = bytes(self.buffer)
            self.buffer.clear()
            return line
        line = bytes(self.buffer[: index + 1])
        del self.buffer[: index + 1]
        return line

    def read_headers(self, limit: int = 65536) -> list:
        headers = []
        total = 0
        while True:
            line = self.readline()
            if not line:
                break
            total += len(line)
            if total > limit:
                raise ProtocolError("request headers too large")
            headers.append(line)
            if line in (b"\r\n", b"\n"):
                break
        return headers

    def pending(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def relay(client: socket.socket, upstream: socket.socket, idle_timeout: int = 600) -> None:
    client.settimeout(idle_timeout)
    upstream.settimeout(idle_timeout)

    def pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    forward = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
    backward = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
    forward.start()
    backward.start()
    forward.join()
    backward.join()


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(60)
        reader = BufferedSocket(client)
        try:
            request_line = reader.readline()
        except OSError:
            return
        if not request_line:
            return
        try:
            method, target, version = request_line.decode("latin-1").split()
        except ValueError:
            self._send_error(client, 400, "Malformed request line")
            return

        try:
            headers = reader.read_headers()
        except (ProtocolError, OSError) as exc:
            self._send_error(client, 431, str(exc))
            return

        if method.upper() == "CONNECT":
            self._handle_connect(client, target, reader)
        else:
            self._handle_forward(client, method, target, version, headers, reader)

    def _handle_connect(self, client, target, reader):
        host, port = split_host_port(target, 443)
        if not host:
            self._send_error(client, 400, "Malformed CONNECT target")
            return
        try:
            tunnel = open_tunnel(self.server.config, host, port)
        except TunnelError as exc:
            LOG.warning("CONNECT %s:%s failed: %s", host, port, exc)
            self._send_error(client, 502, f"Upstream tunnel failed: {exc}")
            return

        LOG.info("CONNECT %s:%s", host, port)
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            leftover = reader.pending()
            if leftover:
                tunnel.sendall(leftover)
            relay(client, tunnel.sock, self.server.config.get("idle_timeout", 600))
        except OSError:
            pass
        finally:
            tunnel.close()

    def _handle_forward(self, client, method, target, version, headers, reader):
        parsed = urlsplit(target)
        if not parsed.hostname:
            self._send_error(client, 400, "Proxy requires an absolute URI")
            return
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            tunnel = open_tunnel(self.server.config, host, port)
        except TunnelError as exc:
            LOG.warning("%s %s:%s failed: %s", method, host, port, exc)
            self._send_error(client, 502, f"Upstream tunnel failed: {exc}")
            return

        LOG.info("%s %s:%s%s", method, host, port, path)
        try:
            out = bytearray()
            out += f"{method} {path} {version}\r\n".encode("latin-1")
            for line in headers:
                if line.lower().startswith(b"proxy-connection:"):
                    continue
                out += line
            tunnel.sendall(bytes(out))
            leftover = reader.pending()
            if leftover:
                tunnel.sendall(leftover)
            relay(client, tunnel.sock, self.server.config.get("idle_timeout", 600))
        except OSError:
            pass
        finally:
            tunnel.close()

    @staticmethod
    def _send_error(client, code, message):
        body = message.encode("utf-8", "replace")
        reason = REASONS.get(code, "Error")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            client.sendall(head + body)
        except OSError:
            pass


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, config):
        self.config = config
        super().__init__(address, ProxyHandler)

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (ConnectionError, socket.timeout, OSError)):
            LOG.debug("connection from %s ended", client_address)
        else:
            LOG.exception("error while handling %s", client_address)


def _http_get(url, proxy=None, verify=False, timeout=25):
    if pycurl is None:
        raise RuntimeError("pycurl is not installed")
    buffer = io.BytesIO()
    curl = pycurl.Curl()
    try:
        curl.setopt(pycurl.URL, url)
        curl.setopt(pycurl.WRITEDATA, buffer)
        curl.setopt(pycurl.NOPROGRESS, 1)
        curl.setopt(pycurl.TIMEOUT, timeout)
        curl.setopt(pycurl.CONNECTTIMEOUT, timeout)
        curl.setopt(pycurl.USERAGENT, f"{APP_NAME}/{VERSION}")
        curl.setopt(pycurl.FOLLOWLOCATION, 1)
        if proxy:
            curl.setopt(pycurl.PROXY, proxy)
        if not verify:
            curl.setopt(pycurl.SSL_VERIFYPEER, 0)
            curl.setopt(pycurl.SSL_VERIFYHOST, 0)
        curl.perform()
        return curl.getinfo(pycurl.RESPONSE_CODE), buffer.getvalue()
    finally:
        curl.close()


def _git_ls_remote(proxy: str) -> str:
    command = [
        "git",
        "-c", f"http.proxy={proxy}",
        "-c", f"https.proxy={proxy}",
        "ls-remote",
        "https://github.com/octocat/Hello-World.git",
        "HEAD",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail.splitlines()[-1] if detail else f"exit code {result.returncode}")
    first = result.stdout.strip().splitlines()
    return first[0] if first else "no refs returned"


def load_config(args) -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                config.update(stored)
        except (OSError, ValueError):
            LOG.warning("could not read %s, using defaults", CONFIG_PATH)

    env_map = {
        "GITPROXY_UPSTREAM": "upstream_proxy",
        "GITPROXY_LISTEN": "listen_host",
        "GITPROXY_PORT": "listen_port",
        "GITPROXY_USER": "proxy_user",
    }
    for env_name, key in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[key] = value

    if getattr(args, "proxy", None):
        config["upstream_proxy"] = args.proxy
    if getattr(args, "listen", None):
        config["listen_host"] = args.listen
    if getattr(args, "port", None):
        config["listen_port"] = args.port
    if getattr(args, "proxy_auth", None):
        config["proxy_user"] = args.proxy_auth
    if getattr(args, "verify_upstream", None) is not None:
        config["verify_upstream"] = args.verify_upstream

    try:
        config["listen_port"] = int(config["listen_port"])
    except (TypeError, ValueError):
        config["listen_port"] = DEFAULT_CONFIG["listen_port"]
    return config


def cmd_run(config: dict) -> int:
    try:
        server = ThreadingProxyServer((config["listen_host"], config["listen_port"]), config)
    except OSError as exc:
        LOG.error("cannot bind %s:%s - %s", config["listen_host"], config["listen_port"], exc)
        return 1

    port = server.server_address[1]
    local_url = f"http://{config['listen_host']}:{port}"
    LOG.info("local proxy listening on %s", local_url)
    LOG.info("upstream proxy: %s", build_proxy_url(config))
    LOG.info("point git here:  %s install   (or set http.proxy / https.proxy to %s)", _prog(), local_url)
    LOG.info("press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def cmd_install(args, config: dict) -> int:
    local_url = f"http://{config['listen_host']}:{config['listen_port']}"
    scope_flag = "--local" if args.scope == "local" else "--global"
    pairs = [("http.proxy", local_url), ("https.proxy", local_url)]
    if getattr(args, "insecure_git", False):
        pairs.append(("http.sslVerify", "false"))

    for key, value in pairs:
        result = subprocess.run(["git", "config", scope_flag, key, value], capture_output=True, text=True)
        if result.returncode != 0:
            LOG.error("failed to set %s: %s", key, result.stderr.strip())
            return 1
        LOG.info("git config %s %s = %s", scope_flag, key, value)

    LOG.info("start the proxy with: %s run", _prog())
    return 0


def cmd_uninstall(args, config: dict) -> int:
    scope_flag = "--local" if args.scope == "local" else "--global"
    for key in ("http.proxy", "https.proxy"):
        result = subprocess.run(["git", "config", scope_flag, "--unset", key], capture_output=True, text=True)
        if result.returncode == 0:
            LOG.info("removed git config %s %s", scope_flag, key)
    return 0


def cmd_status(args, config: dict) -> int:
    source = str(CONFIG_PATH) if CONFIG_PATH.exists() else "(built-in defaults)"
    print(f"config file    : {source}")
    print(f"upstream proxy : {build_proxy_url(config)}")
    print(f"local listen   : {config['listen_host']}:{config['listen_port']}")
    print(f"pycurl         : {pycurl.version if pycurl else 'not installed'}")
    for scope_name, scope_flag in (("global", "--global"), ("local", "--local")):
        for key in ("http.proxy", "https.proxy"):
            result = subprocess.run(["git", "config", scope_flag, "--get", key], capture_output=True, text=True)
            value = result.stdout.strip()
            if value:
                print(f"git {scope_name:<6}   : {key} = {value}")
    return 0


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(65536)
            if data:
                self.request.sendall(b"ECHO:" + data)
        except OSError:
            pass


class _OriginHandler(socketserver.BaseRequestHandler):
    def handle(self):
        reader = BufferedSocket(self.request)
        try:
            reader.readline()
            reader.read_headers()
            reader.pending()
        except OSError:
            return
        try:
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        except OSError:
            pass


class _DirectTunnel:
    def __init__(self, sock):
        self.sock = sock

    def sendall(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _direct_open_tunnel(config, host, port):
    return _DirectTunnel(socket.create_connection((host, port), timeout=5))


def _serve(server_cls, handler):
    server = server_cls(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cmd_selftest(config: dict) -> int:
    global open_tunnel
    failures = 0

    def check(name, func):
        nonlocal failures
        try:
            detail = func()
            LOG.info("[ OK ] %s%s", name, f" - {detail}" if detail else "")
        except Exception as exc:
            failures += 1
            LOG.error("[FAIL] %s - %s", name, exc)

    echo = _serve(socketserver.ThreadingTCPServer, _EchoHandler)
    origin = _serve(socketserver.ThreadingTCPServer, _OriginHandler)
    proxy = ThreadingProxyServer((config["listen_host"], 0), config)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_port = proxy.server_address[1]

    original_open_tunnel = open_tunnel
    open_tunnel = _direct_open_tunnel
    try:
        def connect_tunnel():
            client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
            try:
                target = f"127.0.0.1:{echo.server_address[1]}"
                client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
                reader = BufferedSocket(client)
                status = reader.readline()
                if b" 200 " not in status:
                    raise RuntimeError(f"status {status!r}")
                reader.read_headers()
                client.sendall(b"ping")
                data = client.recv(4096)
                if data != b"ECHO:ping":
                    raise RuntimeError(f"echo mismatch: {data!r}")
                return "CONNECT -> raw tunnel -> echo"
            finally:
                client.close()
        check("CONNECT tunneling", connect_tunnel)

        def forward():
            client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
            try:
                target = f"http://127.0.0.1:{origin.server_address[1]}/x"
                client.sendall(
                    f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Connection: keep-alive\r\n\r\n".encode()
                )
                data = b""
                while b"\r\n\r\n" not in data and len(data) < 4096:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data.startswith(b"HTTP/1.1 200"):
                    raise RuntimeError(f"unexpected response {data[:40]!r}")
                return "absolute-URI request rewritten and forwarded"
            finally:
                client.close()
        check("plain HTTP forwarding", forward)
    finally:
        open_tunnel = original_open_tunnel
        proxy.shutdown()
        proxy.server_close()
        echo.shutdown()
        echo.server_close()
        origin.shutdown()
        origin.server_close()

    if failures:
        LOG.error("%d self-test(s) failed", failures)
        return 1
    LOG.info("self-test passed (no network or pycurl required)")
    return 0


def cmd_test(args, config: dict) -> int:
    failures = 0

    def check(name, func):
        nonlocal failures
        try:
            detail = func()
            LOG.info("[ OK ] %s%s", name, f" - {detail}" if detail else "")
        except Exception as exc:
            failures += 1
            LOG.error("[FAIL] %s - %s", name, exc)

    LOG.info("upstream proxy : %s", build_proxy_url(config))
    LOG.info("local listen   : %s:%s", config["listen_host"], config["listen_port"])

    check("pycurl available", lambda: pycurl.version if pycurl else _raise("pycurl is not installed"))

    def upstream():
        code, body = _http_get("https://api.github.com/zen", proxy=build_proxy_url(config))
        return f"HTTP {code}, {len(body)} bytes"
    check("internet via upstream proxy", upstream)

    def tunnel():
        t = open_tunnel(config, "github.com", 443)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            tls = context.wrap_socket(t.sock, server_hostname="github.com")
            tls.sendall(b"GET / HTTP/1.0\r\nHost: github.com\r\n\r\n")
            data = tls.recv(64)
            if not data.startswith(b"HTTP/"):
                raise RuntimeError(f"unexpected response {data[:32]!r}")
            return data.splitlines()[0].decode("latin-1")
        finally:
            t.close()
    check("raw tunnel github.com:443", tunnel)

    server = None
    try:
        server = ThreadingProxyServer((config["listen_host"], config["listen_port"]), config)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        local_url = f"http://127.0.0.1:{port}"

        def through_local():
            code, body = _http_get("https://api.github.com/zen", proxy=local_url)
            return f"HTTP {code}, {len(body)} bytes"
        check("internet via local proxy", through_local)

        check("git ls-remote via local proxy", lambda: _git_ls_remote(local_url))
        check("git ls-remote via upstream proxy", lambda: _git_ls_remote(build_proxy_url(config)))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    if failures:
        LOG.error("%d check(s) failed", failures)
        return 1
    LOG.info("all checks passed")
    return 0


def _raise(message):
    raise RuntimeError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="Local HTTP proxy that tunnels Git through a corporate proxy using pycurl.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--log-level", default="INFO", help="logging level (default INFO)")

    def add_common(sub):
        sub.add_argument("--proxy", help="upstream proxy URL")
        sub.add_argument("--listen", help="local listen address (default 127.0.0.1)")
        sub.add_argument("--port", type=int, help="local listen port (default 3129)")
        sub.add_argument("--proxy-auth", dest="proxy_auth", metavar="USER:PASS", help="upstream proxy credentials")
        sub.add_argument("--verify-upstream", dest="verify_upstream", action="store_true", default=None,
                         help="verify TLS certificates on the upstream side")
        sub.add_argument("--no-verify-upstream", dest="verify_upstream", action="store_false",
                         help="do not verify upstream certificates")

    subparsers = parser.add_subparsers(dest="command")
    add_common(subparsers.add_parser("run", help="start the local proxy"))

    install = subparsers.add_parser("install", help="point git at the local proxy")
    add_common(install)
    install.add_argument("--scope", choices=("global", "local"), default="global")
    install.add_argument("--insecure-git", action="store_true", help="also set http.sslVerify=false")

    uninstall = subparsers.add_parser("uninstall", help="remove git proxy settings")
    uninstall.add_argument("--scope", choices=("global", "local"), default="global")

    add_common(subparsers.add_parser("test", help="run connectivity checks"))
    subparsers.add_parser("selftest", help="offline test of the proxy logic (no network)")
    subparsers.add_parser("status", help="show effective configuration")

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "install", "uninstall", "test", "selftest", "status"}
    informational = {"-h", "--help", "--version"}
    if not informational.intersection(argv) and not commands.intersection(argv):
        argv.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else getattr(logging, str(args.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    config = load_config(args)
    command = args.command or "run"

    if command == "run":
        return cmd_run(config)
    if command == "install":
        return cmd_install(args, config)
    if command == "uninstall":
        return cmd_uninstall(args, config)
    if command == "test":
        return cmd_test(args, config)
    if command == "selftest":
        return cmd_selftest(config)
    if command == "status":
        return cmd_status(args, config)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
