"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .negotiation import NegotiationService
from .service import DomainService
from .storage import Database


# 协商接口的 HTTP 分派表：(方法, 路径) -> (服务方法名, 是否需要请求体)
NEGOTIATION_WRITES = {
    ("POST", "/delegations"): "register_delegation",
    ("POST", "/mandates"): "grant_mandate",
    ("POST", "/mandates/revoke"): "revoke_mandate",
    ("POST", "/conflicts"): "declare_conflict",
    ("POST", "/conflicts/clear"): "clear_conflict",
    ("POST", "/proposals"): "create_proposal",
    ("POST", "/clauses"): "add_clause",
    ("POST", "/amendments"): "propose_amendment",
    ("POST", "/amendments/stance"): "set_amendment_stance",
    ("POST", "/amendments/withdraw"): "withdraw_amendment",
    ("POST", "/amendments/merge"): "merge_amendment",
    ("POST", "/statements"): "record_statement",
    ("POST", "/reservations"): "enter_reservation",
    ("POST", "/reservations/withdraw"): "withdraw_reservation",
    ("POST", "/clauses/stance"): "set_clause_stance",
    ("POST", "/clauses/sign"): "sign_clause",
    ("POST", "/clauses/withdraw-signature"): "withdraw_signature",
    ("POST", "/conditions"): "add_ratification_condition",
    ("POST", "/conditions/satisfy"): "satisfy_condition",
    ("POST", "/follow-ups"): "add_follow_up",
    ("POST", "/follow-ups/complete"): "complete_follow_up",
    ("POST", "/translations"): "propose_translation",
    ("POST", "/translations/certify"): "certify_translation",
    ("POST", "/seals"): "seal_facts",
}


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    negotiation = NegotiationService(service.database, service.clock)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        # 规则文本协商与承诺跟踪接口
        write_action = NEGOTIATION_WRITES.get((method, parsed.path))
        if write_action is not None:
            result = getattr(negotiation, write_action)(actor_id=actor_id, **body)
            replayed = bool(result.get("replayed")) if isinstance(result, dict) else False
            return 200 if replayed else 201, result
        if method == "POST" and parsed.path == "/sweep-deadlines":
            return 200, negotiation.sweep_deadlines()
        if method == "GET" and parsed.path == "/clauses/detail":
            query = parse_qs(parsed.query)
            clause_id = query.get("clause_id", [""])[0]
            if not clause_id:
                raise ValidationError("clause_id 不能为空")
            include = query.get("include_content", ["1"])[0] not in ("0", "false")
            return 200, negotiation.get_clause(clause_id, include_content=include)
        if method == "GET" and parsed.path == "/amendments":
            query = parse_qs(parsed.query)
            clause_id = query.get("clause_id", [""])[0]
            if not clause_id:
                raise ValidationError("clause_id 不能为空")
            return 200, {"items": negotiation.list_amendments(clause_id)}
        if method == "GET" and parsed.path == "/amendments/support":
            query = parse_qs(parsed.query)
            amendment_id = query.get("amendment_id", [""])[0]
            if not amendment_id:
                raise ValidationError("amendment_id 不能为空")
            return 200, negotiation.amendment_support(amendment_id)
        if method == "GET" and parsed.path == "/views/delegate":
            query = parse_qs(parsed.query)
            return 200, negotiation.delegate_view(query.get("actor_id", [actor_id])[0])
        if method == "GET" and parsed.path == "/views/translator":
            return 200, negotiation.translator_view(actor_id or None)
        if method == "GET" and parsed.path == "/views/secretary":
            return 200, negotiation.secretary_view(actor_id)
        if method == "GET" and parsed.path == "/views/observer":
            return 200, negotiation.observer_view()
        if method == "GET" and parsed.path == "/public/clauses":
            return 200, negotiation.public_clauses()
        if method == "GET" and parsed.path == "/seals/verify":
            query = parse_qs(parsed.query)
            seal_id = query.get("seal_id", [""])[0]
            if not seal_id:
                raise ValidationError("seal_id 不能为空")
            return 200, negotiation.verify_seal(seal_id)
        if method == "GET" and parsed.path == "/audit/binding":
            query = parse_qs(parsed.query)
            try:
                clause_id = query["clause_id"][0]
                delegation_id = query["delegation_id"][0]
                at = query["at"][0]
            except (KeyError, IndexError) as exc:
                raise ValidationError("必须提供 clause_id、delegation_id 和 at") from exc
            return 200, negotiation.explain_binding(clause_id=clause_id,
                                                    delegation_id=delegation_id, at=at)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    service = DomainService(database)
    Handler.service = service
    # 服务恢复：先按当前时钟对账条件与行动期限，再开始接流量
    NegotiationService(database, service.clock).sweep_deadlines()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
