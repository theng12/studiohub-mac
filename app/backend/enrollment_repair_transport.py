"""One-address, credential-free JSON transport for enrollment repair only."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_ALLOWED_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)
_FORBIDDEN_HEADERS = {
    "connection", "content-length", "forwarded", "host", "proxy-authorization",
    "transfer-encoding", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
}


class OriginInvalid(ValueError):
    """The supplied value is not one canonical private origin."""


class PinnedTransportError(RuntimeError):
    """A redacted bounded-transport failure."""


@dataclass(frozen=True)
class ResolvedOrigin:
    origin: str
    address: str
    host_header: str


def canonical_private_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OriginInvalid("callback_url_invalid") from exc
    if (not any(address in network for network in _ALLOWED_PRIVATE_NETWORKS)
            or address.is_loopback
            or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved):
        raise OriginInvalid("callback_url_invalid")
    return address.compressed


def _origin_parts(value: str) -> tuple[str, str, int, str, str]:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 500:
        raise OriginInvalid("callback_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OriginInvalid("callback_url_invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        raise OriginInvalid("callback_url_invalid")
    host = parsed.hostname
    if "%" in host:
        raise OriginInvalid("callback_url_invalid")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise OriginInvalid("callback_url_invalid") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    port = port or default_port
    if not 1 <= port <= 65535:
        raise OriginInvalid("callback_url_invalid")
    try:
        ipaddress.ip_address(host)
        display_host = f"[{host}]" if ":" in host else host
    except ValueError:
        display_host = host
    port_suffix = "" if port == default_port else f":{port}"
    host_header = f"{display_host}{port_suffix}"
    return parsed.scheme, host, port, host_header, f"{parsed.scheme}://{host_header}"


def resolve_private_origin(
    value: str,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> ResolvedOrigin:
    """Normalize one origin and pin its only unique private DNS address."""
    _, host, port, host_header, origin = _origin_parts(value)
    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise OriginInvalid("callback_url_invalid") from exc
    addresses = set()
    try:
        for family, _socktype, _proto, _canonname, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            addresses.add(canonical_private_address(sockaddr[0]))
    except (IndexError, TypeError, OriginInvalid) as exc:
        raise OriginInvalid("callback_url_invalid") from exc
    if len(addresses) != 1:
        raise OriginInvalid("callback_url_invalid")
    return ResolvedOrigin(origin=origin, address=addresses.pop(), host_header=host_header)


class PinnedJSONConnection:
    """A single direct socket request with no proxy or redirect machinery."""

    direct_peer = ""
    local_address = ""

    def __init__(self, origin: ResolvedOrigin) -> None:
        self.origin = origin
        self._socket = None
        self._deadline = None
        self._socket_lock = threading.Lock()

    async def __aenter__(self) -> "PinnedJSONConnection":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self._abort_socket()
        return None

    async def connect(self, *, timeout: float) -> None:
        """Open and authenticate the pinned socket without writing request bytes."""
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 30):
            raise PinnedTransportError("transport_request_invalid")
        if self._current_socket() is not None:
            return
        deadline = self._absolute_deadline(float(timeout))
        remaining = self._remaining(deadline)
        task = asyncio.create_task(asyncio.to_thread(self._open_socket, deadline))
        task.add_done_callback(self._consume_background_result)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError as exc:
            self._abort_socket()
            raise PinnedTransportError("transport_timeout") from exc
        except asyncio.CancelledError:
            self._abort_socket()
            raise

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 30):
            raise PinnedTransportError("transport_request_invalid")
        deadline = self._absolute_deadline(float(timeout))
        try:
            remaining = self._remaining(deadline)
        except PinnedTransportError:
            self._abort_socket()
            raise
        task = asyncio.create_task(asyncio.to_thread(
            self._request_json, method, path, headers, body, deadline
        ))
        task.add_done_callback(self._consume_background_result)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError as exc:
            self._abort_socket()
            raise PinnedTransportError("transport_timeout") from exc
        except asyncio.CancelledError:
            self._abort_socket()
            raise

    @staticmethod
    def _consume_background_result(task: asyncio.Task) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _remember_socket(self, sock: Any) -> None:
        with self._socket_lock:
            self._socket = sock

    def _absolute_deadline(self, timeout: float) -> float:
        with self._socket_lock:
            if self._deadline is None:
                self._deadline = time.monotonic() + timeout
            return self._deadline

    def _current_socket(self) -> Any:
        with self._socket_lock:
            return self._socket

    def _forget_socket(self, sock: Any) -> None:
        with self._socket_lock:
            if self._socket is sock:
                self._socket = None

    def _abort_socket(self) -> None:
        with self._socket_lock:
            sock = self._socket
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PinnedTransportError("transport_timeout")
        return min(remaining, 30.0)

    def _open_socket(self, deadline: float) -> Any:
        scheme, host, port, _host_header, _origin = _origin_parts(self.origin.origin)
        pinned_address = canonical_private_address(self.origin.address)
        sock = None
        try:
            sock = socket.create_connection(
                (pinned_address, port), timeout=self._remaining(deadline)
            )
            self._remember_socket(sock)
            sock.settimeout(self._remaining(deadline))
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                self._remember_socket(sock)
            sock.settimeout(self._remaining(deadline))
            self.direct_peer = ipaddress.ip_address(sock.getpeername()[0]).compressed
            self.local_address = ipaddress.ip_address(sock.getsockname()[0]).compressed
            if self.direct_peer != pinned_address:
                raise PinnedTransportError("callback_source_mismatch")
            return sock
        except BaseException:
            if sock is not None:
                self._forget_socket(sock)
                try:
                    sock.close()
                except Exception:
                    pass
            raise

    def _request_json(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        deadline: float,
    ) -> tuple[int, dict[str, Any]]:
        method = str(method).upper()
        if method not in {"GET", "POST"} or not path.startswith("/") or "://" in path:
            raise PinnedTransportError("transport_request_invalid")
        request_headers = {
            "Host": self.origin.host_header,
            "Connection": "close",
            "Accept": "application/json",
        }
        for key, value in headers.items():
            name = str(key)
            text = str(value)
            if (name.lower() in _FORBIDDEN_HEADERS or not name
                    or len(name) > 100 or len(text) > 8_192
                    or "\r" in name or "\n" in name or "\r" in text or "\n" in text):
                raise PinnedTransportError("transport_request_invalid")
            request_headers[name] = text
        encoded = None
        if body is not None:
            try:
                encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise PinnedTransportError("transport_request_invalid") from exc
            if len(encoded) > MAX_REQUEST_BYTES:
                raise PinnedTransportError("transport_request_too_large")
            request_headers["Content-Type"] = "application/json"

        _scheme, host, port, _host_header, _origin = _origin_parts(self.origin.origin)
        sock = self._current_socket()
        connection = None
        try:
            if sock is None:
                sock = self._open_socket(deadline)
            sock.settimeout(self._remaining(deadline))
            connection = http.client.HTTPConnection(
                host, port, timeout=self._remaining(deadline)
            )
            connection.sock = sock
            sock.settimeout(self._remaining(deadline))
            connection.request(
                method, path, body=encoded, headers=request_headers,
                encode_chunked=False,
            )
            sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > MAX_RESPONSE_BYTES:
                        raise PinnedTransportError("transport_response_too_large")
                except ValueError as exc:
                    raise PinnedTransportError("transport_response_invalid") from exc
            sock.settimeout(self._remaining(deadline))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PinnedTransportError("transport_response_too_large")
            if not raw:
                parsed: Any = {}
            else:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    if 200 <= response.status < 300:
                        raise PinnedTransportError("transport_response_invalid") from exc
                    parsed = {}
            if not isinstance(parsed, dict):
                raise PinnedTransportError("transport_response_invalid")
            return response.status, parsed
        except PinnedTransportError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise PinnedTransportError("transport_timeout") from exc
        except Exception as exc:
            raise PinnedTransportError("transport_unavailable") from exc
        finally:
            if sock is not None:
                self._forget_socket(sock)
            if connection is not None:
                connection.close()
            elif sock is not None:
                sock.close()


def open_pinned_json(origin: ResolvedOrigin) -> PinnedJSONConnection:
    return PinnedJSONConnection(origin)
