"""后端传输边界：实体白名单、REST 实验入口及 HA 原生服务调用入口。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .configuration import integer, unique_object


class ScopedTransport:
    ACTIONS = {"fan": {"turn_on", "turn_off", "set_percentage", "set_direction", "set_preset_mode"},
               "light": {"turn_on", "turn_off"}, "switch": {"turn_on", "turn_off"},
               "number": {"set_value"}}
    PARAMETERS = {("fan", "turn_on"): {"percentage"}, ("fan", "turn_off"): set(),
                  ("fan", "set_percentage"): {"percentage"}, ("fan", "set_direction"): {"direction"},
                  ("fan", "set_preset_mode"): {"preset_mode"},
                  ("light", "turn_on"): {"brightness", "color_temp_kelvin", "effect"},
                  ("light", "turn_off"): set(), ("switch", "turn_on"): set(),
                  ("switch", "turn_off"): set(), ("number", "set_value"): {"value"}}

    def bind(self, entities):
        if not entities or any(not isinstance(x, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x) for x in entities):
            raise ValueError("invalid entity allowlist")
        self.allowed = frozenset(entities)

    def validate_call(self, domain, action, entity, values=None):
        if entity not in self.allowed or entity.split(".")[0] != domain or action not in self.ACTIONS.get(domain, set()):
            raise ValueError("service call outside configured scope")
        if set(values or {}) - self.PARAMETERS[domain, action]:
            raise ValueError("service parameters outside configured scope")

    def filter_states(self, states):
        result = {s["entity_id"]: s for s in states if s["entity_id"] in self.allowed}
        if set(result) != self.allowed:
            raise ValueError("configured entities are missing")
        return result


class RESTTransport(ScopedTransport):
    def __init__(self, url, token_file, audit=None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("invalid HA URL")
        self.url = url.rstrip("/")
        self.token = Path(token_file).read_text().strip()
        self.audit = audit
        self.allowed = frozenset()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cached = None
        self.cached_at = 0

    def request(self, path, data=None):
        request = urllib.request.Request(self.url + "/api/" + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self.opener.open(request, timeout=4) as response:
            return json.load(response)

    def states(self):
        if self.cached is None or time.monotonic() - self.cached_at >= .5:
            self.cached = self.filter_states(self.request("states"))
            self.cached_at = time.monotonic()
        return self.cached

    def call(self, domain, action, entity, **values):
        self.validate_call(domain, action, entity, values)
        self.request(f"services/{domain}/{action}", {"entity_id": entity, **values})
        self.cached = None
        if self.audit:
            fd = os.open(self.audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps({"time": time.time(), "domain": domain, "action": action,
                                         "entity": entity, "values": values}, ensure_ascii=False) + "\n")


class HomeAssistantTransport(ScopedTransport):
    """供独立后端工作线程调用；不需要 REST token，也不阻塞 HA 事件循环。"""

    def __init__(self, hass, writable=None):
        self.writable = None if writable is None else frozenset(writable)
        self.hass = hass
        self.allowed = frozenset()

    def run_on_loop(self, factory):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self.hass.loop:
            raise RuntimeError("OpenHarmony synchronous router must run in a worker thread")
        future = asyncio.run_coroutine_threadsafe(factory(), self.hass.loop)
        try:
            return future.result(timeout=5)
        except BaseException:
            future.cancel()
            raise

    def states(self):
        async def collect():
            return self.filter_states([state.as_dict() for state in self.hass.states.async_all()])
        return self.run_on_loop(collect)

    def call(self, domain, action, entity, **values):
        self.validate_call(domain, action, entity, values)
        if self.writable is not None and entity not in self.writable:
            raise ValueError("OpenHarmony transport is read-only for this entity")
        async def invoke():
            await self.hass.services.async_call(domain, action, {"entity_id": entity, **values}, blocking=True)
        return self.run_on_loop(invoke)


def make_server(router, port, read_only=False):
    """保持已验证 C 子设备接口；仅回环监听，拒绝未声明的 key/SID/实体。"""
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if self.path != "/bridge":
                    raise ValueError("unknown endpoint")
                size = integer(int(self.headers.get("Content-Length", "0")), 1, 8192)
                self.connection.settimeout(15)
                request = json.loads(self.rfile.read(size), object_pairs_hook=unique_object)
                if not isinstance(request, dict) or set(request) not in ({"key"}, {"key", "sid", "data"}):
                    raise ValueError("invalid request fields")
                if read_only and "data" in request:
                    raise ValueError("read-only backend")
                with lock:
                    state = router.write(request["key"], request["sid"], request["data"]) if "data" in request else router.snapshot(request["key"])
                response = {"ok": True, "state": state}
            except Exception as error:
                response = {"ok": False, "error": type(error).__name__}
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(router, port, read_only=False):
    with make_server(router, port, read_only) as server:
        server.serve_forever()
