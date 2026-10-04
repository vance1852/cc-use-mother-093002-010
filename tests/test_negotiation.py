import threading
import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.negotiation import NegotiationService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

BASE = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class NegotiationFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.base = DomainService(self.database, self.clock)
        self.neg = NegotiationService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        for org in ("o1", "o2", "o3", "sec", "lang"):
            self.base.register_organization(request_id="org-" + org, actor_id="bootstrap",
                                            organization_id=org, name=org)
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                                 display_name="管理员", role="admin", organization_id="sec")
        self.base.register_actor(request_id="sec", actor_id="admin", new_actor_id="sec",
                                 display_name="秘书处", role="secretary", organization_id="sec")
        for actor, org in (("d1", "o1"), ("d2", "o2"), ("d3", "o3")):
            self.base.register_actor(request_id="actor-" + actor, actor_id="admin",
                                     new_actor_id=actor, display_name=actor,
                                     role="delegate", organization_id=org)
        self.base.register_actor(request_id="actor-tr", actor_id="admin", new_actor_id="tr",
                                 display_name="翻译", role="reviewer", organization_id="lang")
        for delegation, org in (("g1", "o1"), ("g2", "o2"), ("g3", "o3")):
            self.neg.register_delegation(request_id="del-" + delegation, actor_id="sec",
                                         delegation_id=delegation, organization_id=org,
                                         name=delegation)
        for actor, delegation in (("d1", "g1"), ("d2", "g2"), ("d3", "g3")):
            self.neg.grant_mandate(request_id="mand-" + actor, actor_id="sec",
                                   mandate_actor_id=actor, delegation_id=delegation)
        self.neg.create_proposal(request_id="prop", actor_id="sec", proposal_id="p1",
                                 title="提案", required_supports=2, amendment_supports=1)
        self.neg.add_clause(request_id="clause", actor_id="d1", proposal_id="p1",
                            clause_id="c1", code="A1", title="条款",
                            language="zh", content="原始文本")

    def advance(self, minutes=0, days=0):
        new_clock = FixedClock(BASE + timedelta(minutes=minutes, days=days))
        self.clock = new_clock
        self.neg = NegotiationService(self.database, new_clock)


