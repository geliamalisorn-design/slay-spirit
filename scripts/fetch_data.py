"""Fetch the fixed, reproducible demo rounds from the public MOEX ISS history API."""
import json
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "rounds.json"

ROUNDS = [
    {
        "ticker": "SBER", "company_name": "Сбербанк", "target": "2020-01-30",
        "title": "Когда неопределённость ещё не стала паникой",
        "intro": "В новостях появляются сообщения о новом заболевании за пределами России. Масштаб последствий пока неясен. Российский рынок оценивает внешний фон, спрос на банковские услуги и устойчивость экономики.",
        "lesson": "К концу января 2020 года инвесторы уже следили за сообщениями о распространении нового коронавируса, но масштаб будущих ограничений оставался неизвестен. В следующие недели пандемия стала глобальным шоком: распродажи затронули широкий рынок, а падение нефти усилило давление на российские активы. Совпадение событий с движением акции само по себе не доказывает единственную причину изменения цены.",
        "sources": ["ВОЗ: заявления о вспышке нового коронавируса, 30.01.2020 — https://www.who.int/news/item/30-01-2020-statement-on-the-second-meeting-of-the-international-health-regulations-(2005)-emergency-committee-regarding-the-outbreak-of-novel-coronavirus-(2019-ncov)", "Банк России: хроника мер в связи с COVID-19 — https://www.cbr.ru/press/event/?id=6521"],
    },
    {
        "ticker": "GAZP", "company_name": "Газпром", "target": "2022-02-18",
        "title": "Геополитический риск и энергетика",
        "intro": "Вокруг Восточной Европы растёт напряжённость. Энергетические рынки уже отличаются высокой волатильностью, а экспортёры газа зависят от цен, поставок и политических решений.",
        "lesson": "В феврале 2022 года геополитические события радикально изменили риск-премию российского рынка. Московская биржа приостанавливала торги акциями, затем возобновляла их постепенно. Для этого раунда окно после решения строится по фактическим торговым сессиям, поэтому календарный разрыв не означает, что котировка существовала каждый день. Нельзя приписывать всё движение одной новости: ограничения торгов, санкции, сырьевые цены и ожидания действовали одновременно.",
        "sources": ["Московская биржа: сообщения о режиме торгов в феврале—марте 2022 — https://www.moex.com/n72573", "Банк России: решения по работе финансового рынка — https://www.cbr.ru/press/event/?id=12722"],
    },
    {
        "ticker": "NVTK", "company_name": "НОВАТЭК", "target": "2022-08-19",
        "title": "Высокие цены на газ и неизвестный горизонт",
        "intro": "Европейские цены на природный газ резко выросли на фоне ограниченного предложения. Экспортёр с крупными проектами СПГ может выиграть от цен, но сталкивается с санкционными, логистическими и инвестиционными рисками.",
        "lesson": "Летом 2022 года европейские котировки газа были крайне волатильны. Высокая спотовая цена не равна гарантированной прибыли конкретного производителя: важны долгосрочные контракты, объёмы поставок, стоимость проектов и ограничения на оборудование и финансирование. Падение или рост отдельной акции нельзя объяснить только динамикой европейского бенчмарка.",
        "sources": ["IEA: Gas Market Report, Q3 2022 — https://www.iea.org/reports/gas-market-report-q3-2022", "Европейская комиссия: меры по энергетическому рынку — https://energy.ec.europa.eu/topics/markets-and-consumers/actions-and-measures-energy-prices_en"],
    },
    {
        "ticker": "TATN", "company_name": "Татнефть", "target": "2023-10-27",
        "title": "Экспортная выручка и курс рубля",
        "intro": "Нефтяные компании получают выручку, связанную с мировыми ценами и валютным курсом. Осенью рубль ослаб, а власти вернули требование об обязательной продаже части валютной выручки крупнейшими экспортёрами.",
        "lesson": "Указ об обязательной продаже валютной выручки был опубликован в октябре 2023 года. Такая мера могла влиять на валютное предложение и ожидания, но её эффект на конкретную акцию неоднозначен: одновременно менялись цена нефти, налоги, дивидендные ожидания и общий аппетит к риску. В разборе важно различать подтверждённый факт политики и гипотезу о его влиянии на котировку.",
        "sources": ["Указ Президента РФ от 11.10.2023 № 771 — http://publication.pravo.gov.ru/document/0001202310110017", "Банк России: обзор валютного рынка — https://www.cbr.ru/analytics/finstab/"],
    },
    {
        "ticker": "LKOH", "company_name": "ЛУКОЙЛ", "target": "2024-06-07",
        "title": "Ставка, инфляция и стоимость капитала",
        "intro": "Инфляция остаётся высокой, и участники рынка обсуждают, как долго ключевая ставка будет оставаться повышенной. Для компаний это влияет на стоимость заимствований, спрос и оценку будущих денежных потоков.",
        "lesson": "В июне 2024 года Банк России сохранил ключевую ставку на уровне 16% и указал на более жёсткую траекторию денежно-кредитной политики. Высокая ставка может давить на оценки акций через ставку дисконтирования и стоимость капитала, однако нефтяные компании зависят также от курса рубля, мировых цен, налогов и выплат акционерам. Это факторы для анализа, а не точное объяснение каждого дневного движения.",
        "sources": ["Банк России: решение по ключевой ставке от 07.06.2024 — https://www.cbr.ru/press/keypr/", "Банк России: среднесрочный прогноз, июнь 2024 — https://www.cbr.ru/about_br/publ/ddkp/"],
    },
]


