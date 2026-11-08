from __future__ import annotations

import json
import os
import sqlite3
import uuid
import hashlib
import hmac
import secrets
import re
from urllib.parse import parse_qs, urlparse
import llm_provider
from http.cookies import SimpleCookie
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "rounds.json"
EDUCATION_PATH = ROOT / "data" / "education.json"
DB_PATH = Path(os.environ.get("SLAY_DB_PATH", str(ROOT / "data" / "stockguessr.sqlite3")))
START_CASH = 100_000
ROUND_COUNT = 5
FORECAST_THRESHOLD_PCT = 5.0
MAX_BODY_BYTES = 64 * 1024
AUTH_COOKIE = "slay_auth"
AUTH_TTL_DAYS = 30
LEVELS = ("easy", "medium", "hard")
TUTORIAL_CASE_INDEX = 1

COMPANIES = [
    {"ticker": "SBER", "name": "Сбербанк", "sector": "Финансы"},
    {"ticker": "GAZP", "name": "Газпром", "sector": "Энергетика"},
    {"ticker": "LKOH", "name": "ЛУКОЙЛ", "sector": "Нефть и газ"},
    {"ticker": "NVTK", "name": "НОВАТЭК", "sector": "Энергетика"},
    {"ticker": "TATN", "name": "Татнефть", "sector": "Нефть и газ"},
    {"ticker": "ROSN", "name": "Роснефть", "sector": "Нефть и газ"},
    {"ticker": "GMKN", "name": "Норникель", "sector": "Металлы"},
    {"ticker": "MTSS", "name": "МТС", "sector": "Телеком"},
    {"ticker": "YNDX", "name": "Яндекс", "sector": "Технологии"},
    {"ticker": "CHMF", "name": "Северсталь", "sector": "Металлы"},
    {"ticker": "ALRS", "name": "АЛРОСА", "sector": "Металлы"},
    {"ticker": "MAGN", "name": "ММК", "sector": "Металлы"},
]
FORECASTS = {"up", "flat", "down"}
STRATEGIES = {"buy": 1.0, "hold": 0.5, "sell": 0.0}