class NegotiationRulesTest(NegotiationFixture):
    def test_proposer_support_not_counted(self):
        self.neg.propose_amendment(request_id="am", actor_id="d1", clause_id="c1",
                                   language="zh", content="修订文本")
        amendment_id = self.neg.list_amendments("c1")[0]["amendment_id"]
        # 提案方不能给自己附议
        with self.assertRaises(ValidationError):
            self.neg.set_amendment_stance(request_id="st-self", actor_id="d1",
                                          amendment_id=amendment_id, position="support")
        self.assertEqual(0, self.neg.amendment_support(amendment_id)["support_count"])

    def test_conflicting_amendments_cannot_both_merge(self):
        self.neg.propose_amendment(request_id="am1", actor_id="d1", clause_id="c1",
                                   language="zh", content="修订一")
        self.neg.propose_amendment(request_id="am2", actor_id="d2", clause_id="c1",
                                   language="zh", content="修订二")
        ids = [a["amendment_id"] for a in self.neg.list_amendments("c1")]
        self.neg.set_amendment_stance(request_id="s1", actor_id="d2",
                                      amendment_id=ids[0], position="support")
        self.neg.merge_amendment(request_id="m1", actor_id="sec", amendment_id=ids[0])
        with self.assertRaises(ConflictError):
            self.neg.merge_amendment(request_id="m2", actor_id="sec", amendment_id=ids[1])
        statuses = {a["amendment_id"]: a["status"] for a in self.neg.list_amendments("c1")}
        self.assertEqual("conflicted", statuses[ids[1]])

    def test_merge_requires_threshold(self):
        self.neg.create_proposal(request_id="prop2", actor_id="sec", proposal_id="p2",
                                 title="高二门槛提案", required_supports=2, amendment_supports=2)
        self.neg.add_clause(request_id="clause2", actor_id="d1", proposal_id="p2",
                            clause_id="c2", code="A2", title="条款二",
                            language="zh", content="文本")
        self.neg.propose_amendment(request_id="am", actor_id="d1", clause_id="c2",
                                   language="zh", content="修订")
        amendment_id = self.neg.list_amendments("c2")[0]["amendment_id"]
        self.neg.set_amendment_stance(request_id="s", actor_id="d2",
                                      amendment_id=amendment_id, position="support")
        with self.assertRaises(ConflictError):
            self.neg.merge_amendment(request_id="m", actor_id="sec", amendment_id=amendment_id)

    def test_conflict_blocks_participation(self):
        self.neg.declare_conflict(request_id="cf", actor_id="d3", clause_id="c1",
                                  rationale="利益关联")
        with self.assertRaises(PermissionDenied):
            self.neg.propose_amendment(request_id="am", actor_id="d3", clause_id="c1",
                                       language="zh", content="修订")
        with self.assertRaises(PermissionDenied):
            self.neg.set_clause_stance(request_id="st", actor_id="d3",
                                       clause_id="c1", position="accept")

    def test_expired_mandate_blocks_delegate(self):
        # 撤销 d1 的长期授权，改为限期授权
        global_id = self.database.connection.execute(
            "SELECT mandate_id FROM mandates WHERE actor_id='d1'").fetchone()["mandate_id"]
        self.neg.revoke_mandate(request_id="rev-mand", actor_id="sec", mandate_id=global_id)
        self.neg.grant_mandate(request_id="mand-limited", actor_id="sec",
                               mandate_actor_id="d1", delegation_id="g1",
                               scope_kind="clause", scope_id="c1",
                               valid_from=iso(BASE),
                               valid_until=iso(BASE + timedelta(days=1)))
        self.advance(days=2)
        with self.assertRaises(PermissionDenied):
            self.neg.set_clause_stance(request_id="st-late", actor_id="d1",
                                       clause_id="c1", position="accept")

    def test_duplicate_signature_does_not_increase_support(self):
        self.neg.set_clause_stance(request_id="s1", actor_id="d1", clause_id="c1",
                                   position="accept")
        first = self.neg.sign_clause(request_id="sign1", actor_id="d1", clause_id="c1")
        second = self.neg.sign_clause(request_id="sign2", actor_id="d1", clause_id="c1")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["support_count"], second["support_count"])

    def test_sign_requires_accepting_stance_on_current_snapshot(self):
        with self.assertRaises(PermissionDenied):
            self.neg.sign_clause(request_id="no-stance", actor_id="d1", clause_id="c1")
        self.neg.set_clause_stance(request_id="oppose", actor_id="d2", clause_id="c1",
                                   position="oppose")
        with self.assertRaises(PermissionDenied):
            self.neg.sign_clause(request_id="opp-sign", actor_id="d2", clause_id="c1")

    def test_conditions_must_be_satisfied_in_order(self):
        self._reach_consensus(conditions=[
            ("domestic_ratification", "国内批准", 10),
            ("deposit", "交存", 20),
        ])
        rows = self.database.connection.execute(
            "SELECT condition_id,sequence FROM ratification_conditions "
            "WHERE delegation_id='g1' ORDER BY sequence").fetchall()
        second = rows[1]["condition_id"]
        with self.assertRaises(ConflictError):
            self.neg.satisfy_condition(request_id="sat2", actor_id="sec",
                                       condition_id=second, evidence="先交存")

    def test_overdue_condition_lapses_and_blocks_effectuation(self):
        self._reach_consensus(conditions=[("notification", "通知", 5)])
        self.advance(days=10)
        self.neg.sweep_deadlines()
        condition_id = self.database.connection.execute(
            "SELECT condition_id FROM ratification_conditions WHERE delegation_id='g1'").fetchone()["condition_id"]
        with self.assertRaises(ConflictError):
            self.neg.satisfy_condition(request_id="late", actor_id="sec",
                                       condition_id=condition_id, evidence="迟到")
        explanation = self.neg.explain_binding(
            clause_id="c1", delegation_id="g1", at=iso(BASE + timedelta(days=12)))
        self.assertFalse(explanation["binding"])
        self.assertTrue(any("超过期限" in r for r in explanation["reasons"]))

    def _stances(self):
        self.neg.set_clause_stance(request_id="st1", actor_id="d1", clause_id="c1",
                                   position="accept")
        self.neg.set_clause_stance(request_id="st2", actor_id="d2", clause_id="c1",
                                   position="accept")

    def _reach_consensus(self, delegation="g1", conditions=None):
        """登记立场；可在签署前为指定代表团先挂前置条件，然后签署形成共识。"""
        self._stances()
        if conditions:
            for index, (kind, label, days) in enumerate(conditions, start=1):
                self.neg.add_ratification_condition(
                    request_id=f"pre-{delegation}-{index}", actor_id="sec",
                    clause_id="c1", delegation_id=delegation, kind=kind, label=label,
                    due_at=iso(BASE + timedelta(days=days)))
        self.neg.sign_clause(request_id="sg1", actor_id="d1", clause_id="c1")
        self.neg.sign_clause(request_id="sg2", actor_id="d2", clause_id="c1")

    def test_consensus_is_pending_until_conditions_then_binding(self):
        self._stances()
        self.neg.add_ratification_condition(request_id="cond", actor_id="sec",
                                            clause_id="c1", delegation_id="g1",
                                            kind="domestic_ratification", label="批准",
                                            due_at=iso(BASE + timedelta(days=30)))
        self.neg.sign_clause(request_id="sg1", actor_id="d1", clause_id="c1")
        self.neg.sign_clause(request_id="sg2", actor_id="d2", clause_id="c1")
        condition_id = self.database.connection.execute(
            "SELECT condition_id FROM ratification_conditions WHERE delegation_id='g1'").fetchone()["condition_id"]
        before = self.neg.explain_binding(clause_id="c1", delegation_id="g1",
                                          at=iso(BASE + timedelta(days=1)))
        self.assertFalse(before["binding"])
        self.advance(days=3)
        result = self.neg.satisfy_condition(request_id="sat", actor_id="sec",
                                            condition_id=condition_id, evidence="批准完成")
        self.assertIsNotNone(result["commitment_id"])
        after = self.neg.explain_binding(clause_id="c1", delegation_id="g1",
                                         at=iso(BASE + timedelta(days=4)))
        self.assertTrue(after["binding"])

    def test_reservation_blocks_only_owner(self):
        self._reach_consensus()
        # g1、g2 均已生效（无条件）
        self.assertTrue(self.neg.explain_binding(
            clause_id="c1", delegation_id="g2", at=iso(BASE + timedelta(days=1)))["binding"])
        # g1 现在加保留不会溯及既往；构造另一条款验证保留阻断
        self.neg.create_proposal(request_id="prop3", actor_id="sec", proposal_id="p3",
                                 title="保留测试", required_supports=2, amendment_supports=1)
        self.neg.add_clause(request_id="clause3", actor_id="d1", proposal_id="p3",
                            clause_id="c3", code="A3", title="条款三",
                            language="zh", content="文本三")
        self.neg.set_clause_stance(request_id="a", actor_id="d1", clause_id="c3",
                                   position="conditional_accept")
        self.neg.enter_reservation(request_id="res", actor_id="d1", clause_id="c3",
                                   rationale="保留")
        self.neg.set_clause_stance(request_id="b", actor_id="d2", clause_id="c3",
                                   position="accept")
        self.neg.sign_clause(request_id="sa", actor_id="d1", clause_id="c3")
        self.neg.sign_clause(request_id="sb", actor_id="d2", clause_id="c3")
        self.assertFalse(self.neg.explain_binding(
            clause_id="c3", delegation_id="g1", at=iso(BASE + timedelta(days=1)))["binding"])
        self.assertTrue(self.neg.explain_binding(
            clause_id="c3", delegation_id="g2", at=iso(BASE + timedelta(days=1)))["binding"])

    def test_concurrent_seal_has_single_result(self):
        self.neg.propose_amendment(request_id="am", actor_id="d1", clause_id="c1",
                                   language="zh", content="修订文本")
        amendment_id = self.neg.list_amendments("c1")[0]["amendment_id"]
        self.neg.set_amendment_stance(request_id="st", actor_id="d2",
                                      amendment_id=amendment_id, position="support")
        errors = []
        results = []

        def seal(tag):
            service = NegotiationService(self.database, self.clock)
            try:
                receipt = service.seal_facts(request_id="seal-" + tag, actor_id="sec",
                                             scope_kind="amendment", scope_id=amendment_id)
                results.append(receipt["resource_id"])
            except ConflictError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=seal, args=(str(i),)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len(results))
        self.assertEqual(7, len(errors))
        seal_rows = self.database.connection.execute(
            "SELECT COUNT(*) AS n FROM seals WHERE scope_kind='amendment' AND scope_id=?",
            (amendment_id,)).fetchone()["n"]
        self.assertEqual(1, seal_rows)

    def test_sealed_facts_immune_to_later_editorial_work(self):
        self.neg.propose_amendment(request_id="am", actor_id="d1", clause_id="c1",
                                   language="zh", content="修订文本")
        amendment_id = self.neg.list_amendments("c1")[0]["amendment_id"]
        self.neg.set_amendment_stance(request_id="st", actor_id="d2",
                                      amendment_id=amendment_id, position="oppose")
        seal = self.neg.seal_facts(request_id="seal", actor_id="sec",
                                   scope_kind="amendment", scope_id=amendment_id)
        # 后续撤回立场并增加译本，封存摘要仍然有效
        self.neg.set_amendment_stance(request_id="st2", actor_id="d2",
                                      amendment_id=amendment_id, position="withdrawn")
        version_id = self.neg.get_clause("c1")["current_version_id"]
        self.neg.propose_translation(request_id="tr", actor_id="tr",
                                     source_version_id=version_id, target_language="en",
                                     target_content="English text")
        self.assertTrue(self.neg.verify_seal(seal["seal_id"])["valid"])

    def test_translation_requires_certification(self):
        version_id = self.neg.get_clause("c1")["current_version_id"]
        link = self.neg.propose_translation(request_id="tr", actor_id="tr",
                                            source_version_id=version_id,
                                            target_language="en", target_content="Text")
        view = {item["link_id"]: item["status"] for item in self.neg.translator_view("tr")["items"]}
        self.assertEqual("proposed", view[link["link_id"]])
        self.neg.certify_translation(request_id="cert", actor_id="tr",
                                     link_id=link["link_id"])
        view = {item["link_id"]: item["status"] for item in self.neg.translator_view("tr")["items"]}
        self.assertEqual("certified", view[link["link_id"]])

    def test_views_are_role_separated(self):
        with self.assertRaises(PermissionDenied):
            self.neg.secretary_view("d1")
        with self.assertRaises(PermissionDenied):
            self.neg.translator_view("d1")
        # 观察员不能调代表视图，但封存等公开事实可查
        with self.assertRaises(PermissionDenied):
            self.neg.delegate_view("tr")

    def test_amendment_blocked_after_consensus(self):
        self._reach_consensus()
        with self.assertRaises(ConflictError):
            self.neg.propose_amendment(request_id="late-am", actor_id="d1",
                                       clause_id="c1", language="zh",
                                       content="共识之后的修订")


