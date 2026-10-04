import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.api import route
from digital_trade_foundation.negotiation import NegotiationService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

BASE = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class NegotiationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = None
        self.base = DomainService(self.database)
        self.neg = NegotiationService(self.database)

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor=""):
        return route(self.base, method, path, body, {"X-Actor-Id": actor})

    def _bootstrap(self):
        self._call("POST", "/organizations", {
            "request_id": "org-sec", "organization_id": "sec", "name": "秘书处"}, "bootstrap")
        for org in ("o1", "o2"):
            self._call("POST", "/organizations", {
                "request_id": "org-" + org, "organization_id": org, "name": org}, "bootstrap")
        self._call("POST", "/actors", {
            "request_id": "actor-admin", "new_actor_id": "admin", "display_name": "管理员",
            "role": "admin", "organization_id": "sec"}, "bootstrap")
        self._call("POST", "/actors", {
            "request_id": "actor-sec", "new_actor_id": "sec", "display_name": "秘书处",
            "role": "secretary", "organization_id": "sec"}, "admin")
        for actor, org in (("d1", "o1"), ("d2", "o2")):
            self._call("POST", "/actors", {
                "request_id": "actor-" + actor, "new_actor_id": actor, "display_name": actor,
                "role": "delegate", "organization_id": org}, "admin")
        for delegation, org in (("g1", "o1"), ("g2", "o2")):
            status, payload = self._call("POST", "/delegations", {
                "request_id": "del-" + delegation, "delegation_id": delegation,
                "organization_id": org, "name": delegation}, "sec")
            self.assertEqual(201, status, payload)
            self._call("POST", "/mandates", {
                "request_id": "mand-" + delegation, "mandate_actor_id": "d" + delegation[1],
                "delegation_id": delegation}, "sec")

    def test_full_negotiation_flow_over_http(self):
        self._bootstrap()
        status, payload = self._call("POST", "/proposals", {
            "request_id": "prop1", "proposal_id": "p1", "title": "数字贸易提案",
            "required_supports": 2, "amendment_supports": 1}, "sec")
        self.assertEqual(201, status, payload)
        status, payload = self._call("POST", "/clauses", {
            "request_id": "clause1", "proposal_id": "p1", "clause_id": "c1",
            "code": "ART-1", "title": "数据条款", "language": "zh",
            "content": "保障跨境数据流动"}, "d1")
        self.assertEqual(201, status, payload)

        # 公众接口初始只显示草案
        status, payload = self._call("GET", "/public/clauses")
        self.assertEqual(200, status)
        c1 = next(i for i in payload["items"] if i["clause_id"] == "c1")
        self.assertEqual("draft", c1["state"])
        self.assertEqual("草案", c1["state_label"])

        # 表态后签署形成共识并立即对无条件方生效
        for actor in ("d1", "d2"):
            status, payload = self._call("POST", "/clauses/stance", {
                "request_id": "stance-" + actor, "clause_id": "c1",
                "position": "accept"}, actor)
            self.assertEqual(201, status, payload)
        status, sign1 = self._call("POST", "/clauses/sign", {
            "request_id": "sign1", "clause_id": "c1"}, "d1")
        self.assertEqual(201, status, sign1)
        status, sign2 = self._call("POST", "/clauses/sign", {
            "request_id": "sign2", "clause_id": "c1"}, "d2")
        self.assertEqual(201, status, sign2)
        self.assertIsNotNone(sign2["consensus_id"])

        # 公众接口明确区分已生效与参与方
        status, payload = self._call("GET", "/public/clauses")
        c1 = next(i for i in payload["items"] if i["clause_id"] == "c1")
        self.assertEqual("in_force", c1["state"])
        self.assertEqual({"g1", "g2"}, {e["delegation_id"] for e in c1["effective_for"]})

        # 时间点审计解释
        status, payload = self._call(
            "GET", "/audit/binding?clause_id=c1&delegation_id=g1&at=" + iso(BASE + timedelta(days=1)))
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["binding"])
        self.assertTrue(payload["reasons"])

        # 秘书处视图可访问；代表不能访问
        status, payload = self._call("GET", "/views/secretary", actor="sec")
        self.assertEqual(200, status)
        status, payload = self._call("GET", "/views/secretary", actor="d1")
        self.assertEqual(403, status)

    def test_unknown_negotiation_route_404(self):
        status, payload = self._call("GET", "/views/nobody")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
