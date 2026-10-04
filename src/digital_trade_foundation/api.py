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


def _receipt_response(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _route_negotiation(service: NegotiationService, method: str, segments: list[str],
                       query: dict[str, list[str]], body: dict[str, Any],
                       actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派规则文本协商与承诺跟踪接口。"""

    if method == "POST" and segments == ["dialogues"]:
        return _receipt_response(service.create_dialogue(actor_id=actor_id, **body))
    if len(segments) >= 2 and segments[0] == "dialogues":
        dialogue_id = segments[1]
        if method == "POST" and len(segments) == 3 and segments[2] == "delegations":
            return _receipt_response(service.enroll_delegation(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "grants":
            return _receipt_response(service.grant_speaking(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "statements":
            return _receipt_response(service.add_statement(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "proposals":
            return _receipt_response(service.create_proposal(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "seals":
            return _receipt_response(service.seal_session(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "actions":
            return _receipt_response(service.create_action(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "GET" and len(segments) == 3 and segments[2] == "actions":
            status = query.get("status", [None])[0]
            return 200, {"items": service.list_actions(dialogue_id, status)}
        if method == "POST" and len(segments) == 3 and segments[2] == "coi":
            return _receipt_response(service.declare_coi(
                actor_id=actor_id, dialogue_id=dialogue_id, **body))
        if method == "GET" and len(segments) == 3 and segments[2] == "public":
            return 200, service.public_view(dialogue_id)
        if method == "GET" and len(segments) == 3 and segments[2] == "view":
            if not actor_id:
                raise ValidationError("X-Actor-Id 不能为空")
            return 200, service.dialogue_view(actor_id=actor_id, dialogue_id=dialogue_id)
        if method == "GET" and len(segments) == 3 and segments[2] == "commitments":
            return 200, {"items": service.list_commitments(dialogue_id)}
    if method == "POST" and len(segments) == 3 and segments[0] == "grants" \
            and segments[2] == "revoke":
        return _receipt_response(service.revoke_speaking(actor_id=actor_id,
                                                         grant_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "proposals" \
            and segments[2] == "clauses":
        return _receipt_response(service.add_clause(actor_id=actor_id,
                                                    proposal_id=segments[1], **body))
    if len(segments) >= 2 and segments[0] == "clauses":
        clause_id = segments[1]
        if method == "GET" and len(segments) == 2:
            return 200, service.get_clause(clause_id)
        if method == "POST" and len(segments) == 3 and segments[2] == "amendments":
            return _receipt_response(service.propose_amendment(
                actor_id=actor_id, clause_id=clause_id, **body))
        if method == "GET" and len(segments) == 3 and segments[2] == "binding":
            delegation_id = query.get("delegation_id", [""])[0]
            at = query.get("at", [""])[0]
            if not delegation_id or not at:
                raise ValidationError("delegation_id 和 at 不能为空")
            return 200, service.explain_binding(clause_id=clause_id,
                                                delegation_id=delegation_id, at=at)
    if len(segments) >= 2 and segments[0] == "amendments":
        amendment_id = segments[1]
        if method == "POST" and len(segments) == 3 and segments[2] == "seconds":
            return _receipt_response(service.second_amendment(
                actor_id=actor_id, amendment_id=amendment_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "withdraw":
            return _receipt_response(service.withdraw_amendment(
                actor_id=actor_id, amendment_id=amendment_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "merge":
            return _receipt_response(service.merge_amendment(
                actor_id=actor_id, amendment_id=amendment_id, **body))
    if len(segments) >= 2 and segments[0] == "versions":
        version_id = segments[1]
        if method == "POST" and len(segments) == 3 and segments[2] == "translations":
            return _receipt_response(service.submit_translation(
                actor_id=actor_id, version_id=version_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "positions":
            return _receipt_response(service.cast_position(
                actor_id=actor_id, version_id=version_id, **body))
        if method == "POST" and len(segments) == 3 and segments[2] == "consensus":
            return _receipt_response(service.form_consensus(
                actor_id=actor_id, version_id=version_id, **body))
        if method == "GET" and len(segments) == 3 and segments[2] == "tally":
            return 200, service.tally(version_id)
    if method == "POST" and len(segments) == 3 and segments[0] == "translations" \
            and segments[2] == "verify":
        return _receipt_response(service.verify_translation(
            actor_id=actor_id, translation_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "statements" \
            and segments[2] == "revisions":
        return _receipt_response(service.revise_statement(
            actor_id=actor_id, statement_id=segments[1], **body))
    if method == "POST" and len(segments) == 5 and segments[0] == "commitments" \
            and segments[2] == "conditions" and segments[4] == "fulfill":
        return _receipt_response(service.fulfill_condition(
            actor_id=actor_id, commitment_id=segments[1], seq=int(segments[3]), **body))
    if method == "GET" and len(segments) == 2 and segments[0] == "commitments":
        return 200, service.get_commitment(segments[1])
    if method == "POST" and len(segments) == 3 and segments[0] == "actions" \
            and segments[2] == "complete":
        return _receipt_response(service.complete_action(actor_id=actor_id,
                                                         action_id=segments[1], **body))
    if method == "POST" and len(segments) == 3 and segments[0] == "coi" \
            and segments[2] == "clear":
        return _receipt_response(service.clear_coi(actor_id=actor_id,
                                                   declaration_id=segments[1], **body))
    if method == "GET" and len(segments) == 2 and segments[0] == "seals":
        return 200, service.verify_seal(segments[1])
    return None


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
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
        segments = [segment for segment in parsed.path.split("/") if segment]
        if isinstance(service, NegotiationService):
            negotiated = _route_negotiation(service, method, segments,
                                            parse_qs(parsed.query), body, actor_id)
            if negotiated is not None:
                return negotiated
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
    Handler.service = NegotiationService(database)
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
