"""HTTP 接口：仅依赖标准库，把用例暴露为 REST 端点。

启动：``python3 -m matchmaking.api``（默认端口 8080）。
所有写操作请求体为 JSON；领域错误映射为 400/404/409。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .repository import Repository
from .service import Service


def build_service() -> Service:
    """工厂函数：测试与演示可用同一方式构造全新后端。"""
    return Service(Repository())


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method, re.compile("^" + pattern + "$"), handler))

    def match(self, method: str, path: str):
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                return handler, match.groupdict()
        return None, {}


def make_router(service: Service) -> Router:
    r = Router()

    # 登记类
    r.add("POST", r"/orgs", lambda body, **_: service.register_organization(
        body["org_id"], body["name"], body["kind"],
        created_from_intent_id=body.get("created_from_intent_id")).to_dict())
    r.add("POST", r"/orgs/(?P<org_id>[^/]+)/contacts", lambda body, org_id, **_:
          service.authorize_contact(org_id, body["name"], body["email"],
                                    body.get("phone", "")).to_dict())
    r.add("POST", r"/orgs/(?P<org_id>[^/]+)/capabilities", lambda body, org_id, **_:
          service.register_capability(
              org_id, body["resource_type"], body.get("scale", 0),
              body.get("region", ""), body.get("detail", "")).to_dict())
    r.add("POST", r"/orgs/(?P<org_id>[^/]+)/resources", lambda body, org_id, **_:
          service.register_resource(
              org_id, body["resource_type"], body["name"], body["capacity"]).to_dict())
    r.add("POST", r"/orgs/(?P<org_id>[^/]+)/exclusions", lambda body, org_id, **_:
          service.add_exclusion(
              org_id, body["reason"], body.get("scope", "org"),
              body.get("target_org_id")).to_dict())
    r.add("POST", r"/orgs/(?P<org_id>[^/]+)/intents", lambda body, org_id, **_:
          service.register_intent(
              org_id, body["title"], body["direction"], body["demand"]).to_dict())

    # 变更与失效类（触发未签约候选重评估）
    r.add("POST", r"/contacts/(?P<contact_id>[^/]+)/revoke", lambda body, contact_id, **_:
          service.revoke_contact(contact_id, body.get("reason", "")))
    r.add("POST", r"/resources/(?P<resource_id>[^/]+)/occupy", lambda body, resource_id, **_:
          service.occupy_resource(resource_id, body["project_id"], body["reserved"]))
    r.add("POST", r"/intents/(?P<intent_id>[^/]+)/revise", lambda body, intent_id, **_:
          service.revise_demand(intent_id, body["demand"], body.get("note", "")))
    r.add("POST", r"/intents/(?P<intent_id>[^/]+)/withdraw", lambda body, intent_id, **_:
          service.withdraw_intent(intent_id, body.get("reason", "")))

    # 撮合与确认
    r.add("POST", r"/matching/run", lambda body, **_: service.run_matching())
    r.add("POST", r"/candidates/(?P<candidate_id>[^/]+)/confirm",
          lambda body, candidate_id, **_: service.confirm_candidate(
              candidate_id, body["org_id"], body["contact_id"],
              reject=body.get("reject", False), note=body.get("note", "")))
    r.add("POST", r"/candidates/(?P<candidate_id>[^/]+)/project",
          lambda body, candidate_id, **_: service.convert_to_project(
              candidate_id, project_id=body.get("project_id")).to_dict())
    r.add("POST", r"/projects/(?P<project_id>[^/]+)/terminate",
          lambda body, project_id, **_: service.terminate_project(
              project_id, body.get("reason", "")).to_dict())

    # 查询与追踪
    r.add("GET", r"/snapshot", lambda q, **_: service.state_snapshot())
    r.add("GET", r"/intents/(?P<intent_id>[^/]+)/timeline", lambda q, intent_id, **_:
          service.timeline(intent_id))
    r.add("GET", r"/lineage", lambda q, **_: service.get_lineage(
        intent_id=q.get("intent_id", [None])[0],
        candidate_id=q.get("candidate_id", [None])[0],
        project_id=q.get("project_id", [None])[0]))
    return r


class Handler(BaseHTTPRequestHandler):
    service: Service
    router: Router
    lock: threading.Lock

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        handler, kwargs = self.router.match("GET", parsed.path)
        if handler is None:
            self._send(404, {"error": "not_found", "message": self.path})
            return
        query = parse_qs(parsed.query)
        with self.lock:
            try:
                self._send(200, handler(query, **kwargs))
            except DomainError as exc:
                self._send_domain_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        handler, kwargs = self.router.match("POST", parsed.path)
        if handler is None:
            self._send(404, {"error": "not_found", "message": self.path})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"error": "validation_error", "message": "请求体不是合法 JSON"})
            return
        with self.lock:
            try:
                self._send(200, handler(body, **kwargs))
            except DomainError as exc:
                self._send_domain_error(exc)

    def _send_domain_error(self, exc: DomainError) -> None:
        status = {
            ValidationError: 400,
            NotFoundError: 404,
            ConflictError: 409,
        }.get(type(exc), 422)
        self._send(status, {"error": exc.code, "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    service = build_service()
    router = make_router(service)
    lock = threading.Lock()

    # BaseHTTPRequestHandler 在 __init__ 中即完成请求处理，
    # 因此依赖必须通过类属性注入，不能在实例化后赋值。
    handler_cls = type(
        "BoundHandler",
        (Handler,),
        {"service": service, "router": router, "lock": lock},
    )
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = create_server(port=port)
    print(f"撮合后端已启动：http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
