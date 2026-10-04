import unittest
from datetime import datetime, timezone

from digital_trade_foundation.api import route
from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.negotiation import NegotiationService
from digital_trade_foundation.storage import Database

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class NegotiationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = NegotiationService(self.database, FixedClock(T0))
        self._seq = 0

    def tearDown(self):
        self.database.close()

    def rid(self):
        self._seq += 1
        return f"api-{self._seq}"

    def post(self, path, body, actor):
        return route(self.service, "POST", path, {"request_id": self.rid(), **body},
                     {"X-Actor-Id": actor})

    def get(self, path, actor=None):
        headers = {"X-Actor-Id": actor} if actor else {}
        return route(self.service, "GET", path, None, headers)

    def bootstrap_dialogue(self):
        self.post("/organizations", {"organization_id": "o-sec", "name": "秘书处机构"},
                  "bootstrap")
        self.post("/actors", {"new_actor_id": "admin", "display_name": "管理员",
                              "role": "admin", "organization_id": "o-sec"}, "bootstrap")
        self.post("/actors", {"new_actor_id": "sec", "display_name": "秘书处官员",
                              "role": "secretariat", "organization_id": "o-sec"}, "admin")
        self.post("/actors", {"new_actor_id": "obs", "display_name": "观察员",
                              "role": "observer", "organization_id": "o-sec"}, "admin")
        for code in ("alpha", "beta"):
            self.post("/organizations", {"organization_id": f"o-{code}",
                                         "name": f"成员{code}"}, "admin")
            self.post("/actors", {"new_actor_id": f"del-{code}",
                                  "display_name": f"代表{code}", "role": "delegate",
                                  "organization_id": f"o-{code}"}, "admin")
        status, _ = self.post("/dialogues", {"dialogue_id": "d1", "title": "数字贸易对话",
                                             "quorum": 2, "support_threshold": 0.5}, "sec")
        self.assertEqual(201, status)
        for code, preconditions in (("alpha", []), ("beta", ["domestic_ratification"])):
            status, _ = self.post("/dialogues/d1/delegations",
                                  {"delegation_id": f"del-{code}",
                                   "organization_id": f"o-{code}",
                                   "name": f"{code}代表团", "can_sign": True,
                                   "preconditions": preconditions}, "sec")
            self.assertEqual(201, status)
        self.post("/dialogues/d1/proposals", {"proposal_id": "p1", "title": "基础提案",
                                              "proposer_delegation_id": "del-alpha"}, "sec")
        status, _ = self.post("/proposals/p1/clauses", {"clause_id": "c1",
                                                        "clause_key": "A.1",
                                                        "language": "zh",
                                                        "text": "第一条文本"}, "sec")
        self.assertEqual(201, status)
        status, clause = self.get("/clauses/c1")
        self.assertEqual(200, status)
        return clause["head_version_id"]

    def test_full_flow_over_http(self):
        version_id = self.bootstrap_dialogue()
        status, _ = self.post(f"/versions/{version_id}/positions",
                              {"delegation_id": "del-alpha", "stance": "accept",
                               "session_key": "s-1"}, "del-alpha")
        self.assertEqual(201, status)
        status, _ = self.post(f"/versions/{version_id}/positions",
                              {"delegation_id": "del-beta", "stance": "conditional_accept",
                               "note": "须国内批准", "session_key": "s-1"}, "del-beta")
        self.assertEqual(201, status)
        status, tally = self.get(f"/versions/{version_id}/tally")
        self.assertEqual(200, status)
        self.assertEqual(2, tally["support"])
        self.assertTrue(tally["qualifies"])
        status, consensus = self.post(f"/versions/{version_id}/consensus", {}, "sec")
        self.assertEqual(201, status)
        status, public = self.get("/dialogues/d1/public")
        self.assertEqual(200, status)
        clause = public["clauses"][0]
        self.assertEqual("accepted", clause["stage"])
        participants = {p["delegation_id"]: p for p in clause["participants"]}
        self.assertEqual("in_effect", participants["del-alpha"]["status"])
        self.assertEqual("accepted", participants["del-beta"]["status"])
        self.assertEqual("须国内批准", participants["del-beta"]["condition"])
        status, binding = self.get("/clauses/c1/binding?delegation_id=del-beta"
                                   "&at=2026-10-01T10:00:00Z")
        self.assertEqual(200, status)
        self.assertFalse(binding["binding"])
        self.assertIn("acceptance_condition", binding["reasons"][-1])
        status, commitments = self.get("/dialogues/d1/commitments", "sec")
        self.assertEqual(200, status)
        beta = [item for item in commitments["items"]
                if item["delegation_id"] == "del-beta"][0]
        for seq in (0, 1):
            status, _ = self.post(
                f"/commitments/{beta['commitment_id']}/conditions/{seq}/fulfill", {}, "sec")
            self.assertEqual(201, status)
        status, binding = self.get("/clauses/c1/binding?delegation_id=del-beta"
                                   "&at=2026-10-02T00:00:00Z")
        self.assertTrue(binding["binding"])

    def test_views_and_seal_over_http(self):
        version_id = self.bootstrap_dialogue()
        self.post("/dialogues/d1/grants", {"grant_id": "g1", "delegation_id": "del-alpha",
                                           "delegate_actor_id": "del-alpha",
                                           "valid_from": "2026-01-01T00:00:00Z",
                                           "valid_until": "2027-01-01T00:00:00Z"}, "sec")
        status, statement = self.post("/dialogues/d1/statements",
                                      {"delegation_id": "del-alpha", "session_key": "s-1",
                                       "body": "发言记录", "grant_id": "g1",
                                       "clause_id": "c1"}, "del-alpha")
        self.assertEqual(201, status)
        self.post(f"/versions/{version_id}/positions",
                  {"delegation_id": "del-alpha", "stance": "accept",
                   "session_key": "s-1"}, "del-alpha")
        status, seal = self.post("/dialogues/d1/seals", {"session_key": "s-1"}, "sec")
        self.assertEqual(201, status)
        status, check = self.get(f"/seals/{seal['resource_id']}")
        self.assertEqual(200, status)
        self.assertTrue(check["valid"])
        status, _ = self.post(f"/statements/{statement['resource_id']}/revisions",
                              {"body": "整理后文本"}, "sec")
        self.assertEqual(409, status)
        status, view = self.get("/dialogues/d1/view", "obs")
        self.assertEqual(200, status)
        self.assertEqual("observer", view["role"])
        status, view = self.get("/dialogues/d1/view", "del-alpha")
        self.assertEqual("delegate", view["role"])
        status, _ = self.get("/dialogues/d1/view")
        self.assertEqual(400, status)

    def test_amendment_flow_over_http(self):
        version_id = self.bootstrap_dialogue()
        status, _ = self.post(f"/clauses/c1/amendments",
                              {"amendment_id": "am-1", "delegation_id": "del-alpha",
                               "proposed_text": "修订后的第一条"}, "del-alpha")
        self.assertEqual(201, status)
        status, _ = self.post("/amendments/am-1/seconds",
                              {"delegation_id": "del-beta"}, "del-beta")
        self.assertEqual(201, status)
        status, merged = self.post("/amendments/am-1/merge", {}, "sec")
        self.assertEqual(201, status)
        status, clause = self.get("/clauses/c1")
        self.assertEqual(merged["resource_id"], clause["head_version_id"])
        self.assertEqual("修订后的第一条", clause["head"]["text"])
        status, tally = self.get(f"/versions/{version_id}/tally")
        self.assertEqual(0, tally["positions"])

    def test_unknown_negotiation_route_returns_404(self):
        status, payload = self.get("/dialogues/d1/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_domain_error_maps_to_status(self):
        self.post("/organizations", {"organization_id": "o-sec", "name": "秘书处机构"},
                  "bootstrap")
        self.post("/actors", {"new_actor_id": "obs", "display_name": "观察员",
                              "role": "observer", "organization_id": "o-sec"}, "bootstrap")
        status, payload = self.post("/dialogues", {"dialogue_id": "d1", "title": "对话"},
                                    "obs")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])


if __name__ == "__main__":
    unittest.main()
