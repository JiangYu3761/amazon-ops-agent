"""Integration checks for scoped suggestions, missing data and operational queue order."""
import json
import os
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from urllib.parse import urlsplit


@unittest.skipUnless(os.getenv("WORKFLOW_INTEGRATION") == "1", "requires isolated PostgreSQL")
class DecisionSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ["DATABASE_URL"]).path.startswith("/homa_workflow_test_"):
            raise RuntimeError("isolated test database required")
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import password_hash
        cls.db = staticmethod(connection)
        cls.anon = TestClient(app)
        email = uuid4().hex + "@test.example"
        password = uuid4().hex
        with connection() as conn:
            user = conn.execute("INSERT INTO app_users(email,display_name) VALUES(%s,'复核试验') RETURNING id", (email,)).fetchone()["id"]
            conn.execute("INSERT INTO workflow_accounts(user_id,password_hash) VALUES(%s,%s)",(user,password_hash(password)))
            cls.product = str(conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'集成验证','US') RETURNING id", (uuid4().hex,)).fetchone()["id"])
        cls.client = TestClient(app, headers={"X-Workflow-Request":"1"})
        cls.client.post("/api/workflow-auth/login",json={"email":email,"password":password}).raise_for_status()

    def test_assist_zero_and_model_contract_no_implicit_send(self):
        body = {"product_id":self.product,"page_id":"ads","section_id":"investigation","site":"US","object_ref":"SP-A",
                "values":{"spend":100,"attributed_sales":0}}
        with patch("app.workbench_insights.ask_external_llm",new_callable=AsyncMock) as model:
            response = self.client.post("/api/workbench/assist",json=body)
            self.assertEqual(response.status_code,200,response.text)
            result = response.json()
            self.assertEqual(result["status"],"incomplete")
            self.assertTrue(result["requires_human"])
            self.assertTrue(any(f["level"]=="missing" and "分母" in f["detail"] for f in result["findings"]))
            model.assert_not_awaited()
            model.return_value=("仅为测试回答","test")
            answer=self.client.post("/api/workbench/assist",json={**body,"use_model":True}).json()
            self.assertEqual((answer["model_status"],answer["model_draft"]),("generated","仅为测试回答"))
            self.assertEqual(model.await_args.args[-1],"ads")
        # Exercise the real HTTP adapter with a local compatible provider, without external account data.
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append((self.path, self.headers["Authorization"], sent))
                payload = json.dumps({"choices": [{"message": {"content": "本地协议验收草稿"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conf = {"LLM_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
                        "LLM_API_KEY": "local-fixture", "LLM_MODEL": "protocol-fixture"}
                with patch.dict(os.environ, conf):
                    answer = self.client.post("/api/workbench/assist",json={**body,"use_model":True}).json()
                self.assertEqual(answer["model_draft"], "本地协议验收草稿")
                self.assertEqual(answer["model_status"], "generated")
                path, auth, sent = captured[0]
                self.assertEqual((path, auth, sent["model"]), ("/v1/chat/completions", "Bearer local-fixture", "protocol-fixture"))
                self.assertIn("SP-A", sent["messages"][1]["content"])
            finally:
                server.shutdown()
                thread.join(timeout=3)
        with self.db() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM workbench_items WHERE product_id=%s",(self.product,)).fetchone())

    def test_error_on_unknown_keys_and_scoped_metrics(self):
        body={"product_id":self.product,"page_id":"inventory","section_id":"replenish","site":"US","object_ref":"X","values":{}}
        self.assertEqual(self.anon.post("/api/workbench/assist",json=body).status_code,403)
        other={**body,"values":{"invented_input":1}}
        self.assertEqual(self.client.post("/api/workbench/assist",json=other).status_code,422)
        response=self.client.post("/api/workbench/assist",json={**body,"site":"DE"})
        self.assertIn(response.status_code,(200,422))
        if response.status_code==200:self.assertFalse(response.json()["metrics"])
        self.assertEqual(self.client.post("/api/workbench/assist",json={**body,"end_date":"2999-01-01"}).status_code,422)

    def test_inbox_missing_source_routes_to_source_coverage_and_deduplicates(self):
        from app.workbench_insights import operating_signals
        product={"id":self.product,"marketplace":"US","model":"M1"}
        data={"period":{"start_date":"2026-11-01","end_date":"2026-11-14","timezone":"America/Los_Angeles"},
              "policy_version":3,"data_quality":{"issues":[]},"modules":{"ads":{"findings":[]},"inventory":{"metrics":[
              {"key":"recommended_replenishment","status":"estimated","inputs":{"per_sku":[
              {"sku":"QA","fulfillable":90,"reliable_inbound":0,"recommended_units":168}]}}]}}}
        prepared=operating_signals(data,product)[0]
        self.assertEqual(prepared["values"],{"suggested_qty":168,"requested_qty":168,"adjustment":"按建议量"})
        self.assertIn("参数版本 3",prepared["source_note"])
        q={"product_id":self.product}
        response=self.client.get("/api/workbench/inbox",params=q)
        self.assertEqual(response.status_code,200,response.text)
        signal=next(s for s in response.json()["signals"] if s["page_id"]=="imports")
        self.assertEqual(signal["section_id"],"source_coverage")
        item=self.client.post("/api/workbench/items",json={**{k:signal[k] for k in ("page_id","section_id","title","site","object_ref","signal_key")},
                              "product_id":self.product,"values":{},"request_id":str(uuid4())})
        self.assertEqual(item.status_code,201,item.text)
        result=self.client.get("/api/workbench/inbox",params=q).json()
        self.assertNotIn(signal["signal_key"],[s["signal_key"] for s in result["signals"]])
        self.assertIn(item.json()["id"],[r["id"] for r in result["items"]])

    def test_priorities_follow_product_timezone(self):
        from app.workbench_insights import ranked_item
        # UTC date changes before Los Angeles. This local deadline has not passed.
        row={"business_timezone":"America/Los_Angeles","status":"draft","values":{"due":"2026-11-21"},
             "definition":{"checks":[{"op":"deadline","keys":["due"]}]}}
        now=datetime(2026,11,22,3,tzinfo=timezone.utc)
        self.assertEqual(ranked_item(row,now)["priority"],1)
        row["values"]["due"]="2026-11-20"
        self.assertEqual(ranked_item(row,now)["priority"],0)