class DataUnavailable(RuntimeError):
    """Raised when the approved local market snapshot cannot be used."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataUnavailable(f"Не найден проверенный набор данных: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataUnavailable(f"Повреждён JSON набора данных: {path}: {exc}") from exc


def validate_cases(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) < ROUND_COUNT:
        raise DataUnavailable("Нужно минимум пять проверенных исторических сценариев")
    required = {"ticker", "company_name", "intro", "title", "end_date", "prices", "source", "sources", "lesson"}
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not required.issubset(item):
            raise DataUnavailable("Сценарий не содержит обязательные поля или имеет неверную структуру")
        prices = item["prices"]
        if not isinstance(prices, list) or len(prices) != 90 or any(not isinstance(p, dict) or p.get("close") is None or p.get("date") is None for p in prices):
            raise DataUnavailable(f"Сценарий {item.get('ticker', '?')} должен содержать 90 торговых наблюдений")
        dates = [p["date"] for p in prices]
        if any(float(p["close"]) <= 0 for p in prices) or dates != sorted(dates) or len(set(dates)) != len(dates):
            raise DataUnavailable(f"Сценарий {item.get('ticker', '?')} содержит неверный порядок или значения котировок")
        try:
            for value in dates:
                datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise DataUnavailable(f"Сценарий {item.get('ticker', '?')} содержит неверную торговую дату") from exc
        if item["end_date"] != dates[59]:
            raise DataUnavailable(f"Дата решения сценария {item.get('ticker', '?')} не совпадает с 60-м наблюдением")
        if item["ticker"] in seen:
            raise DataUnavailable(f"Повторяющийся сценарий {item['ticker']}")
        seen.add(item["ticker"])
        item["scenario_id"] = item.get("scenario_id", f"{item['ticker'].lower()}-{item['end_date']}")
        item["status"] = item.get("status", "approved")
        item.setdefault("market", "MOEX")
        item.setdefault("region", "russia")
        item["source_label"] = item.get("source_label", "Котировки MOEX ISS · TQBR · цена закрытия")
        if ": https://" in str(item["source"]):
            item["source"] = str(item["source"]).split(": https://", 1)[1]
            item["source"] = "https://" + item["source"]
        if not str(item["source"]).startswith("https://iss.moex.com/iss/history/"):
            raise DataUnavailable(f"Сценарий {item['scenario_id']} не содержит прямую ссылку на котировки MOEX ISS")
        source = urlparse(item["source"])
        query = parse_qs(source.query)
        if item["ticker"] not in source.path or query.get("from", [None])[0] != dates[0] or query.get("till", [None])[0] != dates[-1]:
            raise DataUnavailable(f"Ссылка на источник {item['scenario_id']} не совпадает с тикером и окном котировок")
        if item["status"] != "approved":
            raise DataUnavailable(f"Сценарий {item['scenario_id']} не утверждён для игры")
        cases.append(item)
    return cases


def load_cases() -> list[dict[str, Any]]:
    return validate_cases(read_json(DATA_PATH))


def available_scenario_filters(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    periods: dict[str, int] = {}
    geographies: dict[str, int] = {}
    for case in cases:
        year = int(case["end_date"][:4])
        decade = f"{(year // 10) * 10}s"
        periods[decade] = periods.get(decade, 0) + 1
        geography = str(case.get("market", "MOEX")).lower()
        geographies[geography] = geographies.get(geography, 0) + 1
    period_options = ([{"key": "all", "label": "Все доступные годы", "count": len(cases)}] if len(cases) >= ROUND_COUNT else [])
    decade_names = {"1990s": "1990-е", "2000s": "2000-е", "2010s": "2010-е", "2020s": "2020-е"}
    period_options += [{"key": key, "label": decade_names.get(key, key), "count": count} for key, count in sorted(periods.items()) if count >= ROUND_COUNT]
    geo_names = {"moex": "Россия · MOEX", "us": "США", "europe": "Европа"}
    geo_options = [{"key": key, "label": geo_names.get(key, key.upper()), "count": count} for key, count in sorted(geographies.items()) if count >= ROUND_COUNT]
    return {"periods": period_options, "geographies": geo_options}


def cases_for_preferences(cases: list[dict[str, Any]], preferences: dict[str, Any]) -> list[dict[str, Any]]:
    period = str(preferences.get("period", "all"))
    geography = str(preferences.get("geography", "moex")).lower()
    filtered = [case for case in cases
                if (period == "all" or f"{(int(case['end_date'][:4]) // 10) * 10}s" == period)
                and str(case.get("market", "MOEX")).lower() == geography]
    return filtered


def validate_preferences(data: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, str]:
    """Accept only filters backed by at least one complete five-round session."""
    period = str(data.get("period", "all"))
    geography = str(data.get("geography", "moex")).lower()
    filters = available_scenario_filters(cases)
    if period not in {item["key"] for item in filters["periods"]}:
        raise ValueError("Для выбранного периода пока нет пяти проверенных сценариев")
    if geography not in {item["key"] for item in filters["geographies"]}:
        raise ValueError("Для выбранного рынка пока нет пяти проверенных сценариев")
    if len(cases_for_preferences(cases, {"period": period, "geography": geography})) < ROUND_COUNT:
        raise ValueError("С выбранными фильтрами доступно меньше пяти сценариев. Измените период или рынок.")
    return {"period": period, "geography": geography}


def load_education() -> dict[str, dict[str, Any]]:
    raw = read_json(EDUCATION_PATH)
    if not isinstance(raw, dict):
        raise DataUnavailable("Образовательный набор должен быть JSON-объектом")
    return raw


def case_education(case: dict[str, Any], education: dict[str, dict[str, Any]]) -> dict[str, Any]:
    item = education.get(case["scenario_id"], {})
    factors = item.get("factors", [])
    quiz = item.get("quiz", [])
    if not isinstance(factors, list) or not isinstance(quiz, list) or not 2 <= len(quiz) <= 4:
        raise DataUnavailable(f"Сценарий {case['scenario_id']} не содержит 2–4 образовательных вопроса")
    for question in quiz:
        if not isinstance(question, dict) or not {"id", "question", "options", "correct_option", "explanation"}.issubset(question):
            raise DataUnavailable(f"Неверная структура quiz в {case['scenario_id']}")
        if not isinstance(question["options"], list) or not 2 <= len(question["options"]) <= 4:
            raise DataUnavailable(f"Неверные варианты quiz в {case['scenario_id']}")
        if not 0 <= int(question["correct_option"]) < len(question["options"]):
            raise DataUnavailable(f"Неверный правильный вариант quiz в {case['scenario_id']}")
    summary = item.get("summary", {})
    hints = item.get("hints")
    if hints is None:
        first = factors[0] if factors else {"name": "рыночный фон", "description": summary.get("uncertainty", "Сопоставьте несколько факторов.")}
        second = factors[1] if len(factors) > 1 else first
        hints = [
            {"kind": "Экономический фактор", "label": "проверенный контекст", "text": first["description"]},
            {"kind": "Второй сигнал", "label": "проверенный контекст", "text": second["description"]},
            {"kind": "Как читать цену", "label": "учебная подсказка", "text": "Цена отражает ожидания о будущих денежных потоках и риске. Совпадение новости и движения не доказывает причинную связь."},
            {"kind": "Что сопоставить", "label": "учебная подсказка", "text": f"Проверьте, могли ли одновременно влиять {first['name'].lower()} и {second['name'].lower()}. Один фактор редко описывает всё движение."},
            {"kind": "Предел данных", "label": "метод", "text": "Набор содержит дневные цены закрытия, но не полную отчётность, объёмы и все рыночные индикаторы. Отделяйте наблюдение от гипотезы."},
            {"kind": "Рыночные настроения", "label": "реконструкция · не цитата", "text": f"Возможное мнение участника того времени: «{summary.get('uncertainty', 'Исход остаётся неопределённым.')} Я бы сравнил этот сигнал с фактором: {second['name'].lower()}». Это учебная реконструкция, не реальное сообщение."},
        ]
    if not isinstance(hints, list) or len(hints) != 6 or any(not isinstance(h, dict) or not {"kind", "label", "text"}.issubset(h) or not all(isinstance(h[k], str) and h[k].strip() for k in ("kind", "label", "text")) for h in hints):
        raise DataUnavailable(f"Сценарий {case['scenario_id']} должен содержать шесть валидных подсказок")
    return {"factors": factors, "quiz": quiz, "summary": summary, "articles": item.get("articles", {}), "glossary": item.get("glossary", []), "hints": hints}


def review_argument_for_user(db: sqlite3.Connection, user_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Cap external evaluations at ten per account in a rolling 24-hour window."""
    if not os.environ.get("SPIRIT_LLM_API_KEY"):
        return llm_provider.fallback_argument_review(context)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    count = db.execute("SELECT COUNT(*) FROM llm_requests WHERE user_id=? AND created_at>?", (user_id, cutoff)).fetchone()[0]
    if count >= 10:
        fallback = llm_provider.fallback_argument_review(context)
        fallback["availability"] = "daily_limit"
        return fallback
    db.execute("INSERT INTO llm_requests(user_id,created_at) VALUES (?,?)", (user_id, datetime.now(timezone.utc).isoformat()))
    return llm_provider.review_argument(context)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, round_index INTEGER NOT NULL DEFAULT 0,
        round_order TEXT NOT NULL, created_at TEXT NOT NULL,
        completed_at TEXT, user_id TEXT, level TEXT NOT NULL DEFAULT 'easy'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS answers (
        session_id TEXT NOT NULL, round_index INTEGER NOT NULL,
        company TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL,
        forecast TEXT NOT NULL, rationale TEXT NOT NULL, strategy TEXT NOT NULL,
        factors TEXT NOT NULL DEFAULT '[]', quiz_answers TEXT NOT NULL DEFAULT '[]', confidence INTEGER NOT NULL DEFAULT 3, hints_used INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, round_index),
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )""")
    session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
    if "completed_at" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN completed_at TEXT")
    if "user_id" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
    if "level" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN level TEXT NOT NULL DEFAULT 'easy'")
    columns = {row[1] for row in db.execute("PRAGMA table_info(answers)").fetchall()}
    if "quiz_answers" not in columns:
        db.execute("ALTER TABLE answers ADD COLUMN quiz_answers TEXT NOT NULL DEFAULT '[]'")
    if "factors" not in columns:
        db.execute("ALTER TABLE answers ADD COLUMN factors TEXT NOT NULL DEFAULT '[]'")
    if "created_at" not in columns:
        db.execute("ALTER TABLE answers ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    if "confidence" not in columns:
        db.execute("ALTER TABLE answers ADD COLUMN confidence INTEGER NOT NULL DEFAULT 3")
    if "hints_used" not in columns:
        db.execute("ALTER TABLE answers ADD COLUMN hints_used INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS round_hints (
        session_id TEXT NOT NULL, round_index INTEGER NOT NULL, hint_index INTEGER NOT NULL,
        opened_at TEXT NOT NULL, PRIMARY KEY(session_id,round_index,hint_index),
        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        tutorial_completed INTEGER NOT NULL DEFAULT 0, tutorial_step INTEGER NOT NULL DEFAULT 0,
        tutorial_practice_completed INTEGER NOT NULL DEFAULT 0,
        unlocked_level TEXT NOT NULL DEFAULT 'easy', preferences TEXT NOT NULL DEFAULT '{}'
    )""")
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "tutorial_practice_completed" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN tutorial_practice_completed INTEGER NOT NULL DEFAULT 0")
    if "preferences" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
    db.execute("""CREATE TABLE IF NOT EXISTS auth_tokens (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, ip TEXT NOT NULL,
        attempted_at TEXT NOT NULL, succeeded INTEGER NOT NULL DEFAULT 0
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS llm_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    db.commit()
    return db


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))


def public_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    try:
        preferences = json.loads(row["preferences"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        preferences = {}
    return {
        "id": row["id"], "email": row["email"], "display_name": row["display_name"],
        "created_at": row["created_at"], "tutorial_completed": bool(row["tutorial_completed"]),
        "tutorial_step": int(row["tutorial_step"]), "unlocked_level": row["unlocked_level"],
        "levels": {level: LEVELS.index(level) <= LEVELS.index(row["unlocked_level"]) for level in LEVELS},
        "preferences": {"period": preferences.get("period", "all"), "geography": preferences.get("geography", "moex")},
    }


def parse_auth_token(headers: Any) -> str:
    token = headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    cookie = SimpleCookie(headers.get("Cookie", ""))
    return cookie.get(AUTH_COOKIE).value if cookie.get(AUTH_COOKIE) else ""


def current_user(db: sqlite3.Connection, headers: Any) -> sqlite3.Row | None:
    token = parse_auth_token(headers)
    if not token:
        return None
    row = db.execute("SELECT u.* FROM auth_tokens t JOIN users u ON u.id=t.user_id WHERE t.token=? AND t.expires_at>?", (token, datetime.now(timezone.utc).isoformat())).fetchone()
    return row


def new_token(db: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute("INSERT INTO auth_tokens(token,user_id,created_at,expires_at) VALUES (?,?,?,?)", (token, user_id, now.isoformat(), (now + timedelta(days=AUTH_TTL_DAYS)).isoformat()))
    return token


def validate_registration(data: dict[str, Any]) -> tuple[str, str, str]:
    display_name = str(data.get("display_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not 2 <= len(display_name) <= 40:
        raise ValueError("Имя должно содержать от 2 до 40 символов")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Введите корректный email")
    if len(password) < 8 or not re.search(r"[A-Za-zА-Яа-я]", password) or not re.search(r"\d", password):
        raise ValueError("Пароль должен быть не короче 8 символов и содержать буквы и цифры")
    return display_name, email, password


def public_quiz(quiz: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": q["id"], "question": q["question"], "options": q["options"]} for q in quiz]


def public_round(case: dict[str, Any], index: int, education: dict[str, dict[str, Any]], all_cases: list[dict[str, Any]] | None = None, level: str = "easy") -> dict[str, Any]:
    content = case_education(case, education)
    # Anchor the displayed index at the decision point so the revealed segment
    # can continue from 100 without changing scale.
    base = float(case["prices"][59]["close"])
    summary = content.get("summary", {})
    return {
        "index": index,
        "title": case["title"],
        "intro": case["intro"],
        "difficulty": case.get("difficulty", "easy"),
        "article": content.get("articles", {}).get(level, content.get("articles", {}).get("easy", [])),
        "briefing": {
            "event": summary.get("what_happened", case["intro"]),
            "context": summary.get("why_important", "Рынок одновременно учитывает несколько факторов."),
            "circumstances": summary.get("uncertainty", "Доступные данные не позволяют свести изменение к одной причине."),
            "consequences": summary.get("key_takeaway", case.get("lesson", "Решение принимается при неполной информации.")),
        },
        "chart": [{"day": i + 1, "value": round(float(point["close"]) / base * 100, 2)} for i, point in enumerate(case["prices"][:60])],
        "periods": sorted({(int(item["end_date"][:4]), int(item["end_date"][5:7])) for item in (all_cases or [case])}),
        "companies": COMPANIES,
        "factors": content["factors"],
        "quiz": public_quiz(content["quiz"]),
        "glossary": content["glossary"],
        "sources": case.get("sources", []),
        "market_source": case.get("source"),
        "market_source_label": case.get("source_label", "Котировки MOEX ISS · TQBR · цена закрытия"),
        "hint_limit": len(content["hints"]),
        "threshold_pct": FORECAST_THRESHOLD_PCT,
    }


def classify_change(change: float) -> str:
    if change >= FORECAST_THRESHOLD_PCT:
        return "up"
    if change <= -FORECAST_THRESHOLD_PCT:
        return "down"
    return "flat"


def calculate(case: dict[str, Any], education: dict[str, dict[str, Any]], answer: dict[str, Any], argument_feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    prices = case["prices"]
    before, after = float(prices[59]["close"]), float(prices[89]["close"])
    change = (after / before - 1) * 100
    actual_forecast = classify_change(change)
    stock_share = STRATEGIES[answer["strategy"]]
    portfolio_change = change * stock_share
    end_value = round(START_CASH * (1 + portfolio_change / 100))
    content = case_education(case, education)
    quiz_map = {q["id"]: int(q["correct_option"]) for q in content["quiz"]}
    quiz_answers = answer["quiz_answers"]
    quiz_correct = sum(1 for item in quiz_answers if quiz_map.get(item.get("id")) == int(item.get("option", -1)))
    quiz_review = [{
        "id": q["id"], "question": q["question"], "selected_option": next((item.get("option") for item in quiz_answers if item.get("id") == q["id"]), None),
        "correct_option": int(q["correct_option"]), "explanation": q["explanation"],
    } for q in content["quiz"]]
    selected_factors = [content["factors"][i]["name"] for i in answer["factors"]]
    unselected_factors = [factor["name"] for i, factor in enumerate(content["factors"]) if i not in answer["factors"]]
    company_correct = answer["company"] == case["ticker"]
    period_correct = answer["year"] == int(case["end_date"][:4]) and answer["month"] == int(case["end_date"][5:7])
    forecast_correct = answer["forecast"] == actual_forecast
    argument_feedback = argument_feedback or llm_provider.fallback_argument_review({"rationale": answer["rationale"], "selected_factors": selected_factors})
    argument_points = {"weak": 0, "partial": 1, "strong": 2}.get(argument_feedback.get("argument_quality"), 0)
    strategy_matches_hypothesis = {"up": "buy", "flat": "hold", "down": "sell"}.get(answer["forecast"]) == answer["strategy"]
    hints_used = max(0, min(6, int(answer.get("hints_used", 0))))
    historical_points = int(company_correct) + int(period_correct)
    forecast_points = 2 * int(forecast_correct)
    strategy_points = 2 * int(strategy_matches_hypothesis)
    understanding_points = round(2 * quiz_correct / max(1, len(content["quiz"])))
    raw_total = historical_points + forecast_points + strategy_points + understanding_points + argument_points
    max_score = 10 - hints_used
    score = {
        "historical_accuracy": historical_points,
        "forecast_accuracy": int(forecast_correct),
        "understanding": quiz_correct,
        "financial_result": round(portfolio_change, 2),
        "total": min(raw_total, max_score),
        "maximum": max_score,
        "components": {
            "historical": {"points": historical_points, "maximum": 2},
            "forecast": {"points": forecast_points, "maximum": 2},
            "strategy_consistency": {"points": strategy_points, "maximum": 2},
            "understanding": {"points": understanding_points, "maximum": 2, "correct_answers": quiz_correct, "question_count": len(content["quiz"])},
            "argumentation": {"points": argument_points, "maximum": 2},
            "hint_limit": {"used": hints_used, "maximum_used": 6},
        },
    }
    return {
        "scenario_id": case["scenario_id"], "company_correct": company_correct, "period_correct": period_correct,
        "forecast_correct": forecast_correct, "change_pct": round(change, 2), "actual_forecast": actual_forecast,
        "threshold_pct": FORECAST_THRESHOLD_PCT, "strategy": answer["strategy"],
        "portfolio_change_pct": round(portfolio_change, 2), "portfolio_end": end_value,
        "alternatives": {key: round(START_CASH * (1 + change * share / 100)) for key, share in STRATEGIES.items()},
        "ticker": case["ticker"], "company_name": case["company_name"], "end_date": case["end_date"],
        "reveal_prices": prices, "lesson": case["lesson"], "summary": content["summary"],
        "factor_analysis": {
            "selected": selected_factors,
            "other_candidates": unselected_factors,
            "interpretation": "Отмеченные факторы — выбранные вами аналитические сигналы. Неотмеченные остаются возможными второстепенными обстоятельствами; сами цены не доказывают, что какой-либо фактор вызвал движение.",
            "uncertainty": content["summary"].get("uncertainty", "Несколько факторов могли действовать одновременно."),
            "missing_data": "Для более определённого вывода нужны отчётность компании, объёмы торгов, данные по рынку и отрасли, а также сопоставление конкурирующих объяснений.",
        },
        "quiz_result": {"correct": quiz_correct, "total": len(content["quiz"]), "review": quiz_review}, "score": score,
        "argument_feedback": argument_feedback,
        "hints_used": hints_used,
        "source": case["source"], "source_label": case.get("source_label", "Котировки MOEX ISS · TQBR · цена закрытия"), "sources": case.get("sources", []),
        "calculation_note": "Стартовый капитал — 100 000 ₽. «Купить» вкладывает всю сумму в акцию, «Удержать» делит её поровну между акцией и деньгами, «Продать» оставляет всю сумму в деньгах. Комиссии, налоги и дивиденды не учитываются.",
    }


def session_payload(db: sqlite3.Connection, sid: str, all_cases: list[dict[str, Any]], education: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    order = json.loads(row["round_order"])
    answers = db.execute("SELECT * FROM answers WHERE session_id=? ORDER BY round_index", (sid,)).fetchall()
    history = []
    for item in answers:
        payload = json.loads(item["result"])
        payload["answer"] = {"company": item["company"], "year": item["year"], "month": item["month"], "forecast": item["forecast"], "rationale": item["rationale"], "strategy": item["strategy"], "confidence": int(item["confidence"]), "hints_used": int(item["hints_used"]), "factors": json.loads(item["factors"]), "quiz_answers": json.loads(item["quiz_answers"])}
        history.append(payload)
    idx = int(row["round_index"])
    current = public_round(all_cases[order[idx]], idx, education, all_cases, row["level"]) if idx < len(order) else None
    hint_state = None
    if current:
        opened_rows = db.execute("SELECT hint_index FROM round_hints WHERE session_id=? AND round_index=? ORDER BY hint_index", (sid, idx)).fetchall()
        hint_data = case_education(all_cases[order[idx]], education)["hints"]
        opened = [{"index": int(item["hint_index"]), **hint_data[int(item["hint_index"])]} for item in opened_rows]
        hint_state = {"used": len(opened), "remaining": current["hint_limit"] - len(opened), "opened": opened}
    return {"session_id": sid, "round_index": idx, "total_rounds": ROUND_COUNT, "state": "completed" if idx >= ROUND_COUNT else "awaiting_decision", "current": current, "hint_state": hint_state, "history": history, "level": row["level"], "user_id": row["user_id"]}


def profile_payload(db: sqlite3.Connection, user: sqlite3.Row) -> dict[str, Any]:
    sessions = db.execute("SELECT * FROM sessions WHERE user_id=? AND completed_at IS NOT NULL ORDER BY completed_at DESC", (user["id"],)).fetchall()
    answers = db.execute("SELECT a.* FROM answers a JOIN sessions s ON s.id=a.session_id WHERE s.user_id=? ORDER BY a.created_at DESC", (user["id"],)).fetchall()
    total = len(answers)
    forecasts = sum(json.loads(a["result"]).get("score", {}).get("forecast_accuracy", 0) for a in answers)
    historical = sum(json.loads(a["result"]).get("score", {}).get("historical_accuracy", 0) for a in answers)
    understanding = sum(json.loads(a["result"]).get("score", {}).get("understanding", 0) for a in answers)
    score_values = [json.loads(a["result"]).get("score", {}).get("total", 0) for a in answers]
    hints_used = sum(int(a["hints_used"]) for a in answers)
    strategies: dict[str, int] = {key: 0 for key in STRATEGIES}
    for answer in answers:
        if answer["strategy"] in strategies:
            strategies[answer["strategy"]] += 1
    factor_counts: dict[str, int] = {}
    for a in answers:
        result = json.loads(a["result"])
        for index in json.loads(a["factors"]):
            name = next((f["name"] for f in result.get("summary", {}).get("factors", []) if isinstance(f, dict) and f.get("index") == index), f"Фактор {index + 1}")
            factor_counts[name] = factor_counts.get(name, 0) + 1
    strengths = []
    weaknesses = []
    if total >= 3:
        if forecasts / total >= .6: strengths.append("Направление движения цены")
        else: weaknesses.append({"topic": "Направление движения цены", "detail": "Проверьте связь между прогнозом и горизонтом в 30 торговых дней."})
        if understanding / max(1, total * 2) < .6: weaknesses.append({"topic": "Экономический контекст", "detail": "Сравнивайте отраслевой фактор с рисками и ограничениями данных."})
        if historical / max(1, total * 2) >= .6: strengths.append("Чтение исторического контекста")
    recommendations = [{"topic": item["topic"], "detail": item["detail"], "link": "/#tutorial"} for item in weaknesses]
    history = []
    for s in sessions:
        round_rows = db.execute("SELECT * FROM answers WHERE session_id=? ORDER BY round_index", (s["id"],)).fetchall()
        history.append({
            "session_id": s["id"], "level": s["level"], "completed_at": s["completed_at"],
            "rounds": [{
                "round_index": int(a["round_index"]), "company": a["company"], "year": a["year"], "month": a["month"],
                "forecast": a["forecast"], "rationale": a["rationale"], "strategy": a["strategy"],
                "factors": json.loads(a["factors"]), "quiz_answers": json.loads(a["quiz_answers"]),
                "result": json.loads(a["result"]),
            } for a in round_rows],
        })
    return {
        "user": public_user(user),
        "stats": {"completed_sessions": len(sessions), "rounds": total, "forecast_accuracy": round(forecasts / total * 100) if total else None, "historical_accuracy": round(historical / max(1, total * 2) * 100), "quiz_accuracy": round(understanding / max(1, total * 2) * 100), "average_score": round(sum(score_values) / total, 1) if total else None, "average_hints_used": round(hints_used / total, 1) if total else None, "hints_used": hints_used, "strategies": strategies},
        "scenario_filters": available_scenario_filters(load_cases()),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "history": history,
    }


def validate_answer(data: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    required = ["company", "year", "month", "forecast", "rationale", "strategy", "factors", "quiz_answers"]
    if any(key not in data for key in required):
        raise ValueError("Заполните прогноз, стратегию, объяснение и мини-тест")
    if not isinstance(data["rationale"], str):
        raise ValueError("Обоснование должно быть текстом")
    answer = {"company": str(data["company"]), "year": int(data["year"]), "month": int(data["month"]), "forecast": str(data["forecast"]), "rationale": data["rationale"].strip(), "strategy": str(data["strategy"]), "factors": data["factors"], "quiz_answers": data["quiz_answers"]}
    answer["confidence"] = int(data.get("confidence", 3))
    if not 1 <= answer["confidence"] <= 5:
        raise ValueError("Уверенность должна быть от 1 до 5")
    valid_periods = {tuple(period) for period in current.get("periods", [])}
    if answer["company"] not in {item["ticker"] for item in COMPANIES} or (answer["year"], answer["month"]) not in valid_periods:
        raise ValueError("Некорректная компания или период")
    if answer["forecast"] not in FORECASTS or answer["strategy"] not in STRATEGIES:
        raise ValueError("Некорректный прогноз или стратегия")
    if len(answer["rationale"]) > 500:
        raise ValueError("Объяснение должно быть не длиннее 500 символов")
    if not isinstance(answer["factors"], list) or not answer["factors"] or any(not isinstance(item, int) or not 0 <= item < len(current["factors"]) for item in answer["factors"]):
        raise ValueError("Некорректный список факторов")
    if not isinstance(answer["quiz_answers"], list) or len(answer["quiz_answers"]) != len(current["quiz"]):
        raise ValueError("Ответьте на все вопросы мини-теста")
    allowed = {q["id"]: len(q["options"]) for q in current["quiz"]}
    if any(not isinstance(item, dict) or item.get("id") not in allowed or not isinstance(item.get("option"), int) or not 0 <= item["option"] < allowed[item["id"]] for item in answer["quiz_answers"]):
        raise ValueError("Некорректные ответы мини-теста")
    return answer


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, fmt, *args):
        print("[slay-investments] " + fmt % args)

    def send_json(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (headers or {}).get("Cache-Control", "no-store"))
        for name, value in (headers or {}).items():
            if name.lower() != "cache-control":
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_auth_json(self, payload: Any, token: str | None = None, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            secure = "; Secure" if (getattr(self, "connection", None) and getattr(self.connection, "cipher", None)) or self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            self.send_header("Set-Cookie", f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={AUTH_TTL_DAYS * 86400}{secure}")
        else:
            self.send_header("Set-Cookie", f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("Слишком большой запрос")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Тело запроса должно быть JSON-объектом")
        return payload

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            all_cases, education = load_cases(), load_education()
            with connect() as db:
                user = current_user(db, self.headers)
                if path == "/api/preview":
                    previews = []
                    for case in all_cases:
                        base = float(case["prices"][0]["close"])
                        source = case["source"]
                        source_url = source.split(": https://", 1)[-1] if ": https://" in source else source
                        previews.append({
                            "title": case["title"], "ticker": case["ticker"],
                            "company_name": case["company_name"],
                            "chart": [{"day": i + 1, "value": round(float(point["close"]) / base * 100, 2)} for i, point in enumerate(case["prices"][:60])],
                            "start_date": case["prices"][0]["date"],
                            "decision_date": case["prices"][59]["date"],
                            "source": source_url,
                            "source_label": case.get("source_label", "Котировки MOEX ISS · TQBR · цена закрытия"),
                        })
                    return self.send_json({**previews[0], "previews": previews}, headers={"Cache-Control": "public, max-age=3600"})
                if path == "/api/me":
                    return self.send_json({"user": public_user(user)} if user else {"user": None})
                if path == "/api/tutorial/practice":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    return self.send_json({"round": public_round(all_cases[TUTORIAL_CASE_INDEX], 0, education, all_cases)})
                if path == "/api/tutorial":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    return self.send_json({"completed": bool(user["tutorial_completed"]), "step": int(user["tutorial_step"]), "total_steps": 4})
                if path == "/api/profile":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    return self.send_json(profile_payload(db, user))
                if path == "/api/scenario-filters":
                    return self.send_json(available_scenario_filters(all_cases))
                if path == "/api/levels":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    return self.send_json({"levels": public_user(user)["levels"], "unlocked_level": user["unlocked_level"]})
            if path == "/api/session":
                sid = self.headers.get("X-Session-Id", "")
                with connect() as db:
                    user = current_user(db, self.headers)
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    payload = session_payload(db, sid, all_cases, education)
                    if payload and payload.get("user_id") and (not user or payload["user_id"] != user["id"]):
                        return self.send_json({"error": "Сессия принадлежит другому пользователю"}, 403)
                return self.send_json(payload or {"error": "Сессия не найдена"}, 200 if payload else 404)
            if path.startswith("/api/"):
                return self.send_json({"error": "Маршрут не найден"}, 404)
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()
        except DataUnavailable as exc:
            return self.send_json({"error": str(exc), "code": "DATA_UNAVAILABLE"}, 503)
        except json.JSONDecodeError:
            return self.send_json({"error": "Некорректный формат запроса", "code": "INVALID_JSON"}, 400)
        except (ValueError, TypeError) as exc:
            return self.send_json({"error": str(exc) or "Проверьте заполнение данных", "code": "VALIDATION_ERROR"}, 400)
        except Exception:
            import traceback
            traceback.print_exc()
            return self.send_json({"error": "Внутренняя ошибка сервера. Попробуйте позже.", "code": "INTERNAL_ERROR"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
            all_cases, education = load_cases(), load_education()
            with connect() as db:
                user = current_user(db, self.headers)
                if path == "/api/auth/register":
                    display_name, email, password = validate_registration(data)
                    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                        return self.send_json({"error": "Пользователь с таким email уже существует", "code": "EMAIL_EXISTS"}, 409)
                    user_id = uuid.uuid4().hex
                    try:
                        db.execute("INSERT INTO users(id,email,display_name,password_hash,created_at) VALUES (?,?,?,?,?)", (user_id, email, display_name, hash_password(password), datetime.now(timezone.utc).isoformat()))
                    except sqlite3.IntegrityError:
                        return self.send_json({"error": "Пользователь с таким email уже существует", "code": "EMAIL_EXISTS"}, 409)
                    token = new_token(db, user_id)
                    db.commit()
                    return self.send_auth_json({"user": public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())}, token, 201)
                if path == "/api/auth/login":
                    email = str(data.get("email", "")).strip().lower()
                    password = str(data.get("password", ""))
                    ip = self.client_address[0]
                    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                    attempts = db.execute("SELECT COUNT(*) FROM login_attempts WHERE attempted_at>? AND (email=? OR ip=?) AND succeeded=0", (cutoff, email, ip)).fetchone()[0]
                    if attempts >= 8:
                        return self.send_json({"error": "Слишком много попыток. Подождите 10 минут и попробуйте снова."}, 429)
                    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    valid_password = verify_password(password, row["password_hash"] if row else DUMMY_PASSWORD_HASH)
                    if not row or not valid_password:
                        db.execute("INSERT INTO login_attempts(email,ip,attempted_at,succeeded) VALUES (?,?,?,0)", (email, ip, datetime.now(timezone.utc).isoformat()))
                        db.commit()
                        return self.send_json({"error": "Неверный email или пароль"}, 401)
                    db.execute("INSERT INTO login_attempts(email,ip,attempted_at,succeeded) VALUES (?,?,?,1)", (email, ip, datetime.now(timezone.utc).isoformat()))
                    token = new_token(db, row["id"])
                    db.commit()
                    return self.send_auth_json({"user": public_user(row)}, token)
                if path == "/api/auth/logout":
                    token = parse_auth_token(self.headers)
                    if token:
                        db.execute("DELETE FROM auth_tokens WHERE token=?", (token,)); db.commit()
                    return self.send_auth_json({"ok": True}, None)
                if path == "/api/profile/preferences":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    preferences = validate_preferences(data, all_cases)
                    db.execute("UPDATE users SET preferences=? WHERE id=?", (json.dumps(preferences), user["id"]))
                    db.commit()
                    updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                    return self.send_json({"user": public_user(updated), "preferences": preferences})
                if path == "/api/tutorial/practice":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    case = all_cases[TUTORIAL_CASE_INDEX]
                    current = public_round(case, 0, education, all_cases)
                    answer = validate_answer(data, current)
                    content = case_education(case, education)
                    selected = [content["factors"][i]["name"] for i in answer["factors"]]
                    feedback = review_argument_for_user(db, user["id"], {"rationale": answer["rationale"], "brief": case["intro"], "factor_names": [f["name"] for f in content["factors"]], "selected_factors": selected, "forecast": answer["forecast"], "confidence": answer["confidence"]})
                    result = calculate(case, education, answer, feedback)
                    result["answer"] = answer
                    db.execute("UPDATE users SET tutorial_practice_completed=1 WHERE id=?", (user["id"],))
                    db.commit()
                    return self.send_json({"result": result})
                if path == "/api/tutorial/progress":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    step = int(data.get("step", -1))
                    completed = bool(data.get("completed", False))
                    current_step = int(user["tutorial_step"])
                    if step != current_step + 1 or step > 4:
                        return self.send_json({"error": "Некорректный шаг туториала"}, 400)
                    if completed and step < 4:
                        return self.send_json({"error": "Сначала завершите все этапы туториала"}, 400)
                    if completed and not user["tutorial_practice_completed"]:
                        return self.send_json({"error": "Сначала пройдите пробный раунд"}, 400)
                    next_level = "easy" if completed else user["unlocked_level"]
                    db.execute("UPDATE users SET tutorial_step=?, tutorial_completed=?, unlocked_level=? WHERE id=?", (step, int(completed), next_level, user["id"]))
                    db.commit()
                    return self.send_json({"user": public_user(db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())})
                if path == "/api/tutorial/restart":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    db.execute("UPDATE users SET tutorial_step=0, tutorial_practice_completed=0 WHERE id=?", (user["id"],))
                    db.commit()
                    return self.send_json({"user": public_user(db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())})
                if path == "/api/session":
                    requested_level = str(data.get("level", "easy")).lower()
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    if requested_level not in LEVELS:
                        return self.send_json({"error": "Неизвестный уровень"}, 400)
                    if user and LEVELS.index(requested_level) > LEVELS.index(user["unlocked_level"]):
                        return self.send_json({"error": "Сначала завершите предыдущий уровень"}, 403)
                    if user and not user["tutorial_completed"]:
                        return self.send_json({"error": "Сначала завершите туториал"}, 403)
                    try:
                        preferences = json.loads(user["preferences"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        preferences = {}
                    selected_cases = cases_for_preferences(all_cases, preferences)
                    if len(selected_cases) < ROUND_COUNT:
                        return self.send_json({"error": "С выбранными фильтрами доступно меньше пяти сценариев. Измените период или рынок.", "code": "INSUFFICIENT_SCENARIOS"}, 409)
                    order = [all_cases.index(case) for case in selected_cases[:ROUND_COUNT]]
                    sid = uuid.uuid4().hex
                    db.execute("INSERT INTO sessions (id,round_index,round_order,created_at,user_id,level) VALUES (?,?,?,?,?,?)", (sid, 0, json.dumps(order), datetime.now(timezone.utc).isoformat(), user["id"] if user else None, requested_level))
                    db.commit()
                    return self.send_json(session_payload(db, sid, all_cases, education), 201)
                if path == "/api/hint":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    sid = str(data.get("session_id", ""))
                    requested_idx = int(data.get("round_index", -1))
                    hint_idx = int(data.get("hint_index", -1))
                    game = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                    if not game:
                        return self.send_json({"error": "Сессия не найдена"}, 404)
                    if game["user_id"] and game["user_id"] != user["id"]:
                        return self.send_json({"error": "Сессия принадлежит другому пользователю"}, 403)
                    if requested_idx != int(game["round_index"]) or requested_idx >= ROUND_COUNT:
                        return self.send_json({"error": "Раунд уже изменился"}, 409)
                    order = json.loads(game["round_order"])
                    hints = case_education(all_cases[order[requested_idx]], education)["hints"]
                    if not 0 <= hint_idx < len(hints):
                        return self.send_json({"error": "Подсказка не найдена"}, 400)
                    prior = db.execute("SELECT 1 FROM round_hints WHERE session_id=? AND round_index=? AND hint_index=?", (sid, requested_idx, hint_idx)).fetchone()
                    if not prior:
                        used = int(db.execute("SELECT COUNT(*) FROM round_hints WHERE session_id=? AND round_index=?", (sid, requested_idx)).fetchone()[0])
                        if hint_idx != used:
                            return self.send_json({"error": "Открывайте подсказки по порядку"}, 409)
                        db.execute("INSERT INTO round_hints(session_id,round_index,hint_index,opened_at) VALUES (?,?,?,?)", (sid, requested_idx, hint_idx, datetime.now(timezone.utc).isoformat()))
                        db.commit()
                    used = int(db.execute("SELECT COUNT(*) FROM round_hints WHERE session_id=? AND round_index=?", (sid, requested_idx)).fetchone()[0])
                    return self.send_json({"hint": {"index": hint_idx, **hints[hint_idx]}, "used": used, "remaining": len(hints) - used, "max_score": 10 - used})
                if path == "/api/answer":
                    if not user:
                        return self.send_json({"error": "Требуется вход"}, 401)
                    sid = str(data.get("session_id", ""))
                    requested_idx = int(data.get("round_index", -1))
                    db.execute("BEGIN IMMEDIATE")
                    session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                    if not session:
                        return self.send_json({"error": "Сессия не найдена"}, 404)
                    if session["user_id"] and (not user or session["user_id"] != user["id"]):
                        return self.send_json({"error": "Сессия принадлежит другому пользователю"}, 403)
                    idx = int(session["round_index"])
                    if requested_idx < idx:
                        previous = db.execute("SELECT result,company,year,month,forecast,rationale,strategy,factors,quiz_answers,confidence,hints_used FROM answers WHERE session_id=? AND round_index=?", (sid, requested_idx)).fetchone()
                        if previous:
                            result = json.loads(previous["result"])
                            result["answer"] = {"company": previous["company"], "year": previous["year"], "month": previous["month"], "forecast": previous["forecast"], "rationale": previous["rationale"], "strategy": previous["strategy"], "confidence": int(previous["confidence"]), "hints_used": int(previous["hints_used"]), "factors": json.loads(previous["factors"]), "quiz_answers": json.loads(previous["quiz_answers"])}
                            return self.send_json({"result": result, "session": session_payload(db, sid, all_cases, education)})
                    if requested_idx != idx:
                        return self.send_json({"error": "Раунд уже изменился. Обновите игру."}, 409)
                    if idx >= ROUND_COUNT:
                        return self.send_json({"error": "Сессия уже завершена"}, 409)
                    order = json.loads(session["round_order"])
                    case = all_cases[order[idx]]
                    current = public_round(case, idx, education, all_cases)
                    answer = validate_answer(data, current)
                    answer["hints_used"] = int(db.execute("SELECT COUNT(*) FROM round_hints WHERE session_id=? AND round_index=?", (sid, idx)).fetchone()[0])
                    content = case_education(case, education)
                    selected = [content["factors"][i]["name"] for i in answer["factors"]]
                    feedback = review_argument_for_user(db, user["id"], {"rationale": answer["rationale"], "brief": case["intro"], "factor_names": [f["name"] for f in content["factors"]], "selected_factors": selected, "forecast": answer["forecast"], "confidence": answer["confidence"]})
                    result = calculate(case, education, answer, feedback)
                    db.execute("INSERT INTO answers (session_id,round_index,company,year,month,forecast,rationale,strategy,factors,quiz_answers,confidence,hints_used,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, idx, answer["company"], answer["year"], answer["month"], answer["forecast"], answer["rationale"], answer["strategy"], json.dumps(answer["factors"]), json.dumps(answer["quiz_answers"]), answer["confidence"], answer["hints_used"], json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
                    next_idx = idx + 1
                    db.execute("UPDATE sessions SET round_index=?, completed_at=? WHERE id=? AND round_index=?", (next_idx, datetime.now(timezone.utc).isoformat() if next_idx >= ROUND_COUNT else None, sid, idx))
                    if next_idx >= ROUND_COUNT and session["user_id"]:
                        current_level = session["level"]
                        if current_level in LEVELS and LEVELS.index(current_level) < len(LEVELS) - 1:
                            unlocked = LEVELS[LEVELS.index(current_level) + 1]
                            db.execute("UPDATE users SET unlocked_level=? WHERE id=? AND tutorial_completed=1 AND (unlocked_level=? OR unlocked_level=?)", (unlocked, session["user_id"], current_level, LEVELS[LEVELS.index(current_level) + 1]))
                    db.commit()
                    result["answer"] = answer
                    return self.send_json({"result": result, "session": session_payload(db, sid, all_cases, education)})
            return self.send_json({"error": "Маршрут не найден"}, 404)
        except DataUnavailable as exc:
            return self.send_json({"error": str(exc), "code": "DATA_UNAVAILABLE"}, 503)
        except json.JSONDecodeError:
            return self.send_json({"error": "Некорректный формат запроса", "code": "INVALID_JSON"}, 400)
        except (ValueError, TypeError, KeyError) as exc:
            return self.send_json({"error": str(exc) or "Проверьте заполнение данных", "code": "VALIDATION_ERROR"}, 400)
        except Exception:
            import traceback
            traceback.print_exc()
            return self.send_json({"error": "Внутренняя ошибка сервера. Попробуйте позже.", "code": "INTERNAL_ERROR"}, 500)


if __name__ == "__main__":
    load_cases()
    load_education()
    connect().close()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Slay Investments работает: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
