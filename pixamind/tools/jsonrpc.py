"""Minimal JSON-RPC 2.0 client over a keep-alive HTTP connection (stdlib only).

One client per thread: http.client connections are not thread-safe.
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass
class Reply:
    status: int  # HTTP status (0 when the request did not complete)
    body: Any  # decoded JSON body, or the raw text when it is not JSON
    seconds: float  # wall time of the request
    error: str | None = None  # transport error, if any

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and isinstance(self.body, dict) and 'error' not in self.body

    @property
    def result(self) -> Any:
        return self.body.get('result') if isinstance(self.body, dict) else None


class RpcClient:
    def __init__(self, url: str, timeout: float = 60.0):
        parts = urlsplit(url if '://' in url else f'http://{url}')
        self.url = url
        self.scheme = parts.scheme or 'http'
        self.host = parts.hostname or 'localhost'
        self.port = parts.port
        self.path = parts.path or '/'
        if parts.query:
            self.path += '?' + parts.query
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None
        self._next_id = 0

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
            self._conn = cls(self.host, self.port, timeout=self.timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def post(self, payload: dict) -> Reply:
        data = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json', 'Connection': 'keep-alive'}
        for attempt in (1, 2):  # one retry: the server may have closed an idle keep-alive connection
            start = time.perf_counter()
            try:
                conn = self._connection()
                conn.request('POST', self.path, body=data, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                seconds = time.perf_counter() - start
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw.decode(errors='replace')
                return Reply(response.status, body, seconds)
            except (OSError, http.client.HTTPException) as ex:
                self.close()
                if attempt == 2:
                    return Reply(0, None, time.perf_counter() - start, f'{type(ex).__name__}: {ex}')
        raise AssertionError('unreachable')

    def call(self, method: str, params: Any = None) -> Reply:
        self._next_id += 1
        payload = {'jsonrpc': '2.0', 'id': self._next_id, 'method': method}
        if params is not None:
            payload['params'] = params
        return self.post(payload)

    def result(self, method: str, params: Any = None) -> Any:
        """Call and return the result; raise RuntimeError on any error."""
        reply = self.call(method, params)
        if not reply.ok:
            detail = reply.error or (reply.body.get('error') if isinstance(reply.body, dict) else reply.body)
            raise RuntimeError(f'{method} failed on {self.url}: {detail}')
        return reply.result
