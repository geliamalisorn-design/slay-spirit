import json
import tempfile
import unittest
from pathlib import Path
import server


class ServerRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = server.load_cases()
        cls.education = server.load_education()

    def test_approved_snapshot_has_five_full_rounds(self):
        self.assertGreaterEqual(len(self.cases), 5)
        self.assertTrue(all(len(item["prices"]) == 90 for item in self.cases[:5]))

    def test_classification_uses_backend_threshold(self):
        self.assertEqual(server.classify_change(5), "up")
        self.assertEqual(server.classify_change(-5), "down")
        self.assertEqual(server.classify_change(4.99), "flat")

    def test_public_round_does_not_contain_future_prices_or_answers(self):
        payload = server.public_round(self.cases[0], 0, self.education)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(len(payload["chart"]), 60)
        self.assertNotIn("reveal_prices", payload)
        self.assertNotIn("scenario_id", payload)
        self.assertNotIn("correct_option", encoded)
        # The candidate list is visible by design; only the correct answer and future series are withheld.

    def test_round_article_matches_selected_level(self):
        for case in self.cases:
            for level in server.LEVELS:
                payload = server.public_round(case, 0, self.education, self.cases, level)
                self.assertEqual(payload["article"], self.education[case["scenario_id"]]["articles"][level])
                self.assertGreaterEqual(sum(len(paragraph.split()) for paragraph in payload["article"]), 65, f"short article for {case['scenario_id']} / {level}")
        self.assertNotIn("пандемия стала", " ".join(server.public_round(self.cases[0], 0, self.education, self.cases, "easy")["article"]).lower())

    def test_hint_catalog_is_six_items_and_social_mood_is_a_reconstruction(self):
        content = server.case_education(self.cases[0], self.education)
        self.assertEqual(len(content["hints"]), 6)
        mood = content["hints"][-1]
        self.assertEqual(mood["kind"], "Рыночные настроения")
        self.assertIn("не цитата", mood["label"])

    def test_hint_use_caps_total_score_without_changing_market_calculation(self):
        case = self.cases[0]
        answer = {"company": case["ticker"], "year": int(case["end_date"][:4]), "month": int(case["end_date"][5:7]), "forecast": "flat", "strategy": "hold", "rationale": "Сравнил контекст, риск и график.", "factors": [0], "quiz_answers": [{"id": "q1", "option": 0}, {"id": "q2", "option": 0}], "hints_used": 6}
        result = server.calculate(case, self.education, answer)
        self.assertEqual(result["score"]["maximum"], 4)
        self.assertLessEqual(result["score"]["total"], 4)
        self.assertEqual(result["change_pct"], round((case["prices"][89]["close"] / case["prices"][59]["close"] - 1) * 100, 2))

    def test_public_period_options_only_cover_verified_scenarios(self):
        payload = server.public_round(self.cases[0], 0, self.education, self.cases)
        self.assertEqual(payload["periods"], [(2020, 1), (2022, 2), (2022, 8), (2023, 10), (2024, 6)])
        answer = {"company": "SBER", "year": 2028, "month": 1, "forecast": "flat", "strategy": "hold", "rationale": "Проверю сопоставимые сигналы.", "factors": [0], "quiz_answers": [{"id": "q1", "option": 0}, {"id": "q2", "option": 0}]}
        with self.assertRaises(ValueError):
            server.validate_answer(answer, payload)

    def test_scenario_preferences_require_a_complete_verified_session(self):
        valid = server.validate_preferences({"period": "2020s", "geography": "moex"}, self.cases)
        self.assertEqual(valid, {"period": "2020s", "geography": "moex"})
        self.assertEqual(len(server.cases_for_preferences(self.cases, valid)), server.ROUND_COUNT)
        with self.assertRaisesRegex(ValueError, "пяти проверенных сценариев"):
            server.validate_preferences({"period": "2010s", "geography": "moex"}, self.cases)

    def test_profile_stats_include_hint_and_strategy_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_path = server.DB_PATH
            server.DB_PATH = Path(directory) / "profile.sqlite3"
            try:
                with server.connect() as db:
                    db.execute("INSERT INTO users(id,email,display_name,password_hash,created_at) VALUES (?,?,?,?,?)", ("u1", "u@example.test", "User", server.hash_password("SafeTest123"), "2026-01-01T00:00:00+00:00"))
                    user = db.execute("SELECT * FROM users WHERE id='u1'").fetchone()
                    profile = server.profile_payload(db, user)
                self.assertEqual(profile["stats"]["hints_used"], 0)
                self.assertEqual(profile["stats"]["strategies"], {"buy": 0, "hold": 0, "sell": 0})
                self.assertIn("scenario_filters", profile)
            finally:
                server.DB_PATH = previous_path

    def test_portfolio_alternatives_are_deterministic(self):
        case = self.cases[0]
        answer = {"company": case["ticker"], "year": int(case["end_date"][:4]), "month": int(case["end_date"][5:7]), "forecast": "flat", "strategy": "hold", "rationale": "Сравнил контекст, риск и график.", "factors": [0], "quiz_answers": [{"id": "q1", "option": 0}, {"id": "q2", "option": 0}]}
        result = server.calculate(case, self.education, answer)
        self.assertEqual(set(result["alternatives"]), {"buy", "hold", "sell"})
        self.assertEqual(result["portfolio_end"], result["alternatives"]["hold"])

    def test_calculation_note_matches_strategy_exposure(self):
        case = self.cases[0]
        answer = {"company": case["ticker"], "year": int(case["end_date"][:4]), "month": int(case["end_date"][5:7]), "forecast": "flat", "strategy": "hold", "rationale": "Сравнил контекст, риск и график.", "factors": [0], "quiz_answers": [{"id": "q1", "option": 0}, {"id": "q2", "option": 0}]}
        note = server.calculate(case, self.education, answer)["calculation_note"]
        self.assertIn("«Купить» вкладывает всю сумму в акцию", note)
        self.assertIn("«Удержать» делит её поровну", note)
        self.assertIn("«Продать» оставляет всю сумму в деньгах", note)


if __name__ == "__main__":
    unittest.main()
