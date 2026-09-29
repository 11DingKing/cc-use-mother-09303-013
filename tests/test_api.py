"""HTTP API 端到端冒烟测试。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking.api import ApiHandler
from matchmaking.repository import Repository
from matchmaking.service import MatchmakingService


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        data_path = str(Path(self.tmp.name) / "forum.json")
        ApiHandler.service = MatchmakingService(Repository(data_path))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

        _, forum = self.request("POST", "/organizations",
                                {"kind": "forum", "name": "论坛秘书处"})
        _, ent = self.request("POST", "/organizations",
                              {"kind": "enterprise", "name": "华数公司"})
        _, c1 = self.request("POST", "/organizations",
                             {"kind": "college", "name": "第一学院"})
        _, c2 = self.request("POST", "/organizations",
                             {"kind": "college", "name": "第二学院"})

        _, ec = self.request("POST", "/contacts",
                             {"org_id": ent["id"], "name": "王经理",
                              "email": "wang@e.example"})
        _, k1 = self.request("POST", "/contacts",
                             {"org_id": c1["id"], "name": "赵老师", "email": "z@1.example"})
        _, k2 = self.request("POST", "/contacts",
                             {"org_id": c2["id"], "name": "钱老师", "email": "q@2.example"})

        for c in (c1, c2):
            self.request("POST", "/capabilities",
                         {"org_id": c["id"], "name": "机器人实训",
                          "category": "智能制造", "tags": ["工业机器人"]})
        self.request("POST", "/resources",
                     {"owner_org_id": forum["id"], "name": "共享实训中心",
                      "total_qty": 40, "unit": "工位"})

        _, intent = self.request("POST", "/intents",
                                 {"enterprise_org_id": ent["id"], "summary": "寻求合作"})
        _, demand = self.request("POST", "/demands", {
            "intent_id": intent["id"], "title": "机器人实训",
            "capability_categories": ["智能制造"], "tags": ["工业机器人"],
            "required_qty": 30})

        _, candidates = self.request("GET", f"/candidates?demand_id={demand['id']}")
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertTrue(cand["reasons"])

        college_contacts = {c1["id"]: k1, c2["id"]: k2}
        winner = cand["college_org_id"]
        loser_contact = k2 if winner == c1["id"] else k1

        # 越权联系人确认应被拒绝
        status, err = self.request("POST", f"/candidates/{cand['id']}/confirm",
                                   {"party": "college", "contact_id": loser_contact["id"]})
        self.assertEqual(status, 409)
        self.assertEqual(err["code"], "CONFLICT")

        self.request("POST", f"/candidates/{cand['id']}/confirm",
                     {"party": "enterprise", "contact_id": ec["id"]})
        self.request("POST", f"/candidates/{cand['id']}/confirm",
                     {"party": "college", "contact_id": college_contacts[winner]["id"]})
        _, project = self.request("POST", f"/candidates/{cand['id']}/convert", {})
        self.assertEqual(project["status"], "negotiating")
        self.assertTrue(project["frozen_basis"]["resource_allocations"])

        _, signed = self.request("POST", f"/projects/{project['id']}/sign", {})
        self.assertEqual(signed["status"], "active")

        _, timeline = self.request("GET", f"/demands/{demand['id']}/timeline")
        types_ = {e["event_type"] for e in timeline}
        self.assertIn("project_signed", types_)

        _, trace = self.request("GET", f"/intents/{intent['id']}/trace")
        self.assertEqual(trace["intent"]["id"], intent["id"])

    def test_validation_errors_are_json(self) -> None:
        status, body = self.request("POST", "/organizations", {"kind": "weird", "name": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
