"""规则文本协商与承诺跟踪领域服务。

在基础服务的组织、操作者、幂等回执和哈希链审计能力之上，为国际对话机制
提供提案、条款版本、翻译对应、发言授权、利益冲突、保留意见、生效条件、
签署资格和后续行动的可追溯协商流程。

核心规则：
- 立场（接受、条件接受、保留、反对）始终针对同一文本快照（clause version），
  支持情况按快照计算，同一代表团重复表态不增加支持数；
- 修订必须基于当前头版本并获得其他代表团附议，合并后同基础的其余修订自动
  标记为冲突，互相冲突的修订不能同时合并；
- 场次封存后，封存范围内的发言与表决事实不再受后续文字整理影响，同一场次
  的并发封存只能有一个结果；
- 达到程序要求（法定人数、零反对、支持数达标）后只形成待核准共识，承诺须
  等国内或组织前置条件按顺序满足后才对对应参与方生效；
- 所有状态持久化在 SQLite 中，服务恢复后继续等待条件和行动期限。
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

STANCES = frozenset({"accept", "conditional_accept", "reserve", "reject"})
SECRETARIAT_ROLES = frozenset({"secretariat", "admin"})
TRANSLATION_ROLES = frozenset({"translator", "secretariat", "admin"})
FULL_VIEW_ROLES = frozenset({"secretariat", "admin", "auditor"})
SUPPORTIVE_STANCES = ("accept", "conditional_accept")
COMMITTED_STANCES = ("accept", "conditional_accept", "reserve")


def _parse_time(value: str, field: str) -> datetime:
    """把 ISO 8601 文本解析为带时区的 UTC 时间。"""

    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field} 不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    """把时间统一为 UTC Z 文本。"""

    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _position_digest(row: Any) -> str:
    """计算一条表决事实的稳定摘要。"""

    return digest({
        "position_id": row["position_id"],
        "version_id": row["version_id"],
        "delegation_id": row["delegation_id"],
        "stance": row["stance"],
        "note": row["note"],
        "created_at": row["created_at"],
    })


def _statement_digest(statement_id: str, body: str) -> str:
    """计算一条发言事实的稳定摘要。"""

    return digest({"statement_id": statement_id, "body": body})


class NegotiationService(DomainService):
    """协调规则文本协商与承诺跟踪的领域规则。"""

    # ---------- 内部助手 ----------

    def _dialogue(self, connection, dialogue_id: str):
        row = connection.execute(
            "SELECT * FROM dialogues WHERE dialogue_id=?", (dialogue_id,)).fetchone()
        if row is None:
            raise NotFoundError("对话机制不存在")
        return row

    def _delegation(self, connection, dialogue_id: str, delegation_id: str):
        row = connection.execute(
            "SELECT * FROM delegations WHERE dialogue_id=? AND delegation_id=?",
            (dialogue_id, delegation_id)).fetchone()
        if row is None:
            raise NotFoundError("代表团不存在")
        return row

    def _clause(self, connection, clause_id: str):
        row = connection.execute(
            "SELECT * FROM clauses WHERE clause_id=?", (clause_id,)).fetchone()
        if row is None:
            raise NotFoundError("条款不存在")
        return row

    def _version(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM clause_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("条款版本不存在")
        return row

    def _require_secretariat(self, actor) -> None:
        if actor.role not in SECRETARIAT_ROLES:
            raise PermissionDenied("需要秘书处角色")

    def _require_delegate(self, actor, delegation) -> None:
        if actor.role != "delegate" or actor.organization_id != delegation["organization_id"]:
            raise PermissionDenied("只能由本代表团授权代表执行")

    def _normalize_preconditions(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("preconditions 必须是数组")
        normalized = []
        for item in value:
            if isinstance(item, str):
                kind, note = item, None
            elif isinstance(item, dict):
                kind, note = item.get("kind"), item.get("note")
            else:
                raise ValidationError("前置条件必须是字符串或对象")
            kind = str(kind).strip() if kind is not None else ""
            if not kind or len(kind) > 60:
                raise ValidationError("前置条件类型不能为空且不能超过 60 个字符")
            normalized.append({"kind": kind, "note": note})
        return normalized

    def _has_active_coi(self, connection, actor_id: str, dialogue_id: str,
                        clause_id: str | None) -> bool:
        row = connection.execute(
            "SELECT 1 FROM coi_declarations WHERE actor_id=? AND dialogue_id=? AND status='active' "
            "AND (clause_id IS NULL OR clause_id=?)",
            (actor_id, dialogue_id, clause_id)).fetchone()
        return row is not None

    def _session_sealed(self, connection, dialogue_id: str, session_key: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM seals WHERE dialogue_id=? AND session_key=?",
            (dialogue_id, session_key)).fetchone() is not None

    def _seal_covers(self, connection, dialogue_id: str, session_key: str, created_at: str) -> bool:
        rows = connection.execute(
            "SELECT sealed_at FROM seals WHERE dialogue_id=? AND session_key=?",
            (dialogue_id, session_key)).fetchall()
        moment = _parse_time(created_at, "created_at")
        return any(_parse_time(row["sealed_at"], "sealed_at") >= moment for row in rows)

    def _session_manifest(self, connection, dialogue_id: str, session_key: str,
                          sealed_moment: datetime) -> tuple[list[dict[str, Any]], int, int]:
        statements = connection.execute(
            "SELECT * FROM statements WHERE dialogue_id=? AND session_key=? "
            "ORDER BY created_at, statement_id", (dialogue_id, session_key)).fetchall()
        positions = connection.execute(
            "SELECT * FROM positions WHERE dialogue_id=? AND session_key=? "
            "ORDER BY created_at, position_id", (dialogue_id, session_key)).fetchall()
        entries: list[dict[str, Any]] = []
        statement_count = 0
        for row in statements:
            if _parse_time(row["created_at"], "created_at") <= sealed_moment:
                entries.append({"kind": "statement", "id": row["statement_id"],
                                "hash": _statement_digest(row["statement_id"], row["body"])})
                statement_count += 1
        position_count = 0
        for row in positions:
            if _parse_time(row["created_at"], "created_at") <= sealed_moment:
                entries.append({"kind": "position", "id": row["position_id"],
                                "hash": _position_digest(row)})
                position_count += 1
        return entries, statement_count, position_count

    def _tally(self, connection, dialogue, version_id: str) -> dict[str, Any]:
        eligible = connection.execute(
            "SELECT * FROM delegations WHERE dialogue_id=? AND can_sign=1 ORDER BY delegation_id",
            (dialogue["dialogue_id"],)).fetchall()
        positions = connection.execute(
            "SELECT * FROM positions WHERE version_id=? ORDER BY delegation_id",
            (version_id,)).fetchall()
        eligible_ids = {row["delegation_id"] for row in eligible}
        stances: dict[str, Any] = {}
        counts = {"accept": 0, "conditional_accept": 0, "reserve": 0, "reject": 0}
        for position in positions:
            if position["delegation_id"] not in eligible_ids:
                continue
            stances[position["delegation_id"]] = {"stance": position["stance"],
                                                  "note": position["note"]}
            counts[position["stance"]] += 1
        support = counts["accept"] + counts["conditional_accept"]
        required = max(1, math.ceil(dialogue["support_threshold"] * len(eligible)))
        qualifies = (len(stances) >= dialogue["quorum"]
                     and counts["reject"] == 0 and support >= required)
        return {"version_id": version_id, "eligible_total": len(eligible),
                "quorum": dialogue["quorum"], "support_threshold": dialogue["support_threshold"],
                "required_support": required, "support": support, "counts": counts,
                "positions": len(stances), "qualifies": qualifies, "stances": stances}

    # ---------- 对话机制与代表团 ----------

    def create_dialogue(self, *, request_id: str, actor_id: str, dialogue_id: str,
                        title: str, quorum: int = 1,
                        support_threshold: float = 2 / 3) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id, "title": title,
                   "quorum": quorum, "support_threshold": support_threshold}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            dialogue_id = self._identifier(dialogue_id, "dialogue_id")
            title = self._text(title, "title")
            try:
                quorum = int(quorum)
            except (TypeError, ValueError) as exc:
                raise ValidationError("quorum 必须是正整数") from exc
            if quorum < 1:
                raise ValidationError("quorum 必须是正整数")
            try:
                threshold = float(support_threshold)
            except (TypeError, ValueError) as exc:
                raise ValidationError("support_threshold 必须在 (0, 1] 之间") from exc
            if not 0 < threshold <= 1:
                raise ValidationError("support_threshold 必须在 (0, 1] 之间")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO dialogues(dialogue_id,title,status,quorum,support_threshold,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (dialogue_id, title, "open", quorum, threshold, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("对话机制编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="dialogue.created",
                             resource_type="dialogue", resource_id=dialogue_id,
                             detail={"title": title, "quorum": quorum,
                                     "support_threshold": threshold},
                             occurred_at=self._now())
                return "dialogue", dialogue_id, {"dialogue_id": dialogue_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_dialogue", payload=payload, create=create)

    def enroll_delegation(self, *, request_id: str, actor_id: str, dialogue_id: str,
                          delegation_id: str, organization_id: str, name: str,
                          can_sign: bool = True,
                          preconditions: list[Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id,
                   "delegation_id": delegation_id, "organization_id": organization_id,
                   "name": name, "can_sign": can_sign, "preconditions": preconditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            self._dialogue(connection, dialogue_id)
            delegation_id = self._identifier(delegation_id, "delegation_id")
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("合作机构不存在")
            can_sign_value = 1 if can_sign else 0
            normalized = self._normalize_preconditions(preconditions)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO delegations(delegation_id,dialogue_id,organization_id,name,"
                        "can_sign,preconditions_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        (delegation_id, dialogue_id, organization_id, name, can_sign_value,
                         canonical_json(normalized), self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("代表团编号或机构已经登记") from exc
                append_event(connection, actor_id=actor_id, action="delegation.enrolled",
                             resource_type="delegation", resource_id=delegation_id,
                             detail={"dialogue_id": dialogue_id, "organization_id": organization_id,
                                     "can_sign": can_sign_value, "preconditions": normalized},
                             occurred_at=self._now())
                return "delegation", delegation_id, {"delegation_id": delegation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="enroll_delegation", payload=payload, create=create)

    # ---------- 发言授权与发言 ----------

    def grant_speaking(self, *, request_id: str, actor_id: str, dialogue_id: str,
                       grant_id: str, delegation_id: str, delegate_actor_id: str,
                       valid_from: str, valid_until: str,
                       scope: str = "dialogue") -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id, "grant_id": grant_id,
                   "delegation_id": delegation_id, "delegate_actor_id": delegate_actor_id,
                   "valid_from": valid_from, "valid_until": valid_until, "scope": scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            self._dialogue(connection, dialogue_id)
            delegation = self._delegation(connection, dialogue_id, delegation_id)
            target = self._actor(connection, delegate_actor_id)
            if target.role != "delegate":
                raise ValidationError("被授权人必须是代表角色")
            if target.organization_id != delegation["organization_id"]:
                raise ValidationError("被授权人不属于该代表团")
            start = _parse_time(valid_from, "valid_from")
            end = _parse_time(valid_until, "valid_until")
            if not start < end:
                raise ValidationError("授权有效期必须满足 valid_from 早于 valid_until")
            scope = str(scope).strip() or "dialogue"
            if scope != "dialogue":
                if not scope.startswith("clause:"):
                    raise ValidationError("scope 必须是 dialogue 或 clause:<clause_id>")
                clause = self._clause(connection, scope.split(":", 1)[1])
                if clause["dialogue_id"] != dialogue_id:
                    raise ValidationError("授权范围条款不属于该对话")
            grant_id = self._identifier(grant_id, "grant_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO speaking_grants(grant_id,dialogue_id,delegation_id,actor_id,"
                        "scope,valid_from,valid_until,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (grant_id, dialogue_id, delegation_id, delegate_actor_id, scope,
                         _iso(start), _iso(end), actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("授权编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="speaking.granted",
                             resource_type="speaking_grant", resource_id=grant_id,
                             detail={"dialogue_id": dialogue_id, "delegation_id": delegation_id,
                                     "delegate_actor_id": delegate_actor_id, "scope": scope},
                             occurred_at=self._now())
                return "speaking_grant", grant_id, {"grant_id": grant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_speaking", payload=payload, create=create)

    def revoke_speaking(self, *, request_id: str, actor_id: str, grant_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "grant_id": grant_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            grant = connection.execute(
                "SELECT * FROM speaking_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if grant is None:
                raise NotFoundError("发言授权不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if grant["revoked_at"] is not None:
                    raise ConflictError("发言授权已撤销")
                connection.execute("UPDATE speaking_grants SET revoked_at=? WHERE grant_id=?",
                                   (self._now(), grant_id))
                append_event(connection, actor_id=actor_id, action="speaking.revoked",
                             resource_type="speaking_grant", resource_id=grant_id,
                             detail={"dialogue_id": grant["dialogue_id"]},
                             occurred_at=self._now())
                return "speaking_grant", grant_id, {"grant_id": grant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_speaking", payload=payload, create=create)

    def add_statement(self, *, request_id: str, actor_id: str, dialogue_id: str,
                      delegation_id: str, session_key: str, body: str, grant_id: str,
                      clause_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id,
                   "delegation_id": delegation_id, "session_key": session_key, "body": body,
                   "grant_id": grant_id, "clause_id": clause_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._dialogue(connection, dialogue_id)
            self._delegation(connection, dialogue_id, delegation_id)
            session_key = self._text(session_key, "session_key", 80)
            body = self._text(body, "body", 2000)
            grant = connection.execute(
                "SELECT * FROM speaking_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if grant is None or grant["dialogue_id"] != dialogue_id:
                raise NotFoundError("发言授权不存在")
            if grant["actor_id"] != actor.actor_id:
                raise PermissionDenied("只能使用本人的发言授权")
            if grant["delegation_id"] != delegation_id:
                raise PermissionDenied("发言授权不属于该代表团")
            if grant["revoked_at"] is not None:
                raise PermissionDenied("发言授权已撤销")
            now = self.clock.now()
            if not (_parse_time(grant["valid_from"], "valid_from") <= now
                    <= _parse_time(grant["valid_until"], "valid_until")):
                raise PermissionDenied("发言授权不在有效期内")
            if grant["scope"] != "dialogue":
                if clause_id is None or grant["scope"] != f"clause:{clause_id}":
                    raise PermissionDenied("发言授权范围不覆盖该条款")
            if clause_id is not None:
                clause = self._clause(connection, clause_id)
                if clause["dialogue_id"] != dialogue_id:
                    raise ValidationError("条款不属于该对话")
            if self._session_sealed(connection, dialogue_id, session_key):
                raise ConflictError("场次已封存，不能再追加发言")

            def create() -> tuple[str, str, dict[str, Any]]:
                statement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO statements(statement_id,dialogue_id,session_key,delegation_id,"
                    "actor_id,clause_id,grant_id,body,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (statement_id, dialogue_id, session_key, delegation_id, actor.actor_id,
                     clause_id, grant_id, body, _statement_digest(statement_id, body), self._now()))
                append_event(connection, actor_id=actor_id, action="statement.recorded",
                             resource_type="statement", resource_id=statement_id,
                             detail={"dialogue_id": dialogue_id, "session_key": session_key,
                                     "delegation_id": delegation_id, "clause_id": clause_id},
                             occurred_at=self._now())
                return "statement", statement_id, {"statement_id": statement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_statement", payload=payload, create=create)

    def revise_statement(self, *, request_id: str, actor_id: str, statement_id: str,
                         body: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "statement_id": statement_id, "body": body}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            row = connection.execute(
                "SELECT * FROM statements WHERE statement_id=?", (statement_id,)).fetchone()
            if row is None:
                raise NotFoundError("发言记录不存在")
            body = self._text(body, "body", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                if self._seal_covers(connection, row["dialogue_id"], row["session_key"],
                                     row["created_at"]):
                    raise ConflictError("发言事实已封存，不受后续文字整理影响")
                new_hash = _statement_digest(statement_id, body)
                connection.execute(
                    "UPDATE statements SET body=?, content_hash=? WHERE statement_id=?",
                    (body, new_hash, statement_id))
                append_event(connection, actor_id=actor_id, action="statement.revised",
                             resource_type="statement", resource_id=statement_id,
                             detail={"old_hash": row["content_hash"], "new_hash": new_hash},
                             occurred_at=self._now())
                return "statement", statement_id, {"statement_id": statement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="revise_statement", payload=payload, create=create)

    # ---------- 提案、条款与修订 ----------

    def create_proposal(self, *, request_id: str, actor_id: str, dialogue_id: str,
                        proposal_id: str, title: str,
                        proposer_delegation_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id,
                   "proposal_id": proposal_id, "title": title,
                   "proposer_delegation_id": proposer_delegation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._dialogue(connection, dialogue_id)
            delegation = self._delegation(connection, dialogue_id, proposer_delegation_id)
            if actor.role not in SECRETARIAT_ROLES:
                self._require_delegate(actor, delegation)
            proposal_id = self._identifier(proposal_id, "proposal_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO proposals(proposal_id,dialogue_id,title,"
                        "proposer_delegation_id,status,created_at) VALUES(?,?,?,?,?,?)",
                        (proposal_id, dialogue_id, title, proposer_delegation_id, "open",
                         self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("提案编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="proposal.created",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"dialogue_id": dialogue_id, "title": title,
                                     "proposer_delegation_id": proposer_delegation_id},
                             occurred_at=self._now())
                return "proposal", proposal_id, {"proposal_id": proposal_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_proposal", payload=payload, create=create)

    def add_clause(self, *, request_id: str, actor_id: str, proposal_id: str,
                   clause_id: str, clause_key: str, language: str, text: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "clause_id": clause_id,
                   "clause_key": clause_key, "language": language, "text": text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if proposal is None:
                raise NotFoundError("提案不存在")
            delegation = self._delegation(connection, proposal["dialogue_id"],
                                          proposal["proposer_delegation_id"])
            if actor.role not in SECRETARIAT_ROLES:
                self._require_delegate(actor, delegation)
            clause_id = self._identifier(clause_id, "clause_id")
            clause_key = self._text(clause_key, "clause_key", 40)
            language = self._text(language, "language", 20)
            text = self._text(text, "text", 5000)

            def create() -> tuple[str, str, dict[str, Any]]:
                version_id = uuid.uuid4().hex
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO clauses(clause_id,proposal_id,dialogue_id,clause_key,"
                        "head_version_id,created_at) VALUES(?,?,?,?,?,?)",
                        (clause_id, proposal_id, proposal["dialogue_id"], clause_key,
                         version_id, now))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("条款编号已经存在") from exc
                connection.execute(
                    "INSERT INTO clause_versions(version_id,clause_id,version_no,language,text,"
                    "amendment_id,content_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (version_id, clause_id, 1, language, text, None,
                     digest({"language": language, "text": text}), actor_id, now))
                append_event(connection, actor_id=actor_id, action="clause.added",
                             resource_type="clause", resource_id=clause_id,
                             detail={"proposal_id": proposal_id, "clause_key": clause_key,
                                     "version_id": version_id},
                             occurred_at=now)
                return "clause", clause_id, {"clause_id": clause_id, "version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_clause", payload=payload, create=create)

    def propose_amendment(self, *, request_id: str, actor_id: str, clause_id: str,
                          amendment_id: str, delegation_id: str, proposed_text: str,
                          language: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "amendment_id": amendment_id,
                   "delegation_id": delegation_id, "proposed_text": proposed_text,
                   "language": language}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            clause = self._clause(connection, clause_id)
            delegation = self._delegation(connection, clause["dialogue_id"], delegation_id)
            self._require_delegate(actor, delegation)
            if not delegation["can_sign"]:
                raise PermissionDenied("没有签署资格的代表团不能提出修订")
            amendment_id = self._identifier(amendment_id, "amendment_id")
            proposed_text = self._text(proposed_text, "proposed_text", 5000)
            head = self._version(connection, clause["head_version_id"])
            language = self._text(language, "language", 20) if language else head["language"]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO amendments(amendment_id,clause_id,base_version_id,"
                        "proposer_delegation_id,language,proposed_text,status,created_at,"
                        "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (amendment_id, clause_id, clause["head_version_id"], delegation_id,
                         language, proposed_text, "proposed", now, now))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("修订编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="amendment.proposed",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": clause_id,
                                     "base_version_id": clause["head_version_id"]},
                             occurred_at=now)
                return "amendment", amendment_id, {"amendment_id": amendment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_amendment", payload=payload, create=create)

    def second_amendment(self, *, request_id: str, actor_id: str, amendment_id: str,
                         delegation_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "amendment_id": amendment_id,
                   "delegation_id": delegation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            amendment = connection.execute(
                "SELECT * FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
            if amendment is None:
                raise NotFoundError("修订不存在")
            clause = self._clause(connection, amendment["clause_id"])
            delegation = self._delegation(connection, clause["dialogue_id"], delegation_id)
            self._require_delegate(actor, delegation)
            if not delegation["can_sign"]:
                raise PermissionDenied("没有签署资格的代表团不能附议")
            if amendment["proposer_delegation_id"] == delegation_id:
                raise ValidationError("附议必须来自其他代表团")
            if amendment["status"] not in ("proposed", "seconded"):
                raise ConflictError("当前状态的修订不能附议")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO amendment_seconds(amendment_id,delegation_id,actor_id,"
                        "created_at) VALUES(?,?,?,?)",
                        (amendment_id, delegation_id, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该代表团已附议过此修订") from exc
                connection.execute(
                    "UPDATE amendments SET status='seconded', updated_at=? WHERE amendment_id=?",
                    (self._now(), amendment_id))
                append_event(connection, actor_id=actor_id, action="amendment.seconded",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"delegation_id": delegation_id}, occurred_at=self._now())
                return "amendment", amendment_id, {"amendment_id": amendment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="second_amendment", payload=payload, create=create)

    def withdraw_amendment(self, *, request_id: str, actor_id: str,
                           amendment_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "amendment_id": amendment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            amendment = connection.execute(
                "SELECT * FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
            if amendment is None:
                raise NotFoundError("修订不存在")
            clause = self._clause(connection, amendment["clause_id"])
            delegation = self._delegation(connection, clause["dialogue_id"],
                                          amendment["proposer_delegation_id"])
            if actor.role not in SECRETARIAT_ROLES:
                self._require_delegate(actor, delegation)

            def create() -> tuple[str, str, dict[str, Any]]:
                if amendment["status"] not in ("proposed", "seconded", "conflicted"):
                    raise ConflictError("当前状态的修订不能撤回")
                connection.execute(
                    "UPDATE amendments SET status='withdrawn', updated_at=? WHERE amendment_id=?",
                    (self._now(), amendment_id))
                append_event(connection, actor_id=actor_id, action="amendment.withdrawn",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": amendment["clause_id"]},
                             occurred_at=self._now())
                return "amendment", amendment_id, {"amendment_id": amendment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_amendment", payload=payload, create=create)

    def merge_amendment(self, *, request_id: str, actor_id: str,
                        amendment_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "amendment_id": amendment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            amendment = connection.execute(
                "SELECT * FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
            if amendment is None:
                raise NotFoundError("修订不存在")
            clause = self._clause(connection, amendment["clause_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if amendment["status"] == "conflicted":
                    raise ConflictError("修订与已合并文本冲突，不能合并")
                if amendment["status"] == "withdrawn":
                    raise ConflictError("修订已撤回，不能合并")
                if amendment["status"] == "merged":
                    raise ConflictError("修订已合并")
                seconds = connection.execute(
                    "SELECT COUNT(*) AS count FROM amendment_seconds WHERE amendment_id=?",
                    (amendment_id,)).fetchone()["count"]
                if seconds < 1:
                    raise ConflictError("修订须至少获得一个其他代表团附议后才能合并")
                if clause["head_version_id"] != amendment["base_version_id"]:
                    raise ConflictError("修订基础文本已变化，不能合并")
                head = self._version(connection, clause["head_version_id"])
                version_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO clause_versions(version_id,clause_id,version_no,language,text,"
                    "amendment_id,content_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (version_id, clause["clause_id"], head["version_no"] + 1,
                     amendment["language"], amendment["proposed_text"], amendment_id,
                     digest({"language": amendment["language"],
                             "text": amendment["proposed_text"]}), actor_id, now))
                connection.execute("UPDATE clauses SET head_version_id=? WHERE clause_id=?",
                                   (version_id, clause["clause_id"]))
                connection.execute(
                    "UPDATE amendments SET status='merged', merged_version_id=?, updated_at=? "
                    "WHERE amendment_id=?", (version_id, now, amendment_id))
                connection.execute(
                    "UPDATE amendments SET status='conflicted', updated_at=? "
                    "WHERE clause_id=? AND base_version_id=? AND status IN ('proposed','seconded')",
                    (now, clause["clause_id"], amendment["base_version_id"]))
                append_event(connection, actor_id=actor_id, action="amendment.merged",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": clause["clause_id"],
                                     "new_version_id": version_id,
                                     "version_no": head["version_no"] + 1},
                             occurred_at=now)
                return "clause_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="merge_amendment", payload=payload, create=create)

    # ---------- 翻译对应 ----------

    def submit_translation(self, *, request_id: str, actor_id: str, version_id: str,
                           language: str, text: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "language": language, "text": text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in TRANSLATION_ROLES:
                raise PermissionDenied("需要翻译审校角色")
            version = self._version(connection, version_id)
            language = self._text(language, "language", 20)
            text = self._text(text, "text", 5000)
            if language == version["language"]:
                raise ValidationError("翻译语言不能与原文语言相同")

            def create() -> tuple[str, str, dict[str, Any]]:
                translation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO translations(translation_id,version_id,language,text,status,"
                    "content_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (translation_id, version_id, language, text, "draft",
                     digest({"version_id": version_id, "language": language, "text": text}),
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="translation.submitted",
                             resource_type="translation", resource_id=translation_id,
                             detail={"version_id": version_id, "language": language},
                             occurred_at=self._now())
                return "translation", translation_id, {"translation_id": translation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_translation", payload=payload, create=create)

    def verify_translation(self, *, request_id: str, actor_id: str, translation_id: str,
                           approve: bool = True) -> WriteReceipt:
        payload = {"actor_id": actor_id, "translation_id": translation_id, "approve": approve}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in TRANSLATION_ROLES:
                raise PermissionDenied("需要翻译审校角色")
            translation = connection.execute(
                "SELECT * FROM translations WHERE translation_id=?", (translation_id,)).fetchone()
            if translation is None:
                raise NotFoundError("翻译不存在")
            version = self._version(connection, translation["version_id"])
            clause = self._clause(connection, version["clause_id"])
            if translation["created_by"] == actor.actor_id:
                raise PermissionDenied("不能审校本人提交的翻译")
            if self._has_active_coi(connection, actor.actor_id, clause["dialogue_id"],
                                    clause["clause_id"]):
                raise PermissionDenied("已申报利益冲突，不能审校该条款翻译")
            approve = bool(approve)

            def create() -> tuple[str, str, dict[str, Any]]:
                if translation["status"] != "draft":
                    raise ConflictError("翻译不在待审状态")
                if approve:
                    try:
                        connection.execute(
                            "UPDATE translations SET status='verified', verified_by=?, "
                            "verified_at=? WHERE translation_id=?",
                            (actor_id, self._now(), translation_id))
                    except sqlite3.IntegrityError as exc:
                        raise ConflictError("该语言已有审定译文") from exc
                    action = "translation.verified"
                else:
                    connection.execute(
                        "UPDATE translations SET status='rejected', verified_by=?, "
                        "verified_at=? WHERE translation_id=?",
                        (actor_id, self._now(), translation_id))
                    action = "translation.rejected"
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="translation", resource_id=translation_id,
                             detail={"version_id": translation["version_id"],
                                     "language": translation["language"]},
                             occurred_at=self._now())
                return "translation", translation_id, {"translation_id": translation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_translation", payload=payload, create=create)

    # ---------- 立场表态与支持计算 ----------

    def cast_position(self, *, request_id: str, actor_id: str, version_id: str,
                      delegation_id: str, stance: str, session_key: str,
                      note: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "delegation_id": delegation_id, "stance": stance,
                   "session_key": session_key, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            version = self._version(connection, version_id)
            clause = self._clause(connection, version["clause_id"])
            dialogue = self._dialogue(connection, clause["dialogue_id"])
            delegation = self._delegation(connection, dialogue["dialogue_id"], delegation_id)
            self._require_delegate(actor, delegation)
            if not delegation["can_sign"]:
                raise PermissionDenied("该代表团没有签署资格，不能参与表决")
            if stance not in STANCES:
                raise ValidationError("stance 不在允许范围内")
            note = None if note is None else self._text(note, "note", 500)
            if stance in ("conditional_accept", "reserve") and not note:
                raise ValidationError("条件接受或保留必须说明内容")
            session_key = self._text(session_key, "session_key", 80)
            if self._has_active_coi(connection, actor.actor_id, dialogue["dialogue_id"],
                                    clause["clause_id"]):
                raise PermissionDenied("已申报利益冲突，不能就该条款表态")
            if self._session_sealed(connection, dialogue["dialogue_id"], session_key):
                raise ConflictError("场次已封存，不能再追加表决")

            def create() -> tuple[str, str, dict[str, Any]]:
                consensus = connection.execute(
                    "SELECT 1 FROM consensus_records WHERE clause_id=? AND version_id=?",
                    (clause["clause_id"], version_id)).fetchone()
                if consensus is not None:
                    raise ConflictError("该文本快照已形成共识，立场不得更改")
                existing = connection.execute(
                    "SELECT * FROM positions WHERE version_id=? AND delegation_id=?",
                    (version_id, delegation_id)).fetchone()
                now = self._now()
                if existing is not None:
                    if self._seal_covers(connection, dialogue["dialogue_id"], session_key,
                                         existing["created_at"]):
                        raise ConflictError("表决事实已封存，不受后续文字整理影响")
                    connection.execute(
                        "UPDATE positions SET stance=?, note=?, actor_id=?, created_at=? "
                        "WHERE position_id=?",
                        (stance, note, actor_id, now, existing["position_id"]))
                    append_event(connection, actor_id=actor_id, action="position.updated",
                                 resource_type="position", resource_id=existing["position_id"],
                                 detail={"version_id": version_id,
                                         "delegation_id": delegation_id, "stance": stance},
                                 occurred_at=now)
                    return "position", existing["position_id"], {
                        "position_id": existing["position_id"]}
                position_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO positions(position_id,dialogue_id,clause_id,version_id,"
                    "delegation_id,stance,note,session_key,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (position_id, dialogue["dialogue_id"], clause["clause_id"], version_id,
                     delegation_id, stance, note, session_key, actor_id, now))
                append_event(connection, actor_id=actor_id, action="position.cast",
                             resource_type="position", resource_id=position_id,
                             detail={"version_id": version_id, "delegation_id": delegation_id,
                                     "stance": stance},
                             occurred_at=now)
                return "position", position_id, {"position_id": position_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="cast_position", payload=payload, create=create)

    def tally(self, version_id: str) -> dict[str, Any]:
        """按文本快照计算支持情况。"""

        connection = self.database.connection
        version = self._version(connection, version_id)
        clause = self._clause(connection, version["clause_id"])
        dialogue = self._dialogue(connection, clause["dialogue_id"])
        return self._tally(connection, dialogue, version_id)

    # ---------- 共识与承诺 ----------

    def form_consensus(self, *, request_id: str, actor_id: str,
                       version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            version = self._version(connection, version_id)
            clause = self._clause(connection, version["clause_id"])
            dialogue = self._dialogue(connection, clause["dialogue_id"])
            if self._has_active_coi(connection, actor.actor_id, dialogue["dialogue_id"],
                                    clause["clause_id"]):
                raise PermissionDenied("已申报利益冲突，不能主持该条款的共识形成")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM consensus_records WHERE clause_id=? AND version_id=?",
                    (clause["clause_id"], version_id)).fetchone()
                if existing:
                    raise ConflictError("该文本快照已经形成共识记录")
                tally = self._tally(connection, dialogue, version_id)
                if not tally["qualifies"]:
                    raise ConflictError("未达到形成共识的程序要求")
                consensus_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "UPDATE consensus_records SET status='superseded' "
                    "WHERE clause_id=? AND status='pending_ratification'",
                    (clause["clause_id"],))
                connection.execute(
                    "INSERT INTO consensus_records(consensus_id,dialogue_id,clause_id,"
                    "version_id,status,tally_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (consensus_id, dialogue["dialogue_id"], clause["clause_id"], version_id,
                     "pending_ratification", canonical_json(tally), actor_id, now))
                commitment_count = 0
                for delegation_id, stance_info in tally["stances"].items():
                    stance = stance_info["stance"]
                    if stance not in COMMITTED_STANCES:
                        continue
                    delegation = self._delegation(connection, dialogue["dialogue_id"],
                                                  delegation_id)
                    preconditions = json.loads(delegation["preconditions_json"])
                    conditions: list[dict[str, Any]] = []
                    if stance == "conditional_accept":
                        conditions.append({"kind": "acceptance_condition",
                                           "note": stance_info["note"]})
                    conditions.extend(preconditions)
                    commitment_id = uuid.uuid4().hex
                    status = "pending" if conditions else "in_force"
                    effective_at = None if conditions else now
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,consensus_id,dialogue_id,"
                        "clause_id,version_id,delegation_id,stance,note,status,effective_at,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (commitment_id, consensus_id, dialogue["dialogue_id"],
                         clause["clause_id"], version_id, delegation_id, stance,
                         stance_info["note"], status, effective_at, now))
                    for seq, condition in enumerate(conditions):
                        connection.execute(
                            "INSERT INTO commitment_conditions(condition_id,commitment_id,seq,"
                            "kind,note,status) VALUES(?,?,?,?,?,?)",
                            (uuid.uuid4().hex, commitment_id, seq, condition["kind"],
                             condition.get("note"), "pending"))
                    commitment_count += 1
                    if not conditions:
                        append_event(connection, actor_id=actor_id,
                                     action="commitment.in_force",
                                     resource_type="commitment", resource_id=commitment_id,
                                     detail={"delegation_id": delegation_id,
                                             "clause_id": clause["clause_id"],
                                             "reason": "无前置条件，共识形成即生效"},
                                     occurred_at=now)
                append_event(connection, actor_id=actor_id, action="consensus.formed",
                             resource_type="consensus", resource_id=consensus_id,
                             detail={"clause_id": clause["clause_id"], "version_id": version_id,
                                     "support": tally["support"],
                                     "required_support": tally["required_support"],
                                     "commitments": commitment_count},
                             occurred_at=now)
                return "consensus", consensus_id, {"consensus_id": consensus_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="form_consensus", payload=payload, create=create)

    def fulfill_condition(self, *, request_id: str, actor_id: str, commitment_id: str,
                          seq: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "seq": seq}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            commitment = connection.execute(
                "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
            if commitment is None:
                raise NotFoundError("承诺不存在")
            try:
                seq = int(seq)
            except (TypeError, ValueError) as exc:
                raise ValidationError("seq 必须是整数") from exc

            def create() -> tuple[str, str, dict[str, Any]]:
                condition = connection.execute(
                    "SELECT * FROM commitment_conditions WHERE commitment_id=? AND seq=?",
                    (commitment_id, seq)).fetchone()
                if condition is None:
                    raise NotFoundError("前置条件不存在")
                if condition["status"] == "satisfied":
                    raise ConflictError("前置条件已满足")
                blockers = connection.execute(
                    "SELECT COUNT(*) AS count FROM commitment_conditions "
                    "WHERE commitment_id=? AND seq<? AND status!='satisfied'",
                    (commitment_id, seq)).fetchone()["count"]
                if blockers:
                    raise ConflictError("前置条件必须按顺序满足")
                now = self._now()
                connection.execute(
                    "UPDATE commitment_conditions SET status='satisfied', satisfied_at=?, "
                    "satisfied_by=? WHERE condition_id=?",
                    (now, actor_id, condition["condition_id"]))
                append_event(connection, actor_id=actor_id,
                             action="commitment.condition_satisfied",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"seq": seq, "kind": condition["kind"]}, occurred_at=now)
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM commitment_conditions "
                    "WHERE commitment_id=? AND status!='satisfied'",
                    (commitment_id,)).fetchone()["count"]
                status = commitment["status"]
                if remaining == 0 and status != "in_force":
                    connection.execute(
                        "UPDATE commitments SET status='in_force', effective_at=? "
                        "WHERE commitment_id=?", (now, commitment_id))
                    append_event(connection, actor_id=actor_id, action="commitment.in_force",
                                 resource_type="commitment", resource_id=commitment_id,
                                 detail={"delegation_id": commitment["delegation_id"],
                                         "clause_id": commitment["clause_id"]},
                                 occurred_at=now)
                    status = "in_force"
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                     "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="fulfill_condition", payload=payload, create=create)

    # ---------- 封存 ----------

    def seal_session(self, *, request_id: str, actor_id: str, dialogue_id: str,
                     session_key: str, sealed_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id,
                   "session_key": session_key, "sealed_at": sealed_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            self._dialogue(connection, dialogue_id)
            session_key = self._text(session_key, "session_key", 80)
            sealed_moment = _parse_time(sealed_at, "sealed_at") if sealed_at else self.clock.now()
            sealed_at_text = _iso(sealed_moment)
            if self._has_active_coi(connection, actor.actor_id, dialogue_id, None):
                raise PermissionDenied("已申报利益冲突，不能主持封存")

            def create() -> tuple[str, str, dict[str, Any]]:
                entries, statement_count, position_count = self._session_manifest(
                    connection, dialogue_id, session_key, sealed_moment)
                seal_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO seals(seal_id,dialogue_id,session_key,sealed_at,"
                        "manifest_hash,statement_count,position_count,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (seal_id, dialogue_id, session_key, sealed_at_text, digest(entries),
                         statement_count, position_count, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该场次已经封存，并发封存只能有一个结果") from exc
                append_event(connection, actor_id=actor_id, action="session.sealed",
                             resource_type="seal", resource_id=seal_id,
                             detail={"dialogue_id": dialogue_id, "session_key": session_key,
                                     "sealed_at": sealed_at_text,
                                     "statement_count": statement_count,
                                     "position_count": position_count},
                             occurred_at=self._now())
                return "seal", seal_id, {"seal_id": seal_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_session", payload=payload, create=create)

    def verify_seal(self, seal_id: str) -> dict[str, Any]:
        """重算封存清单，确认封存事实未被后续整理改动。"""

        connection = self.database.connection
        seal = connection.execute("SELECT * FROM seals WHERE seal_id=?", (seal_id,)).fetchone()
        if seal is None:
            raise NotFoundError("封存记录不存在")
        entries, statement_count, position_count = self._session_manifest(
            connection, seal["dialogue_id"], seal["session_key"],
            _parse_time(seal["sealed_at"], "sealed_at"))
        valid = (digest(entries) == seal["manifest_hash"]
                 and statement_count == seal["statement_count"]
                 and position_count == seal["position_count"])
        return {"seal_id": seal_id, "dialogue_id": seal["dialogue_id"],
                "session_key": seal["session_key"], "sealed_at": seal["sealed_at"],
                "valid": valid, "statement_count": seal["statement_count"],
                "position_count": seal["position_count"],
                "manifest_hash": seal["manifest_hash"]}

    # ---------- 后续行动 ----------

    def create_action(self, *, request_id: str, actor_id: str, dialogue_id: str,
                      action_id: str, assignee_delegation_id: str, title: str,
                      due_at: str, clause_id: str | None = None,
                      detail: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id, "action_id": action_id,
                   "assignee_delegation_id": assignee_delegation_id, "title": title,
                   "due_at": due_at, "clause_id": clause_id, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            self._dialogue(connection, dialogue_id)
            self._delegation(connection, dialogue_id, assignee_delegation_id)
            action_id = self._identifier(action_id, "action_id")
            title = self._text(title, "title")
            detail = None if detail is None else self._text(detail, "detail", 1000)
            due_text = _iso(_parse_time(due_at, "due_at"))
            if clause_id is not None:
                clause = self._clause(connection, clause_id)
                if clause["dialogue_id"] != dialogue_id:
                    raise ValidationError("条款不属于该对话")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO followup_actions(action_id,dialogue_id,clause_id,"
                        "assignee_delegation_id,title,detail,due_at,status,created_by,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (action_id, dialogue_id, clause_id, assignee_delegation_id, title,
                         detail, due_text, "open", actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("行动编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="action.created",
                             resource_type="followup_action", resource_id=action_id,
                             detail={"dialogue_id": dialogue_id,
                                     "assignee_delegation_id": assignee_delegation_id,
                                     "due_at": due_text},
                             occurred_at=self._now())
                return "followup_action", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_action", payload=payload, create=create)

    def complete_action(self, *, request_id: str, actor_id: str,
                        action_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "action_id": action_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            action = connection.execute(
                "SELECT * FROM followup_actions WHERE action_id=?", (action_id,)).fetchone()
            if action is None:
                raise NotFoundError("后续行动不存在")
            delegation = self._delegation(connection, action["dialogue_id"],
                                          action["assignee_delegation_id"])
            if actor.role not in SECRETARIAT_ROLES:
                self._require_delegate(actor, delegation)

            def create() -> tuple[str, str, dict[str, Any]]:
                if action["status"] != "open":
                    raise ConflictError("后续行动不在待处理状态")
                connection.execute(
                    "UPDATE followup_actions SET status='done', completed_at=? "
                    "WHERE action_id=?", (self._now(), action_id))
                append_event(connection, actor_id=actor_id, action="action.completed",
                             resource_type="followup_action", resource_id=action_id,
                             detail={"dialogue_id": action["dialogue_id"]},
                             occurred_at=self._now())
                return "followup_action", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_action", payload=payload, create=create)

    def list_actions(self, dialogue_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """列出后续行动；逾期状态按当前时间即时计算，服务恢复后继续有效。"""

        connection = self.database.connection
        self._dialogue(connection, dialogue_id)
        rows = connection.execute(
            "SELECT * FROM followup_actions WHERE dialogue_id=? ORDER BY due_at, action_id",
            (dialogue_id,)).fetchall()
        now = self.clock.now()
        items = []
        for row in rows:
            effective = row["status"]
            if row["status"] == "open" and _parse_time(row["due_at"], "due_at") < now:
                effective = "overdue"
            if status and effective != status:
                continue
            items.append({"action_id": row["action_id"], "dialogue_id": row["dialogue_id"],
                          "clause_id": row["clause_id"],
                          "assignee_delegation_id": row["assignee_delegation_id"],
                          "title": row["title"], "detail": row["detail"],
                          "due_at": row["due_at"], "status": row["status"],
                          "effective_status": effective,
                          "completed_at": row["completed_at"],
                          "created_at": row["created_at"]})
        return items

    # ---------- 利益冲突 ----------

    def declare_coi(self, *, request_id: str, actor_id: str, dialogue_id: str,
                    declaration_id: str, target_actor_id: str, description: str,
                    clause_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dialogue_id": dialogue_id,
                   "declaration_id": declaration_id, "target_actor_id": target_actor_id,
                   "description": description, "clause_id": clause_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._dialogue(connection, dialogue_id)
            target = self._actor(connection, target_actor_id)
            if actor.actor_id != target.actor_id and actor.role not in SECRETARIAT_ROLES:
                raise PermissionDenied("只能为本人申报利益冲突")
            description = self._text(description, "description", 500)
            if clause_id is not None:
                clause = self._clause(connection, clause_id)
                if clause["dialogue_id"] != dialogue_id:
                    raise ValidationError("条款不属于该对话")
            declaration_id = self._identifier(declaration_id, "declaration_id")
            delegation = connection.execute(
                "SELECT * FROM delegations WHERE dialogue_id=? AND organization_id=?",
                (dialogue_id, target.organization_id)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO coi_declarations(declaration_id,dialogue_id,clause_id,"
                        "actor_id,delegation_id,description,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (declaration_id, dialogue_id, clause_id, target.actor_id,
                         delegation["delegation_id"] if delegation else None, description,
                         "active", actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("申报编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="coi.declared",
                             resource_type="coi_declaration", resource_id=declaration_id,
                             detail={"dialogue_id": dialogue_id, "clause_id": clause_id,
                                     "target_actor_id": target.actor_id},
                             occurred_at=self._now())
                return "coi_declaration", declaration_id, {"declaration_id": declaration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_coi", payload=payload, create=create)

    def clear_coi(self, *, request_id: str, actor_id: str,
                  declaration_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "declaration_id": declaration_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_secretariat(actor)
            declaration = connection.execute(
                "SELECT * FROM coi_declarations WHERE declaration_id=?",
                (declaration_id,)).fetchone()
            if declaration is None:
                raise NotFoundError("利益冲突申报不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if declaration["status"] != "active":
                    raise ConflictError("利益冲突申报已解除")
                connection.execute(
                    "UPDATE coi_declarations SET status='cleared', cleared_at=? "
                    "WHERE declaration_id=?", (self._now(), declaration_id))
                append_event(connection, actor_id=actor_id, action="coi.cleared",
                             resource_type="coi_declaration", resource_id=declaration_id,
                             detail={"dialogue_id": declaration["dialogue_id"]},
                             occurred_at=self._now())
                return "coi_declaration", declaration_id, {"declaration_id": declaration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="clear_coi", payload=payload, create=create)

    # ---------- 查询与视图 ----------

    def get_clause(self, clause_id: str) -> dict[str, Any]:
        connection = self.database.connection
        clause = self._clause(connection, clause_id)
        head = self._version(connection, clause["head_version_id"])
        return {"clause_id": clause["clause_id"], "proposal_id": clause["proposal_id"],
                "dialogue_id": clause["dialogue_id"], "clause_key": clause["clause_key"],
                "head_version_id": clause["head_version_id"],
                "head": {"version_id": head["version_id"], "version_no": head["version_no"],
                         "language": head["language"], "text": head["text"]},
                "created_at": clause["created_at"]}

    def _commitment_view(self, connection, row) -> dict[str, Any]:
        conditions = connection.execute(
            "SELECT * FROM commitment_conditions WHERE commitment_id=? ORDER BY seq",
            (row["commitment_id"],)).fetchall()
        return {"commitment_id": row["commitment_id"], "consensus_id": row["consensus_id"],
                "clause_id": row["clause_id"], "version_id": row["version_id"],
                "delegation_id": row["delegation_id"], "stance": row["stance"],
                "note": row["note"], "status": row["status"],
                "effective_at": row["effective_at"],
                "conditions": [{"seq": c["seq"], "kind": c["kind"], "note": c["note"],
                                "status": c["status"], "satisfied_at": c["satisfied_at"],
                                "satisfied_by": c["satisfied_by"]} for c in conditions],
                "created_at": row["created_at"]}

    def get_commitment(self, commitment_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM commitments WHERE commitment_id=?",
                                 (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return self._commitment_view(connection, row)

    def list_commitments(self, dialogue_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._dialogue(connection, dialogue_id)
        rows = connection.execute(
            "SELECT * FROM commitments WHERE dialogue_id=? ORDER BY created_at, commitment_id",
            (dialogue_id,)).fetchall()
        return [self._commitment_view(connection, row) for row in rows]

    def _clause_public(self, connection, clause) -> dict[str, Any]:
        head = self._version(connection, clause["head_version_id"])
        translations = connection.execute(
            "SELECT language, text FROM translations WHERE version_id=? AND status='verified' "
            "ORDER BY language", (head["version_id"],)).fetchall()
        consensus_rows = connection.execute(
            "SELECT * FROM consensus_records WHERE clause_id=?", (clause["clause_id"],)).fetchall()
        consensus = None
        if consensus_rows:
            consensus = sorted(consensus_rows,
                               key=lambda row: _parse_time(row["created_at"], "created_at"),
                               reverse=True)[0]
        result: dict[str, Any] = {
            "clause_id": clause["clause_id"],
            "clause_key": clause["clause_key"],
            "stage": "draft",
            "head_version": {"version_id": head["version_id"],
                             "version_no": head["version_no"],
                             "language": head["language"], "text": head["text"]},
            "translations": [{"language": row["language"], "text": row["text"]}
                             for row in translations],
        }
        if consensus is None or consensus["status"] != "pending_ratification":
            return result
        tally = json.loads(consensus["tally_json"])
        commitments = connection.execute(
            "SELECT * FROM commitments WHERE consensus_id=? ORDER BY delegation_id",
            (consensus["consensus_id"],)).fetchall()
        by_delegation = {row["delegation_id"]: row for row in commitments}
        eligible = connection.execute(
            "SELECT * FROM delegations WHERE dialogue_id=? AND can_sign=1 "
            "ORDER BY delegation_id", (clause["dialogue_id"],)).fetchall()
        participants = []
        for delegation in eligible:
            commitment = by_delegation.get(delegation["delegation_id"])
            if commitment is None:
                participants.append({"delegation_id": delegation["delegation_id"],
                                     "name": delegation["name"], "status": "not_committed"})
                continue
            if commitment["status"] == "in_force":
                status = "in_effect"
            elif commitment["stance"] == "reserve":
                status = "reserved"
            else:
                status = "accepted"
            entry: dict[str, Any] = {"delegation_id": delegation["delegation_id"],
                                     "name": delegation["name"], "status": status}
            if commitment["stance"] == "conditional_accept":
                entry["condition"] = commitment["note"]
            if commitment["stance"] == "reserve":
                entry["reservation"] = commitment["note"]
            participants.append(entry)
        result["stage"] = "accepted"
        result["consensus"] = {"consensus_id": consensus["consensus_id"],
                               "version_id": consensus["version_id"],
                               "formed_at": consensus["created_at"],
                               "support": tally["support"],
                               "required_support": tally["required_support"]}
        result["participants"] = participants
        return result

    def public_view(self, dialogue_id: str) -> dict[str, Any]:
        """公众视图：清楚区分草案、已接受、保留与已生效内容。"""

        connection = self.database.connection
        dialogue = self._dialogue(connection, dialogue_id)
        clauses = connection.execute(
            "SELECT * FROM clauses WHERE dialogue_id=? ORDER BY clause_key, clause_id",
            (dialogue_id,)).fetchall()
        return {"dialogue_id": dialogue_id, "title": dialogue["title"],
                "status": dialogue["status"], "generated_at": self._now(),
                "clauses": [self._clause_public(connection, row) for row in clauses]}

    def _translations_for_dialogue(self, connection, dialogue_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT t.* FROM translations t "
            "JOIN clause_versions v ON t.version_id=v.version_id "
            "JOIN clauses c ON v.clause_id=c.clause_id "
            "WHERE c.dialogue_id=? ORDER BY t.created_at, t.translation_id",
            (dialogue_id,)).fetchall()
        return [{"translation_id": row["translation_id"], "version_id": row["version_id"],
                 "language": row["language"], "text": row["text"], "status": row["status"],
                 "created_by": row["created_by"], "verified_by": row["verified_by"],
                 "verified_at": row["verified_at"]} for row in rows]

    def _delegation_view(self, connection, dialogue_id: str, delegation,
                         actor) -> dict[str, Any]:
        positions = connection.execute(
            "SELECT * FROM positions WHERE dialogue_id=? AND delegation_id=? "
            "ORDER BY created_at, position_id", (dialogue_id, delegation["delegation_id"])).fetchall()
        grants = connection.execute(
            "SELECT * FROM speaking_grants WHERE dialogue_id=? AND delegation_id=? "
            "ORDER BY created_at, grant_id", (dialogue_id, delegation["delegation_id"])).fetchall()
        commitments = connection.execute(
            "SELECT * FROM commitments WHERE dialogue_id=? AND delegation_id=? "
            "ORDER BY created_at, commitment_id",
            (dialogue_id, delegation["delegation_id"])).fetchall()
        actions = [item for item in self.list_actions(dialogue_id)
                   if item["assignee_delegation_id"] == delegation["delegation_id"]]
        return {"delegation_id": delegation["delegation_id"], "name": delegation["name"],
                "can_sign": bool(delegation["can_sign"]),
                "positions": [{"position_id": row["position_id"],
                               "version_id": row["version_id"], "stance": row["stance"],
                               "note": row["note"], "created_at": row["created_at"]}
                              for row in positions],
                "grants": [{"grant_id": row["grant_id"], "actor_id": row["actor_id"],
                            "scope": row["scope"], "valid_from": row["valid_from"],
                            "valid_until": row["valid_until"],
                            "revoked_at": row["revoked_at"]} for row in grants],
                "commitments": [self._commitment_view(connection, row) for row in commitments],
                "actions": actions}

    def _secretariat_view(self, connection, dialogue_id: str) -> dict[str, Any]:
        delegations = connection.execute(
            "SELECT * FROM delegations WHERE dialogue_id=? ORDER BY delegation_id",
            (dialogue_id,)).fetchall()
        positions = connection.execute(
            "SELECT * FROM positions WHERE dialogue_id=? ORDER BY created_at, position_id",
            (dialogue_id,)).fetchall()
        amendments = connection.execute(
            "SELECT a.* FROM amendments a JOIN clauses c ON a.clause_id=c.clause_id "
            "WHERE c.dialogue_id=? ORDER BY a.created_at, a.amendment_id",
            (dialogue_id,)).fetchall()
        statements = connection.execute(
            "SELECT * FROM statements WHERE dialogue_id=? ORDER BY created_at, statement_id",
            (dialogue_id,)).fetchall()
        seals = connection.execute(
            "SELECT * FROM seals WHERE dialogue_id=? ORDER BY created_at, seal_id",
            (dialogue_id,)).fetchall()
        coi = connection.execute(
            "SELECT * FROM coi_declarations WHERE dialogue_id=? "
            "ORDER BY created_at, declaration_id", (dialogue_id,)).fetchall()
        amendment_items = []
        for row in amendments:
            seconds = connection.execute(
                "SELECT COUNT(*) AS count FROM amendment_seconds WHERE amendment_id=?",
                (row["amendment_id"],)).fetchone()["count"]
            amendment_items.append({"amendment_id": row["amendment_id"],
                                    "clause_id": row["clause_id"],
                                    "base_version_id": row["base_version_id"],
                                    "proposer_delegation_id": row["proposer_delegation_id"],
                                    "status": row["status"], "seconds": seconds,
                                    "merged_version_id": row["merged_version_id"]})
        return {
            "delegations": [{"delegation_id": row["delegation_id"], "name": row["name"],
                             "organization_id": row["organization_id"],
                             "can_sign": bool(row["can_sign"]),
                             "preconditions": json.loads(row["preconditions_json"])}
                            for row in delegations],
            "positions": [{"position_id": row["position_id"], "version_id": row["version_id"],
                           "delegation_id": row["delegation_id"], "stance": row["stance"],
                           "note": row["note"], "session_key": row["session_key"],
                           "created_at": row["created_at"]} for row in positions],
            "amendments": amendment_items,
            "statements": [{"statement_id": row["statement_id"],
                            "session_key": row["session_key"],
                            "delegation_id": row["delegation_id"], "body": row["body"],
                            "created_at": row["created_at"]} for row in statements],
            "seals": [{"seal_id": row["seal_id"], "session_key": row["session_key"],
                       "sealed_at": row["sealed_at"],
                       "statement_count": row["statement_count"],
                       "position_count": row["position_count"]} for row in seals],
            "coi_declarations": [{"declaration_id": row["declaration_id"],
                                  "actor_id": row["actor_id"], "clause_id": row["clause_id"],
                                  "status": row["status"], "description": row["description"]}
                                 for row in coi],
            "commitments": self.list_commitments(dialogue_id),
            "actions": self.list_actions(dialogue_id),
            "translations": self._translations_for_dialogue(connection, dialogue_id),
        }

    def dialogue_view(self, *, actor_id: str, dialogue_id: str) -> dict[str, Any]:
        """按操作者角色返回不同视图：代表、翻译审校、秘书处和观察员各不相同。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._dialogue(connection, dialogue_id)
        public = self.public_view(dialogue_id)
        if actor.role == "observer":
            return {"role": actor.role, "public": public}
        if actor.role == "translator":
            translations = self._translations_for_dialogue(connection, dialogue_id)
            return {"role": actor.role, "public": public, "translations": translations,
                    "verification_queue": [item for item in translations
                                           if item["status"] == "draft"]}
        if actor.role == "delegate":
            delegation = connection.execute(
                "SELECT * FROM delegations WHERE dialogue_id=? AND organization_id=?",
                (dialogue_id, actor.organization_id)).fetchone()
            if delegation is None:
                raise PermissionDenied("操作者不属于本对话的代表团")
            return {"role": actor.role, "public": public,
                    "my_delegation": self._delegation_view(connection, dialogue_id,
                                                           delegation, actor)}
        if actor.role in FULL_VIEW_ROLES:
            view = {"role": actor.role, "public": public}
            view.update(self._secretariat_view(connection, dialogue_id))
            return view
        raise PermissionDenied("当前角色没有对话视图")

    def explain_binding(self, *, clause_id: str, delegation_id: str,
                        at: str) -> dict[str, Any]:
        """解释某文本在指定时点为何对特定参与方具有或不具有约束。"""

        connection = self.database.connection
        clause = self._clause(connection, clause_id)
        at_moment = _parse_time(at, "at")
        delegation = connection.execute(
            "SELECT * FROM delegations WHERE dialogue_id=? AND delegation_id=?",
            (clause["dialogue_id"], delegation_id)).fetchone()
        if delegation is None:
            raise NotFoundError("代表团不存在")
        consensus_rows = connection.execute(
            "SELECT * FROM consensus_records WHERE clause_id=?", (clause_id,)).fetchall()
        consensus = None
        for row in sorted(consensus_rows,
                          key=lambda item: _parse_time(item["created_at"], "created_at"),
                          reverse=True):
            if _parse_time(row["created_at"], "created_at") <= at_moment:
                consensus = row
                break
        result: dict[str, Any] = {"clause_id": clause_id, "delegation_id": delegation_id,
                                  "at": at, "binding": False, "effective_at": None,
                                  "reservation": None, "consensus": None,
                                  "conditions": [], "reasons": []}
        if consensus is None:
            result["reasons"].append(f"截至 {at} 该条款尚未形成待核准共识，文本仅为草案")
            return result
        result["consensus"] = {"consensus_id": consensus["consensus_id"],
                               "version_id": consensus["version_id"],
                               "status": consensus["status"],
                               "formed_at": consensus["created_at"]}
        commitment = connection.execute(
            "SELECT * FROM commitments WHERE consensus_id=? AND delegation_id=?",
            (consensus["consensus_id"], delegation_id)).fetchone()
        if commitment is None:
            tally = json.loads(consensus["tally_json"])
            stance = tally["stances"].get(delegation_id)
            if stance is None:
                result["reasons"].append("共识形成时该参与方未对该文本快照表态，未加入任何承诺")
            else:
                result["reasons"].append(
                    f"共识形成时该参与方立场为 {stance['stance']}，未加入承诺")
            return result
        result["reasons"].append(
            f"该参与方在共识形成时立场为 {commitment['stance']}，承诺进入待核准状态")
        if commitment["stance"] == "reserve":
            result["reservation"] = commitment["note"]
            result["reasons"].append(
                f"该参与方作出保留：{commitment['note']}，保留部分不受约束")
        conditions = connection.execute(
            "SELECT * FROM commitment_conditions WHERE commitment_id=? ORDER BY seq",
            (commitment["commitment_id"],)).fetchall()
        result["conditions"] = [{"seq": row["seq"], "kind": row["kind"],
                                 "status": row["status"],
                                 "satisfied_at": row["satisfied_at"]} for row in conditions]
        unmet = [row for row in conditions
                 if row["satisfied_at"] is None
                 or _parse_time(row["satisfied_at"], "satisfied_at") > at_moment]
        if unmet:
            first = unmet[0]
            if first["satisfied_at"] is None:
                detail = "至今未满足"
            else:
                detail = f"实际满足时间为 {first['satisfied_at']}"
            result["reasons"].append(
                f"前置条件 {first['seq']}（{first['kind']}）截至该时点未满足"
                f"（{detail}），承诺尚未生效")
            return result
        effective_at = commitment["effective_at"] or consensus["created_at"]
        result["binding"] = True
        result["effective_at"] = effective_at
        result["reasons"].append(
            f"全部前置条件已按顺序满足，承诺自 {effective_at} 起对该参与方具有约束")
        return result