def fetch(ticker, start_date, end_date):
    all_rows = []
    offset = 0
    while True:
        query = urlencode({
            "from": start_date, "till": end_date, "start": offset,
            "iss.meta": "off", "iss.only": "history", "history.columns": "TRADEDATE,SECID,BOARDID,CLOSE",
            "limit": 100,
        })
        url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/securities/{ticker}.json?{query}"
        req = Request(url, headers={"User-Agent": "StockGuessr educational demo"})
        with urlopen(req, timeout=30) as response:
            data = json.load(response)["history"]
        rows = data["data"]
        all_rows.extend(rows)
        if len(rows) < 100:
            break
        offset += len(rows)
        time.sleep(.15)
    # TQBR is the main board for ordinary shares. Exclude empty sessions.
    by_day = {}
    for trade_date, secid, boardid, close in all_rows:
        if boardid == "TQBR" and close is not None:
            by_day[trade_date] = float(close)
    return [{"date": d, "close": by_day[d]} for d in sorted(by_day)]


def main():
    out = []
    for item in ROUNDS:
        target = date.fromisoformat(item.pop("target"))
        start = target - timedelta(days=180)
        end = target + timedelta(days=180)
        history = fetch(item["ticker"], start.isoformat(), end.isoformat())
        eligible = [i for i, row in enumerate(history) if row["date"] <= target.isoformat()]
        if not eligible:
            raise SystemExit(f"No MOEX history before target: {item['ticker']} {target}")
        pivot = eligible[-1]
        window = history[pivot - 59:pivot + 31]
        if len(window) != 90:
            raise SystemExit(f"Need 60+30 sessions, got {len(window)} for {item['ticker']}: {len(window)}")
        item["end_date"] = window[59]["date"]
        item["prices"] = window
        item["scenario_id"] = f"{item['ticker'].lower()}-{item['end_date']}"
        item["status"] = "approved"
        item["source_label"] = "Котировки MOEX ISS · TQBR · цена закрытия"
        item["source"] = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/securities/{item['ticker']}.json?from={window[0]['date']}&till={window[-1]['date']}"
        out.append(item)
        print(f"{item['ticker']}: {window[0]['date']} — {window[59]['date']} + 30 sessions to {window[-1]['date']}")
        time.sleep(.2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(out)} rounds to {OUT}")


if __name__ == "__main__":
    main()