class PersistenceTest(unittest.TestCase):
    def test_state_survives_service_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            fixture = NegotiationService(database, FixedClock(BASE))
            base = DomainService(database, FixedClock(BASE))
            base.register_organization(request_id="org-sec", actor_id="bootstrap",
                                       organization_id="sec", name="秘书处")
            base.register_organization(request_id="org-o1", actor_id="bootstrap",
                                       organization_id="o1", name="成员")
            base.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin",
                                display_name="管理员", role="admin", organization_id="sec")
            base.register_actor(request_id="actor-sec", actor_id="admin", new_actor_id="sec",
                                display_name="秘书处", role="secretary", organization_id="sec")
            base.register_actor(request_id="actor-d1", actor_id="admin", new_actor_id="d1",
                                display_name="代表", role="delegate", organization_id="o1")
            fixture.register_delegation(request_id="dg", actor_id="sec",
                                        delegation_id="g1", organization_id="o1", name="一团")
            fixture.grant_mandate(request_id="m", actor_id="sec", mandate_actor_id="d1",
                                  delegation_id="g1")
            fixture.create_proposal(request_id="p", actor_id="sec", proposal_id="p1",
                                    title="t", required_supports=1, amendment_supports=1)
            fixture.add_clause(request_id="c", actor_id="sec", proposal_id="p1",
                               clause_id="c1", code="X", title="t",
                               language="zh", content="内容")
            database.close()

            database2 = Database(path)
            restarted = NegotiationService(database2, FixedClock(BASE))
            clause = restarted.get_clause("c1")
            self.assertEqual("内容", clause["content"])
            database2.close()


if __name__ == "__main__":
    unittest.main()
