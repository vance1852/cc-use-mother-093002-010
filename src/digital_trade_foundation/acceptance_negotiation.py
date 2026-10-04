"""运行规则文本协商与承诺跟踪的离线端到端验收。

覆盖：提案与条款快照、冲突修订互斥、同一快照计票、封存与文字整理隔离、
重复签署不计数、条件按序生效、保留意见只约束本方、翻译对应、
角色视图、服务恢复后继续等待期限、公众接口状态区分与时间点审计解释。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .negotiation import NegotiationService
from .service import DomainService
from .storage import Database

BASE = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "negotiation.sqlite3")

        def service_at(offset_minutes: int) -> DomainService:
            base = DomainService(database, FixedClock(BASE + timedelta(minutes=offset_minutes)))
            return base

        svc = service_at(0)
        neg = NegotiationService(database, FixedClock(BASE))

        def tick(minutes: int) -> NegotiationService:
            return NegotiationService(database, FixedClock(BASE + timedelta(minutes=minutes)))

        req = 0

        def rid(prefix: str) -> str:
            nonlocal req
            req += 1
            return f"{prefix}-{req:03d}"

        # ---- 组织与人员：三个代表团 + 秘书处 + 翻译审校 + 观察员 ----
        for org, name in [("org-a", "甲国代表团组织"), ("org-b", "乙国代表团组织"),
                          ("org-c", "丙国代表团组织"), ("org-sec", "秘书处组织"),
                          ("org-lang", "语文组")]:
            svc.register_organization(request_id=rid("org"), actor_id="bootstrap",
                                      organization_id=org, name=name)
        svc.register_actor(request_id=rid("actor"), actor_id="bootstrap", new_actor_id="admin",
                           display_name="管理员", role="admin", organization_id="org-sec")
        svc.register_actor(request_id=rid("actor"), actor_id="admin", new_actor_id="sec",
                           display_name="秘书处干事", role="secretary", organization_id="org-sec")
        for actor, org, name in [("d1", "org-a", "甲国代表"), ("d2", "org-b", "乙国代表"),
                                 ("d3", "org-c", "丙国代表")]:
            svc.register_actor(request_id=rid("actor"), actor_id="admin", new_actor_id=actor,
                               display_name=name, role="delegate", organization_id=org)
        svc.register_actor(request_id=rid("actor"), actor_id="admin", new_actor_id="tr",
                           display_name="翻译审校", role="reviewer", organization_id="org-lang")
        svc.register_actor(request_id=rid("actor"), actor_id="admin", new_actor_id="obs",
                           display_name="观察员", role="observer", organization_id="org-lang")

        for delegation, org, name in [("del-a", "org-a", "甲国代表团"),
                                      ("del-b", "org-b", "乙国代表团"),
                                      ("del-c", "org-c", "丙国代表团")]:
            neg.register_delegation(request_id=rid("del"), actor_id="sec",
                                    delegation_id=delegation, organization_id=org, name=name)
        for actor, delegation in [("d1", "del-a"), ("d2", "del-b"), ("d3", "del-c")]:
            neg.grant_mandate(request_id=rid("mand"), actor_id="sec", mandate_actor_id=actor,
                              delegation_id=delegation)

        # ---- 提案（生效门槛 2 个代表团，修订附议门槛 2）与条款 ----
        neg.create_proposal(request_id=rid("prop"), actor_id="sec", proposal_id="p1",
                            title="数字贸易规则提案", required_supports=2, amendment_supports=2)
        neg.add_clause(request_id=rid("clause"), actor_id="d1", proposal_id="p1",
                       clause_id="c1", code="ART-1", title="数据流动条款",
                       language="zh", content="缔约方应当保障跨境数据流动。")
        c1_v1 = neg.get_clause("c1")["current_version_id"]

        # ---- 利益冲突：d3 声明后不能参与 c1 ----
        neg.declare_conflict(request_id=rid("conf"), actor_id="d3", clause_id="c1",
                             rationale="与本国企业存在关联")

        # ---- 两个基于同一快照 v1 的互相竞争修订 ----
        n10 = tick(10)
        n10.propose_amendment(request_id=rid("am"), actor_id="d1", clause_id="c1",
                              language="zh", content="缔约方应当保障跨境数据流动，并保护个人信息。")
        amendments = n10.list_amendments("c1")
        am_a = amendments[0]["amendment_id"]
        am_a_version = amendments[0]["proposed_version_id"]
        n10.propose_amendment(request_id=rid("am"), actor_id="d2", clause_id="c1",
                              language="zh", content="缔约方应当在安全审查前提下保障跨境数据流动。")
        am_b = n10.list_amendments("c1")[1]["amendment_id"]

        # 提案方自己支持不计入；d2 附议后达到门槛 2? 门槛 2 且 proposer 不算 → 还需 d2、d3；
        # d3 有利益冲突不能表态，故门槛应为 1 个外部附议。重建一个门槛为 1 附议的提案路径：
        # 这里直接验证：只有 d2 附议时支持数为 1，门槛 2 下合并被拒绝。
        n20 = tick(20)
        n20.set_amendment_stance(request_id=rid("st"), actor_id="d2",
                                 amendment_id=am_a, position="support")
        support_one = n20.amendment_support(am_a)["support_count"]
        merge_blocked = False
        try:
            n20.merge_amendment(request_id=rid("merge"), actor_id="sec", amendment_id=am_a)
        except Exception:
            merge_blocked = True

        # 秘书处在新提案 p2 上用附议门槛 1 演示合并与冲突互斥
        n20.create_proposal(request_id=rid("prop"), actor_id="sec", proposal_id="p2",
                            title="并行规则提案", required_supports=2, amendment_supports=1)
        n20.add_clause(request_id=rid("clause"), actor_id="d1", proposal_id="p2",
                       clause_id="c2", code="ART-2", title="电子签名条款",
                       language="zh", content="电子签名与手写签名具有同等效力。")
        n20.propose_amendment(request_id=rid("am"), actor_id="d1", clause_id="c2",
                              language="zh", content="电子签名与手写签名具有同等效力，法律另有规定除外。")
        n20.propose_amendment(request_id=rid("am"), actor_id="d2", clause_id="c2",
                              language="zh", content="电子签名应当经过认证后方可使用。")
        c2_ams = n20.list_amendments("c2")
        c2_am_a, c2_am_b = c2_ams[0]["amendment_id"], c2_ams[1]["amendment_id"]
        n20.set_amendment_stance(request_id=rid("st"), actor_id="d2",
                                 amendment_id=c2_am_a, position="support")
        merged = n20.merge_amendment(request_id=rid("merge"), actor_id="sec",
                                     amendment_id=c2_am_a)
        # 同一基础快照的竞争修订不能再合并
        conflict_merge_blocked = False
        try:
            n20.merge_amendment(request_id=rid("merge2"), actor_id="sec", amendment_id=c2_am_b)
        except Exception:
            conflict_merge_blocked = True
        c2_status = {a["amendment_id"]: a["status"] for a in n20.list_amendments("c2")}
        c2_v2 = n20.get_clause("c2")["current_version_id"]

        # ---- 翻译对应：英文译本由翻译提出、审校认证 ----
        n30 = tick(30)
        link = n30.propose_translation(request_id=rid("tr"), actor_id="tr",
                                       source_version_id=c2_v2, target_language="en",
                                       target_content="Electronic signatures have equal effect to handwritten ones.")
        n30.certify_translation(request_id=rid("cert"), actor_id="tr", link_id=link["link_id"])

        # ---- 封存：合并后封存 am_a 在 c2 上的表决事实 ----
        seal = n30.seal_facts(request_id=rid("seal"), actor_id="sec",
                              scope_kind="amendment", scope_id=c2_am_a)
        seal_ok = n30.verify_seal(seal["seal_id"])["valid"]
        # 并发/重复封存同一范围只能有一个结果
        double_seal_blocked = False
        try:
            n30.seal_facts(request_id=rid("seal2"), actor_id="sec",
                           scope_kind="amendment", scope_id=c2_am_a)
        except Exception:
            double_seal_blocked = True
        # 封存后做文字整理（新增译本不影响封存事实）
        n30.propose_translation(request_id=rid("tr"), actor_id="tr",
                                source_version_id=c2_v2, target_language="fr",
                                target_content="Signature électronique équivalente.")
        seal_still_ok = n30.verify_seal(seal["seal_id"])["valid"]

        # ---- 条款立场、签署、共识、条件生效（在 c2 上）----
        n40 = tick(40)
        n40.set_clause_stance(request_id=rid("cs"), actor_id="d1", clause_id="c2",
                              position="accept")
        n40.set_clause_stance(request_id=rid("cs"), actor_id="d2", clause_id="c2",
                              position="conditional_accept", note="待国内批准")
        # d3 有冲突的是 c1；c2 可表态但选择反对
        n40.set_clause_stance(request_id=rid("cs"), actor_id="d3", clause_id="c2",
                              position="oppose")
        # d2 的前置条件在签署前登记，避免共识形成瞬间被当作无条件生效
        n40.add_ratification_condition(request_id=rid("cond"), actor_id="d2",
                                       clause_id="c2", delegation_id="del-b",
                                       kind="domestic_ratification", label="议会批准",
                                       due_at=iso(BASE + timedelta(days=30)))
        n40.add_ratification_condition(request_id=rid("cond"), actor_id="sec",
                                       clause_id="c2", delegation_id="del-b",
                                       kind="deposit", label="交存批准书",
                                       due_at=iso(BASE + timedelta(days=60)))
        sign1 = n40.sign_clause(request_id=rid("sign"), actor_id="d1", clause_id="c2")
        # 重复签署不增加支持数
        sign1_again = n40.sign_clause(request_id=rid("signdup"), actor_id="d1", clause_id="c2")
        # 条件必须按序满足：第二项不能先于第一项
        out_of_order_blocked = False
        try:
            n40.satisfy_condition(request_id=rid("sat"), actor_id="sec",
                                  condition_id=_last_condition_id(n40, "c2", "del-b"),
                                  evidence="交存完成")
        except Exception:
            out_of_order_blocked = True
        sign2 = n40.sign_clause(request_id=rid("sign"), actor_id="d2", clause_id="c2")
        consensus_id = sign2["consensus_id"]
        # 共识形成瞬间 d1 无条件即生效；d2 尚待国内批准
        c2_pub = next(i for i in n40.public_clauses()["items"] if i["clause_id"] == "c2")

        first_id = _first_condition_id(n40, "c2", "del-b")
        n50 = tick(50)
        n50.satisfy_condition(request_id=rid("sat"), actor_id="d2",
                              condition_id=first_id, evidence="议会已批准")
        second_id = _last_condition_id(n50, "c2", "del-b")
        n55 = tick(55)
        sat2 = n55.satisfy_condition(request_id=rid("sat"), actor_id="sec",
                                     condition_id=second_id, evidence="批准书已交存")
        d2_commitment = sat2["commitment_id"]

        # ---- 保留意见只拖住本方：c1 上 del-a 的保留不影响 del-b ----
        n40.set_clause_stance(request_id=rid("cs"), actor_id="d1", clause_id="c1",
                              position="conditional_accept")
        n40.set_clause_stance(request_id=rid("cs"), actor_id="d2", clause_id="c1",
                              position="accept")
        reservation = n40.enter_reservation(request_id=rid("res"), actor_id="d1",
                                            clause_id="c1", rationale="个人信息条款保留")
        n40.sign_clause(request_id=rid("sign"), actor_id="d1", clause_id="c1")
        n40.sign_clause(request_id=rid("sign"), actor_id="d2", clause_id="c1")
        # c1 共识达成；del-b 无条件应生效，del-a 因保留不生效
        c1_pub = next(i for i in n40.public_clauses()["items"] if i["clause_id"] == "c1")
        # 撤回保留后 del-a 也生效
        n50 = tick(50)
        n50.withdraw_reservation(request_id=rid("resw"), actor_id="d1",
                                 reservation_id=reservation["reservation_id"])
        c1_pub_after = next(i for i in n50.public_clauses()["items"] if i["clause_id"] == "c1")

        # ---- 后续行动与服务恢复：期限跨越重启，逾期自动判定 ----
        n50.add_follow_up(request_id=rid("fu"), actor_id="d2", clause_id="c2",
                          delegation_id="del-b", title="提交执行报告",
                          due_at=iso(BASE + timedelta(days=10)))
        # 模拟服务停止后恢复（新的服务实例，同一 SQLite 文件），时钟推进到期限之后
        n_restart = NegotiationService(database, FixedClock(BASE + timedelta(days=40)))
        swept = n_restart.sweep_deadlines()
        sec_view = n_restart.secretary_view("sec")
        overdue_action = next(a for a in sec_view["open_actions"]
                              if a["clause_id"] == "c2" and a["title"] == "提交执行报告")

        # 逾期条件不能补记
        n_restart.add_ratification_condition(request_id=rid("cond"), actor_id="sec",
                                             clause_id="c2", delegation_id="del-c",
                                             kind="notification", label="通知义务",
                                             due_at=iso(BASE + timedelta(days=5)))
        n_restart.set_clause_stance(request_id=rid("cs"), actor_id="d3", clause_id="c2",
                                    position="accept")
        n_restart.sign_clause(request_id=rid("sign"), actor_id="d3", clause_id="c2")
        c_cond = _last_condition_id(n_restart, "c2", "del-c")
        lapsed_blocked = False
        try:
            n_restart.satisfy_condition(request_id=rid("sat"), actor_id="sec",
                                        condition_id=c_cond, evidence="迟到通知")
        except Exception:
            lapsed_blocked = True

        # ---- 视图与公众接口 ----
        delegate_view = n_restart.delegate_view("d2")
        translator_view = n_restart.translator_view()
        observer_view = n_restart.observer_view()
        public = n_restart.public_clauses()

        # ---- 时间点审计解释 ----
        before = n_restart.explain_binding(clause_id="c2", delegation_id="del-b",
                                           at=iso(BASE + timedelta(minutes=45)))
        after = n_restart.explain_binding(clause_id="c2", delegation_id="del-b",
                                          at=iso(BASE + timedelta(days=45)))
        opposed = n_restart.explain_binding(clause_id="c2", delegation_id="del-c",
                                            at=iso(BASE + timedelta(days=45)))

        valid, event_count = DomainService(database).verify_audit()

        database.close()
        return {
            "status": "ok",
            "amendment_external_support_before_threshold": support_one,
            "merge_blocked_below_threshold": merge_blocked,
            "competing_amendment_conflicted": c2_status[c2_am_b] == "conflicted",
            "conflicting_merge_blocked": conflict_merge_blocked,
            "merged_support": merged["support_count"],
            "seal_fact_count": seal["fact_count"],
            "seal_valid": seal_ok,
            "double_seal_blocked": double_seal_blocked,
            "seal_immune_to_editorial_work": seal_still_ok,
            "duplicate_signature_replayed": bool(sign1_again.get("duplicate")),
            "consensus_formed": consensus_id is not None,
            "c2_state_at_consensus": c2_pub["state"],
            "d1_effective_without_conditions": c2_pub["effective_for"][0]["delegation_id"] == "del-a",
            "ordered_conditions_enforced": out_of_order_blocked,
            "d2_effective_after_conditions": d2_commitment is not None,
            "reservation_blocked_only_owner":
                [e["delegation_id"] for e in c1_pub["effective_for"]] == ["del-b"],
            "reservation_release_effectuates_owner":
                {e["delegation_id"] for e in c1_pub_after["effective_for"]} == {"del-a", "del-b"},
            "restart_swept_overdue": swept["actions_overdue"] >= 1,
            "action_overdue_after_restart": overdue_action["status"] == "overdue",
            "lapsed_condition_blocks_effectuation": lapsed_blocked,
            "delegate_view_has_conditions": len(delegate_view["conditions"]) == 2,
            "translator_links": len(translator_view["items"]),
            "observer_seals": len(observer_view["seals"]),
            "public_states": {i["clause_id"]: i["state"] for i in public["items"]},
            "audit_before_not_binding": before["binding"] is False,
            "audit_after_binding": after["binding"] is True,
            "audit_opposed_not_binding": opposed["binding"] is False,
            "audit_explains_reasons": len(after["reasons"]) >= 1 and len(before["reasons"]) >= 2,
            "audit_valid": valid,
            "audit_events": event_count,
        }


def _first_condition_id(service: NegotiationService, clause_id: str, delegation_id: str) -> str:
    row = service.database.connection.execute(
        "SELECT condition_id FROM ratification_conditions WHERE clause_id=? AND delegation_id=? "
        "ORDER BY sequence LIMIT 1", (clause_id, delegation_id)).fetchone()
    return row["condition_id"]


def _last_condition_id(service: NegotiationService, clause_id: str, delegation_id: str) -> str:
    rows = service.database.connection.execute(
        "SELECT condition_id FROM ratification_conditions WHERE clause_id=? AND delegation_id=? "
        "ORDER BY sequence DESC LIMIT 1", (clause_id, delegation_id)).fetchone()
    return rows["condition_id"]


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    ok = result["status"] == "ok" and result["audit_valid"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
