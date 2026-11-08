"""Validate the checked-in game snapshot used by backend tests and CI."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
rounds = json.loads((ROOT / "data" / "rounds.json").read_text(encoding="utf-8"))
education = json.loads((ROOT / "data" / "education.json").read_text(encoding="utf-8"))

assert len(rounds) >= 5, "At least five rounds are required"
for item in rounds[:5]:
    assert item.get("status") == "approved", f"Round {item.get('ticker')} is not approved"
    assert len(item.get("prices", [])) == 90, f"Round {item.get('ticker')} must have 90 prices"
    key = f"{item['ticker'].lower()}-{item['end_date']}"
    quiz = education[key]["quiz"]
    assert 2 <= len(quiz) <= 4, f"Round {key} must have 2 to 4 quiz questions"

print("Dataset valid: five approved rounds, 90 prices each, 2 to 4 quiz questions each.")
