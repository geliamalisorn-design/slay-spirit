import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import server


class GameApiIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.llm_env_patch = patch.dict(os.environ, {"SPIRIT_LLM_API_KEY": ""})
        cls.llm_env_patch.start()
        cls.db_path_patch = patch.object(server, "DB_PATH", Path(cls.temp_dir.name) / "integration.sqlite3")
        cls.db_path_patch.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.db_path_patch.stop()
        cls.llm_env_patch.stop()
        cls.temp_dir.cleanup()

    def request_json(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, {"content_type": response.getheader("Content-Type"), "set_cookie": response.getheader("Set-Cookie")}, raw)
        connection.close()
        return result

    def test_static_frontend_is_served(self):
        status, content_type, body = self.request_json("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type["content_type"])
        self.assertIn(b"/app.js", body)

    def test_landing_preview_contains_only_the_approved_sixty_day_window(self):
        status, _, body = self.request_json("GET", "/api/preview")
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertEqual(len(preview["chart"]), 60)
        self.assertTrue(preview["source"].startswith("https://iss.moex.com/iss/history/"))
        self.assertIn("MOEX ISS", preview["source_label"])
        self.assertGreaterEqual(len(preview["previews"]), 5)

    def test_complete_five_round_game_through_http_api(self):
        status, _, _ = self.request_json("POST", "/api/session", {"level": "easy"})
        self.assertEqual(status, 401)
        status, headers, body = self.request_json("POST", "/api/auth/register", {"display_name": "Integration QA", "email": f"qa-{uuid.uuid4().hex}@example.test", "password": "SafeTest123"})
        self.assertEqual(status, 201)
        cookie = headers["set_cookie"].split(";", 1)[0]
        auth = {"Cookie": cookie}
        status, _, body = self.request_json("GET", "/api/scenario-filters")
        self.assertEqual(status, 200)
        filters = json.loads(body)
        self.assertEqual([item["key"] for item in filters["periods"]], ["all", "2020s"])
        self.assertEqual([item["key"] for item in filters["geographies"]], ["moex"])
        status, _, body = self.request_json("POST", "/api/profile/preferences", {"period": "2010s", "geography": "moex"}, auth)
        self.assertEqual(status, 400)
        status, _, body = self.request_json("POST", "/api/profile/preferences", {"period": "2020s", "geography": "moex"}, auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["user"]["preferences"], {"period": "2020s", "geography": "moex"})
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 4, "completed": True}, auth)
        self.assertEqual(status, 400)
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 1, "completed": False}, auth)
        self.assertEqual(status, 200)
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 2, "completed": False}, auth)
        self.assertEqual(status, 200)
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 3, "completed": False}, auth)
        self.assertEqual(status, 200)
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 4, "completed": True}, auth)
        self.assertEqual(status, 400)
        status, _, body = self.request_json("GET", "/api/tutorial/practice", headers=auth)
        self.assertEqual(status, 200)
        practice = json.loads(body)["round"]
        self.assertEqual(len(practice["chart"]), 60)
        self.assertNotIn("reveal_prices", practice)
        self.assertNotIn("correct_option", practice["quiz"][0])
        practice_year, practice_month = practice["periods"][0]
        sample_answer = {
            "company": "SBER", "year": practice_year, "month": practice_month, "forecast": "down",
            "rationale": "Практический ответ для проверки исторического сценария.",
            "strategy": "hold", "factors": [0],
            "quiz_answers": [{"id": question["id"], "option": 0} for question in practice["quiz"]],
        }
        status, _, body = self.request_json("POST", "/api/tutorial/practice", sample_answer, auth)
        self.assertEqual(status, 200)
        status, _, body = self.request_json("GET", "/api/profile", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats"]["rounds"], 0)
        status, _, body = self.request_json("POST", "/api/tutorial/progress", {"step": 4, "completed": True}, auth)
        self.assertEqual(status, 200)
        status, _, _ = self.request_json("POST", "/api/session", {"level": "medium"}, auth)
        self.assertEqual(status, 403)
        status, _, body = self.request_json("POST", "/api/session", {"level": "easy"}, auth)
        self.assertEqual(status, 201)
        session = json.loads(body)
        session_id = session["session_id"]
        self.assertEqual(session["round_index"], 0)

        for index in range(server.ROUND_COUNT):
            current = session["current"]
            case = server.load_cases()[index]
            if index == 0:
                hint_payload = {"session_id": session_id, "round_index": index, "hint_index": 0}
                status, _, body = self.request_json("POST", "/api/hint", hint_payload, auth)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["used"], 1)
                status, _, body = self.request_json("POST", "/api/hint", hint_payload, auth)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["used"], 1, "A repeated hint request must be idempotent")
                status, _, body = self.request_json("GET", "/api/session", headers={**auth, "X-Session-Id": session_id})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["hint_state"]["used"], 1)
            if index == 1:
                for hint_index in range(6):
                    status, _, body = self.request_json("POST", "/api/hint", {"session_id": session_id, "round_index": index, "hint_index": hint_index}, auth)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["remaining"], 5 - hint_index)
            answer = {
                "session_id": session_id,
                "round_index": index,
                "company": case["ticker"],
                "year": int(case["end_date"][:4]),
                "month": int(case["end_date"][5:7]),
                "forecast": "flat",
                "strategy": "hold",
                "rationale": "Сравнил данные и рыночный контекст.",
                "factors": [0],
                "quiz_answers": [{"id": question["id"], "option": 0} for question in current["quiz"]],
            }
            if index == 0:
                provider_error = HTTPError("https://llm-proxy.spirit.t-education.com/v1/responses", 503, "unavailable", {}, None)
                with patch.dict(os.environ, {"SPIRIT_LLM_API_KEY": "test-spirit-key"}), patch.object(server.llm_provider, "urlopen", side_effect=provider_error):
                    status, _, body = self.request_json("POST", "/api/answer", answer, auth)
            else:
                status, _, body = self.request_json("POST", "/api/answer", answer, auth)
            self.assertEqual(status, 200)
            response = json.loads(body)
            self.assertEqual(response["result"]["scenario_id"], case["scenario_id"])
            self.assertEqual(response["session"]["round_index"], index + 1)
            self.assertEqual(len(response["result"]["reveal_prices"]), 90)
            if index == 0:
                self.assertEqual(response["result"]["argument_feedback"]["provider"], "rules")
                self.assertEqual(response["result"]["score"]["maximum"], 9)
                self.assertEqual(response["result"]["hints_used"], 1)
            if index == 1:
                self.assertEqual(response["result"]["score"]["maximum"], 4)
                self.assertLessEqual(response["result"]["score"]["total"], 4)

            if index == 0:
                status, _, duplicate_body = self.request_json("POST", "/api/answer", answer, auth)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(duplicate_body)["result"]["scenario_id"], case["scenario_id"])
            session = response["session"]

        self.assertEqual(session["state"], "completed")
        self.assertEqual(len(session["history"]), server.ROUND_COUNT)
        self.assertIsNone(session["current"])
        status, _, body = self.request_json("GET", "/api/levels", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["levels"]["medium"])
        status, _, body = self.request_json("GET", "/api/profile", headers=auth)
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertEqual(profile["stats"]["completed_sessions"], 1)
        self.assertEqual(profile["stats"]["hints_used"], 7)
        self.assertEqual(profile["stats"]["strategies"]["hold"], server.ROUND_COUNT)
        self.assertEqual(profile["user"]["preferences"]["period"], "2020s")
        self.assertEqual(len(profile["history"][0]["rounds"]), server.ROUND_COUNT)
        for next_level in ("medium", "hard"):
            status, _, body = self.request_json("POST", "/api/session", {"level": next_level}, auth)
            self.assertEqual(status, 201)
            next_session = json.loads(body)
            for round_index in range(server.ROUND_COUNT):
                current = next_session["current"]
                case = server.load_cases()[round_index]
                answer = {
                    "session_id": next_session["session_id"], "round_index": round_index,
                    "company": case["ticker"], "year": int(case["end_date"][:4]), "month": int(case["end_date"][5:7]),
                    "forecast": "flat", "strategy": "hold", "rationale": "Сравнил несколько факторов и сохранил неопределённость.",
                    "factors": [0], "quiz_answers": [{"id": question["id"], "option": 0} for question in current["quiz"]],
                }
                status, _, body = self.request_json("POST", "/api/answer", answer, auth)
                self.assertEqual(status, 200)
                next_session = json.loads(body)["session"]
            status, _, body = self.request_json("GET", "/api/levels", headers=auth)
            levels = json.loads(body)["levels"]
            if next_level == "medium":
                self.assertTrue(levels["hard"])
        self.assertTrue(levels["easy"] and levels["medium"] and levels["hard"])
        status, _, body = self.request_json("POST", "/api/tutorial/restart", {}, auth)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["user"]["tutorial_completed"])
        status, _, body = self.request_json("GET", "/api/tutorial", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["step"], 0)


if __name__ == "__main__":
    unittest.main()
