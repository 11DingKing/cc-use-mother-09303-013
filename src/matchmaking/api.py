"""HTTP API：基于标准库 http.server 的 JSON 接口。

启动：``python3 -m matchmaking.api --port 8080 --data data/forum.json``
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .repository import Repository
from .service import MatchmakingService


def _ok(payload: Any) -> tuple[int, Any]:
    return 200, payload


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "ForumMatchmaking/1.0"

    # 由 serve() 注入
    service: MatchmakingService = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _BadRequest(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return value

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = {}
        if method == "POST":
            try:
                body = self._read_json()
            except _BadRequest as exc:
                self._send(400, {"error": exc.message})
                return
        svc = self.service
        try:
            status, payload = ROUTES.match(method, path, query, body, svc)
            self._send(status, payload)
        except DomainError as exc:
            http_status = {
                "VALIDATION_ERROR": 400, "NOT_FOUND": 404, "CONFLICT": 409,
                "ILLEGAL_STATE": 409,
            }.get(exc.code, 400)
            self._send(http_status, {"error": str(exc), "code": exc.code})
        except _NotFound as exc:
            self._send(404, {"error": exc.message})
        except _BadRequest as exc:
            self._send(400, {"error": exc.message})


class _BadRequest(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class _NotFound(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


# ---- 极简路由表 -------------------------------------------------------------

class _Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, list[str], Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self._routes.append((method, pattern.strip("/").split("/"), handler))

    def match(self, method: str, path: str, query: dict, body: dict, svc) -> tuple[int, Any]:
        parts = path.strip("/").split("/")
        for m, pattern, handler in self._routes:
            if m != method or len(parts) != len(pattern):
                continue
            kwargs: dict[str, Any] = {}
            for actual, expected in zip(parts, pattern):
                if expected.startswith("{") and expected.endswith("}"):
                    kwargs[expected[1:-1]] = actual
                elif actual != expected:
                    break
            else:
                return handler(svc, query=query, body=body, **kwargs)
        raise _NotFound(f"没有匹配的路由：{method} {path}")


ROUTES = _Router()


# ---- 处理器 -----------------------------------------------------------------

def h_health(svc, *, query, body):
    return _ok({"status": "ok"})


def h_register_org(svc, *, query, body):
    return 201, svc.register_organization(body["kind"], body["name"]).to_dict()


def h_add_contact(svc, *, query, body):
    return 201, svc.add_contact(
        body["org_id"], body["name"], body["email"],
        body.get("authorized_demand_ids")).to_dict()


def h_invalidate_contact(svc, *, query, body, contact_id):
    return _ok(svc.invalidate_contact(contact_id, body.get("reason", "")).to_dict())


def h_add_capability(svc, *, query, body):
    return 201, svc.add_capability(
        body["org_id"], body["name"], body["category"], body.get("tags")).to_dict()


def h_add_resource(svc, *, query, body):
    return 201, svc.add_resource(
        body["owner_org_id"], body["name"], int(body["total_qty"]), body["unit"],
        body.get("capability_id")).to_dict()


def h_occupy_resource(svc, *, query, body, resource_id):
    return 201, svc.occupy_resource(
        resource_id, int(body["qty"]), body.get("reference", "external")).to_dict()


def h_reduce_resource(svc, *, query, body, resource_id):
    return _ok(svc.reduce_resource_capacity(
        resource_id, int(body["new_total_qty"]), body.get("reason", "")).to_dict())


def h_add_exclusion(svc, *, query, body):
    return 201, svc.add_exclusion(body["org_a_id"], body["org_b_id"], body["reason"]).to_dict()


def h_register_intent(svc, *, query, body):
    return 201, svc.register_intent(
        body["enterprise_org_id"], body["summary"],
        body.get("target_college_org_id")).to_dict()


def h_create_demand(svc, *, query, body):
    return 201, svc.create_demand(
        body["intent_id"], title=body["title"],
        capability_categories=body["capability_categories"],
        capability_tags=body.get("tags", []), required_qty=int(body["required_qty"]),
        location=body.get("location"), note=body.get("note", "")).to_dict()


def h_revise_demand(svc, *, query, body, demand_id):
    return _ok(svc.revise_demand(
        demand_id, title=body["title"],
        capability_categories=body["capability_categories"],
        capability_tags=body.get("tags", []), required_qty=int(body["required_qty"]),
        location=body.get("location"), note=body.get("note", "")).to_dict())


def h_withdraw_intent(svc, *, query, body, intent_id):
    return _ok(svc.withdraw_intent(intent_id, body.get("reason", "")).to_dict())


def h_withdraw_demand(svc, *, query, body, demand_id):
    return _ok(svc.withdraw_demand(demand_id, body.get("reason", "")).to_dict())


def h_run_matching(svc, *, query, body):
    return _ok(svc.run_matching().to_dict())


def h_list_candidates(svc, *, query, body):
    return _ok([c.to_dict() for c in svc.list_candidates(
        query.get("demand_id"), query.get("status"))])


def h_confirm(svc, *, query, body, candidate_id):
    return _ok(svc.confirm_candidate(
        candidate_id, body["party"], body["contact_id"], body.get("note", "")).to_dict())


def h_reject(svc, *, query, body, candidate_id):
    return _ok(svc.reject_candidate(
        candidate_id, body["party"], body.get("reason", "")).to_dict())


def h_convert(svc, *, query, body, candidate_id):
    return 201, svc.convert_to_project(candidate_id).to_dict()


def h_sign(svc, *, query, body, project_id):
    return _ok(svc.sign_project(project_id).to_dict())


def h_terminate(svc, *, query, body, project_id):
    return _ok(svc.terminate_project(project_id, body.get("reason", "")).to_dict())


def h_timeline(svc, *, query, body, demand_id):
    return _ok([e.to_dict() for e in svc.get_timeline(demand_id)])


def h_trace_intent(svc, *, query, body, intent_id):
    return _ok(svc.trace_intent(intent_id))


ROUTES.add("GET", "/health", h_health)
ROUTES.add("POST", "/organizations", h_register_org)
ROUTES.add("POST", "/contacts", h_add_contact)
ROUTES.add("POST", "/contacts/{contact_id}/invalidate", h_invalidate_contact)
ROUTES.add("POST", "/capabilities", h_add_capability)
ROUTES.add("POST", "/resources", h_add_resource)
ROUTES.add("POST", "/resources/{resource_id}/occupy", h_occupy_resource)
ROUTES.add("POST", "/resources/{resource_id}/reduce", h_reduce_resource)
ROUTES.add("POST", "/exclusions", h_add_exclusion)
ROUTES.add("POST", "/intents", h_register_intent)
ROUTES.add("POST", "/demands", h_create_demand)
ROUTES.add("POST", "/demands/{demand_id}/revise", h_revise_demand)
ROUTES.add("POST", "/demands/{demand_id}/withdraw", h_withdraw_demand)
ROUTES.add("POST", "/intents/{intent_id}/withdraw", h_withdraw_intent)
ROUTES.add("POST", "/matching/run", h_run_matching)
ROUTES.add("GET", "/candidates", h_list_candidates)
ROUTES.add("POST", "/candidates/{candidate_id}/confirm", h_confirm)
ROUTES.add("POST", "/candidates/{candidate_id}/reject", h_reject)
ROUTES.add("POST", "/candidates/{candidate_id}/convert", h_convert)
ROUTES.add("POST", "/projects/{project_id}/sign", h_sign)
ROUTES.add("POST", "/projects/{project_id}/terminate", h_terminate)
ROUTES.add("GET", "/demands/{demand_id}/timeline", h_timeline)
ROUTES.add("GET", "/intents/{intent_id}/trace", h_trace_intent)


def serve(port: int = 8080, data_path: str | None = None) -> None:
    repo = Repository(data_path)
    service = MatchmakingService(repo)
    ApiHandler.service = service
    httpd = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    print(f"国际论坛项目撮合后端已启动：http://0.0.0.0:{port}（数据文件：{data_path or '内存'}）")
    httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="国际论坛项目撮合后端")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None, help="JSON 持久化文件路径")
    args = parser.parse_args()
    serve(args.port, args.data)


if __name__ == "__main__":
    main()
