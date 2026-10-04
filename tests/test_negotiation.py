import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import (ConflictError, NotFoundError,
                                             PermissionDenied, ValidationError)
from digital_trade_foundation.negotiation import NegotiationService
from digital_trade_foundation.storage import Database

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class NegotiationTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = NegotiationService(self.database, FixedClock(T0))
        self._seq = 0
        self.service.register_organization(request_id="boot-org", actor_id="bootstrap",
                                           organization_id="o-sec", name="秘书处机构")
        self.service.register_actor(request_id="boot-admin", actor_id="bootstrap",
                                    new_actor_id="admin", display_name="系统管理员",
                                    role="admin", organization_id="o-sec")
        self.service.register_actor(request_id="boot-sec", actor_id="admin",
                                    new_actor_id="sec", display_name="秘书处官员",
                                    role="secretariat", organization_id="o-sec")
        self.service.register_actor(request_id="boot-tr1", actor_id="admin",
                                    new_actor_id="tr1", display_name="翻译审校一",
                                    role="translator", organization_id="o-sec")
        self.service.register_actor(request_id="boot-tr2", actor_id="admin",
                                    new_actor_id="tr2", display_name="翻译审校二",
                                    role="translator", organization_id="o-sec")
        self.service.register_actor(request_id="boot-obs", actor_id="admin",
                                    new_actor_id="obs", display_name="观察员",
                                    role="observer", organization_id="o-sec")
        for code in ("alpha", "beta", "gamma"):
            self.service.register_organization(request_id=f"org-{code}", actor_id="admin",
                                               organization_id=f"o-{code}", name=f"成员{code}")
            self.service.register_actor(request_id=f"actor-{code}", actor_id="admin",
                                        new_actor_id=f"del-{code}", display_name=f"代表{code}",
                                        role="delegate", organization_id=f"o-{code}")
        self.service.create_dialogue(request_id="dlg", actor_id="sec", dialogue_id="d1",
                                     title="数字贸易秩序对话", quorum=2, support_threshold=0.5)
        self.service.enroll_delegation(request_id="enroll-alpha", actor_id="sec",
                                       dialogue_id="d1", delegation_id="del-alpha",
                                       organization_id="o-alpha", name="阿尔法代表团",
                                       can_sign=True, preconditions=[])
        self.service.enroll_delegation(request_id="enroll-beta", actor_id="sec",
                                       dialogue_id="d1", delegation_id="del-beta",
                                       organization_id="o-beta", name="贝塔代表团",
                                       can_sign=True,
                                       preconditions=["domestic_ratification"])
        self.service.enroll_delegation(request_id="enroll-gamma", actor_id="sec",
                                       dialogue_id="d1", delegation_id="del-gamma",
                                       organization_id="o-gamma", name="伽马观察团",
                                       can_sign=False, preconditions=[])

    def tearDown(self):
        self.database.close()

    def rid(self):
        self._seq += 1
        return f"req-{self._seq}"

    def add_clause(self, key="A.1", text="原始文本"):
        self.service.create_proposal(request_id=self.rid(), actor_id="sec",
                                     dialogue_id="d1", proposal_id=f"p-{key}",
                                     title=f"提案{key}",
                                     proposer_delegation_id="del-alpha")
        self.service.add_clause(request_id=self.rid(), actor_id="sec",
                                proposal_id=f"p-{key}", clause_id=f"c-{key}",
                                clause_key=key, language="zh", text=text)
        return f"c-{key}", self.service.get_clause(f"c-{key}")["head_version_id"]

    def accept(self, version_id, delegation, stance="accept", note=None, session="s-1"):
        return self.service.cast_position(request_id=self.rid(), actor_id=f"del-{delegation}",
                                          version_id=version_id,
                                          delegation_id=f"del-{delegation}",
                                          stance=stance, note=note, session_key=session)

    def test_full_lifecycle_to_in_force(self):
        clause_id, version_id = self.add_clause()
        translation = self.service.submit_translation(
            request_id=self.rid(), actor_id="tr1", version_id=version_id,
            language="en", text="Original text")
        self.service.verify_translation(request_id=self.rid(), actor_id="tr2",
                                        translation_id=translation.resource_id)
        self.accept(version_id, "alpha")
        self.accept(version_id, "beta")
        receipt = self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                              version_id=version_id)
        commitments = {item["delegation_id"]: item
                       for item in self.service.list_commitments("d1")}
        self.assertEqual("in_force", commitments["del-alpha"]["status"])
        self.assertEqual("pending", commitments["del-beta"]["status"])
        beta = commitments["del-beta"]
        self.assertEqual("domestic_ratification", beta["conditions"][0]["kind"])
        self.service.fulfill_condition(request_id=self.rid(), actor_id="sec",
                                       commitment_id=beta["commitment_id"], seq=0)
        updated = self.service.get_commitment(beta["commitment_id"])
        self.assertEqual("in_force", updated["status"])
        self.assertIsNotNone(updated["effective_at"])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)

    def test_support_computed_on_same_snapshot(self):
        clause_id, version_id = self.add_clause()
        self.accept(version_id, "alpha")
        self.accept(version_id, "beta")
        self.service.propose_amendment(request_id=self.rid(), actor_id="del-alpha",
                                       clause_id=clause_id, amendment_id="am-1",
                                       delegation_id="del-alpha", proposed_text="修订文本")
        self.service.second_amendment(request_id=self.rid(), actor_id="del-beta",
                                      amendment_id="am-1", delegation_id="del-beta")
        merged = self.service.merge_amendment(request_id=self.rid(), actor_id="sec",
                                              amendment_id="am-1")
        new_version = merged.resource_id
        old_tally = self.service.tally(version_id)
        self.assertEqual(2, old_tally["support"])
        self.assertTrue(old_tally["qualifies"])
        new_tally = self.service.tally(new_version)
        self.assertEqual(0, new_tally["support"])
        self.assertFalse(new_tally["qualifies"])
        with self.assertRaises(ConflictError):
            self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                        version_id=new_version)
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_id)

    def test_duplicate_position_does_not_increase_support(self):
        _, version_id = self.add_clause()
        self.accept(version_id, "alpha")
        self.accept(version_id, "alpha")
        self.service.register_actor(request_id=self.rid(), actor_id="admin",
                                    new_actor_id="del-alpha-2", display_name="阿尔法副代表",
                                    role="delegate", organization_id="o-alpha")
        self.service.cast_position(request_id=self.rid(), actor_id="del-alpha-2",
                                   version_id=version_id, delegation_id="del-alpha",
                                   stance="accept", session_key="s-1")
        self.accept(version_id, "beta")
        tally = self.service.tally(version_id)
        self.assertEqual(2, tally["support"])
        self.assertEqual(2, tally["positions"])

    def test_conflicting_amendments_cannot_both_merge(self):
        clause_id, _ = self.add_clause()
        for amendment_id, text in (("am-a", "方案甲"), ("am-b", "方案乙")):
            self.service.propose_amendment(request_id=self.rid(), actor_id="del-alpha",
                                           clause_id=clause_id, amendment_id=amendment_id,
                                           delegation_id="del-alpha", proposed_text=text)
            self.service.second_amendment(request_id=self.rid(), actor_id="del-beta",
                                          amendment_id=amendment_id,
                                          delegation_id="del-beta")
        self.service.merge_amendment(request_id=self.rid(), actor_id="sec",
                                     amendment_id="am-a")
        with self.assertRaises(ConflictError):
            self.service.merge_amendment(request_id=self.rid(), actor_id="sec",
                                         amendment_id="am-b")
        view = self.service.dialogue_view(actor_id="sec", dialogue_id="d1")
        statuses = {item["amendment_id"]: item["status"] for item in view["amendments"]}
        self.assertEqual("merged", statuses["am-a"])
        self.assertEqual("conflicted", statuses["am-b"])

    def test_merge_requires_second_and_open_status(self):
        clause_id, _ = self.add_clause()
        self.service.propose_amendment(request_id=self.rid(), actor_id="del-alpha",
                                       clause_id=clause_id, amendment_id="am-solo",
                                       delegation_id="del-alpha", proposed_text="单独修订")
        with self.assertRaises(ConflictError):
            self.service.merge_amendment(request_id=self.rid(), actor_id="sec",
                                         amendment_id="am-solo")
        with self.assertRaises(ValidationError):
            self.service.second_amendment(request_id=self.rid(), actor_id="del-alpha",
                                          amendment_id="am-solo",
                                          delegation_id="del-alpha")
        self.service.second_amendment(request_id=self.rid(), actor_id="del-beta",
                                      amendment_id="am-solo", delegation_id="del-beta")
        with self.assertRaises(ConflictError):
            self.service.second_amendment(request_id=self.rid(), actor_id="del-beta",
                                          amendment_id="am-solo",
                                          delegation_id="del-beta")
        self.service.withdraw_amendment(request_id=self.rid(), actor_id="del-alpha",
                                        amendment_id="am-solo")
        with self.assertRaises(ConflictError):
            self.service.merge_amendment(request_id=self.rid(), actor_id="sec",
                                         amendment_id="am-solo")

    def test_conditional_acceptance_adds_ordered_precondition(self):
        _, version_id = self.add_clause()
        self.accept(version_id, "alpha")
        self.accept(version_id, "beta", stance="conditional_accept",
                    note="须完成议会审议")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_id)
        commitments = {item["delegation_id"]: item
                       for item in self.service.list_commitments("d1")}
        beta = commitments["del-beta"]
        kinds = [condition["kind"] for condition in beta["conditions"]]
        self.assertEqual(["acceptance_condition", "domestic_ratification"], kinds)
        with self.assertRaises(ConflictError):
            self.service.fulfill_condition(request_id=self.rid(), actor_id="sec",
                                           commitment_id=beta["commitment_id"], seq=1)
        self.service.fulfill_condition(request_id=self.rid(), actor_id="sec",
                                       commitment_id=beta["commitment_id"], seq=0)
        self.assertEqual("pending",
                         self.service.get_commitment(beta["commitment_id"])["status"])
        self.service.fulfill_condition(request_id=self.rid(), actor_id="sec",
                                       commitment_id=beta["commitment_id"], seq=1)
        self.assertEqual("in_force",
                         self.service.get_commitment(beta["commitment_id"])["status"])

    def test_reservation_does_not_block_independent_clause(self):
        _, version_a = self.add_clause("A.1")
        _, version_b = self.add_clause("B.1")
        self.accept(version_a, "alpha")
        self.accept(version_a, "beta", stance="reserve", note="保留第三款")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_a)
        self.accept(version_b, "alpha")
        self.accept(version_b, "beta")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_b)
        public = self.service.public_view("d1")
        clauses = {item["clause_key"]: item for item in public["clauses"]}
        self.assertEqual("accepted", clauses["A.1"]["stage"])
        self.assertEqual("accepted", clauses["B.1"]["stage"])
        beta_a = {p["delegation_id"]: p for p in clauses["A.1"]["participants"]}["del-beta"]
        self.assertEqual("reserved", beta_a["status"])
        self.assertEqual("保留第三款", beta_a["reservation"])
        commitments = {item["clause_id"]: item for item in self.service.list_commitments("d1")
                       if item["delegation_id"] == "del-beta"}
        self.assertEqual("pending", commitments["c-A.1"]["status"])
        self.assertEqual("pending", commitments["c-B.1"]["status"])

    def test_sealed_facts_are_immutable(self):
        clause_id, version_id = self.add_clause()
        self.service.grant_speaking(request_id=self.rid(), actor_id="sec",
                                    dialogue_id="d1", grant_id="g-1",
                                    delegation_id="del-alpha", delegate_actor_id="del-alpha",
                                    valid_from="2026-01-01T00:00:00Z",
                                    valid_until="2027-01-01T00:00:00Z")
        statement = self.service.add_statement(request_id=self.rid(), actor_id="del-alpha",
                                               dialogue_id="d1", delegation_id="del-alpha",
                                               session_key="s-1", body="我方支持该条款",
                                               grant_id="g-1", clause_id=clause_id)
        self.accept(version_id, "alpha")
        seal = self.service.seal_session(request_id=self.rid(), actor_id="sec",
                                         dialogue_id="d1", session_key="s-1")
        check = self.service.verify_seal(seal.resource_id)
        self.assertTrue(check["valid"])
        self.assertEqual(1, check["statement_count"])
        self.assertEqual(1, check["position_count"])
        with self.assertRaises(ConflictError):
            self.service.revise_statement(request_id=self.rid(), actor_id="sec",
                                          statement_id=statement.resource_id,
                                          body="整理后的发言")
        with self.assertRaises(ConflictError):
            self.accept(version_id, "alpha", stance="reject")
        with self.assertRaises(ConflictError):
            self.service.add_statement(request_id=self.rid(), actor_id="del-alpha",
                                       dialogue_id="d1", delegation_id="del-alpha",
                                       session_key="s-1", body="补充发言", grant_id="g-1")
        self.assertTrue(self.service.verify_seal(seal.resource_id)["valid"])

    def test_concurrent_seal_has_single_result(self):
        _, version_id = self.add_clause()
        self.accept(version_id, "alpha")
        first = self.service.seal_session(request_id="seal-req", actor_id="sec",
                                          dialogue_id="d1", session_key="s-9")
        replay = self.service.seal_session(request_id="seal-req", actor_id="sec",
                                           dialogue_id="d1", session_key="s-9")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.seal_session(request_id=self.rid(), actor_id="sec",
                                      dialogue_id="d1", session_key="s-9")

    def test_recovery_keeps_conditions_and_deadlines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "negotiation.sqlite3"
            database = Database(path)
            service = NegotiationService(database, FixedClock(T0))
            service.register_organization(request_id="r1", actor_id="bootstrap",
                                          organization_id="o-sec", name="秘书处机构")
            service.register_actor(request_id="r2", actor_id="bootstrap",
                                   new_actor_id="admin", display_name="系统管理员",
                                   role="admin", organization_id="o-sec")
            service.register_actor(request_id="r2b", actor_id="admin",
                                   new_actor_id="sec", display_name="秘书处官员",
                                   role="secretariat", organization_id="o-sec")
            service.register_organization(request_id="r3", actor_id="admin",
                                          organization_id="o-alpha", name="成员甲")
            service.register_actor(request_id="r4", actor_id="admin", new_actor_id="del-alpha",
                                   display_name="代表甲", role="delegate",
                                   organization_id="o-alpha")
            service.create_dialogue(request_id="r5", actor_id="sec", dialogue_id="d1",
                                    title="恢复演练", quorum=1, support_threshold=1.0)
            service.enroll_delegation(request_id="r6", actor_id="sec", dialogue_id="d1",
                                      delegation_id="del-alpha",
                                      organization_id="o-alpha", name="甲代表团",
                                      can_sign=True,
                                      preconditions=["domestic_ratification",
                                                     "organizational_approval"])
            service.create_proposal(request_id="r7", actor_id="sec", dialogue_id="d1",
                                    proposal_id="p1", title="提案",
                                    proposer_delegation_id="del-alpha")
            service.add_clause(request_id="r8", actor_id="sec", proposal_id="p1",
                               clause_id="c1", clause_key="A.1", language="zh",
                               text="文本")
            version_id = service.get_clause("c1")["head_version_id"]
            service.cast_position(request_id="r9", actor_id="del-alpha",
                                  version_id=version_id, delegation_id="del-alpha",
                                  stance="accept", session_key="s-1")
            service.form_consensus(request_id="r10", actor_id="sec", version_id=version_id)
            service.create_action(request_id="r11", actor_id="sec", dialogue_id="d1",
                                  action_id="act-1", assignee_delegation_id="del-alpha",
                                  title="提交国内通报",
                                  due_at=_iso(T0 + timedelta(days=1)))
            commitment = service.list_commitments("d1")[0]
            service.fulfill_condition(request_id="r12", actor_id="sec",
                                      commitment_id=commitment["commitment_id"], seq=0)
            database.close()

            later = T0 + timedelta(days=2)
            reopened = NegotiationService(Database(path), FixedClock(later))
            pending = reopened.get_commitment(commitment["commitment_id"])
            self.assertEqual("pending", pending["status"])
            self.assertEqual("satisfied", pending["conditions"][0]["status"])
            self.assertEqual("pending", pending["conditions"][1]["status"])
            actions = reopened.list_actions("d1")
            self.assertEqual("overdue", actions[0]["effective_status"])
            reopened.fulfill_condition(request_id="r13", actor_id="sec",
                                       commitment_id=commitment["commitment_id"], seq=1)
            done = reopened.get_commitment(commitment["commitment_id"])
            self.assertEqual("in_force", done["status"])
            explanation = reopened.explain_binding(clause_id="c1",
                                                   delegation_id="del-alpha",
                                                   at=_iso(later))
            self.assertTrue(explanation["binding"])
            valid, _ = reopened.verify_audit()
            self.assertTrue(valid)
            reopened.database.close()

    def test_public_view_distinguishes_categories(self):
        self.add_clause("D.1")
        _, version_accept = self.add_clause("E.1")
        _, version_reserved = self.add_clause("F.1")
        self.accept(version_accept, "alpha")
        self.accept(version_accept, "beta")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_accept)
        self.accept(version_reserved, "alpha")
        self.accept(version_reserved, "beta", stance="reserve", note="保留第二款")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_reserved)
        public = self.service.public_view("d1")
        clauses = {item["clause_key"]: item for item in public["clauses"]}
        self.assertEqual("draft", clauses["D.1"]["stage"])
        self.assertNotIn("participants", clauses["D.1"])
        accepted = {p["delegation_id"]: p for p in clauses["E.1"]["participants"]}
        self.assertEqual("in_effect", accepted["del-alpha"]["status"])
        self.assertEqual("accepted", accepted["del-beta"]["status"])
        reserved = {p["delegation_id"]: p for p in clauses["F.1"]["participants"]}
        self.assertEqual("reserved", reserved["del-beta"]["status"])
        self.assertEqual("in_effect", reserved["del-alpha"]["status"])

    def test_role_views_differ(self):
        _, version_id = self.add_clause()
        translation = self.service.submit_translation(
            request_id=self.rid(), actor_id="tr1", version_id=version_id,
            language="en", text="Draft translation")
        self.accept(version_id, "alpha")
        observer = self.service.dialogue_view(actor_id="obs", dialogue_id="d1")
        self.assertEqual("observer", observer["role"])
        self.assertEqual(["public"], [key for key in observer if key != "role"])
        translator = self.service.dialogue_view(actor_id="tr1", dialogue_id="d1")
        self.assertEqual(translation.resource_id,
                         translator["verification_queue"][0]["translation_id"])
        delegate = self.service.dialogue_view(actor_id="del-alpha", dialogue_id="d1")
        self.assertEqual("del-alpha", delegate["my_delegation"]["delegation_id"])
        self.assertEqual(1, len(delegate["my_delegation"]["positions"]))
        secretariat = self.service.dialogue_view(actor_id="sec", dialogue_id="d1")
        self.assertIn("positions", secretariat)
        self.assertIn("amendments", secretariat)
        self.assertEqual(3, len(secretariat["delegations"]))
        self.service.register_organization(request_id=self.rid(), actor_id="admin",
                                           organization_id="o-delta", name="非成员")
        self.service.register_actor(request_id=self.rid(), actor_id="admin",
                                    new_actor_id="del-delta", display_name="非对话代表",
                                    role="delegate", organization_id="o-delta")
        with self.assertRaises(PermissionDenied):
            self.service.dialogue_view(actor_id="del-delta", dialogue_id="d1")

    def test_explain_binding_across_time(self):
        clause_id, version_id = self.add_clause()
        self.accept(version_id, "alpha")
        self.accept(version_id, "beta")
        self.service.form_consensus(request_id=self.rid(), actor_id="sec",
                                    version_id=version_id)
        before = self.service.explain_binding(clause_id=clause_id,
                                              delegation_id="del-beta",
                                              at=_iso(T0 - timedelta(hours=1)))
        self.assertFalse(before["binding"])
        self.assertIn("尚未形成待核准共识", before["reasons"][0])
        pending = self.service.explain_binding(clause_id=clause_id,
                                               delegation_id="del-beta", at=_iso(T0))
        self.assertFalse(pending["binding"])
        self.assertIn("domestic_ratification", pending["reasons"][-1])
        observer_view = self.service.explain_binding(clause_id=clause_id,
                                                     delegation_id="del-gamma",
                                                     at=_iso(T0))
        self.assertFalse(observer_view["binding"])
        self.assertIn("未对该文本快照表态", observer_view["reasons"][-1])
        self.service.clock = FixedClock(T0 + timedelta(hours=2))
        beta = {item["delegation_id"]: item
                for item in self.service.list_commitments("d1")}["del-beta"]
        self.service.fulfill_condition(request_id=self.rid(), actor_id="sec",
                                       commitment_id=beta["commitment_id"], seq=0)
        between = self.service.explain_binding(clause_id=clause_id,
                                               delegation_id="del-beta", at=_iso(T0))
        self.assertFalse(between["binding"])
        self.assertIn("实际满足时间", between["reasons"][-1])
        after = self.service.explain_binding(clause_id=clause_id,
                                             delegation_id="del-beta",
                                             at=_iso(T0 + timedelta(hours=3)))
        self.assertTrue(after["binding"])
        self.assertIsNotNone(after["effective_at"])

    def test_coi_blocks_sensitive_actions(self):
        clause_id, version_id = self.add_clause()
        translation = self.service.submit_translation(
            request_id=self.rid(), actor_id="tr1", version_id=version_id,
            language="en", text="Translation")
        self.service.declare_coi(request_id=self.rid(), actor_id="sec",
                                 dialogue_id="d1", declaration_id="coi-1",
                                 target_actor_id="tr2",
                                 description="与条款涉及企业存在关联",
                                 clause_id=clause_id)
        with self.assertRaises(PermissionDenied):
            self.service.verify_translation(request_id=self.rid(), actor_id="tr2",
                                            translation_id=translation.resource_id)
        self.service.declare_coi(request_id=self.rid(), actor_id="del-alpha",
                                 dialogue_id="d1", declaration_id="coi-2",
                                 target_actor_id="del-alpha",
                                 description="自我申报", clause_id=clause_id)
        with self.assertRaises(PermissionDenied):
            self.accept(version_id, "alpha")
        self.service.clear_coi(request_id=self.rid(), actor_id="sec",
                               declaration_id="coi-1")
        self.service.clear_coi(request_id=self.rid(), actor_id="sec",
                               declaration_id="coi-2")
        self.service.verify_translation(request_id=self.rid(), actor_id="tr2",
                                        translation_id=translation.resource_id)
        self.accept(version_id, "alpha")

    def test_speaking_authorization_enforced(self):
        clause_id, _ = self.add_clause()
        with self.assertRaises(NotFoundError):
            self.service.add_statement(request_id=self.rid(), actor_id="del-alpha",
                                       dialogue_id="d1", delegation_id="del-alpha",
                                       session_key="s-1", body="无授权发言", grant_id="g-x")
        self.service.grant_speaking(request_id=self.rid(), actor_id="sec",
                                    dialogue_id="d1", grant_id="g-ok",
                                    delegation_id="del-alpha", delegate_actor_id="del-alpha",
                                    valid_from="2026-01-01T00:00:00Z",
                                    valid_until="2027-01-01T00:00:00Z")
        self.service.add_statement(request_id=self.rid(), actor_id="del-alpha",
                                   dialogue_id="d1", delegation_id="del-alpha",
                                   session_key="s-1", body="授权发言", grant_id="g-ok")
        with self.assertRaises(PermissionDenied):
            self.service.add_statement(request_id=self.rid(), actor_id="del-beta",
                                       dialogue_id="d1", delegation_id="del-beta",
                                       session_key="s-1", body="冒用授权", grant_id="g-ok")
        self.service.grant_speaking(request_id=self.rid(), actor_id="sec",
                                    dialogue_id="d1", grant_id="g-expired",
                                    delegation_id="del-beta", delegate_actor_id="del-beta",
                                    valid_from="2026-01-01T00:00:00Z",
                                    valid_until="2026-06-01T00:00:00Z")
        with self.assertRaises(PermissionDenied):
            self.service.add_statement(request_id=self.rid(), actor_id="del-beta",
                                       dialogue_id="d1", delegation_id="del-beta",
                                       session_key="s-1", body="过期授权",
                                       grant_id="g-expired")
        self.service.grant_speaking(request_id=self.rid(), actor_id="sec",
                                    dialogue_id="d1", grant_id="g-scoped",
                                    delegation_id="del-beta", delegate_actor_id="del-beta",
                                    valid_from="2026-01-01T00:00:00Z",
                                    valid_until="2027-01-01T00:00:00Z",
                                    scope=f"clause:{clause_id}")
        with self.assertRaises(PermissionDenied):
            self.service.add_statement(request_id=self.rid(), actor_id="del-beta",
                                       dialogue_id="d1", delegation_id="del-beta",
                                       session_key="s-1", body="范围外发言",
                                       grant_id="g-scoped")
        self.service.revoke_speaking(request_id=self.rid(), actor_id="sec",
                                     grant_id="g-ok")
        with self.assertRaises(PermissionDenied):
            self.service.add_statement(request_id=self.rid(), actor_id="del-alpha",
                                       dialogue_id="d1", delegation_id="del-alpha",
                                       session_key="s-1", body="撤销后发言", grant_id="g-ok")

    def test_non_signatory_cannot_vote_or_amend(self):
        clause_id, version_id = self.add_clause()
        with self.assertRaises(PermissionDenied):
            self.accept(version_id, "gamma")
        with self.assertRaises(PermissionDenied):
            self.service.propose_amendment(request_id=self.rid(), actor_id="del-gamma",
                                           clause_id=clause_id, amendment_id="am-g",
                                           delegation_id="del-gamma",
                                           proposed_text="观察团修订")

    def test_idempotent_replay_and_payload_conflict(self):
        first = self.service.create_dialogue(request_id="dlg-2", actor_id="sec",
                                             dialogue_id="d2", title="第二场对话")
        replay = self.service.create_dialogue(request_id="dlg-2", actor_id="sec",
                                              dialogue_id="d2", title="第二场对话")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.create_dialogue(request_id="dlg-2", actor_id="sec",
                                         dialogue_id="d3", title="另一场对话")

    def test_translation_review_rules(self):
        _, version_id = self.add_clause()
        first = self.service.submit_translation(request_id=self.rid(), actor_id="tr1",
                                                version_id=version_id, language="en",
                                                text="First")
        with self.assertRaises(PermissionDenied):
            self.service.verify_translation(request_id=self.rid(), actor_id="tr1",
                                            translation_id=first.resource_id)
        second = self.service.submit_translation(request_id=self.rid(), actor_id="tr2",
                                                 version_id=version_id, language="en",
                                                 text="Second")
        self.service.verify_translation(request_id=self.rid(), actor_id="tr2",
                                        translation_id=first.resource_id)
        with self.assertRaises(ConflictError):
            self.service.verify_translation(request_id=self.rid(), actor_id="sec",
                                            translation_id=second.resource_id)
        with self.assertRaises(ValidationError):
            self.service.submit_translation(request_id=self.rid(), actor_id="tr1",
                                            version_id=version_id, language="zh",
                                            text="同语言")


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
