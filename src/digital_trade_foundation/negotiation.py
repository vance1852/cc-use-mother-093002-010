"""规则文本协商与承诺跟踪领域服务。

在基础登记服务之上建立可追溯的协商过程：提案与条款版本、修订合并、
翻译对应、发言授权、利益冲突、保留意见、签署资格、生效前置条件、
后续行动、封存与时间点审计解释。

关键不变量：
- 所有支持数都基于同一文本快照（version_id）计算；
- 互相冲突的修订（同一基础快照）至多有一个被合并；
- 立场事实只追加，封存与后续文字整理互不影响；
- 达到程序门槛只形成待核准共识，承诺按参与方、按前置条件顺序生效；
- 重复签署不增加支持数；并发封存同一范围只有一个结果。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .storage import Database

AMENDMENT_POSITIONS = frozenset({"support", "oppose", "conditional", "withdrawn"})
CLAUSE_POSITIONS = frozenset({"accept", "conditional_accept", "oppose", "withdrawn"})
CONDITION_KINDS = frozenset({"domestic_ratification", "organizational_approval", "notification", "deposit"})


class NegotiationService:
    """协调协商、承诺生效、视图与审计解释。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not value or len(value) > 64:
            raise ValidationError(f"{field} 不能为空且不能超过 64 个字符")
        return value

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_role(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _delegation_for(self, connection, actor: Actor):
        row = connection.execute(
            "SELECT * FROM delegations WHERE organization_id=? AND active=1", (actor.organization_id,)
        ).fetchone()
        if row is None:
            raise PermissionDenied("该组织没有启用的代表团")
        return row

    def _mandate(self, connection, actor: Actor, delegation_id: str,
                 scope_kind: str, scope_id: str, proposal_id: str | None = None):
        """返回在当前时点覆盖指定范围且有效的发言授权。

        条款级动作同时接受 global、覆盖该条款提案的 proposal 授权，以及条款授权。
        """
        now = self._now()
        rows = connection.execute(
            "SELECT * FROM mandates WHERE actor_id=? AND delegation_id=? "
            "AND revoked_at IS NULL AND valid_from<=? AND valid_until>=?",
            (actor.actor_id, delegation_id, now, now),
        ).fetchall()
        for row in rows:
            if row["scope_kind"] == "global":
                return row
            if row["scope_kind"] == scope_kind and (row["scope_id"] is None or row["scope_id"] == scope_id):
                return row
            if scope_kind == "clause" and row["scope_kind"] == "proposal" \
                    and proposal_id is not None and row["scope_id"] == proposal_id:
                return row
        raise PermissionDenied("没有覆盖该议题且在有效期内的发言授权")

    def _assert_no_conflict(self, connection, actor: Actor, clause_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM conflict_declarations WHERE actor_id=? AND clause_id=? AND status='open' LIMIT 1",
            (actor.actor_id, clause_id),
        ).fetchone()
        if row is not None:
            raise PermissionDenied("存在未清除的利益冲突，不能参与该条款")

    def _clause(self, connection, clause_id: str):
        row = connection.execute("SELECT * FROM clauses WHERE clause_id=?", (clause_id,)).fetchone()
        if row is None:
            raise NotFoundError("条款不存在")
        return row

    def _version(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM text_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("文本快照不存在")
        return row

    def _amendment(self, connection, amendment_id: str):
        row = connection.execute("SELECT * FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
        if row is None:
            raise NotFoundError("修订不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _append_stance_fact(self, connection, *, subject_kind: str, subject_id: str,
                            version_id: str, delegation_id: str, actor_id: str,
                            position: str, detail: dict[str, Any]) -> None:
        material = {"subject_kind": subject_kind, "subject_id": subject_id, "version_id": version_id,
                    "delegation_id": delegation_id, "actor_id": actor_id, "position": position,
                    "detail": detail, "occurred_at": self._now()}
        fact_hash = digest(material)
        connection.execute(
            "INSERT INTO stance_events(fact_id,subject_kind,subject_id,version_id,delegation_id,actor_id,"
            "position,detail_json,fact_hash,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, subject_kind, subject_id, version_id, delegation_id, actor_id,
             position, canonical_json(detail), fact_hash, self._now()),
        )

    def _sweep_locked(self, connection) -> dict[str, int]:
        """在持锁事务内把到期未满足的条件与未完成行动标记为逾期。"""
        now = self._now()
        lapsed = connection.execute(
            "UPDATE ratification_conditions SET status='lapsed', lapsed_at=? "
            "WHERE status='pending' AND due_at IS NOT NULL AND due_at<?",
            (now, now),
        ).rowcount
        overdue = connection.execute(
            "UPDATE follow_up_actions SET status='overdue' WHERE status='open' AND due_at<?",
            (now,),
        ).rowcount
        return {"conditions_lapsed": lapsed, "actions_overdue": overdue}

    def sweep_deadlines(self) -> dict[str, int]:
        """供服务恢复或定时调用：按当前时钟推进期限状态。"""
        with self.database.transaction(immediate=True) as connection:
            result = self._sweep_locked(connection)
        return result

    # ------------------------------------------------------------------ 代表团与授权

    def register_delegation(self, *, request_id: str, actor_id: str,
                            delegation_id: str, organization_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "delegation_id": delegation_id,
                   "organization_id": organization_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            delegation_id = self._identifier(delegation_id, "delegation_id")
            name = self._text(name, "name", 200)
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO delegations(delegation_id,organization_id,name,active,created_at) "
                        "VALUES(?,?,?,1,?)",
                        (delegation_id, organization_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("代表团编号已存在或该组织已有代表团") from exc
                append_event(connection, actor_id=actor_id, action="delegation.registered",
                             resource_type="delegation", resource_id=delegation_id,
                             detail={"organization_id": organization_id, "name": name}, occurred_at=self._now())
                return "delegation", delegation_id, {"delegation_id": delegation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_delegation", payload=payload, create=create)

    def grant_mandate(self, *, request_id: str, actor_id: str, mandate_actor_id: str,
                      delegation_id: str, scope_kind: str = "global", scope_id: str | None = None,
                      valid_from: str | None = None, valid_until: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "mandate_actor_id": mandate_actor_id,
                   "delegation_id": delegation_id, "scope_kind": scope_kind, "scope_id": scope_id,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            delegate = self._actor(connection, mandate_actor_id)
            delegation = connection.execute("SELECT * FROM delegations WHERE delegation_id=?",
                                            (delegation_id,)).fetchone()
            if delegation is None:
                raise NotFoundError("代表团不存在")
            if delegate.organization_id != delegation["organization_id"] and actor.role != "admin":
                raise PermissionDenied("只能向本组织代表团成员授权")
            if scope_kind not in ("global", "proposal", "clause"):
                raise ValidationError("scope_kind 必须是 global/proposal/clause")
            if scope_kind != "global" and not scope_id:
                raise ValidationError("限定范围的授权必须提供 scope_id")
            now = self._now()
            valid_from = valid_from or now
            valid_until = valid_until or "9999-12-31T23:59:59Z"
            if valid_until <= valid_from:
                raise ValidationError("授权截止时间必须晚于生效时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                mandate_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO mandates(mandate_id,delegation_id,actor_id,scope_kind,scope_id,"
                    "valid_from,valid_until,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (mandate_id, delegation_id, mandate_actor_id, scope_kind, scope_id,
                     valid_from, valid_until, now),
                )
                append_event(connection, actor_id=actor_id, action="mandate.granted",
                             resource_type="mandate", resource_id=mandate_id,
                             detail={"delegation_id": delegation_id, "actor_id": mandate_actor_id,
                                     "scope_kind": scope_kind, "scope_id": scope_id,
                                     "valid_from": valid_from, "valid_until": valid_until},
                             occurred_at=now)
                return "mandate", mandate_id, {"mandate_id": mandate_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_mandate", payload=payload, create=create)

    def revoke_mandate(self, *, request_id: str, actor_id: str, mandate_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "mandate_id": mandate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            mandate = connection.execute("SELECT * FROM mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
            if mandate is None:
                raise NotFoundError("授权不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if mandate["revoked_at"] is not None:
                    return "mandate", mandate_id, {"mandate_id": mandate_id, "revoked": True}
                connection.execute("UPDATE mandates SET revoked_at=? WHERE mandate_id=?",
                                   (self._now(), mandate_id))
                append_event(connection, actor_id=actor_id, action="mandate.revoked",
                             resource_type="mandate", resource_id=mandate_id,
                             detail={"delegation_id": mandate["delegation_id"]}, occurred_at=self._now())
                return "mandate", mandate_id, {"mandate_id": mandate_id, "revoked": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_mandate", payload=payload, create=create)

    # ------------------------------------------------------------------ 利益冲突

    def declare_conflict(self, *, request_id: str, actor_id: str, clause_id: str,
                         rationale: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "secretary", "admin")
            self._clause(connection, clause_id)
            rationale = self._text(rationale, "rationale", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                conflict_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO conflict_declarations(conflict_id,actor_id,clause_id,rationale,"
                        "status,declared_at) VALUES(?,?,?,?,'open',?)",
                        (conflict_id, actor_id, clause_id, rationale, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该参与方对此条款已有未清除的利益冲突声明") from exc
                append_event(connection, actor_id=actor_id, action="conflict.declared",
                             resource_type="conflict", resource_id=conflict_id,
                             detail={"clause_id": clause_id, "rationale": rationale},
                             occurred_at=self._now())
                return "conflict", conflict_id, {"conflict_id": conflict_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_conflict", payload=payload, create=create)

    def clear_conflict(self, *, request_id: str, actor_id: str, conflict_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "conflict_id": conflict_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            row = connection.execute("SELECT * FROM conflict_declarations WHERE conflict_id=?",
                                     (conflict_id,)).fetchone()
            if row is None:
                raise NotFoundError("利益冲突声明不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "cleared":
                    return "conflict", conflict_id, {"conflict_id": conflict_id, "cleared": True}
                connection.execute(
                    "UPDATE conflict_declarations SET status='cleared',cleared_at=?,cleared_by=? WHERE conflict_id=?",
                    (self._now(), actor_id, conflict_id),
                )
                append_event(connection, actor_id=actor_id, action="conflict.cleared",
                             resource_type="conflict", resource_id=conflict_id,
                             detail={"clause_id": row["clause_id"]}, occurred_at=self._now())
                return "conflict", conflict_id, {"conflict_id": conflict_id, "cleared": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="clear_conflict", payload=payload, create=create)

    # ------------------------------------------------------------------ 提案、条款与文本

    def create_proposal(self, *, request_id: str, actor_id: str, proposal_id: str, title: str,
                        required_supports: int | None = None,
                        amendment_supports: int = 2) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "title": title,
                   "required_supports": required_supports, "amendment_supports": amendment_supports}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            if actor.role == "delegate":
                self._delegation_for(connection, actor)
            proposal_id = self._identifier(proposal_id, "proposal_id")
            title = self._text(title, "title", 300)
            if int(amendment_supports) < 1:
                raise ValidationError("修订附议门槛至少为 1")
            if required_supports is not None and int(required_supports) < 1:
                raise ValidationError("生效支持门槛至少为 1")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO proposals(proposal_id,title,required_supports,amendment_supports,"
                        "status,created_by,created_at) VALUES(?,?,?,?,'open',?,?)",
                        (proposal_id, title, required_supports, int(amendment_supports),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("提案编号已存在") from exc
                append_event(connection, actor_id=actor_id, action="proposal.created",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"title": title, "required_supports": required_supports,
                                     "amendment_supports": amendment_supports},
                             occurred_at=self._now())
                return "proposal", proposal_id, {"proposal_id": proposal_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_proposal", payload=payload, create=create)

    def add_clause(self, *, request_id: str, actor_id: str, proposal_id: str, clause_id: str,
                   code: str, title: str, language: str, content: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "clause_id": clause_id,
                   "code": code, "title": title, "language": language, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            delegation = self._delegation_for(connection, actor) if actor.role == "delegate" else None
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                          (proposal_id,)).fetchone()
            if proposal is None:
                raise NotFoundError("提案不存在")
            clause_id = self._identifier(clause_id, "clause_id")
            code = self._text(code, "code", 80)
            title = self._text(title, "title", 300)
            language = self._text(language, "language", 16)
            content = self._text(content, "content", 20000)

            def create() -> tuple[str, str, dict[str, Any]]:
                version_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO clauses(clause_id,proposal_id,code,title,status,created_at) "
                        "VALUES(?,?,?,?,'draft',?)",
                        (clause_id, proposal_id, code, title, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("条款编号已存在或条款代码在提案内重复") from exc
                content_hash = digest({"language": language, "content": content})
                connection.execute(
                    "INSERT INTO text_versions(version_id,clause_id,language,kind,content,content_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, clause_id, language, "initial", content, content_hash,
                     actor_id, self._now()),
                )
                connection.execute("UPDATE clauses SET current_version_id=? WHERE clause_id=?",
                                   (version_id, clause_id))
                append_event(connection, actor_id=actor_id, action="clause.added",
                             resource_type="clause", resource_id=clause_id,
                             detail={"proposal_id": proposal_id, "code": code,
                                     "version_id": version_id, "language": language,
                                     "delegation_id": delegation["delegation_id"] if delegation else None},
                             occurred_at=self._now())
                return "clause", clause_id, {"clause_id": clause_id, "version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_clause", payload=payload, create=create)

    # ------------------------------------------------------------------ 修订与立场

    def propose_amendment(self, *, request_id: str, actor_id: str, clause_id: str,
                          language: str, content: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "language": language, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            self._assert_no_conflict(connection, actor, clause_id)
            base_version_id = clause["current_version_id"]
            base = self._version(connection, base_version_id)
            if clause["status"] != "draft":
                raise ConflictError("条款已形成共识，不能再对其提出修订；须另开程序")
            language = self._text(language, "language", 16)
            content = self._text(content, "content", 20000)
            if content == base["content"] and language == base["language"]:
                raise ValidationError("修订内容与当前快照完全相同")

            def create() -> tuple[str, str, dict[str, Any]]:
                amendment_id = uuid.uuid4().hex
                version_id = uuid.uuid4().hex
                content_hash = digest({"language": language, "content": content})
                connection.execute(
                    "INSERT INTO text_versions(version_id,clause_id,language,kind,content,content_hash,"
                    "supersedes_version_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (version_id, clause_id, language, "amendment", content, content_hash,
                     base_version_id, actor_id, self._now()),
                )
                connection.execute(
                    "INSERT INTO amendments(amendment_id,clause_id,base_version_id,proposed_version_id,"
                    "proposer_delegation_id,status,created_at) VALUES(?,?,?,?,?,'proposed',?)",
                    (amendment_id, clause_id, base_version_id, version_id,
                     delegation["delegation_id"], self._now()),
                )
                append_event(connection, actor_id=actor_id, action="amendment.proposed",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": clause_id, "base_version_id": base_version_id,
                                     "proposed_version_id": version_id,
                                     "delegation_id": delegation["delegation_id"]},
                             occurred_at=self._now())
                return "amendment", amendment_id, {"amendment_id": amendment_id, "version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_amendment", payload=payload, create=create)

    def set_amendment_stance(self, *, request_id: str, actor_id: str, amendment_id: str,
                             position: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "amendment_id": amendment_id, "position": position, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            amendment = self._amendment(connection, amendment_id)
            clause_id = amendment["clause_id"]
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            self._assert_no_conflict(connection, actor, clause_id)
            if position not in AMENDMENT_POSITIONS:
                raise ValidationError(f"修订立场必须是 {sorted(AMENDMENT_POSITIONS)} 之一")
            if amendment["status"] != "proposed":
                raise ConflictError("该修订已结束表决，不能改变立场")
            note = str(note).strip()[:1000]
            base_version_id = amendment["base_version_id"]
            delegation_id = delegation["delegation_id"]
            if delegation_id == amendment["proposer_delegation_id"] and position == "support":
                raise ValidationError("提案方的附议不计入支持数")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM stances WHERE subject_kind='amendment' AND subject_id=? AND delegation_id=?",
                    (amendment_id, delegation_id),
                ).fetchone()
                stance_id = existing["stance_id"] if existing else uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stances(stance_id,subject_kind,subject_id,version_id,delegation_id,"
                    "actor_id,position,created_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(subject_kind,subject_id,delegation_id) DO UPDATE SET "
                    "version_id=excluded.version_id,actor_id=excluded.actor_id,"
                    "position=excluded.position,created_at=excluded.created_at",
                    (stance_id, "amendment", amendment_id, base_version_id, delegation_id,
                     actor_id, position, self._now()),
                )
                self._append_stance_fact(
                    connection, subject_kind="amendment", subject_id=amendment_id,
                    version_id=base_version_id, delegation_id=delegation_id, actor_id=actor_id,
                    position=position, detail={"note": note, "replaced": existing["position"] if existing else None},
                )
                append_event(connection, actor_id=actor_id, action="amendment.stance_recorded",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"delegation_id": delegation_id, "position": position,
                                     "version_id": base_version_id}, occurred_at=self._now())
                support = self._amendment_support_locked(connection, amendment)
                return "stance", stance_id, {"stance_id": stance_id, "support_count": support}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_amendment_stance", payload=payload, create=create)

    def _amendment_support_locked(self, connection, amendment) -> int:
        """基于修订的基础快照计算有效附议数。"""
        rows = connection.execute(
            "SELECT s.delegation_id AS delegation_id FROM stances s "
            "JOIN delegations d ON d.delegation_id=s.delegation_id "
            "WHERE s.subject_kind='amendment' AND s.subject_id=? AND s.version_id=? "
            "AND s.position='support' AND d.active=1 "
            "AND NOT EXISTS (SELECT 1 FROM conflict_declarations c WHERE c.actor_id=s.actor_id "
            "AND c.clause_id=? AND c.status='open')",
            (amendment["amendment_id"], amendment["base_version_id"], amendment["clause_id"]),
        ).fetchall()
        counted = {r["delegation_id"] for r in rows if r["delegation_id"] != amendment["proposer_delegation_id"]}
        return len(counted)

    def withdraw_amendment(self, *, request_id: str, actor_id: str, amendment_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "amendment_id": amendment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "secretary", "admin")
            amendment = self._amendment(connection, amendment_id)
            if actor.role == "delegate":
                delegation = self._delegation_for(connection, actor)
                if delegation["delegation_id"] != amendment["proposer_delegation_id"]:
                    raise PermissionDenied("只有提案代表团或秘书处可以撤回修订")

            def create() -> tuple[str, str, dict[str, Any]]:
                if amendment["status"] != "proposed":
                    raise ConflictError("该修订已经结束，不能撤回")
                connection.execute("UPDATE amendments SET status='withdrawn' WHERE amendment_id=?",
                                   (amendment_id,))
                append_event(connection, actor_id=actor_id, action="amendment.withdrawn",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": amendment["clause_id"]}, occurred_at=self._now())
                return "amendment", amendment_id, {"amendment_id": amendment_id, "status": "withdrawn"}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_amendment", payload=payload, create=create)

    def merge_amendment(self, *, request_id: str, actor_id: str, amendment_id: str) -> dict[str, Any]:
        """达到附议门槛后合并修订；同一基础快照只允许一个修订合并。"""
        payload = {"actor_id": actor_id, "amendment_id": amendment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            amendment = self._amendment(connection, amendment_id)
            clause = self._clause(connection, amendment["clause_id"])
            if amendment["status"] == "merged":
                raise ConflictError("该修订已经合并")
            if amendment["status"] != "proposed":
                raise ConflictError("该修订已撤回或作废，不能合并")
            if clause["current_version_id"] != amendment["base_version_id"]:
                raise ConflictError("基础快照已被其他修订取代，本修订与已合并文本冲突")
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                          (clause["proposal_id"],)).fetchone()
            support = self._amendment_support_locked(connection, amendment)
            if support < proposal["amendment_supports"]:
                raise ConflictError(f"附议数不足：{support}/{proposal['amendment_supports']}")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    cursor = connection.execute(
                        "UPDATE amendments SET status='merged',merged_at=? "
                        "WHERE amendment_id=? AND status='proposed'",
                        (now, amendment_id),
                    )
                except Exception as exc:
                    raise ConflictError("同一文本快照已有另一个修订合并，冲突修订不能同时成立") from exc
                if cursor.rowcount == 0:
                    raise ConflictError("修订状态已被并发改变，同一快照只能有一个修订合并")
                # 同一基础快照上的其他待决修订标记为冲突，不能再合并
                connection.execute(
                    "UPDATE amendments SET status='conflicted' WHERE clause_id=? AND base_version_id=? "
                    "AND status='proposed' AND amendment_id<>?",
                    (amendment["clause_id"], amendment["base_version_id"], amendment_id),
                )
                connection.execute("UPDATE clauses SET current_version_id=? WHERE clause_id=?",
                                   (amendment["proposed_version_id"], amendment["clause_id"]))
                append_event(connection, actor_id=actor_id, action="amendment.merged",
                             resource_type="amendment", resource_id=amendment_id,
                             detail={"clause_id": amendment["clause_id"],
                                     "base_version_id": amendment["base_version_id"],
                                     "proposed_version_id": amendment["proposed_version_id"],
                                     "support_count": support}, occurred_at=now)
                return "amendment", amendment_id, {"amendment_id": amendment_id,
                                                    "status": "merged", "support_count": support}

            return self._idempotent(connection, request_id=request_id,
                                    action="merge_amendment", payload=payload, create=create)

    # ------------------------------------------------------------------ 发言、保留意见

    def record_statement(self, *, request_id: str, actor_id: str, clause_id: str,
                         content: str, amendment_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "content": content,
                   "amendment_id": amendment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            self._assert_no_conflict(connection, actor, clause_id)
            content = self._text(content, "content", 10000)
            version_id = clause["current_version_id"]
            if amendment_id is not None:
                amendment = self._amendment(connection, amendment_id)
                if amendment["clause_id"] != clause_id:
                    raise ValidationError("发言引用的修订不属于该条款")

            def create() -> tuple[str, str, dict[str, Any]]:
                statement_id = uuid.uuid4().hex
                content_hash = digest({"content": content, "version_id": version_id,
                                       "delegation_id": delegation["delegation_id"]})
                connection.execute(
                    "INSERT INTO statements(statement_id,amendment_id,clause_id,version_id,"
                    "delegation_id,actor_id,content,content_hash,made_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (statement_id, amendment_id, clause_id, version_id,
                     delegation["delegation_id"], actor_id, content, content_hash, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="statement.recorded",
                             resource_type="statement", resource_id=statement_id,
                             detail={"clause_id": clause_id, "version_id": version_id,
                                     "delegation_id": delegation["delegation_id"],
                                     "amendment_id": amendment_id, "content_hash": content_hash},
                             occurred_at=self._now())
                return "statement", statement_id, {"statement_id": statement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_statement", payload=payload, create=create)

    def enter_reservation(self, *, request_id: str, actor_id: str, clause_id: str,
                          rationale: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            rationale = self._text(rationale, "rationale", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO reservations(reservation_id,clause_id,version_id,delegation_id,"
                        "rationale,status,created_at) VALUES(?,?,?,?,?,'active',?)",
                        (reservation_id, clause_id, clause["current_version_id"],
                         delegation["delegation_id"], rationale, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该代表团对该条款已有生效中的保留意见") from exc
                append_event(connection, actor_id=actor_id, action="reservation.entered",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"clause_id": clause_id, "version_id": clause["current_version_id"],
                                     "delegation_id": delegation["delegation_id"]},
                             occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="enter_reservation", payload=payload, create=create)

    def withdraw_reservation(self, *, request_id: str, actor_id: str, reservation_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "reservation_id": reservation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "secretary", "admin")
            row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?",
                                     (reservation_id,)).fetchone()
            if row is None:
                raise NotFoundError("保留意见不存在")
            if actor.role == "delegate":
                delegation = self._delegation_for(connection, actor)
                if delegation["delegation_id"] != row["delegation_id"]:
                    raise PermissionDenied("只能撤回本代表团的保留意见")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "withdrawn":
                    return "reservation", reservation_id, {"reservation_id": reservation_id, "status": "withdrawn"}
                connection.execute(
                    "UPDATE reservations SET status='withdrawn',withdrawn_at=? WHERE reservation_id=?",
                    (self._now(), reservation_id),
                )
                append_event(connection, actor_id=actor_id, action="reservation.withdrawn",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"clause_id": row["clause_id"],
                                     "delegation_id": row["delegation_id"]},
                             occurred_at=self._now())
                # 保留解除后若共识、签署与条件均已齐备，承诺可以生效
                commitment_id = self._maybe_effectuate_locked(
                    connection, clause_id=row["clause_id"],
                    delegation_id=row["delegation_id"], now=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id,
                                                        "status": "withdrawn",
                                                        "commitment_id": commitment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_reservation", payload=payload, create=create)

    # ------------------------------------------------------------------ 条款立场、共识、签署

    def set_clause_stance(self, *, request_id: str, actor_id: str, clause_id: str,
                          position: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "position": position, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            self._assert_no_conflict(connection, actor, clause_id)
            if position not in CLAUSE_POSITIONS:
                raise ValidationError(f"条款立场必须是 {sorted(CLAUSE_POSITIONS)} 之一")
            note = str(note).strip()[:1000]
            version_id = clause["current_version_id"]
            delegation_id = delegation["delegation_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM stances WHERE subject_kind='clause' AND subject_id=? AND delegation_id=?",
                    (clause_id, delegation_id),
                ).fetchone()
                stance_id = existing["stance_id"] if existing else uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stances(stance_id,subject_kind,subject_id,version_id,delegation_id,"
                    "actor_id,position,created_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(subject_kind,subject_id,delegation_id) DO UPDATE SET "
                    "version_id=excluded.version_id,actor_id=excluded.actor_id,"
                    "position=excluded.position,created_at=excluded.created_at",
                    (stance_id, "clause", clause_id, version_id, delegation_id,
                     actor_id, position, self._now()),
                )
                self._append_stance_fact(
                    connection, subject_kind="clause", subject_id=clause_id,
                    version_id=version_id, delegation_id=delegation_id, actor_id=actor_id,
                    position=position,
                    detail={"note": note, "replaced": existing["position"] if existing else None,
                            "replaced_version": existing["version_id"] if existing else None},
                )
                append_event(connection, actor_id=actor_id, action="clause.stance_recorded",
                             resource_type="clause", resource_id=clause_id,
                             detail={"delegation_id": delegation_id, "position": position,
                                     "version_id": version_id}, occurred_at=self._now())
                return "stance", stance_id, {"stance_id": stance_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_clause_stance", payload=payload, create=create)

    def _signing_support_locked(self, connection, clause, version_id: str) -> int:
        """基于条款当前快照计算有效签署代表团数（重复签署不增加）。"""
        rows = connection.execute(
            "SELECT s.delegation_id AS delegation_id FROM signatures s "
            "JOIN delegations d ON d.delegation_id=s.delegation_id "
            "WHERE s.clause_id=? AND s.version_id=? AND s.status='active' AND d.active=1",
            (clause["clause_id"], version_id),
        ).fetchall()
        return len({r["delegation_id"] for r in rows})

    def sign_clause(self, *, request_id: str, actor_id: str, clause_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id}
        with self.database.transaction(immediate=True) as connection:
            self._sweep_locked(connection)
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            self._mandate(connection, actor, delegation["delegation_id"], "clause", clause_id, proposal_id=clause["proposal_id"])
            self._assert_no_conflict(connection, actor, clause_id)
            version_id = clause["current_version_id"]
            delegation_id = delegation["delegation_id"]
            stance = connection.execute(
                "SELECT * FROM stances WHERE subject_kind='clause' AND subject_id=? AND delegation_id=?",
                (clause_id, delegation_id),
            ).fetchone()
            if stance is None or stance["version_id"] != version_id or \
                    stance["position"] not in ("accept", "conditional_accept"):
                raise PermissionDenied("签署资格要求：先对当前文本快照作出接受或条件接受")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT signature_id,status FROM signatures WHERE clause_id=? AND version_id=? AND delegation_id=?",
                    (clause_id, version_id, delegation_id),
                ).fetchone()
                if existing is not None and existing["status"] == "active":
                    # 重复签署幂等返回，不新增支持数
                    support = self._signing_support_locked(connection, clause, version_id)
                    return "signature", existing["signature_id"], \
                        {"signature_id": existing["signature_id"], "duplicate": True,
                         "support_count": support}
                now = self._now()
                if existing is not None:
                    signature_id = existing["signature_id"]
                    connection.execute(
                        "UPDATE signatures SET status='active',actor_id=?,signed_at=?,withdrawn_at=NULL "
                        "WHERE signature_id=?",
                        (actor_id, now, signature_id),
                    )
                else:
                    signature_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO signatures(signature_id,clause_id,version_id,delegation_id,actor_id,"
                        "status,signed_at) VALUES(?,?,?,?,?,'active',?)",
                        (signature_id, clause_id, version_id, delegation_id, actor_id, now),
                    )
                append_event(connection, actor_id=actor_id, action="clause.signed",
                             resource_type="signature", resource_id=signature_id,
                             detail={"clause_id": clause_id, "version_id": version_id,
                                     "delegation_id": delegation_id}, occurred_at=now)
                support = self._signing_support_locked(connection, clause, version_id)
                formed = self._maybe_form_consensus_locked(connection, clause=clause,
                                                           version_id=version_id, support=support,
                                                           formed_by=actor_id, now=now)
                # 共识此前已形成时，本次新签署方若条件齐备应立即生效
                late_commitment = None
                if formed is None:
                    late_commitment = self._maybe_effectuate_locked(
                        connection, clause_id=clause_id, delegation_id=delegation_id, now=now)
                return "signature", signature_id, {"signature_id": signature_id,
                                                    "support_count": support,
                                                    "consensus_id": formed,
                                                    "commitment_id": late_commitment}

            return self._idempotent(connection, request_id=request_id,
                                    action="sign_clause", payload=payload, create=create)

    def _maybe_form_consensus_locked(self, connection, *, clause, version_id: str,
                                    support: int, formed_by: str, now: str) -> str | None:
        """达到程序门槛时只形成待核准共识（不直接生效）。"""
        proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                      (clause["proposal_id"],)).fetchone()
        required = proposal["required_supports"]
        if required is None or support < required:
            return None
        existing = connection.execute(
            "SELECT consensus_id FROM consensuses WHERE clause_id=? AND version_id=?",
            (clause["clause_id"], version_id),
        ).fetchone()
        if existing is not None:
            return existing["consensus_id"]
        consensus_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO consensuses(consensus_id,clause_id,version_id,support_count,required_supports,"
            "status,formed_by,formed_at) VALUES(?,?,?,?,?,'pending_ratification',?,?)",
            (consensus_id, clause["clause_id"], version_id, support, required, formed_by, now),
        )
        if clause["status"] == "draft":
            connection.execute("UPDATE clauses SET status='accepted' WHERE clause_id=?",
                               (clause["clause_id"],))
        append_event(connection, actor_id=formed_by, action="consensus.formed",
                     resource_type="consensus", resource_id=consensus_id,
                     detail={"clause_id": clause["clause_id"], "version_id": version_id,
                             "support_count": support, "required_supports": required,
                             "status": "pending_ratification"}, occurred_at=now)
        # 共识形成瞬间：已签署且前置条件全部按序满足（或没有条件）的参与方立即生效；
        # 仍有待满足条件的参与方继续等待，不被其他参与方或其他条款拖住。
        for sig in connection.execute(
                "SELECT DISTINCT delegation_id FROM signatures WHERE clause_id=? AND version_id=? "
                "AND status='active'", (clause["clause_id"], version_id)):
            self._maybe_effectuate_locked(connection, clause_id=clause["clause_id"],
                                          delegation_id=sig["delegation_id"], now=now)
        return consensus_id

    def withdraw_signature(self, *, request_id: str, actor_id: str, clause_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "admin")
            clause = self._clause(connection, clause_id)
            delegation = self._delegation_for(connection, actor)
            version_id = clause["current_version_id"]
            delegation_id = delegation["delegation_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM signatures WHERE clause_id=? AND version_id=? AND delegation_id=? "
                    "AND status='active'",
                    (clause_id, version_id, delegation_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("当前快照没有可撤回的签署")
                connection.execute(
                    "UPDATE signatures SET status='withdrawn',withdrawn_at=? WHERE signature_id=?",
                    (self._now(), row["signature_id"]),
                )
                append_event(connection, actor_id=actor_id, action="signature.withdrawn",
                             resource_type="signature", resource_id=row["signature_id"],
                             detail={"clause_id": clause_id, "delegation_id": delegation_id},
                             occurred_at=self._now())
                return "signature", row["signature_id"], {"signature_id": row["signature_id"],
                                                           "status": "withdrawn"}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_signature", payload=payload, create=create)

    # ------------------------------------------------------------------ 前置条件与生效

    def add_ratification_condition(self, *, request_id: str, actor_id: str, clause_id: str,
                                   delegation_id: str, kind: str, label: str,
                                   due_at: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "delegation_id": delegation_id,
                   "kind": kind, "label": label, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            clause = self._clause(connection, clause_id)
            target = connection.execute("SELECT * FROM delegations WHERE delegation_id=?",
                                        (delegation_id,)).fetchone()
            if target is None:
                raise NotFoundError("代表团不存在")
            if actor.role == "delegate":
                own = self._delegation_for(connection, actor)
                if own["delegation_id"] != delegation_id:
                    raise PermissionDenied("只能为本代表团登记前置条件")
            if kind not in CONDITION_KINDS:
                raise ValidationError(f"条件类型必须是 {sorted(CONDITION_KINDS)} 之一")
            label = self._text(label, "label", 300)
            if due_at is not None:
                due_at = self._text(due_at, "due_at", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                if clause["status"] == "in_force" and connection.execute(
                    "SELECT 1 FROM commitments WHERE clause_id=? AND delegation_id=?",
                    (clause_id, delegation_id),
                ).fetchone():
                    raise ConflictError("承诺已生效，不能追加前置条件")
                next_sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence)+1,1) AS next FROM ratification_conditions "
                    "WHERE clause_id=? AND delegation_id=?",
                    (clause_id, delegation_id),
                ).fetchone()["next"]
                condition_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ratification_conditions(condition_id,clause_id,delegation_id,sequence,"
                    "kind,label,due_at,status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (condition_id, clause_id, delegation_id, next_sequence, kind, label,
                     due_at, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="condition.added",
                             resource_type="condition", resource_id=condition_id,
                             detail={"clause_id": clause_id, "delegation_id": delegation_id,
                                     "sequence": next_sequence, "kind": kind, "due_at": due_at},
                             occurred_at=self._now())
                return "condition", condition_id, {"condition_id": condition_id,
                                                    "sequence": next_sequence}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_ratification_condition", payload=payload, create=create)

    def satisfy_condition(self, *, request_id: str, actor_id: str, condition_id: str,
                          evidence: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "condition_id": condition_id, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            self._sweep_locked(connection)
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            row = connection.execute("SELECT * FROM ratification_conditions WHERE condition_id=?",
                                     (condition_id,)).fetchone()
            if row is None:
                raise NotFoundError("前置条件不存在")
            if actor.role == "delegate":
                own = self._delegation_for(connection, actor)
                if own["delegation_id"] != row["delegation_id"]:
                    raise PermissionDenied("只能满足本代表团的前置条件")
            evidence = str(evidence).strip()[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "satisfied":
                    return "condition", condition_id, {"condition_id": condition_id, "status": "satisfied"}
                if row["status"] == "lapsed":
                    raise ConflictError("该条件已超过行动期限，不能补记满足")
                previous = connection.execute(
                    "SELECT * FROM ratification_conditions WHERE clause_id=? AND delegation_id=? "
                    "AND sequence<? AND status<>'satisfied' ORDER BY sequence",
                    (row["clause_id"], row["delegation_id"], row["sequence"]),
                ).fetchall()
                if previous:
                    raise ConflictError("必须按顺序满足前置条件，前序条件尚未完成")
                now = self._now()
                connection.execute(
                    "UPDATE ratification_conditions SET status='satisfied',evidence=?,satisfied_at=? "
                    "WHERE condition_id=?",
                    (evidence, now, condition_id),
                )
                append_event(connection, actor_id=actor_id, action="condition.satisfied",
                             resource_type="condition", resource_id=condition_id,
                             detail={"clause_id": row["clause_id"], "delegation_id": row["delegation_id"],
                                     "sequence": row["sequence"]}, occurred_at=now)
                commitment_id = self._maybe_effectuate_locked(
                    connection, clause_id=row["clause_id"], delegation_id=row["delegation_id"], now=now)
                return "condition", condition_id, {"condition_id": condition_id,
                                                    "status": "satisfied",
                                                    "commitment_id": commitment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="satisfy_condition", payload=payload, create=create)

    def _maybe_effectuate_locked(self, connection, *, clause_id: str,
                                 delegation_id: str, now: str) -> str | None:
        """该参与方全部前置条件按序满足且未逾期、且已签署待核准共识时，承诺生效。"""
        existing = connection.execute(
            "SELECT commitment_id FROM commitments WHERE clause_id=? AND delegation_id=?",
            (clause_id, delegation_id),
        ).fetchone()
        if existing is not None:
            return existing["commitment_id"]
        clause = self._clause(connection, clause_id)
        signature = connection.execute(
            "SELECT 1 FROM signatures WHERE clause_id=? AND delegation_id=? AND version_id=? AND status='active'",
            (clause_id, delegation_id, clause["current_version_id"]),
        ).fetchone()
        if signature is None:
            return None
        consensus = connection.execute(
            "SELECT 1 FROM consensuses WHERE clause_id=? AND version_id=? AND status='pending_ratification'",
            (clause_id, clause["current_version_id"]),
        ).fetchone()
        if consensus is None:
            return None
        reservation = connection.execute(
            "SELECT 1 FROM reservations WHERE clause_id=? AND delegation_id=? AND version_id=? "
            "AND status='active'",
            (clause_id, delegation_id, clause["current_version_id"]),
        ).fetchone()
        if reservation is not None:
            # 保留意见只阻止该参与方自身的承诺，不影响其他独立条款与其他参与方
            return None
        conditions = connection.execute(
            "SELECT status FROM ratification_conditions WHERE clause_id=? AND delegation_id=? ORDER BY sequence",
            (clause_id, delegation_id),
        ).fetchall()
        if any(c["status"] != "satisfied" for c in conditions):
            return None
        commitment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO commitments(commitment_id,clause_id,delegation_id,version_id,effective_at) "
            "VALUES(?,?,?,?,?)",
            (commitment_id, clause_id, delegation_id, clause["current_version_id"], now),
        )
        connection.execute("UPDATE clauses SET status='in_force' WHERE clause_id=?", (clause_id,))
        append_event(connection, actor_id="system", action="commitment.effective",
                     resource_type="commitment", resource_id=commitment_id,
                     detail={"clause_id": clause_id, "delegation_id": delegation_id,
                             "version_id": clause["current_version_id"], "effective_at": now},
                     occurred_at=now)
        return commitment_id

    # ------------------------------------------------------------------ 后续行动

    def add_follow_up(self, *, request_id: str, actor_id: str, clause_id: str,
                      delegation_id: str, title: str, due_at: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "clause_id": clause_id, "delegation_id": delegation_id,
                   "title": title, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            self._clause(connection, clause_id)
            if connection.execute("SELECT 1 FROM delegations WHERE delegation_id=?",
                                  (delegation_id,)).fetchone() is None:
                raise NotFoundError("代表团不存在")
            if actor.role == "delegate":
                own = self._delegation_for(connection, actor)
                if own["delegation_id"] != delegation_id:
                    raise PermissionDenied("只能为本代表团登记后续行动")
            title = self._text(title, "title", 300)
            due_at = self._text(due_at, "due_at", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                action_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO follow_up_actions(action_id,clause_id,delegation_id,title,due_at,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?)",
                    (action_id, clause_id, delegation_id, title, due_at, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="follow_up.added",
                             resource_type="follow_up", resource_id=action_id,
                             detail={"clause_id": clause_id, "delegation_id": delegation_id,
                                     "due_at": due_at}, occurred_at=self._now())
                return "follow_up", action_id, {"action_id": action_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_follow_up", payload=payload, create=create)

    def complete_follow_up(self, *, request_id: str, actor_id: str, action_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "action_id": action_id}
        with self.database.transaction(immediate=True) as connection:
            self._sweep_locked(connection)
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "delegate", "admin")
            row = connection.execute("SELECT * FROM follow_up_actions WHERE action_id=?",
                                     (action_id,)).fetchone()
            if row is None:
                raise NotFoundError("后续行动不存在")
            if actor.role == "delegate":
                own = self._delegation_for(connection, actor)
                if own["delegation_id"] != row["delegation_id"]:
                    raise PermissionDenied("只能完成本代表团的后续行动")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "completed":
                    return "follow_up", action_id, {"action_id": action_id, "status": "completed"}
                now = self._now()
                connection.execute(
                    "UPDATE follow_up_actions SET status='completed',completed_at=? WHERE action_id=?",
                    (now, action_id),
                )
                append_event(connection, actor_id=actor_id, action="follow_up.completed",
                             resource_type="follow_up", resource_id=action_id,
                             detail={"clause_id": row["clause_id"], "delegation_id": row["delegation_id"]},
                             occurred_at=now)
                return "follow_up", action_id, {"action_id": action_id, "status": "completed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_follow_up", payload=payload, create=create)

    # ------------------------------------------------------------------ 翻译对应

    def propose_translation(self, *, request_id: str, actor_id: str, source_version_id: str,
                            target_language: str, target_content: str,
                            note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "source_version_id": source_version_id,
                   "target_language": target_language, "target_content": target_content, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "translator", "reviewer", "admin")
            source = self._version(connection, source_version_id)
            target_language = self._text(target_language, "target_language", 16)
            target_content = self._text(target_content, "target_content", 20000)
            if target_language == source["language"]:
                raise ValidationError("翻译目标语言必须与源快照不同")

            def create() -> tuple[str, str, dict[str, Any]]:
                target_version_id = uuid.uuid4().hex
                content_hash = digest({"language": target_language, "content": target_content})
                connection.execute(
                    "INSERT INTO text_versions(version_id,clause_id,language,kind,content,content_hash,"
                    "supersedes_version_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (target_version_id, source["clause_id"], target_language, "translation",
                     target_content, content_hash, source_version_id, actor_id, self._now()),
                )
                link_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO translation_links(link_id,source_version_id,target_version_id,"
                        "translator_actor_id,status,note,created_at) VALUES(?,?,?,?,'proposed',?,?)",
                        (link_id, source_version_id, target_version_id, actor_id,
                         str(note).strip()[:1000], self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("两个快照之间的翻译对应关系已存在") from exc
                append_event(connection, actor_id=actor_id, action="translation.proposed",
                             resource_type="translation_link", resource_id=link_id,
                             detail={"source_version_id": source_version_id,
                                     "target_version_id": target_version_id,
                                     "target_language": target_language}, occurred_at=self._now())
                return "translation_link", link_id, {"link_id": link_id,
                                                      "target_version_id": target_version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_translation", payload=payload, create=create)

    def certify_translation(self, *, request_id: str, actor_id: str, link_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "link_id": link_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "reviewer", "secretary", "admin")
            row = connection.execute("SELECT * FROM translation_links WHERE link_id=?",
                                     (link_id,)).fetchone()
            if row is None:
                raise NotFoundError("翻译对应关系不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "certified":
                    return "translation_link", link_id, {"link_id": link_id, "status": "certified"}
                connection.execute(
                    "UPDATE translation_links SET status='certified',certified_by=?,certified_at=? "
                    "WHERE link_id=?",
                    (actor_id, self._now(), link_id),
                )
                append_event(connection, actor_id=actor_id, action="translation.certified",
                             resource_type="translation_link", resource_id=link_id,
                             detail={"source_version_id": row["source_version_id"],
                                     "target_version_id": row["target_version_id"]},
                             occurred_at=self._now())
                return "translation_link", link_id, {"link_id": link_id, "status": "certified"}

            return self._idempotent(connection, request_id=request_id,
                                    action="certify_translation", payload=payload, create=create)

    # ------------------------------------------------------------------ 封存

    def seal_facts(self, *, request_id: str, actor_id: str, scope_kind: str,
                   scope_id: str) -> dict[str, Any]:
        """封存指定范围的发言与表决事实；同一范围并发封存只有一个结果。"""
        payload = {"actor_id": actor_id, "scope_kind": scope_kind, "scope_id": scope_id}
        with self.database.transaction(immediate=True) as connection:
            self._sweep_locked(connection)
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin")
            if scope_kind not in ("amendment", "clause"):
                raise ValidationError("封存范围必须是 amendment 或 clause")
            if scope_kind == "amendment":
                amendment = self._amendment(connection, scope_id)
                clause_id = amendment["clause_id"]
            else:
                self._clause(connection, scope_id)
                clause_id = scope_id

            def create() -> tuple[str, str, dict[str, Any]]:
                facts: list[tuple[str, str, str]] = []
                params_stance = {"subject_kind": scope_kind, "subject_id": scope_id}
                for row in connection.execute(
                        "SELECT fact_id,fact_hash FROM stance_events WHERE subject_kind=:subject_kind "
                        "AND subject_id=:subject_id ORDER BY occurred_at,fact_id", params_stance):
                    facts.append(("stance", row["fact_id"], row["fact_hash"]))
                if scope_kind == "amendment":
                    statement_rows = connection.execute(
                        "SELECT statement_id,content_hash FROM statements WHERE amendment_id=? "
                        "ORDER BY made_at,statement_id", (scope_id,))
                else:
                    statement_rows = connection.execute(
                        "SELECT statement_id,content_hash FROM statements WHERE clause_id=? "
                        "ORDER BY made_at,statement_id", (scope_id,))
                for row in statement_rows:
                    facts.append(("statement", row["statement_id"], row["content_hash"]))
                if scope_kind == "clause":
                    for row in connection.execute(
                            "SELECT signature_id,signed_at,version_id,delegation_id FROM signatures "
                            "WHERE clause_id=? ORDER BY signed_at,signature_id", (scope_id,)):
                        facts.append(("signature", row["signature_id"],
                                      digest({"version_id": row["version_id"],
                                              "delegation_id": row["delegation_id"],
                                              "signed_at": row["signed_at"]})))
                aggregate_material = sorted(
                    ({"fact_kind": k, "fact_key": f, "fact_hash": h} for k, f, h in facts),
                    key=lambda item: (item["fact_kind"], item["fact_key"]))
                aggregate_hash = digest(aggregate_material)
                seal_id = uuid.uuid4().hex
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO seals(seal_id,scope_kind,scope_id,aggregate_hash,fact_count,"
                        "sealed_by,sealed_at) VALUES(?,?,?,?,?,?,?)",
                        (seal_id, scope_kind, scope_id, aggregate_hash, len(facts), actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("该范围已经封存，并发封存只能产生一个结果") from exc
                connection.executemany(
                    "INSERT INTO sealed_facts(seal_id,fact_kind,fact_key,fact_hash) VALUES(?,?,?,?)",
                    [(seal_id, k, f, h) for k, f, h in facts],
                )
                append_event(connection, actor_id=actor_id, action="facts.sealed",
                             resource_type="seal", resource_id=seal_id,
                             detail={"scope_kind": scope_kind, "scope_id": scope_id,
                                     "fact_count": len(facts), "aggregate_hash": aggregate_hash},
                             occurred_at=now)
                return "seal", seal_id, {"seal_id": seal_id, "fact_count": len(facts),
                                          "aggregate_hash": aggregate_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_facts", payload=payload, create=create)

    def verify_seal(self, seal_id: str) -> dict[str, Any]:
        """用封存时保存的事实快照复核聚合摘要，并回源检测事实是否被改动。"""
        row = self.database.connection.execute("SELECT * FROM seals WHERE seal_id=?",
                                                (seal_id,)).fetchone()
        if row is None:
            raise NotFoundError("封存不存在")
        sealed_rows = self.database.connection.execute(
            "SELECT * FROM sealed_facts WHERE seal_id=? ORDER BY fact_kind,fact_key",
            (seal_id,)).fetchall()
        material = [{"fact_kind": r["fact_kind"], "fact_key": r["fact_key"], "fact_hash": r["fact_hash"]}
                    for r in sealed_rows]
        recomputed = digest(material)
        # 回源核对：立场事实、发言内容与签署事实必须与封存时逐字节一致
        live_mismatches: list[str] = []
        for r in sealed_rows:
            if r["fact_kind"] == "stance":
                live = self.database.connection.execute(
                    "SELECT fact_hash FROM stance_events WHERE fact_id=?", (r["fact_key"],)).fetchone()
            elif r["fact_kind"] == "statement":
                live = self.database.connection.execute(
                    "SELECT content_hash AS fact_hash FROM statements WHERE statement_id=?",
                    (r["fact_key"],)).fetchone()
            else:
                sig = self.database.connection.execute(
                    "SELECT * FROM signatures WHERE signature_id=?", (r["fact_key"],)).fetchone()
                live_hash = digest({"version_id": sig["version_id"],
                                    "delegation_id": sig["delegation_id"],
                                    "signed_at": sig["signed_at"]}) if sig else None
                if live_hash != r["fact_hash"]:
                    live_mismatches.append(r["fact_key"])
                continue
            if live is None or live["fact_hash"] != r["fact_hash"]:
                live_mismatches.append(r["fact_key"])
        return {"seal_id": seal_id, "scope_kind": row["scope_kind"], "scope_id": row["scope_id"],
                "fact_count": row["fact_count"], "stored_hash": row["aggregate_hash"],
                "recomputed_hash": recomputed,
                "tampered_facts": live_mismatches,
                "valid": recomputed == row["aggregate_hash"] and not live_mismatches}

    # ------------------------------------------------------------------ 视图

    def _serialise_version(self, row) -> dict[str, Any]:
        return {"version_id": row["version_id"], "clause_id": row["clause_id"],
                "language": row["language"], "kind": row["kind"],
                "supersedes_version_id": row["supersedes_version_id"],
                "content_hash": row["content_hash"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def get_clause(self, clause_id: str, *, include_content: bool = True) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM clauses WHERE clause_id=?",
                                               (clause_id,)).fetchone()
        if row is None:
            raise NotFoundError("条款不存在")
        current = self.database.connection.execute("SELECT * FROM text_versions WHERE version_id=?",
                                                    (row["current_version_id"],)).fetchone()
        result = {"clause_id": clause_id, "proposal_id": row["proposal_id"], "code": row["code"],
                  "title": row["title"], "status": row["status"],
                  "current_version_id": row["current_version_id"],
                  "current_language": current["language"],
                  "content_hash": current["content_hash"],
                  "created_at": row["created_at"]}
        if include_content:
            result["content"] = current["content"]
        translations = self.database.connection.execute(
            "SELECT tv.version_id AS version_id,tv.language AS language,tl.status AS link_status "
            "FROM translation_links tl JOIN text_versions tv ON tv.version_id=tl.target_version_id "
            "WHERE tl.source_version_id=? ORDER BY tv.language", (row["current_version_id"],)).fetchall()
        result["translations"] = [{"version_id": r["version_id"], "language": r["language"],
                                    "status": r["link_status"]} for r in translations]
        return result

    def list_amendments(self, clause_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT a.*, COUNT(s.stance_id) AS stance_rows FROM amendments a "
            "LEFT JOIN stances s ON s.subject_kind='amendment' AND s.subject_id=a.amendment_id "
            "AND s.version_id=a.base_version_id AND s.position='support' "
            "WHERE a.clause_id=? GROUP BY a.amendment_id ORDER BY a.created_at,a.rowid", (clause_id,)).fetchall()
        items = []
        for row in rows:
            items.append({"amendment_id": row["amendment_id"], "clause_id": row["clause_id"],
                          "base_version_id": row["base_version_id"],
                          "proposed_version_id": row["proposed_version_id"],
                          "proposer_delegation_id": row["proposer_delegation_id"],
                          "status": row["status"], "merged_at": row["merged_at"],
                          "created_at": row["created_at"]})
        return items

    def amendment_support(self, amendment_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            amendment = self._amendment(connection, amendment_id)
            support = self._amendment_support_locked(connection, amendment)
        return {"amendment_id": amendment_id, "support_count": support, "status": amendment["status"]}

    def delegate_view(self, actor_id: str) -> dict[str, Any]:
        """代表视图：本代表团的授权、立场、签署、条件与行动。"""
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "delegate", "auditor", "admin")
            delegation = connection.execute(
                "SELECT * FROM delegations WHERE organization_id=? AND active=1",
                (actor.organization_id,)).fetchone()
            if delegation is None:
                raise NotFoundError("该操作者所属组织没有代表团")
            delegation_id = delegation["delegation_id"]
            now = self._now()
            mandates = [dict(m) for m in connection.execute(
                "SELECT mandate_id,scope_kind,scope_id,valid_from,valid_until,revoked_at "
                "FROM mandates WHERE actor_id=? AND delegation_id=? ORDER BY valid_from",
                (actor_id, delegation_id))]
            for m in mandates:
                m["effective_now"] = (m["revoked_at"] is None and m["valid_from"] <= now <= m["valid_until"])
            stances = [dict(r) for r in connection.execute(
                "SELECT subject_kind,subject_id,version_id,position,created_at FROM stances "
                "WHERE delegation_id=? ORDER BY created_at", (delegation_id,))]
            signatures = [dict(r) for r in connection.execute(
                "SELECT signature_id,clause_id,version_id,status,signed_at FROM signatures "
                "WHERE delegation_id=? ORDER BY signed_at", (delegation_id,))]
            conditions = [dict(r) for r in connection.execute(
                "SELECT condition_id,clause_id,sequence,kind,label,due_at,status,satisfied_at,lapsed_at "
                "FROM ratification_conditions WHERE delegation_id=? ORDER BY clause_id,sequence",
                (delegation_id,))]
            actions = [dict(r) for r in connection.execute(
                "SELECT action_id,clause_id,title,due_at,status,completed_at FROM follow_up_actions "
                "WHERE delegation_id=? ORDER BY due_at", (delegation_id,))]
            reservations = [dict(r) for r in connection.execute(
                "SELECT reservation_id,clause_id,version_id,status,created_at FROM reservations "
                "WHERE delegation_id=? ORDER BY created_at", (delegation_id,))]
        return {"delegation_id": delegation_id, "name": delegation["name"], "mandates": mandates,
                "stances": stances, "signatures": signatures, "conditions": conditions,
                "follow_ups": actions, "reservations": reservations}

    def translator_view(self, actor_id: str | None = None) -> dict[str, Any]:
        """翻译审校视图：翻译对应关系及其认证状态。"""
        if actor_id:
            with self.database.transaction() as connection:
                actor = self._actor(connection, actor_id)
                self._require_role(actor, "translator", "reviewer", "secretary", "admin", "auditor")
        rows = self.database.connection.execute(
            "SELECT tl.link_id AS link_id,tl.source_version_id AS source_version_id,"
            "tl.target_version_id AS target_version_id,tl.status AS status,"
            "tv.language AS target_language,sv.language AS source_language,tv.clause_id AS clause_id,"
            "tl.created_at AS created_at FROM translation_links tl "
            "JOIN text_versions tv ON tv.version_id=tl.target_version_id "
            "JOIN text_versions sv ON sv.version_id=tl.source_version_id "
            "ORDER BY tl.created_at").fetchall()
        return {"items": [dict(r) for r in rows]}

    def secretary_view(self, actor_id: str) -> dict[str, Any]:
        """秘书处视图：程序门槛、共识、生效与逾期事项。"""
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "secretary", "admin", "auditor")
            self._sweep_locked(connection)
            clauses = [dict(r) for r in connection.execute(
                "SELECT c.clause_id AS clause_id,c.proposal_id AS proposal_id,c.code AS code,"
                "c.title AS title,c.status AS status,c.current_version_id AS current_version_id,"
                "p.required_supports AS required_supports FROM clauses c "
                "JOIN proposals p ON p.proposal_id=c.proposal_id ORDER BY c.created_at")]
            for item in clauses:
                support = connection.execute(
                    "SELECT COUNT(DISTINCT delegation_id) AS n FROM signatures "
                    "WHERE clause_id=? AND version_id=? AND status='active'",
                    (item["clause_id"], item["current_version_id"])).fetchone()["n"]
                item["support_count"] = support
                item["reservation_count"] = connection.execute(
                    "SELECT COUNT(*) AS n FROM reservations WHERE clause_id=? AND status='active'",
                    (item["clause_id"],)).fetchone()["n"]
                item["effective_delegations"] = [r["delegation_id"] for r in connection.execute(
                    "SELECT delegation_id FROM commitments WHERE clause_id=? ORDER BY effective_at",
                    (item["clause_id"],))]
            pending_conditions = [dict(r) for r in connection.execute(
                "SELECT condition_id,clause_id,delegation_id,sequence,kind,label,due_at,status "
                "FROM ratification_conditions WHERE status<>'satisfied' ORDER BY due_at")]
            open_actions = [dict(r) for r in connection.execute(
                "SELECT action_id,clause_id,delegation_id,title,due_at,status FROM follow_up_actions "
                "WHERE status<>'completed' ORDER BY due_at")]
            consensuses = [dict(r) for r in connection.execute(
                "SELECT consensus_id,clause_id,version_id,support_count,required_supports,formed_at "
                "FROM consensuses ORDER BY formed_at")]
        return {"clauses": clauses, "consensuses": consensuses,
                "pending_conditions": pending_conditions, "open_actions": open_actions}

    def observer_view(self) -> dict[str, Any]:
        """观察员视图：公开程序事实（草案整理稿不含表决态度明细）。"""
        seals = [dict(r) for r in self.database.connection.execute(
            "SELECT seal_id,scope_kind,scope_id,fact_count,aggregate_hash,sealed_at "
            "FROM seals ORDER BY sealed_at")]
        statements = [dict(r) for r in self.database.connection.execute(
            "SELECT statement_id,amendment_id,clause_id,version_id,delegation_id,made_at "
            "FROM statements ORDER BY made_at")]
        return {"seals": seals, "statements": statements}

    def public_clauses(self) -> dict[str, Any]:
        """公众接口：明确区分草案、已接受、保留与已生效内容。"""
        with self.database.transaction() as connection:
            self._sweep_locked(connection)
            items = []
            for row in connection.execute(
                    "SELECT c.*,tv.content AS content,tv.language AS language,"
                    "tv.content_hash AS content_hash FROM clauses c "
                    "JOIN text_versions tv ON tv.version_id=c.current_version_id ORDER BY c.created_at"):
                active_reservations = [r["delegation_id"] for r in connection.execute(
                    "SELECT delegation_id FROM reservations WHERE clause_id=? AND status='active'",
                    (row["clause_id"],))]
                effective = [{"delegation_id": r["delegation_id"], "effective_at": r["effective_at"]}
                             for r in connection.execute(
                    "SELECT delegation_id,effective_at FROM commitments WHERE clause_id=? ORDER BY effective_at",
                    (row["clause_id"],))]
                consensus = connection.execute(
                    "SELECT support_count,required_supports,formed_at FROM consensuses "
                    "WHERE clause_id=? AND version_id=? ORDER BY formed_at DESC LIMIT 1",
                    (row["clause_id"], row["current_version_id"])).fetchone()
                items.append({
                    "clause_id": row["clause_id"], "code": row["code"], "title": row["title"],
                    "state": row["status"],
                    "state_label": {"draft": "草案", "accepted": "已接受（待核准）",
                                    "in_force": "已生效"}[row["status"]],
                    "language": row["language"], "content": row["content"],
                    "content_hash": row["content_hash"],
                    "consensus": dict(consensus) if consensus else None,
                    "active_reservations": active_reservations,
                    "effective_for": effective,
                })
        return {"items": items}

    # ------------------------------------------------------------------ 时间点审计解释

    def explain_binding(self, *, clause_id: str, delegation_id: str, at: str) -> dict[str, Any]:
        """解释某条款在指定时点为何对特定参与方具有/不具有约束力。"""
        at = self._text(at, "at", 40)
        connection = self.database.connection
        clause = connection.execute("SELECT * FROM clauses WHERE clause_id=?", (clause_id,)).fetchone()
        if clause is None:
            raise NotFoundError("条款不存在")
        if connection.execute("SELECT 1 FROM delegations WHERE delegation_id=?",
                              (delegation_id,)).fetchone() is None:
            raise NotFoundError("代表团不存在")

        # 当时点的有效文本快照 = 初始快照 + 截至该时点已合并修订的推进链；
        # 译本与未合并/已撤回/已作废的修订不计入。
        initial = connection.execute(
            "SELECT * FROM text_versions WHERE clause_id=? AND kind='initial' "
            "ORDER BY created_at,rowid LIMIT 1", (clause_id,)).fetchone()
        if initial is None or initial["created_at"] > at:
            return {"clause_id": clause_id, "delegation_id": delegation_id, "at": at,
                    "binding": False, "reasons": ["该时点条款尚未形成任何文本快照"]}
        version_id = initial["version_id"]
        for merged in connection.execute(
                "SELECT proposed_version_id FROM amendments WHERE clause_id=? AND status='merged' "
                "AND merged_at<=? ORDER BY merged_at,rowid", (clause_id, at)):
            version_id = merged["proposed_version_id"]
        version = self._version(connection, version_id)
        reasons: list[str] = []

        fact = connection.execute(
            "SELECT * FROM stance_events WHERE subject_kind='clause' AND subject_id=? "
            "AND delegation_id=? AND version_id=? AND occurred_at<=? "
            "ORDER BY occurred_at DESC,rowid DESC LIMIT 1",
            (clause_id, delegation_id, version_id, at)).fetchone()
        position = fact["position"] if fact else None
        if fact is None:
            reasons.append("该参与方在该时点前未对该文本快照作过条款立场表态")
        elif fact["position"] == "oppose":
            reasons.append("该参与方对该快照持反对立场")
        elif fact["position"] == "withdrawn":
            reasons.append("该参与方的立场已于该时点前撤回")

        signature = connection.execute(
            "SELECT * FROM signatures WHERE clause_id=? AND delegation_id=? AND version_id=? "
            "AND signed_at<=? ORDER BY signed_at DESC LIMIT 1",
            (clause_id, delegation_id, version_id, at)).fetchone()
        signed = signature is not None and signature["status"] == "active"
        if not signed:
            reasons.append("该时点前没有针对该快照的有效签署（重复签署不另计）")

        consensus = connection.execute(
            "SELECT * FROM consensuses WHERE clause_id=? AND version_id=? AND formed_at<=?",
            (clause_id, version_id, at)).fetchone()
        if consensus is None:
            reasons.append("该时点尚未形成待核准共识（程序支持门槛未达到）")

        conditions = connection.execute(
            "SELECT * FROM ratification_conditions WHERE clause_id=? AND delegation_id=? ORDER BY sequence",
            (clause_id, delegation_id)).fetchall()
        condition_trace = []
        pending = False
        for c in conditions:
            if c["satisfied_at"] is not None and c["satisfied_at"] <= at:
                state = "satisfied"
            elif c["lapsed_at"] is not None and c["lapsed_at"] <= at:
                state = "lapsed"
            elif c["due_at"] is not None and c["due_at"] <= at:
                state = "lapsed"
                pending = True
            else:
                state = "pending"
                if c["satisfied_at"] is None or c["satisfied_at"] > at:
                    pending = True
            condition_trace.append({"condition_id": c["condition_id"], "sequence": c["sequence"],
                                    "kind": c["kind"], "label": c["label"],
                                    "due_at": c["due_at"], "state_at": state})
        if conditions:
            first_incomplete = next((t for t in condition_trace if t["state_at"] != "satisfied"), None)
            if first_incomplete is not None:
                if first_incomplete["state_at"] == "lapsed":
                    reasons.append(f"第 {first_incomplete['sequence']} 项前置条件已超过期限，生效链中断")
                else:
                    reasons.append(f"第 {first_incomplete['sequence']} 项前置条件在该时点尚未按序满足")
        else:
            reasons.append("没有登记任何前置条件")

        reservation = connection.execute(
            "SELECT * FROM reservations WHERE clause_id=? AND delegation_id=? AND version_id=? "
            "AND created_at<=? ORDER BY created_at DESC LIMIT 1",
            (clause_id, delegation_id, version_id, at)).fetchone()
        reserved = reservation is not None and reservation["status"] == "active" and \
            (reservation["withdrawn_at"] is None or reservation["withdrawn_at"] > at)

        commitment = connection.execute(
            "SELECT * FROM commitments WHERE clause_id=? AND delegation_id=? AND effective_at<=?",
            (clause_id, delegation_id, at)).fetchone()
        binding = commitment is not None
        if binding:
            reasons = [f"承诺已于 {commitment['effective_at']} 生效，该时点对参与方具有约束力"]
        elif consensus is not None and signed and position in ("accept", "conditional_accept") and not reserved:
            reasons.append("共识与签署均已具备，仍在等待前置条件按序满足；暂不具有约束力")

        return {"clause_id": clause_id, "delegation_id": delegation_id, "at": at,
                "version_id": version_id, "version_created_at": version["created_at"],
                "position_at": position, "signed_at": signature["signed_at"] if signature else None,
                "consensus_formed_at": consensus["formed_at"] if consensus else None,
                "conditions": condition_trace,
                "reservation_active": reserved,
                "commitment_effective_at": commitment["effective_at"] if commitment else None,
                "binding": binding, "reasons": reasons}
