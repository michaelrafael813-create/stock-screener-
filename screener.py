#!/usr/bin/env python3
"""
סורק מניות "Buy the Dip" איכותי — שוק אמריקאי, שווי שוק מעל 2 מיליארד $.

מקורות (חינמיים):
  * רשימת מניות ושווי שוק: Nasdaq screener
  * דוחות כספיים: SEC (XBRL companyfacts) — רשמי, היסטוריה מלאה
  * מחירים: Yahoo Finance (yfinance)

הרצה: python screener.py
  FULL=true      — מכריח רענון של כל הדוחות (אחרת פעם ביום)
  MAX_TICKERS=50 — לבדיקה מהירה על מעט מניות
"""
import os, sys, json, time, math, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

# ===================================================================
#  הגדרות — כאן משנים ספים בלי לגעת בשאר הקוד
# ===================================================================
CONFIG = {
    "min_market_cap": 1_000_000_000,
    "years": 5,                 # תקופת מדידה בסיסית
    "recent_years": 3,          # בדיקת מגמה טרייה
    # איכות
    "rev_growth_min": 0.08, "rev_growth_good": 0.10,
    "eps_growth_min": 0.10,
    "fcf_growth_min": 0.10,
    "roic_min": 0.15,
    "nd_ebitda_max": 2.0,
    "int_cov_min": 8.0,
    "dilution_max_3y": 0.02,    # עד 2% עלייה במספר המניות ב-3 שנים
    "margin_tolerance": 0.015,  # "יציב" = לא ירד יותר מ-1.5 נק' אחוז
    # תמחור
    "pe_vs_hist_good": -0.15,   # 15% מתחת לממוצע ההיסטורי של החברה
    "pfcf_max": 20, "ev_ebitda_max": 14, "ev_ebit_max": 18,
    "peg_max": 1.5,
    "mos_min": 0.20, "mos_good": 0.30,
    "discount_rate": 0.10, "terminal_growth": 0.03, "dcf_growth_cap": 0.15,
    # ירידה
    "dip_min": 0.20, "dip_good": 0.30,
    # משקלות הציון
    "w_quality": 0.40, "w_value": 0.35, "w_dip": 0.25,
    "red_flag_penalty": 10,
    "fundamentals_max_age_hours": 20,
    # תעודות סל וקריפטו (רק ללשונית התבניות)
    "etf_min_dollar_volume": 5_000_000,   # מחזור יומי ממוצע מינימלי
    "exclude_leveraged_etfs": True,       # בלי ממונפות והפוכות
}

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

COINS = {
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "XRP-USD": "XRP", "BNB-USD": "BNB",
    "ADA-USD": "Cardano", "DOGE-USD": "Dogecoin", "AVAX-USD": "Avalanche", "LINK-USD": "Chainlink",
    "DOT-USD": "Polkadot", "LTC-USD": "Litecoin", "BCH-USD": "Bitcoin Cash", "XLM-USD": "Stellar",
    "TRX-USD": "TRON", "ATOM-USD": "Cosmos", "NEAR-USD": "NEAR Protocol", "UNI-USD": "Uniswap",
    "AAVE-USD": "Aave", "HBAR-USD": "Hedera", "ETC-USD": "Ethereum Classic", "ICP-USD": "Internet Computer",
    "FIL-USD": "Filecoin", "ALGO-USD": "Algorand",
}

# גיבוי למקרה ש-Nasdaq לא עונה: תעודות סל מרכזיות
ETF_FALLBACK = """SPY QQQ IWM DIA VTI VOO IVV RSP MDY IJH IJR VB VO VUG VTV IWF IWD SCHD SCHG SCHX VIG VYM DGRO NOBL
XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC SMH SOXX IGV SKYY CIBR HACK BOTZ ARKK ARKG ARKW TAN ICLN LIT URA
XBI IBB IHI KRE KBE XHB ITB XRT IYT JETS XOP OIH XME GDX GDXJ SIL COPX PAVE MOO IGF
EFA EEM VEA VWO IEFA IEMG EWJ EWZ EWG EWU EWC EWY EWT EWA INDA FXI KWEB MCHI EWW EZU VGK ACWI
GLD IAU SLV PPLT USO UNG DBC DBA PDBC CPER
TLT IEF SHY AGG BND LQD HYG JNK TIP EMB MUB BIL SGOV
IBIT FBTC ARKB BITB GBTC BITO ETHA FETH ETHE""".split()


def is_leveraged(name):
    import re
    return bool(re.search(r"(\b[23]x\b|-[123]x\b|ultrashort|ultra(?!\s*-?\s*short)|inverse|\bshort\b(?!\s*-?\s*(term|duration|maturity|dated|income|bond|treasury))|\bbear\b|leveraged|daily .*(bull|bear))", name, re.I))


def is_crypto_name(name):
    import re
    return bool(re.search(r"(bitcoin|\bether\b|ethereum|solana|\bxrp\b|crypto|litecoin|dogecoin|avalanche|chainlink|cardano|digital asset)", name, re.I))

# ה-SEC דורש שם ומייל אמיתיים של מי שמפעיל את הסורק
CONTACT_NAME = "Michael Rafael"
CONTACT_EMAIL = os.environ.get("SEC_CONTACT") or "mikeyrafale480@gmail.com"
SEC = requests.Session()
SEC.headers.update({"User-Agent": f"{CONTACT_NAME} {CONTACT_EMAIL}", "Accept-Encoding": "gzip, deflate"})


def sec_json(url, tries=4):
    """בקשה ל-SEC עם ניסיונות חוזרים והודעת שגיאה ברורה."""
    last = ""
    for attempt in range(tries):
        try:
            r = SEC.get(url, timeout=60)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last = f"תשובה לא תקינה: {r.text[:200]!r}"
            elif r.status_code == 404:
                return None
            else:
                last = f"קוד {r.status_code}: {r.text[:200]!r}"
        except Exception as e:
            last = repr(e)
        time.sleep(3 + attempt * 5)
    raise RuntimeError(f"ה-SEC לא ענה עבור {url} — {last}")

VERSION = "2.0"
FULL = os.environ.get("FULL", "").lower() == "true"
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "0") or 0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ===================================================================
#  1. רשימת המניות
# ===================================================================
def fetch_universe():
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    headers = NASDAQ_HEADERS
    cache = DATA / "universe.json"
    try:
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        rows = r.json()["data"]["rows"]
        log(f"Nasdaq: {len(rows)} מניות")
    except Exception as e:
        log("שגיאה בטעינת רשימת המניות מ-Nasdaq:", e)
        if cache.exists():
            log("משתמש ברשימה השמורה מהריצה הקודמת")
            return json.loads(cache.read_text())
        raise

    def num(x):
        try:
            return float(str(x).replace("$", "").replace(",", ""))
        except Exception:
            return None

    tick = sec_json("https://www.sec.gov/files/company_tickers.json")
    cik_of = {v["ticker"].upper(): int(v["cik_str"]) for v in tick.values()}

    by_cik = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym or "^" in sym:
            continue
        sym = sym.replace("/", "-")
        cap = num(row.get("marketCap"))
        last = num(row.get("lastsale"))
        if not cap or cap < CONFIG["min_market_cap"] or not last:
            continue
        cik = cik_of.get(sym)
        if not cik:
            continue
        item = {"t": sym, "cik": cik, "name": row.get("name", "").replace(" Common Stock", "").replace(" Class A", "").strip(),
                "sector": row.get("sector") or "", "industry": row.get("industry") or "",
                "cap": cap, "last": last}
        # מניה אחת לכל חברה (למשל GOOG / GOOGL)
        if cik not in by_cik or cap > by_cik[cik]["cap"]:
            by_cik[cik] = item
    uni = sorted(by_cik.values(), key=lambda x: -x["cap"])
    cache.write_text(json.dumps(uni))
    log(f"ביקום: {len(uni)} חברות מעל {CONFIG['min_market_cap']/1e9:.0f} מיליארד $")
    return uni


def fetch_etf_universe():
    """רשימת תעודות סל מ-Nasdaq. מחזיר [{t, name, crypto}]."""
    cache = DATA / "etfs.json"
    rows = []
    try:
        r = requests.get("https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=10000&download=true",
                         headers=NASDAQ_HEADERS, timeout=60)
        js = r.json().get("data") or {}
        rows = (js.get("data") or {}).get("rows") or js.get("rows") or []
    except Exception as e:
        log("שגיאה בטעינת רשימת תעודות הסל:", e)
    out = []
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        name = (row.get("companyName") or row.get("name") or sym).strip()
        if not sym or "^" in sym or "/" in sym or "." in sym:
            continue
        out.append({"t": sym, "name": name})
    if len(out) > 200:
        cache.write_text(json.dumps(out))
        log(f"Nasdaq: {len(out)} תעודות סל")
    elif cache.exists():
        out = json.loads(cache.read_text())
        log(f"משתמש ברשימת תעודות סל שמורה ({len(out)})")
    else:
        out = [{"t": t, "name": t} for t in ETF_FALLBACK]
        log(f"משתמש ברשימת הגיבוי ({len(out)} תעודות)")
    res = []
    for e in out:
        if CONFIG["exclude_leveraged_etfs"] and is_leveraged(e["name"]):
            continue
        e["crypto"] = is_crypto_name(e["name"]) or e["t"] in ("IBIT", "FBTC", "ARKB", "BITB", "GBTC", "BITO", "ETHA", "FETH", "ETHE")
        res.append(e)
    return res


# ===================================================================
#  2. דוחות כספיים מה-SEC
# ===================================================================
FLOW = {
    "rev": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"],
    "ni": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "opinc": ["OperatingIncomeLoss"],
    "gp": ["GrossProfit"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "int": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
INSTANT = {
    "debt_total": ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
    "debt_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_cur": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "DebtCurrent"],
    "st_borrow": ["ShortTermBorrowings", "CommercialPaper"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
UNIT = {"eps": "USD/shares", "shares": "shares"}

# חברות זרות שמדווחות ל-SEC בתקן הבינלאומי (IFRS) — טפסי 20-F / 40-F
FLOW_IFRS = {
    "rev": ["Revenue", "RevenueFromContractsWithCustomers"],
    "ni": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare"],
    "opinc": ["ProfitLossFromOperatingActivities"],
    "gp": ["GrossProfit"],
    "cogs": ["CostOfSales"],
    "ocf": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment"],
    "da": ["DepreciationAndAmortisationExpense", "AdjustmentsForDepreciationAndAmortisationExpense",
           "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"],
    "int": ["InterestExpense", "FinanceCosts"],
    "tax": ["IncomeTaxExpenseContinuingOperations"],
    "pretax": ["ProfitLossBeforeTax"],
    "shares": ["AdjustedWeightedAverageShares", "WeightedAverageShares"],
}
INSTANT_IFRS = {
    "debt_total": ["Borrowings"],
    "debt_nc": ["LongtermBorrowings", "NoncurrentPortionOfNoncurrentBorrowings"],
    "debt_cur": ["CurrentPortionOfLongtermBorrowings", "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
    "st_borrow": ["ShorttermBorrowings"],
    "cash": ["CashAndCashEquivalents"],
    "sti": ["ShorttermDepositsNotClassifiedAsCashEquivalents"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
}
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
EXTRACT_VERSION = 2
MONEY_KEYS = ("rev", "ni", "eps", "opinc", "gp", "cogs", "ocf", "capex", "da", "int", "tax", "pretax", "debt", "cash", "equity")


def is_annual(form):
    return form.startswith(ANNUAL_FORMS)


def d(s):
    return dt.date.fromisoformat(s)


def collect(facts, tags, unit, tax="us-gaap"):
    out = []
    g = facts.get("facts", {}).get(tax, {})
    for prio, tag in enumerate(tags):
        node = g.get(tag)
        if not node:
            continue
        for f in node.get("units", {}).get(unit, []):
            form = f.get("form", "")
            if not (is_annual(form) or form.startswith("10-Q")):
                continue
            out.append((prio, f.get("start"), f["end"], f["val"], f.get("filed", ""), form))
    return out


def annual_flow(entries):
    best = {}
    for prio, s, e, v, filed, form in entries:
        if not s or not is_annual(form):
            continue
        if not 330 <= (d(e) - d(s)).days <= 400:
            continue
        cur = best.get(e)
        if cur is None or (prio, -int(filed.replace("-", "") or 0)) < (cur[0], -int(cur[1].replace("-", "") or 0)):
            best[e] = (prio, filed, v)
    return {e: x[2] for e, x in best.items()}


def ttm_flow(entries, ann):
    """TTM = שנה אחרונה + מצטבר השנה הנוכחית − מצטבר אשתקד באותה תקופה.
    מחזיר גם את קצב השינוי מול אשתקד (YTD מול YTD, או שנה מול שנה)."""
    if not ann:
        return None, None, None
    last_fy = max(ann)
    lf = d(last_fy)
    fys = sorted(ann)
    ann_yoy = (ann[fys[-1]] / ann[fys[-2]] - 1) if len(fys) > 1 and ann[fys[-2]] and ann[fys[-2]] > 0 else None
    tags = sorted({x[0] for x in entries})
    for p in tags:
        ent = [x for x in entries if x[0] == p and x[1]]
        a = {e: v for (pp, s, e, v, f, fm) in ent if is_annual(fm) and 330 <= (d(e) - d(s)).days <= 400}
        if last_fy not in a:
            continue
        ytd = [x for x in ent if x[5].startswith("10-Q") and abs((d(x[1]) - lf).days - 1) <= 12 and (d(x[2]) - lf).days > 60]
        if not ytd:
            return a[last_fy], last_fy, ann_yoy
        cur = max(ytd, key=lambda x: (x[2], x[4]))
        dur = (d(cur[2]) - d(cur[1])).days
        prior = [x for x in ent if abs((d(cur[2]) - d(x[2])).days - 365) <= 14 and abs((d(x[2]) - d(x[1])).days - dur) <= 14]
        if not prior:
            continue
        pr = max(prior, key=lambda x: x[4])
        yoy = (cur[3] / pr[3] - 1) if pr[3] and pr[3] > 0 else None
        return a[last_fy] + cur[3] - pr[3], cur[2], yoy
    return ann[last_fy], last_fy, ann_yoy


def instants(entries, annual_only):
    best = {}
    for prio, s, e, v, filed, form in entries:
        if s:
            continue
        if annual_only and not is_annual(form):
            continue
        key = (prio, -int(filed.replace("-", "") or 0))
        cur = best.get(e)
        if cur is None or key < cur[0]:
            best[e] = (key, v)
    return {e: x[1] for e, x in best.items()}


def near(series, date, tol=20):
    if not series:
        return None
    t = d(date)
    best, bd = None, tol + 1
    for e, v in series.items():
        dd = abs((d(e) - t).days)
        if dd < bd:
            best, bd = v, dd
    return best


def latest(series):
    if not series:
        return None, None
    e = max(series)
    return series[e], e


def detect_reporting(facts):
    """איזה תקן (US-GAAP / IFRS) ובאיזה מטבע החברה מדווחת."""
    best = None
    for tax, tags in (("us-gaap", FLOW["rev"]), ("ifrs-full", FLOW_IFRS["rev"])):
        g = facts.get("facts", {}).get(tax, {})
        for tag in tags:
            for unit, arr in g.get(tag, {}).get("units", {}).items():
                if "/" in unit or len(unit) != 3:
                    continue
                n = sum(1 for f in arr if is_annual(f.get("form", "")) and f.get("start"))
                if n and (best is None or n > best[2]):
                    best = (tax, unit, n)
    return (best[0], best[1]) if best else ("us-gaap", "USD")


def extract(facts):
    """מחזיר סדרות שנתיות + TTM + מאזן אחרון, בפורמט קומפקטי לשמירה."""
    tax, ccy = detect_reporting(facts)
    FL, IN = (FLOW, INSTANT) if tax == "us-gaap" else (FLOW_IFRS, INSTANT_IFRS)
    units = {"eps": f"{ccy}/shares", "shares": "shares"}
    flows, ttm, yoy, ttm_end = {}, {}, {}, None
    for k, tags in FL.items():
        ent = collect(facts, tags, units.get(k, ccy), tax)
        ann = annual_flow(ent)
        flows[k] = ann
        if k in ("rev", "ni", "eps", "opinc", "ocf", "capex", "da", "int", "gp", "cogs"):
            v, end, chg = ttm_flow(ent, ann)
            ttm[k], yoy[k] = v, chg
            if k == "rev":
                ttm_end = end
    if len(flows["rev"]) < 3:
        return None
    fy = sorted(flows["rev"])[-7:]

    inst_a, inst_l = {}, {}
    for k, tags in IN.items():
        ent = collect(facts, tags, ccy, tax)
        inst_a[k] = instants(ent, True)
        inst_l[k] = instants(ent, False)

    def debt_at(src, date):
        tot = near(src["debt_total"], date)
        if tot is None:
            nc, cu = near(src["debt_nc"], date), near(src["debt_cur"], date)
            tot = (nc or 0) + (cu or 0) if (nc is not None or cu is not None) else 0
        return tot + (near(src["st_borrow"], date) or 0)

    def cash_at(src, date):
        return (near(src["cash"], date) or 0) + (near(src["sti"], date) or 0)

    series = {"fy": fy}
    for k in FL:
        series[k] = [near(flows[k], e, 10) for e in fy]
    series["debt"] = [debt_at(inst_a, e) for e in fy]
    series["cash"] = [cash_at(inst_a, e) for e in fy]
    series["equity"] = [near(inst_a["equity"], e) for e in fy]

    def last_date(keys):
        ds = [max(inst_l[k]) for k in keys if inst_l[k]]
        return max(ds) if ds else fy[-1]
    d_debt = last_date(["debt_total", "debt_nc", "debt_cur"])
    d_cash = last_date(["cash"])
    d_eq = last_date(["equity"])
    bal = {"date": max(d_debt, d_cash, d_eq), "debt": debt_at(inst_l, d_debt), "cash": cash_at(inst_l, d_cash),
           "equity": near(inst_l["equity"], d_eq)}
    return {"s": series, "ttm": ttm, "yoy": yoy, "ttm_end": ttm_end, "bal": bal, "tax": tax, "ccy": ccy}


def refresh_fundamentals(universe):
    path = DATA / "fundamentals.json"
    old = json.loads(path.read_text()) if path.exists() else {"updated": None, "by_ticker": {}}
    age_h = 1e9
    if old.get("updated"):
        age_h = (dt.datetime.utcnow() - dt.datetime.fromisoformat(old["updated"])).total_seconds() / 3600
    def needs(t):
        x = old["by_ticker"].get(t)
        if x is None:
            return True
        if x.get("none"):  # לא נמצאו נתונים — מנסים שוב פעם בשבוע או כשהפענוח שודרג
            return x.get("xv") != EXTRACT_VERSION or (dt.date.today() - dt.date.fromisoformat(x["date"])).days >= 7
        return False
    missing = [u for u in universe if needs(u["t"])]
    if not FULL and age_h < CONFIG["fundamentals_max_age_hours"] and not missing:
        log(f"דוחות עדכניים ({age_h:.1f} שעות) — מדלג על רענון")
        return {k: v for k, v in old["by_ticker"].items() if not v.get("none")}
    todo = universe if (FULL or age_h >= CONFIG["fundamentals_max_age_hours"]) else missing
    log(f"מוריד דוחות מה-SEC עבור {len(todo)} חברות...")
    out = dict(old["by_ticker"])
    for i, u in enumerate(todo):
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{u['cik']:010d}.json"
        try:
            facts = sec_json(url, tries=3)
        except RuntimeError as e:
            facts = None
            if i < 3:
                log("  ", e)
        time.sleep(0.12)  # מגבלת ה-SEC: עד 10 בקשות בשנייה
        x = None
        if facts:
            try:
                x = extract(facts)
            except Exception as e:
                log("  שגיאה בפענוח", u["t"], e)
        if x:
            out[u["t"]] = x
        elif u["t"] not in out or out[u["t"]].get("none"):
            out[u["t"]] = {"none": True, "date": dt.date.today().isoformat(), "xv": EXTRACT_VERSION}
        if (i + 1) % 100 == 0:
            log(f"  {i + 1}/{len(todo)}")
    path.write_text(json.dumps(clean({"updated": dt.datetime.utcnow().isoformat(), "by_ticker": out}), separators=(",", ":")))
    return {k: v for k, v in out.items() if not v.get("none")}


# ===================================================================
#  3. מחירים
# ===================================================================
HL = {}  # מחיר גבוה/נמוך יומי — למיקום הסגירה בטווח היום


def fetch_prices(tickers, period="5y"):
    """מחזיר (מחירי סגירה, מחזורי מסחר) לכל מניה."""
    import yfinance as yf
    out, vols = {}, {}
    for i in range(0, len(tickers), 80):
        chunk = tickers[i:i + 80]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1d", auto_adjust=False, progress=False,
                                 threads=True, group_by="ticker")
                break
            except Exception as e:
                log("  שגיאת מחירים, מנסה שוב:", e)
                time.sleep(10 * (attempt + 1))
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                s = df[t]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
                s = s.dropna()
                if getattr(s.index, "tz", None) is not None:
                    s.index = s.index.tz_localize(None)
                if len(s) > 60:
                    out[t] = s
                    try:
                        v = (df[t]["Volume"] if isinstance(df.columns, pd.MultiIndex) else df["Volume"]).reindex(s.index)
                        if getattr(v.index, "tz", None) is not None:
                            v.index = v.index.tz_localize(None)
                        vols[t] = v.fillna(0)
                    except Exception:
                        pass
                    try:
                        sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                        hh, ll = sub["High"].reindex(s.index), sub["Low"].reindex(s.index)
                        HL[t] = (hh.values[-160:].astype(float), ll.values[-160:].astype(float))
                    except Exception:
                        pass
            except Exception:
                pass
        time.sleep(1.5)
        log(f"  מחירים: {min(i + 80, len(tickers))}/{len(tickers)}")
    return out, vols


# ===================================================================
#  3ב. בורסות זרות (רק ללשונית התבניות) + שערי מטבע
# ===================================================================
INTL = {
    "TA": {"n": "תל אביב", "sfx": ".TA", "ccy": "אג'", "url": "https://en.wikipedia.org/wiki/TA-125_Index", "col": r"symbol|ticker",
           "fb": "TEVA LUMI POLI DSCT MZTF FIBI NICE ICL ESLT BEZQ AZRG TSEM NVMI CAMT PHOE HARL CLIS ORL DLEKG ENLT ALHE AMOT BIG MLSR SPEN OPCE STRS SAE FOX ELCO ELAL ENRG NWMD PZOL MGDL"},
    "L": {"n": "לונדון", "sfx": ".L", "ccy": "פני", "url": "https://en.wikipedia.org/wiki/FTSE_100_Index", "col": r"ticker|epic|symbol",
          "fb": "AZN SHEL HSBA ULVR BP RIO GSK REL DGE BATS LSEG GLEN AAL NG LLOY BARC VOD PRU TSCO CPG RKT EXPN BA IMB SSE NWG STAN III AHT HLN BT-A ABF AV LGEN SGE WPP IHG RR SMIN SPX MNDI BNZL INF ITRK HL LAND BLND SGRO PSON KGF SBRY NXT JD AUTO RTO SDR PSN BDEV TW MKS FRES ANTO ENT WTB SMT ADM HIK BKG CNA UU SVT DCC SN ICG IMI HLMA"},
    "DE": {"n": "גרמניה", "sfx": ".DE", "ccy": "€", "url": "https://en.wikipedia.org/wiki/DAX", "col": r"ticker|symbol",
           "fb": "SAP SIE ALV DTE AIR MBG MUV2 BAS BMW IFX DHL DB1 BAYN ADS VOW3 RWE DBK EOAN HEN3 MRK CBK SHL HEI DTG BEIR CON ENR FRE FME HNR1 MTX PAH3 P911 QIA RHM SRT3 SY1 VNA ZAL"},
    "PA": {"n": "צרפת", "sfx": ".PA", "ccy": "€", "url": "https://en.wikipedia.org/wiki/CAC_40", "col": r"ticker|symbol",
           "fb": "MC OR RMS TTE SAN AI SU AIR BNP SAF CS EL DG KER RI BN ACA GLE ENGI CAP ORA VIE LR PUB SGO STLAP ML HO STMPA ERF ALO DSY TEP URW EN VIV AC"},
    "T": {"n": "יפן", "sfx": ".T", "ccy": "¥", "url": "https://en.wikipedia.org/wiki/Nikkei_225", "rx": r"(?:TYO|TSE)\s*:\s*(\d{4})",
          "fb": "7203 6758 9984 6861 8306 9432 6098 8035 9983 4063 6501 7974 8058 8031 8001 4502 6367 6954 7267 6902 8316 8411 9433 4519 4568 6594 7741 6981 6273 4661 2914 3382 7011 6146 6857 8766 6503 4543 6702 6752 7751 5108 9020 9022 8801 8802 4901 6301 7201 6762 2802 4452 6723 7733"},
    "TO": {"n": "קנדה", "sfx": ".TO", "ccy": "C$", "url": "https://en.wikipedia.org/wiki/S%26P/TSX_60", "col": r"symbol|ticker",
           "fb": "RY TD ENB SHOP CNR CP BMO BNS CM BN TRI CNQ SU ATD MFC WCN CSU NTR FTS IMO TRP L SLF IFC GIB-A AEM ABX WPM FNV CCO TECK-B BCE T MRU DOL QSR NA POW CVE GWO X EMA H CAE CCL-B CTC-A FM K OTEX RCI-B SAP WN TOU BAM CU"},
    "HK": {"n": "הונג קונג", "sfx": ".HK", "ccy": "HK$", "url": "https://en.wikipedia.org/wiki/Hang_Seng_Index", "rx": r"SEHK\s*:\s*(\d{1,5})",
           "fb": "0700 9988 3690 0005 1299 0939 1398 3988 0941 0388 2318 1810 9618 1211 0883 0857 0386 2388 0011 0016 0001 1113 0002 0003 0006 0027 1928 2020 2319 0291 1109 0688 1093 2269 9999 9888 1024 2015 9868 9961 0981 0992 2628 2382 0669 0066 0823 0267 0960 1088 2899 0175 6618 0241 1876"},
    "CN": {"n": "סין", "sfx": "", "ccy": "CN¥", "url": "https://en.wikipedia.org/wiki/CSI_300_Index", "rx": r"(SSE|SZSE)\s*:\s*(\d{6})",
           "fb": "600519 300750 601318 600036 000858 601166 000333 600276 601012 002594 600900 601398 601288 601988 600030 000651 002415 300059 600887 601899 600309 000568 600809 002475 300760 601888 600028 601857 600050 601728 600941 000001 002714 300124 603259 688981"},
}
WIKI_UA = {"User-Agent": "Mozilla/5.0 (personal stock screener; contact via GitHub)"}


def _wiki_tables(html):
    import re, html as H
    out = []
    for tb in re.findall(r"<table[^>]*wikitable[^>]*>(.*?)</table>", html, re.S):
        grid = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
            grid.append([H.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells])
        out.append(grid)
    return out


def _norm(ex, raw):
    import re
    x = raw.strip().upper()
    if ex == "T":
        return x + ".T" if re.fullmatch(r"\d{4}", x) else None
    if ex == "HK":
        return x.zfill(4) + ".HK" if re.fullmatch(r"\d{1,5}", x) else None
    if ex == "CN":
        if not re.fullmatch(r"\d{6}", x):
            return None
        return x + (".SS" if x[0] in "69" else ".SZ")
    x = re.sub(r"\.(L|DE|PA|TO|TA)$", "", x).replace(".", "-")
    x = x.split()[0] if x else x
    return x + INTL[ex]["sfx"] if re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,7}", x or "") else None


def intl_tickers():
    import re
    res = {}
    for ex, cfg in INTL.items():
        syms = []
        try:
            html = requests.get(cfg["url"], headers=WIKI_UA, timeout=30).text
            if "rx" in cfg:
                for m in re.findall(cfg["rx"], html):
                    if isinstance(m, tuple):
                        m = m[1]
                    syms.append(_norm(ex, m))
            if "col" in cfg or len(syms) < 10:
                for grid in _wiki_tables(html):
                    if not grid:
                        continue
                    hdr = [h.lower() for h in grid[0]]
                    idx = [i for i, h in enumerate(hdr) if re.search(cfg.get("col", r"code|ticker|symbol"), h)]
                    if not idx:
                        continue
                    for row in grid[1:]:
                        if len(row) > idx[0]:
                            syms.append(_norm(ex, row[idx[0]]))
        except Exception as e:
            log(f"  {cfg['n']}: לא הצלחתי לקרוא מוויקיפדיה ({e})")
        syms = list(dict.fromkeys(x for x in syms if x))
        fb = [_norm(ex, x) for x in cfg["fb"].split()]
        if len(syms) < len(fb) * 0.6:
            log(f"  {cfg['n']}: משתמש ברשימת הגיבוי")
            syms = list(dict.fromkeys(syms + [x for x in fb if x]))
        for t in syms:
            res[t] = ex
        log(f"  {cfg['n']}: {len(syms)} מניות")
    return res


def fetch_fx(ccys):
    """כמה דולר שווה יחידה אחת של כל מטבע."""
    import yfinance as yf
    fx = {"USD": 1.0}
    for c in ccys:
        if c in fx:
            continue
        for sym, inv in ((f"{c}USD=X", False), (f"USD{c}=X", True)):
            try:
                h = yf.download(sym, period="1mo", progress=False, auto_adjust=False)
                col = h["Close"]
                v = float((col.iloc[:, 0] if hasattr(col, "columns") else col).dropna().iloc[-1])
                if v > 0:
                    fx[c] = 1 / v if inv else v
                    break
            except Exception:
                pass
        if c not in fx:
            log(f"  אין שער עבור {c} — חברות במטבע הזה יידלגו")
    return fx


# ===================================================================
#  3ג. מספר עסקאות יומי (Massive / Polygon לשעבר) — מניות ותעודות בארה"ב
# ===================================================================
MASSIVE_KEY = os.environ.get("MASSIVE_API_KEY", "").strip()


def fetch_transactions(dates, wanted):
    """שומר ווליום ומספר עסקאות לכל יום מסחר. קריאה אחת ליום מחזירה את כל השוק.
    בתוכנית החינמית: 5 קריאות לדקה, ולכן ממתינים ~13 שניות בין קריאות."""
    path = DATA / "trades.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    if not MASSIVE_KEY:
        log("אין מפתח MASSIVE_API_KEY — מדלג על נתוני עסקאות")
        return cache
    todo = [x for x in dates[-60:] if x not in cache][-70:]
    if todo:
        log(f"מוריד נתוני עסקאות עבור {len(todo)} ימי מסחר (כ-{len(todo) * 13 // 60 + 1} דקות)...")
    for i, day in enumerate(todo):
        url = f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{day}?adjusted=true&apiKey={MASSIVE_KEY}"
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=60)
                if r.status_code == 429:
                    time.sleep(65)
                    continue
                if r.status_code in (401, 403):
                    log("  מפתח Massive לא תקין או חסרה הרשאה — מדלג")
                    return cache
                js = r.json()
                res = js.get("results") or []
                if res:
                    cache[day] = {x["T"]: [x.get("v"), x.get("n")] for x in res if x.get("T") in wanted}
                break
            except Exception as e:
                log("  שגיאת עסקאות:", e)
                time.sleep(15)
        if i < len(todo) - 1:
            time.sleep(13)
    keep = sorted(cache)[-70:]
    cache = {k: cache[k] for k in keep}
    path.write_text(json.dumps(cache, separators=(",", ":")))
    return cache


def tx_stats(t, close, trades):
    """מספר עסקאות ממוצע, גודל עסקה ממוצע, והשינוי בו."""
    days = sorted(trades)
    rows = [(d, trades[d].get(t)) for d in days]
    rows = [(d, x[0], x[1]) for d, x in rows if x and x[0] and x[1]]
    if len(rows) < 25:
        return None
    n = np.array([x[2] for x in rows], float)
    v = np.array([x[1] for x in rows], float)
    ats = v / n
    a10, a_prev = float(np.mean(ats[-10:])), float(np.mean(ats[-30:-10]))
    n10, n_prev = float(np.mean(n[-10:])), float(np.mean(n[-30:-10]))
    return {"n20": int(np.mean(n[-20:])), "ats": round(a10, 1),
            "ats_chg": round(a10 / a_prev - 1, 3) if a_prev else None,
            "n_chg": round(n10 / n_prev - 1, 3) if n_prev else None}


# ===================================================================
#  4. חישוב מדדים וציון
# ===================================================================
def cagr(a, b, years):
    if a is None or b is None or a <= 0 or b <= 0 or years <= 0:
        return None
    return (b / a) ** (1 / years) - 1


def window(vals, n):
    """n+1 נקודות אחרונות שאינן ריקות (לחישוב צמיחה על פני n שנים)."""
    v = [x for x in vals if x is not None]
    return v[-(n + 1):]


def safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def pct(x, digits=0):
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def num(x, digits=1):
    return "—" if x is None else f"{x:.{digits}f}"


def rsi(s, n=14):
    delta = s.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def technical(s):
    price = float(s.iloc[-1])
    sma50 = s.rolling(50).mean()
    sma200 = s.rolling(200).mean()
    slope50 = float(sma50.iloc[-1] - sma50.iloc[-11]) if len(s) > 60 else 0
    r = rsi(s)
    recent_low = float(s.iloc[-10:].min())
    low60 = float(s.iloc[-60:].min())
    if price > sma50.iloc[-1] and slope50 > 0:
        state = "up"
    elif price < sma50.iloc[-1] and slope50 < 0 and recent_low <= low60 * 1.005:
        state = "down"
    else:
        state = "flat"
    return {"state": state, "rsi": round(r, 1) if math.isfinite(r) else None, "sma50": round(float(sma50.iloc[-1]), 2),
            "sma200": round(float(sma200.iloc[-1]), 2) if len(s) >= 200 else None,
            "above200": bool(len(s) >= 200 and price > sma200.iloc[-1])}


def check(label, status, value, weight):
    """status: 1 עובר, 0.5 חלקי, 0 נכשל, None אין נתון."""
    return {"l": label, "s": status, "v": value, "w": weight}


def section_score(checks):
    w = sum(c["w"] for c in checks if c["s"] is not None)
    if w == 0:
        return None
    return round(100 * sum(c["w"] * c["s"] for c in checks if c["s"] is not None) / w, 1)


def to_usd(F, fx):
    """ממיר את כל הסכומים לדולר בשער אחד — שומר על יחסי הצמיחה כמו שהם."""
    if fx == 1:
        return F
    mul = lambda v: None if v is None else v * fx
    S = {k: ([mul(x) for x in v] if k in MONEY_KEYS else v) for k, v in F["s"].items()}
    T = {k: (mul(v) if k in MONEY_KEYS else v) for k, v in F["ttm"].items()}
    B = {k: (mul(v) if k in MONEY_KEYS else v) for k, v in F["bal"].items()}
    return {**F, "s": S, "ttm": T, "bal": B}


def compute(u, F, s, fx=1.0):
    C = CONFIG
    F = to_usd(F, fx)
    S, T, B = F["s"], F["ttm"], F["bal"]
    fy = S["fy"]
    n = C["years"]
    price = float(s.iloc[-1])
    cap = u["cap"] * price / u["last"] if u.get("last") else u["cap"]

    rev, ni, eps, opi = S["rev"], S["ni"], S["eps"], S["opinc"]
    ocf, capex, da = S["ocf"], S["capex"], S["da"]
    fcf = [None if o is None else o - (c or 0) for o, c in zip(ocf, capex)]

    def margin(a, b):
        return [safe_div(x, y) for x, y in zip(a, b)]

    gp = [g if g is not None else (r - c if r is not None and c is not None else None) for g, r, c in zip(S["gp"], rev, S["cogs"])]
    gm, om, fm = margin(gp, rev), margin(opi, rev), margin(fcf, rev)

    def trend_ok(m):
        v = [x for x in m if x is not None]
        if len(v) < 3:
            return None, None
        delta = v[-1] - np.mean(v[-4:-1])
        st = 1 if delta >= -C["margin_tolerance"] else (0.5 if delta >= -2 * C["margin_tolerance"] else 0)
        return st, delta

    w_rev, w_eps, w_fcf = window(rev, n), window(eps, n), window(fcf, n)
    rev_g = cagr(w_rev[0], w_rev[-1], len(w_rev) - 1) if len(w_rev) >= 3 else None
    eps_g = cagr(w_eps[0], w_eps[-1], len(w_eps) - 1) if len(w_eps) >= 3 else None
    fcf_g = cagr(w_fcf[0], w_fcf[-1], len(w_fcf) - 1) if len(w_fcf) >= 3 else None
    r3 = window(rev, C["recent_years"])
    rev_g3 = cagr(r3[0], r3[-1], len(r3) - 1) if len(r3) >= 3 else None

    # TTM ומאזן
    rev_t, ni_t, eps_t, op_t = T.get("rev"), T.get("ni"), T.get("eps"), T.get("opinc")
    prev_eps_fy = eps[-1] if F.get("ttm_end") != fy[-1] else (eps[-2] if len(eps) > 1 else None)
    fcf_t = None if T.get("ocf") is None else T["ocf"] - (T.get("capex") or 0)
    ebitda_t = None if op_t is None else op_t + (T.get("da") or 0)
    debt, cash, eq = B.get("debt") or 0, B.get("cash") or 0, B.get("equity")
    net_debt = debt - cash
    tax_rates = [safe_div(t, p) for t, p in zip(S["tax"], S["pretax"]) if t is not None and p and p > 0]
    tax = min(max(tax_rates[-1], 0), 0.35) if tax_rates else 0.21
    ic = (debt + (eq or 0) - cash) if eq is not None else None
    roic = None
    if op_t is not None and ic is not None:
        roic = (op_t * (1 - tax) / ic) if ic > 0 else (9.99 if op_t > 0 else None)
    # ROIC לפני 3 שנים
    roic_old = None
    if len(fy) >= 4 and opi[-4] is not None and S["equity"][-4] is not None:
        ic_o = (S["debt"][-4] or 0) + S["equity"][-4] - (S["cash"][-4] or 0)
        if ic_o > 0:
            roic_old = opi[-4] * (1 - tax) / ic_o
    nd_ebitda = safe_div(net_debt, ebitda_t) if ebitda_t and ebitda_t > 0 else None
    int_t = T.get("int")
    int_cov = safe_div(op_t, int_t) if int_t and int_t > 0 else (99 if op_t and op_t > 0 else None)
    sh = [x for x in S["shares"] if x]
    sh3 = sh[-4:] if len(sh) >= 4 else sh
    sh_chg3 = (sh3[-1] / sh3[0] - 1) if len(sh3) >= 2 and sh3[0] else None
    if sh_chg3 is not None and abs(sh_chg3) > 0.6:  # פיצול מניות מעוות את ההשוואה
        sh_chg3 = None
    sh_chg1 = (sh[-1] / sh[-2] - 1) if len(sh) >= 2 and sh[-2] else None
    if sh_chg1 is not None and abs(sh_chg1) > 0.6:
        sh_chg1 = None

    gm_st, gm_d = trend_ok(gm)
    om_st, om_d = trend_ok(om)
    fm_st, fm_d = trend_ok(fm)
    fcf_pos = [x for x in fcf[-n:] if x is not None]

    def tier3(x, good, ok):
        if x is None:
            return None
        return 1 if x >= good else (0.5 if x >= ok else 0)

    Q = [
        check("צמיחת הכנסות (שנתי ממוצע)", tier3(rev_g, C["rev_growth_good"], C["rev_growth_min"]), pct(rev_g, 1), 3),
        check("צמיחת EPS", tier3(eps_g, C["eps_growth_min"], C["eps_growth_min"] * 0.6), pct(eps_g, 1), 3),
        check("FCF חיובי בכל השנים", None if not fcf_pos else (1 if all(x > 0 for x in fcf_pos) else 0),
              f"{sum(1 for x in fcf_pos if x > 0)}/{len(fcf_pos)} שנים", 2),
        check("צמיחת FCF", tier3(fcf_g, C["fcf_growth_min"], C["fcf_growth_min"] * 0.5), pct(fcf_g, 1), 2),
        check("שולי FCF יציבים/עולים", fm_st, "—" if fm_d is None else f"{fm_d * 100:+.1f} נק'", 1.5),
        check("ROIC", tier3(roic, C["roic_min"], 0.10), "מעל 100%" if roic and roic > 1 else pct(roic, 1), 3),
        check("Gross Margin יציב/עולה", gm_st, "—" if gm_d is None else f"{gm_d * 100:+.1f} נק'", 1.5),
        check("Operating Margin יציב/עולה", om_st, "—" if om_d is None else f"{om_d * 100:+.1f} נק'", 1.5),
        check("Net Debt / EBITDA", None if ebitda_t is None else (1 if net_debt <= 0 or (nd_ebitda is not None and nd_ebitda < C["nd_ebitda_max"]) else (0.5 if nd_ebitda is not None and nd_ebitda < 3 else 0)),
              "מזומן נטו" if net_debt <= 0 else num(nd_ebitda), 2),
        check("Interest Coverage", tier3(int_cov, C["int_cov_min"], 4), "ללא חוב משמעותי" if int_cov == 99 else num(int_cov), 1),
        check("ללא דילול (3 שנים)", None if sh_chg3 is None else (1 if sh_chg3 <= C["dilution_max_3y"] else (0.5 if sh_chg3 <= 0.05 else 0)),
              "—" if sh_chg3 is None else f"{sh_chg3 * 100:+.1f}%", 1.5),
    ]

    # ----- תמחור -----
    shares_now = cap / price
    pe = safe_div(cap, ni_t) if ni_t and ni_t > 0 else None
    pfcf = safe_div(cap, fcf_t) if fcf_t and fcf_t > 0 else None
    ev = cap + net_debt
    ev_ebitda = safe_div(ev, ebitda_t) if ebitda_t and ebitda_t > 0 else None
    ev_ebit = safe_div(ev, op_t) if op_t and op_t > 0 else None

    hist_pe, hist_pfcf, hist_eve = [], [], []
    start_i = len(fy) - min(n, len(fy))
    for i, e in enumerate(fy[-n:]):
        idx = start_i + i
        ts = pd.Timestamp(e)
        if ts < s.index[0]:
            continue
        p_then = float(s.asof(ts))
        cap_then = cap * p_then / price
        sh_then = S["shares"][idx]
        if sh_then and sh[-1] and 0.7 < sh_then / sh[-1] < 1.3:
            cap_then *= sh_then / sh[-1]
        if ni[idx] and ni[idx] > 0:
            hist_pe.append(cap_then / ni[idx])
        if fcf[idx] and fcf[idx] > 0:
            hist_pfcf.append(cap_then / fcf[idx])
        eb = (opi[idx] or 0) + (da[idx] or 0)
        if eb > 0:
            hist_eve.append((cap_then + (S["debt"][idx] or 0) - (S["cash"][idx] or 0)) / eb)
    med = lambda v: float(np.median(v)) if len(v) >= 2 else None
    pe_h, pfcf_h, eve_h = med(hist_pe), med(hist_pfcf), med(hist_eve)
    vs = lambda cur, h: (cur / h - 1) if cur and h else None
    pe_vs, pfcf_vs, eve_vs = vs(pe, pe_h), vs(pfcf, pfcf_h), vs(ev_ebitda, eve_h)
    # צמיחה שמרנית: הנמוכה מבין הממוצע ההיסטורי לבין הקצב הנוכחי
    rev_now = F.get("yoy", {}).get("rev")
    eps_now = (eps_t / prev_eps_fy - 1) if eps_t is not None and prev_eps_fy and prev_eps_fy > 0 else None
    eps_g_cons = min([x for x in (eps_g, eps_now) if x is not None], default=None)
    peg = safe_div(pe, eps_g_cons * 100) if pe and eps_g_cons and eps_g_cons > 0 else None
    peg_negative = pe is not None and eps_g_cons is not None and eps_g_cons <= 0

    # DCF פשוט ושמרני
    iv = mos = None
    g_in = [x for x in (rev_g, fcf_g) if x is not None]
    import re as _re
    is_fin = u["sector"] in ("Finance", "Real Estate") or bool(_re.search(
        r"bank|insur|financ|credit|savings|investment manag|broker|real estate|reit", u.get("industry") or "", _re.I))
    if fcf_t and fcf_t > 0 and g_in and not is_fin:
        avg3 = np.mean([x for x in fcf[-3:] if x is not None] or [fcf_t])
        base = min(fcf_t, avg3 * 1.5) if avg3 > 0 else fcf_t
        g_hist = float(np.mean(g_in))
        g = min(g_hist, rev_now) if rev_now is not None else g_hist
        g = min(max(g, 0), C["dcf_growth_cap"])
        r, tg = C["discount_rate"], C["terminal_growth"]
        pv, f = 0, base
        for y in range(1, 11):
            gy = g if y <= 5 else g + (tg - g) * (y - 5) / 5
            f *= 1 + gy
            pv += f / (1 + r) ** y
        pv += f * (1 + tg) / (r - tg) / (1 + r) ** 10
        iv = (pv - net_debt) / shares_now
        mos = 1 - price / iv if iv > 0 else None

    def cheap(cur, hist_vs, absmax):
        if cur is None:
            return None
        if hist_vs is not None and hist_vs <= C["pe_vs_hist_good"]:
            return 1
        if cur <= absmax:
            return 1 if hist_vs is None or hist_vs <= 0 else 0.5
        return 0.5 if hist_vs is not None and hist_vs <= 0 else 0

    hv = lambda h: "" if h is None else f" ({h * 100:+.0f}% מול ההיסטוריה)"
    V = [
        check("P/E מול ההיסטוריה של החברה", None if pe is None or pe_vs is None else (1 if pe_vs <= C["pe_vs_hist_good"] else (0.5 if pe_vs <= 0 else 0)),
              num(pe) + hv(pe_vs), 2),
        check("P/FCF", cheap(pfcf, pfcf_vs, C["pfcf_max"]), num(pfcf) + hv(pfcf_vs), 2),
        check("EV/EBITDA", cheap(ev_ebitda, eve_vs, C["ev_ebitda_max"]), num(ev_ebitda) + hv(eve_vs), 1.5),
        check("EV/EBIT", None if ev_ebit is None else (1 if ev_ebit <= C["ev_ebit_max"] else (0.5 if ev_ebit <= C["ev_ebit_max"] * 1.3 else 0)), num(ev_ebit), 1),
        check("PEG (לפי צמיחה שמרנית)", 0 if peg_negative else (None if peg is None else (1 if peg <= 1 else (0.5 if peg <= C["peg_max"] else 0))),
              "צמיחה שלילית כרגע" if peg_negative else num(peg, 2), 1.5),
        check("Margin of Safety (DCF שמרני)", None if mos is None else (0.5 if mos > 0.75 else (1 if mos >= C["mos_good"] else (0.5 if mos >= C["mos_min"] else 0))),
              pct(mos) + ("" if iv is None else f" (שווי פנימי ${iv:,.0f})"), 3),
    ]
    cheap_signals = sum(1 for c in V if c["s"] == 1)
    V.append(check("כמה מדדים מצביעים על זולות", 1 if cheap_signals >= 3 else (0.5 if cheap_signals == 2 else 0), f"{cheap_signals} מדדים", 1.5))

    # ----- אופי הירידה -----
    hi52 = float(s.iloc[-252:].max())
    hi5 = float(s.max())
    dd52 = 1 - price / hi52
    dd5 = 1 - price / hi5
    rev_yoy = F.get("yoy", {}).get("rev")
    prev_eps = eps[-1] if F.get("ttm_end") != fy[-1] else (eps[-2] if len(eps) > 1 else None)
    avg_fcf3 = np.mean([x for x in fcf[-3:] if x is not None]) if any(x is not None for x in fcf[-3:]) else None
    nd_prev = (S["debt"][-1] or 0) - (S["cash"][-1] or 0)
    debt_jump = (net_debt - nd_prev) / ebitda_t if ebitda_t and ebitda_t > 0 else None

    D = [
        check("ירידה מהשיא (52 שבועות)", 1 if dd52 >= C["dip_good"] else (0.5 if dd52 >= C["dip_min"] else 0), pct(dd52), 3),
        check("ההכנסות עדיין צומחות/יציבות", None if rev_yoy is None else (1 if rev_yoy >= 0.01 else (0.5 if rev_yoy >= -0.02 else 0)), pct(rev_yoy, 1), 2),
        check("EPS צומח או מחזיק", None if eps_t is None or prev_eps is None else (1 if eps_t >= prev_eps else (0.5 if eps_t >= prev_eps * 0.85 else 0)),
              f"{num(eps_t, 2)} מול {num(prev_eps, 2)}", 1.5),
        check("FCF לא נפגע מבנית", None if fcf_t is None or not avg_fcf3 else (1 if fcf_t >= avg_fcf3 * 0.85 else (0.5 if fcf_t >= avg_fcf3 * 0.6 else 0)),
              "—" if fcf_t is None or not avg_fcf3 else f"{fcf_t / avg_fcf3 * 100:.0f}% מהממוצע", 1.5),
        check("אין קפיצה בחוב", None if debt_jump is None else (1 if debt_jump <= 0.25 else (0.5 if debt_jump <= 0.75 else 0)),
              "—" if debt_jump is None else f"{debt_jump:+.2f}× EBITDA", 1),
        check("אין דילול בשנה האחרונה", None if sh_chg1 is None else (1 if sh_chg1 <= 0.01 else (0.5 if sh_chg1 <= 0.03 else 0)),
              "—" if sh_chg1 is None else f"{sh_chg1 * 100:+.1f}%", 1),
    ]

    # ----- דגלים -----
    red, yellow = [], []
    rv = [x for x in rev if x is not None]
    if len(rv) >= 3 and rv[-1] < rv[-2] < rv[-3]:
        red.append("ירידה מתמשכת בהכנסות (שנתיים ברצף)")
    ev_ = [x for x in eps if x is not None]
    if len(ev_) >= 3 and ev_[-1] < ev_[-2] < ev_[-3]:
        red.append("ירידה מתמשכת ב-EPS (שנתיים ברצף)")
    if fcf_t is not None and fcf_t < 0:
        red.append("FCF שלילי")
    if roic is not None and roic < 0.08:
        red.append(f"ROIC נמוך ({pct(roic)})")
    elif roic is not None and roic_old and roic_old > 0 and roic < roic_old * 0.6 and roic < 1:
        red.append(f"ROIC בירידה חדה ({pct(roic_old)} → {pct(roic)})")
    omv = [x for x in om if x is not None]
    if len(omv) >= 4 and omv[-1] < omv[-2] < omv[-3] < omv[-4]:
        red.append("שולי רווח תפעולי מתכווצים 3 שנים ברצף")
    if nd_ebitda is not None and nd_ebitda > 3.5:
        red.append(f"חוב גבוה ({nd_ebitda:.1f}× EBITDA)")
    elif debt_jump is not None and debt_jump > 1:
        red.append("חוב עולה במהירות")
    if sh_chg3 is not None and sh_chg3 > 0.08:
        red.append(f"דילול משמעותי ({sh_chg3 * 100:+.0f}% ב-3 שנים)")

    if rev_g and rev_g3 is not None and rev_yoy is not None and rev_yoy < rev_g * 0.5:
        yellow.append(f"האטה בצמיחה: {pct(rev_yoy, 1)} עכשיו מול {pct(rev_g, 1)} בממוצע")
    if om_d is not None and om_d < -0.02:
        yellow.append("ירידה בשולי הרווח התפעולי בשנה האחרונה")
    if eps_g is None and len(window(eps, n)) >= 3:
        yellow.append("EPS היה שלילי בתחילת התקופה — צמיחה לא ניתנת לחישוב")
    if eq is not None and eq < 0:
        yellow.append("הון עצמי שלילי (בדרך כלל בגלל רכישות חוזרות) — ROIC לא אמין")
    if F.get("tax") == "ifrs-full" or F.get("ccy", "USD") != "USD":
        yellow.append(f"חברה זרה (מדווחת ב-{F.get('ccy', 'USD')}) — נתונים שנתיים בלבד, הומרו לדולר בשער של היום")
    fin = is_fin
    if mos is not None and mos > 0.75:
        yellow.append("שווי פנימי חריג (Margin of Safety מעל 75%) — כנראה שהחישוב לא מתאים לחברה הזו, למשל חברה מחזורית כמו נפט וגז")
    data_to = F.get("ttm_end") or fy[-1]
    stale = (dt.date.today() - d(data_to)).days > 450
    if stale:
        yellow.append(f"הדוחות האחרונים שנמצאו ישנים (עד {data_to}) — המספרים עלולים לא לשקף את המצב היום")
    if fin:
        yellow.append("חברה פיננסית/נדל\"ן — המדדים של מזומן חופשי, ROIC ו-EBITDA פחות מתאימים לה, ולכן לא מחושב לה שווי פנימי")
    if dd5 - dd52 > 0.15:
        yellow.append(f"המניה {pct(dd5)} מתחת לשיא של 5 שנים")

    qs, vsc, ds = section_score(Q), section_score(V), section_score(D)
    tech = technical(s)
    parts = [(qs, C["w_quality"]), (vsc, C["w_value"]), (ds, C["w_dip"])]
    wsum = sum(w for x, w in parts if x is not None)
    base_score = sum(x * w for x, w in parts if x is not None) / wsum if wsum else 0
    coverage = sum(1 for c in Q + V + D if c["s"] is not None) / len(Q + V + D)
    score = base_score - C["red_flag_penalty"] * len(red) + {"up": 3, "flat": 2, "down": 0}[tech["state"]]
    if coverage < 0.6:
        score -= 15
        yellow.append("חסרים נתונים רבים — הציון פחות אמין")
    score = round(max(0, min(100, score)), 1)

    if coverage < 0.5:
        tier = "nodata"
    elif score >= 70 and not red and dd52 >= C["dip_min"] and (qs or 0) >= 65 and not fin and not stale:
        tier = "green"
    elif score >= 55 and len(red) <= 1 and dd52 >= 0.12:
        tier = "yellow"
    elif (qs or 0) >= 65 and not red and dd52 < C["dip_min"]:
        tier = "watch"
    else:
        tier = "other"

    r2 = lambda x, k=3: None if x is None else round(float(x), k)
    return {
        "t": u["t"], "cik": u["cik"], "name": u["name"], "sector": u["sector"], "industry": u["industry"],
        "price": round(price, 2), "cap": round(cap / 1e9, 2),
        "score": score, "q": qs, "v": vsc, "d": ds, "tier": tier, "cov": round(coverage, 2),
        "tech": tech, "dd52": r2(dd52), "dd5": r2(dd5),
        "rev_g": r2(rev_g), "eps_g": r2(eps_g), "fcf_g": r2(fcf_g), "roic": r2(roic), "nd_ebitda": r2(nd_ebitda, 2),
        "pe": r2(pe, 1), "pe_h": r2(pe_h, 1), "pfcf": r2(pfcf, 1), "ev_ebitda": r2(ev_ebitda, 1), "peg": r2(peg, 2),
        "iv": r2(iv, 2), "mos": r2(mos),
        "red": red, "yellow": yellow, "checks": {"q": Q, "v": V, "d": D},
        "series": {"fy": [e[:4] for e in fy[-6:]],
                   "rev": [r2(x / 1e9 if x else None) for x in rev[-6:]],
                   "eps": [r2(x, 2) for x in eps[-6:]],
                   "fcf": [r2(x / 1e9 if x is not None else None) for x in fcf[-6:]],
                   "om": [r2(x) for x in om[-6:]]},
        "data_to": F.get("ttm_end") or fy[-1],
    }


# ===================================================================
#  4ב. תבניות גרפיות (לא משפיעות על הציון)
# ===================================================================
def swings(c, k=5):
    """אינדקסים של שיאים ושפלים מקומיים."""
    hi, lo = [], []
    for i in range(k, len(c) - k):
        w = c[i - k:i + k + 1]
        if c[i] == w.max() and (not hi or i - hi[-1] > 1):
            hi.append(i)
        if c[i] == w.min() and (not lo or i - lo[-1] > 1):
            lo.append(i)
    return hi, lo


def vol_ratio(v, days=5):
    """המחזור הגבוה ב-days הימים האחרונים ביחס לממוצע 50 יום."""
    if v is None or len(v) < 60:
        return None
    avg = float(np.mean(v[-55:-5]))
    if avg <= 0:
        return None
    return round(float(np.max(v[-days:])) / avg, 2)


def _status(c, pivot):
    """פרצה = מעל הפיבוט, פרצה ב-10 הימים האחרונים ולא רחוק ממנו. קרובה = עד 5% מתחת."""
    price = c[-1]
    dist = price / pivot - 1
    if dist > 0:
        if dist <= 0.06 and np.min(c[-11:-1]) <= pivot * 1.005:
            return "breakout", dist
        return None, dist
    if dist >= -0.05:
        return "near", dist
    return None, dist


def _mk(key, name, c, pivot):
    st, dist = _status(c, pivot)
    if not st:
        return None
    return {"k": key, "n": name, "st": st, "pivot": round(float(pivot), 2), "dist": round(float(dist), 4)}


def pat_cup_handle(c):
    n = len(c)
    if n < 160:
        return None
    rr = n - 40 + int(np.argmax(c[n - 40:n - 3]))
    h = n - 1 - rr
    if not 5 <= h <= 35:
        return None
    rim = c[rr]
    hlow = float(np.min(c[rr:]))
    if not 0.03 <= 1 - hlow / rim <= 0.15:
        return None
    lo_i, hi_i = max(0, rr - 325), rr - 35
    if hi_i - lo_i < 10:
        return None
    lp = lo_i + int(np.argmax(c[lo_i:hi_i]))
    left = c[lp]
    if not 0.90 * left <= rim <= 1.05 * left:
        return None
    bi = lp + int(np.argmin(c[lp:rr + 1]))
    bottom = c[bi]
    if not 0.12 <= 1 - bottom / left <= 0.40:
        return None
    if not 0.2 <= (bi - lp) / (rr - lp) <= 0.8:
        return None
    if hlow < bottom + 0.5 * (left - bottom):
        return None
    pre = c[max(0, lp - 120):lp]
    if len(pre) > 20 and left < np.min(pre) * 1.15:
        return None
    return _mk("cup", "ספל וידית", c, rim)


def pat_flat_base(c):
    n = len(c)
    for L in range(65, 24, -5):
        if n < L + 70:
            continue
        base = c[-L - 1:-1]
        top, bot = float(np.max(base)), float(np.min(base))
        if top / bot - 1 > 0.15:
            continue
        start = n - 1 - L
        pre = c[max(0, start - 60):start]
        if len(pre) < 20 or c[start] < np.min(pre) * 1.20:
            continue
        return _mk("flat", "בסיס שטוח", c, top)
    return None


def pat_bull_flag(c, v):
    n = len(c)
    for f in range(5, 21):
        p = n - 1 - f
        if p < 30:
            break
        top = c[p]
        if np.max(c[p:max(p + 1, n - 3)]) > top * 1.005:
            continue
        lo_i = p - 20 + int(np.argmin(c[p - 20:p]))
        pole_lo = c[lo_i]
        if p - lo_i < 3 or top < np.max(c[lo_i:p + 1]) or top / pole_lo - 1 < 0.20:
            continue
        retr = (top - np.min(c[p:])) / (top - pole_lo)
        if not 0.05 <= retr <= 0.5:
            continue
        if v is not None and len(v) == n and np.mean(v[p + 1:]) >= np.mean(v[lo_i:p + 1]):
            continue
        return _mk("flag", "דגל שורי", c, top)
    return None


def pat_asc_triangle(c):
    for W in (60, 90):
        if len(c) < W + 10:
            continue
        w = c[-W:]
        hi, lo = swings(w, 4)
        hi = [i for i in hi if i < W - 3]
        if len(hi) < 2 or len(lo) < 2:
            continue
        R = max(w[i] for i in hi)
        tops = [i for i in hi if w[i] >= R * 0.97]
        if len(tops) < 2 or tops[-1] - tops[0] < 15:
            continue
        lows = [w[i] for i in lo if i > tops[0] - 10]
        if len(lows) < 2 or not all(b > a for a, b in zip(lows, lows[1:])):
            continue
        if lows[-1] < lows[0] * 1.03 or lows[0] > R * 0.95:
            continue
        return _mk("tri", "משולש עולה", c, R)
    return None


def pat_high52(c):
    if len(c) < 265:
        return None
    prev = float(np.max(c[-260:-5]))
    if np.max(c[-5:]) > prev and c[-1] >= prev * 0.98:
        return {"k": "hi52", "n": "פריצת שיא 52 שבועות", "st": "breakout", "pivot": round(prev, 2), "dist": round(float(c[-1] / prev - 1), 4)}
    return None


def pat_golden(c):
    if len(c) < 215:
        return None
    s = pd.Series(c)
    s50, s200 = s.rolling(50).mean().values, s.rolling(200).mean().values
    if s50[-1] > s200[-1]:
        for d in range(1, 11):
            if s50[-1 - d] <= s200[-1 - d]:
                return {"k": "golden", "n": "Golden Cross", "st": "breakout", "pivot": round(float(s200[-1]), 2),
                        "dist": round(float(c[-1] / s200[-1] - 1), 4), "days": d}
    return None


def tech_patterns(close, vol):
    c = close.values.astype(float)
    v = vol.values.astype(float) if vol is not None else None
    found = []
    for fn in (pat_cup_handle, pat_flat_base, pat_asc_triangle, pat_high52, pat_golden):
        try:
            x = fn(c)
            if x:
                found.append(x)
        except Exception:
            pass
    try:
        x = pat_bull_flag(c, v)
        if x:
            found.append(x)
    except Exception:
        pass
    return found


def bottom_patterns(close, vol):
    """תצורות של סוף ירידה — לרשימת ה-Buy the Dip."""
    c = close.values.astype(float)
    v = vol.values.astype(float) if vol is not None else None
    out = []
    n = len(c)
    if n < 160:
        return out
    hi1y = float(np.max(c[-252:]))
    w = c[-150:]
    hi, lo = swings(w, 5)
    # תחתית כפולה
    done = False
    for j in reversed(lo):
        if done or j < len(w) - 60:
            break
        for i in lo:
            if j - i < 15 or abs(w[j] / w[i] - 1) > 0.04:
                continue
            mid = float(np.max(w[i:j]))
            low = min(w[i], w[j])
            if mid >= max(w[i], w[j]) * 1.08 and low <= hi1y * 0.85 and c[-1] >= low * 1.03:
                out.append({"k": "dbl", "n": "תחתית כפולה" + (" (מאושרת)" if c[-1] > mid else "")})
                done = True
                break
    # שפלים עולים
    lows = [w[i] for i in lo if i >= len(w) - 120]
    if len(lows) >= 3 and lows[-1] > lows[-2] * 1.02 > 0 and lows[-2] > lows[-3] * 1.02 and c[-1] > lows[-1] and lows[-3] <= hi1y * 0.85:
        out.append({"k": "hl", "n": "שפלים עולים"})
    # פריצה מבסיס עם ווליום
    base = c[-35:-5]
    if np.max(c[-5:]) > np.max(base) and np.max(base) / np.min(base) - 1 <= 0.15 and c[-60] > np.max(base) * 1.1:
        vr = vol_ratio(v) if v is not None else None
        if vr and vr >= 1.5:
            out.append({"k": "base", "n": "פריצה מבסיס עם ווליום"})
    # חזרה מעל ממוצע 200
    s200 = pd.Series(c).rolling(200).mean().values
    if n >= 340 and c[-1] > s200[-1]:
        below = np.sum(c[-135:-15] < s200[-135:-15])
        crossed = np.any(c[-15:-1] <= s200[-15:-1])
        if below >= 60 and crossed:
            out.append({"k": "ma200", "n": "חזרה מעל ממוצע 200"})
    return out


def risk_stats(close, spy, hl=None):
    """תנודה יומית ממוצעת (%) ובטא מול ה-S&P 500."""
    c = close.astype(float)
    atr = None
    if hl is not None and len(hl[0]) >= 15:
        hh, ll = hl
        cv = c.values[-len(hh):]
        prev = np.concatenate([[cv[0]], cv[:-1]])
        tr = np.maximum(hh - ll, np.maximum(abs(hh - prev), abs(ll - prev)))
        tr = tr[-20:]
        tr = tr[np.isfinite(tr)]
        if len(tr) >= 10:
            atr = float(np.mean(tr / cv[-len(tr):]))
    if atr is None and len(c) > 21:
        atr = float(c.pct_change().abs().iloc[-20:].mean())
    beta = None
    if spy is not None and len(c) > 130:
        j = pd.concat([c.pct_change(), spy.pct_change()], axis=1, join="inner").dropna().iloc[-252:]
        if len(j) > 100 and j.iloc[:, 1].var() > 0:
            beta = float(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var())
            if not -3 <= beta <= 5:  # ערך לא סביר — כנראה בעיה בנתונים
                beta = None
    return {"atr": None if atr is None else round(atr, 4), "beta": None if beta is None else round(beta, 2)}


def chart_points(close, days=160, pts=80):
    c = close.values[-days:]
    idx = np.linspace(0, len(c) - 1, min(pts, len(c))).astype(int)
    return [round(float(c[i]), 2) for i in idx]


def ma150_info(close, days=160, pts=80):
    """מרחק המחיר מממוצע 150 יום, כיוון הממוצע, והקו לגרף."""
    if len(close) < 175:
        return None, None, None
    m = close.rolling(150).mean()
    dist = float(close.iloc[-1] / m.iloc[-1] - 1)
    rising = bool(m.iloc[-1] > m.iloc[-21])
    mv = m.values[-days:]
    idx = np.linspace(0, len(mv) - 1, min(pts, len(mv))).astype(int)
    line = [None if not math.isfinite(mv[i]) else round(float(mv[i]), 2) for i in idx]
    return round(dist, 4), rising, line


# ===================================================================
#  4ד. תזמון וסיכון (ציון נפרד — לא נכנס לציון הראשי)
# ===================================================================
SECTOR_ETF = {"Technology": "XLK", "Finance": "XLF", "Health Care": "XLV", "Energy": "XLE", "Industrials": "XLI",
              "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Utilities": "XLU",
              "Basic Materials": "XLB", "Real Estate": "XLRE", "Telecommunications": "XLC"}


def rsi_series(s, n=14):
    delta = s.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def rsi_status(close):
    """סטטוס RSI לפי כיוון וחציות — לא לפי הרמה בלבד."""
    r = rsi_series(close)
    v = r.values
    if len(v) < 40:
        return {"k": "na", "n": "—", "s": None, "rsi": None}
    now = float(v[-1])
    slope3 = v[-1] - v[-4]
    new_low = close.iloc[-1] <= close.iloc[-20:].min() * 1.005

    def crossed(level, hold, within=10):
        for i in range(len(v) - within, len(v) - hold + 1):
            if v[i - 1] < level <= v[i] and all(x >= level for x in v[i:]):
                return True
        return False

    if now > 75 and slope3 > 0:
        st = ("hot", "מתוח (מעל 75)", 0.5)
    elif slope3 < -1 and now < 50 and new_low:
        st = ("falling", "יורד — מומנטום שלילי", 0)
    elif crossed(50, 3):
        st = ("cross50", "חצה 50 והחזיק", 1)
    elif crossed(30, 2):
        st = ("cross30", "חצה 30 והחזיק", 1)
    elif np.min(v[-10:]) < 30 and slope3 > 0:
        st = ("turning", "יוצא ממכירת יתר", 0.5)
    elif all(x > 50 for x in v[-5:]):
        st = ("bull", "מעל 50 — מומנטום חיובי", 1)
    elif slope3 < -1 and now < 50:
        st = ("weak", "חלש ויורד", 0)
    else:
        st = ("neutral", "ניטרלי", 0.5)
    return {"k": st[0], "n": st[1], "s": st[2], "rsi": round(now, 1)}


def divergences(close):
    """דייברג'נס רגיל: שורי בשפלים, דובי בשיאים (60 יום אחרונים)."""
    c = close.values[-80:].astype(float)
    r = rsi_series(close).values[-80:]
    hi, lo = swings(c, 3)
    out = []
    lo = [i for i in lo if i >= len(c) - 60]
    if len(lo) >= 2:
        a, b = lo[-2], lo[-1]
        if b >= len(c) - 25 and c[b] < c[a] and r[b] >= r[a] + 5 and r[a] < 35:
            out.append("bull")
    hi = [i for i in hi if i >= len(c) - 60]
    if len(hi) >= 2:
        a, b = hi[-2], hi[-1]
        if b >= len(c) - 25 and c[b] > c[a] and r[b] <= r[a] - 5 and r[a] > 70:
            out.append("bear")
    return out


def macd_state(close):
    e12, e26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
    m = e12 - e26
    sig = m.ewm(span=9, adjust=False).mean()
    d = (m - sig).values
    cross = any(d[i - 1] <= 0 < d[i] for i in range(len(d) - 7, len(d)))
    return {"above": bool(d[-1] > 0), "cross": bool(cross and d[-1] > 0)}


def ret_n(s, n):
    return float(s.iloc[-1] / s.iloc[-n - 1] - 1) if s is not None and len(s) > n else None


def timing(close, vol, spy, sec, iv, hl=None, tx=None):
    c = close
    price = float(c.iloc[-1])
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    T, flags = [], []

    # --- מגמה ---
    above200 = slope200 = None
    if len(c) >= 225:
        above200 = price / float(s200.iloc[-1]) - 1
        slope200 = float(s200.iloc[-1] / s200.iloc[-21] - 1)
        T.append(check("מעל ממוצע 200 (או קרוב אליו)", 1 if above200 >= 0 else (0.5 if above200 >= -0.05 and slope200 >= -0.005 else 0),
                       f"{above200 * 100:+.1f}%", 2))
        T.append(check("ממוצע 200 לא יורד", 1 if slope200 >= -0.002 else (0.5 if slope200 >= -0.01 else 0),
                       "עולה" if slope200 > 0.002 else ("שטוח" if slope200 >= -0.002 else "יורד"), 1.5))
    slope50 = float(s50.iloc[-1] / s50.iloc[-11] - 1) if len(c) >= 65 else None
    if slope50 is not None:
        T.append(check("ממוצע 50 שטוח או עולה", 1 if slope50 >= -0.003 else (0.5 if slope50 >= -0.015 else 0),
                       "עולה" if slope50 > 0.003 else ("שטוח" if slope50 >= -0.003 else "יורד"), 1))

    # --- חוזק יחסי ---
    r1, r3 = ret_n(c, 21), ret_n(c, 63)
    rs_spy = (r1 - ret_n(spy, 21)) if r1 is not None and ret_n(spy, 21) is not None else None
    rs_sec = (r1 - ret_n(sec, 21)) if r1 is not None and sec is not None and ret_n(sec, 21) is not None else None
    if rs_spy is not None:
        T.append(check("חזקה מה-S&P 500 בחודש האחרון", 1 if rs_spy >= 0 else (0.5 if rs_spy >= -0.05 else 0), f"{rs_spy * 100:+.1f}%", 1.5))
    if rs_sec is not None:
        T.append(check("חזקה מהסקטור בחודש האחרון", 1 if rs_sec >= 0 else (0.5 if rs_sec >= -0.05 else 0), f"{rs_sec * 100:+.1f}%", 1))

    # --- ווליום ---
    breakdown = False
    if vol is not None and len(vol) == len(c) and len(c) > 90:
        ch = c.diff().values[-60:]
        vv = vol.values[-60:].astype(float)
        dn, up = vv[ch < 0], vv[ch > 0]
        vs = volume_score(c, vol, hl, tx)
        if vs is not None:
            T.append(check("ציון ווליום (לחץ המכירה נחלש?)", 1 if vs["score"] >= 4 else (0.5 if vs["score"] >= -3 else 0),
                           f"{vs['score']:+d} — {vs['cls']}", 2))
        avg = float(np.mean(vol.values[-80:-20])) or 1
        support = float(c.iloc[-80:-20].min())
        for i in range(len(c) - 20, len(c)):
            if c.iloc[i] < support * 0.99 and vol.iloc[i] >= 2 * avg:
                breakdown = True
        if breakdown:
            flags.append("שבירת תמיכה במחזור כבד ב-20 הימים האחרונים")
        T.append(check("אין שבירת תמיכה במחזור כבד", 0 if breakdown else 1, "נמצאה" if breakdown else "לא נמצאה", 1.5))

    # --- ירידה מסודרת או קריסה ---
    d1 = c.pct_change().values[-90:]
    worst_i = int(np.nanargmin(d1))
    worst = float(d1[worst_i])
    worst_date = c.index[-90 + worst_i].strftime("%d/%m")
    T.append(check("ירידה מסודרת (בלי נפילה חדה ביום אחד)", 1 if worst > -0.08 else (0.5 if worst > -0.15 else 0),
                   f"הגרוע: {worst * 100:.1f}% ({worst_date})", 1.5))
    if worst <= -0.15:
        flags.append(f"נפילה של {worst * 100:.0f}% ביום אחד ב-{worst_date} — לבדוק מה קרה (דוח? אירוע?)")

    # --- RSI חכם ---
    rs = rsi_status(c)
    weekly = c.resample("W-FRI").last().dropna()
    wr = float(rsi_series(weekly).iloc[-1]) if len(weekly) > 30 else None
    if rs["s"] is not None:
        # בירידה מתחת לממוצע 200 יורד, RSI של 40–50 הוא ריבאונד חלש
        sc = rs["s"]
        if above200 is not None and above200 < 0 and slope200 < 0 and rs["k"] in ("neutral",) and 40 <= rs["rsi"] < 50:
            sc, rs["n"] = 0, "ריבאונד חלש מתחת לממוצע 200"
        T.append(check("סטטוס RSI יומי", sc, f"{rs['n']} ({rs['rsi']})", 2))
    if wr is not None:
        T.append(check("RSI שבועי (התמונה הגדולה)", 1 if wr >= 50 else (0.5 if wr >= 40 else 0), f"{wr:.0f}", 1))
    dv = divergences(c)
    if "bull" in dv:
        T.append(check("דייברג'נס שורי (שפל נמוך במחיר, גבוה ב-RSI)", 1, "נמצא", 2))
    if "bear" in dv:
        flags.append("דייברג'נס דובי — המומנטום נחלש ליד השיא")
    md = macd_state(c)
    T.append(check("MACD מעל קו האות", 1 if md["above"] else 0, "חצה למעלה לאחרונה" if md["cross"] else ("מעל" if md["above"] else "מתחת"), 1))

    # --- תמיכה ותוכנית סיכון ---
    cv = c.values[-140:].astype(float)
    hi, lo = swings(cv, 5)
    lows_below = [cv[i] for i in lo if cv[i] < price * 0.995 and i >= len(cv) - 90]
    cands = [x for x in lows_below]
    for m in (s50.iloc[-1], s200.iloc[-1] if len(c) >= 200 else None):
        if m is not None and math.isfinite(m) and m < price:
            cands.append(float(m))
    for i in hi:  # שיא קודם שנפרץ הופך לתמיכה
        if cv[i] < price * 0.995 and i < len(cv) - 10:
            cands.append(float(cv[i]))
    support = max(cands) if cands else None
    near_sup = (price / support - 1) if support else None
    if near_sup is not None:
        T.append(check("קרובה לתמיכה (עד 5% מעליה)", 1 if near_sup <= 0.05 else (0.5 if near_sup <= 0.10 else 0), f"{near_sup * 100:.1f}% מעל ${support:,.2f}", 1.5))
    stop_base = lows_below[-1] if lows_below else float(np.min(cv[-20:]))
    stop = stop_base * 0.98
    risk = 1 - stop / price if price > stop else None
    reward = (iv / price - 1) if iv and iv > price else None
    rr = (reward / risk) if reward and risk and risk > 0.005 else None
    if risk is not None:
        T.append(check("יחס סיכוי-סיכון (מול השווי הפנימי)", None if rr is None else (1 if rr >= 3 else (0.5 if rr >= 2 else 0)),
                       "—" if rr is None else ("מעל 10 : 1" if rr > 10 else f"{rr:.1f} : 1"), 2))

    score = section_score(T)
    new_low = bool(price <= float(c.iloc[-20:].min()) * 1.005)
    downtrend = bool(above200 is not None and above200 < 0 and slope200 < 0)
    red = (downtrend and (rs["k"] in ("falling", "weak") or (new_low and (rs["rsi"] or 50) < 40))) or breakdown
    deep_down = bool(downtrend and above200 < -0.10)
    light = "red" if red else ("green" if score is not None and score >= 65 and not deep_down else "amber")
    return {"score": score, "light": light, "checks": T, "flags": flags,
            "rsi": rs, "wrsi": None if wr is None else round(wr, 1), "div": dv, "macd": md,
            "plan": {"stop": round(stop, 2), "risk": None if risk is None else round(risk, 4),
                     "target": None if not iv else round(iv, 2), "reward": None if reward is None else round(reward, 4),
                     "rr": None if rr is None else round(rr, 2), "support": None if support is None else round(support, 2)}}


# ===================================================================
#  4ה. ציון ווליום (-10 עד +10) — האם לחץ המכירה נחלש או נמשך
# ===================================================================
def volume_score(close, vol, hl=None, tx=None):
    if vol is None or len(vol) != len(close) or len(close) < 90:
        return None
    c = close.values.astype(float)
    v = vol.values.astype(float)
    if np.mean(v[-50:]) <= 0:
        return None
    r = np.diff(c) / c[:-1]
    r = np.concatenate([[0], r])
    avg20 = float(np.mean(v[-21:-1])) or 1
    avg50 = float(np.mean(v[-51:-1])) or 1
    rv20, rv50 = v[-1] / avg20, v[-1] / avg50
    score, notes = 0, []

    def add(x, txt):
        nonlocal score
        score += x
        notes.append((x, txt))

    # 1. ירידה בווליום יורד מול גל העלייה שלפניה
    hi_i = len(c) - 40 + int(np.argmax(c[-40:]))
    drop = 1 - c[-1] / c[hi_i]
    if drop >= 0.05 and hi_i >= 25 and len(c) - hi_i >= 3:
        pull = np.mean(v[hi_i + 1:])
        upleg = np.mean(v[hi_i - 20:hi_i + 1])
        ratio = pull / upleg if upleg > 0 else 1
        if ratio <= 0.8:
            add(4, f"התיקון מתרחש בווליום נמוך ({ratio:.2f}× מגל העלייה) — מוכרים פחות לחוצים")
        elif ratio <= 1.0:
            add(2, f"ווליום התיקון לא גבוה מגל העלייה ({ratio:.2f}×)")
        elif ratio >= 1.3:
            add(-4, f"הירידה מגיעה בווליום גבוה ({ratio:.2f}× מגל העלייה) — מכירה בשכנוע")
        elif ratio >= 1.1:
            add(-2, f"ווליום הירידה מעט גבוה מגל העלייה ({ratio:.2f}×)")

    # 2. קפיטולציה ואחריה התייבשות
    capit_ok = False
    spike = None
    for i in range(len(c) - 30, len(c) - 3):
        base = np.mean(v[max(0, i - 50):i]) or 1
        if r[i] <= -0.04 and v[i] / base >= 3:
            spike = i
    if spike is not None:
        # אשכול הפאניקה = יום השיא ועד יומיים אחריו; בודקים מה קורה אחר כך
        cap_end = min(spike + 3, len(c) - 1)
        cap_low = float(np.min(c[spike:cap_end]))
        base = float(np.mean(v[max(0, spike - 50):spike])) or avg50
        after = range(cap_end, len(c))
        red_after = [v[i] for i in after if r[i] < 0]
        heavy_red = sum(1 for i in after if r[i] < 0 and v[i] / base >= 2)
        held = np.min(c[cap_end:]) >= cap_low * 0.97
        if heavy_red >= 2:
            add(-5, "אחרי יום הפאניקה ממשיכים ימים אדומים בווליום כבד — המכירה לא נגמרה")
        elif red_after and np.mean(red_after) <= base * 0.9 and held:
            capit_ok = True
            add(3, "קפיטולציה: יום ירידה עם ווליום פי 3+, ואחריו ווליום המכירה התייבש והמחיר לא שבר את השפל")

    # 3. צבירה: ווליום בימים ירוקים מול אדומים (20 יום)
    up, dn = v[-20:][r[-20:] > 0], v[-20:][r[-20:] < 0]
    if len(up) >= 4 and len(dn) >= 4:
        ud = np.mean(up) / np.mean(dn)
        if ud >= 1.3:
            add(4, f"צבירה: בימים ירוקים הווליום גבוה פי {ud:.2f} מבימים אדומים")
        elif ud >= 1.1:
            add(2, f"ימים ירוקים בווליום מעט גבוה מאדומים ({ud:.2f}×)")
        elif ud <= 0.7:
            add(-4, f"הפצה: בימים אדומים הווליום גבוה בהרבה ({1 / ud:.2f}× מירוקים)")
        elif ud <= 0.9:
            add(-2, f"ימים אדומים בווליום גבוה מירוקים ({1 / ud:.2f}×)")

    # 4. תמיכה: מחזיקה או נשברת בווליום
    support = float(np.min(c[-80:-20]))
    broke = [i for i in range(len(c) - 20, len(c)) if c[i] < support * 0.99 and v[i] / avg50 >= 2]
    support_ok = False
    if broke:
        add(-5, f"שבירת תמיכה (${support:,.2f}) בווליום כבד")
    elif np.min(c[-10:]) >= support * 0.99 and np.min(c[-20:]) <= support * 1.08:
        dn10 = v[-10:][r[-10:] < 0]
        if len(dn10) and np.mean(dn10) <= avg50:
            support_ok = True
            add(2, "המחיר מחזיק מעל תמיכה, וימי הירידה בווליום נמוך")

    # 5. פריצה מאושרת בווליום
    green_spike = False
    res = float(np.max(c[-60:-10]))
    for i in range(len(c) - 10, len(c)):
        if c[i] > res and v[i] / avg50 >= 1.5:
            add(3, "פריצת התנגדות בווליום גבוה (פי 1.5+)")
            break
    for i in range(len(c) - 10, len(c)):
        if r[i] > 0 and v[i] / avg50 >= 1.5:
            green_spike = True

    # 6. מכירה מתמשכת
    base_long = float(np.median(v[-150:-50])) or avg50
    if c[-1] < c[-11] * 0.97 and np.mean(v[-10:]) / min(avg50, base_long) >= 1.5 and np.sum(r[-10:] < 0) >= 6:
        add(-4, "המחיר ממשיך לרדת והווליום נשאר גבוה — לחץ מכירה מתמשך")

    # 7. מיקום הסגירה בטווח היומי
    if hl is not None:
        hh, ll = hl
        n = min(10, len(hh))
        good = 0
        for k in range(1, n + 1):
            h_, l_, cc = hh[-k], ll[-k], c[-k]
            if h_ > l_ and r[-k] > 0 and (cc - l_) / (h_ - l_) >= 0.5 and v[-k] >= avg20:
                good += 1
        if good >= 2:
            add(1, f"{good} ימים ירוקים עם סגירה בחצי העליון של הטווח בווליום מעל הממוצע")

    # 8. גודל עסקה ממוצע (רמז תומך בלבד)
    if tx and tx.get("ats_chg") is not None:
        ac, nc = tx["ats_chg"], tx.get("n_chg") or 0
        stabilizing = c[-1] >= np.min(c[-10:]) * 1.01 and np.min(c[-10:]) >= np.min(c[-30:]) * 0.99
        falling = c[-1] < c[-11] * 0.97
        if stabilizing and ac >= 0.4:
            add(3, f"גודל העסקה הממוצע עלה ב-{ac * 100:.0f}% בזמן ההתייצבות — אולי גופים גדולים נכנסים")
        elif stabilizing and ac >= 0.2:
            add(2, f"גודל העסקה הממוצע עלה ב-{ac * 100:.0f}% בזמן ההתייצבות")
        elif falling and nc >= 0.3 and ac <= -0.15:
            add(-2, "בזמן הירידה: יותר עסקאות וקטנות יותר — מכירה מפוזרת")

    score = int(max(-10, min(10, score)))
    # רצף ההיפוך השורי המלא
    if capit_ok and support_ok and green_spike:
        score = max(score, 7)
        notes.append((0, "רצף היפוך שורי: פאניקה ← התייבשות ← תמיכה מחזיקה ← קונים חוזרים בווליום"))
    if score >= 7:
        cls = "אישור חזק"
    elif score >= 4:
        cls = "אישור בינוני"
    elif score >= -3:
        cls = "מעורב / ניטרלי"
    elif score >= -6:
        cls = "חלש — סימני הפצה"
    else:
        cls = "סתירה חזקה"
    return {"score": score, "cls": cls, "rv20": round(rv20, 2), "rv50": round(rv50, 2), "av20": int(np.mean(v[-20:])), "tx": tx,
            "notes": [t for x, t in sorted(notes, key=lambda z: -abs(z[0]))]}


# ===================================================================
#  4ו. וויקוף "קל" — מבנה מחיר/ווליום. רק הצגה ומסנן, לא משפיע על שום ציון.
#  הכלי לא יודע מה גופים גדולים עושים — הוא רק מזהה התנהגות שמתאימה לדפוס.
# ===================================================================
WY_STATE = {"UNKNOWN": "אין מבנה ברור", "POTENTIAL_ACCUMULATION": "איסוף אפשרי", "ACCUMULATION": "איסוף",
            "SPRING_POSSIBLE": "ניעור אפשרי", "SPRING_CONFIRMED": "ניעור מאושר", "MARKUP": "פריצה ועלייה",
            "DISTRIBUTION": "פיזור אפשרי", "MARKDOWN": "המשך ירידה"}


def wyckoff(close, vol, hl, spy=None):
    if hl is None or vol is None or len(vol) != len(close) or len(close) < 260:
        return None
    hh, ll = hl
    n = min(len(hh), 150)
    if n < 100:
        return None
    c = close.values[-n:].astype(float)
    h, l = np.asarray(hh[-n:], float), np.asarray(ll[-n:], float)
    v = vol.values[-n:].astype(float)
    dates = [x.strftime("%d/%m/%y") for x in close.index[-n:]]
    if not (np.all(np.isfinite(h)) and np.all(np.isfinite(l))) or np.mean(v) <= 0:
        return None
    full = close.values.astype(float)
    off = len(full) - n
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
    atr = pd.Series(tr).rolling(20, min_periods=10).mean().values
    avgv = pd.Series(v).shift(1).rolling(20, min_periods=10).mean().values
    rvol = np.where(avgv > 0, v / np.where(avgv > 0, avgv, 1), 1.0)
    rng = h - l
    cloc = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1), 0.5)
    hi52 = float(np.max(full[-252:]))
    dd = 1 - c[-1] / hi52
    A = float(atr[-1]) if np.isfinite(atr[-1]) else float(np.nanmean(tr[-20:]))
    ev, flags, score = [], [], 0

    def a_at(i):
        return atr[i] if np.isfinite(atr[i]) else A

    def add(code, i, txt, pts):
        nonlocal score
        ev.append({"e": code, "d": dates[i], "t": txt})
        score += pts

    # --- שיא מכירות (SC): ווליום חריג, טווח רחב, שפל מקומי, אחרי ירידה ---
    sc = None
    for i in range(10, n - 3):
        prior_hi = float(np.max(full[max(0, off + i - 120):off + i]))
        if rvol[i] >= 2 and rng[i] >= 1.5 * a_at(i) and l[i] <= np.min(l[max(0, i - 10):i + 11]) \
                and cloc[i] >= 0.4 and c[i] <= prior_hi * 0.85:
            sc = i
    support = resistance = None
    spring = sos = None
    if sc is not None:
        add("SC", sc, "שיא מכירות: יום ירידה עם ווליום חריג וטווח רחב, והסגירה לא בשפל — אולי המכירות מתמצות", 15)
        support = float(l[sc])
        win = list(range(sc + 1, min(sc + 11, n)))
        ar = max(win, key=lambda j: h[j]) if win else None
        if ar is not None and c[ar] >= c[sc] + 1.0 * a_at(sc):
            add("AR", ar, "קפיצה אוטומטית: ריבאונד חד אחרי שיא המכירות, שקובע את תקרת הטווח", 8)
            resistance = float(np.max(h[sc:ar + 1]))
            for j in range(ar + 1, n):
                if support * 0.97 <= l[j] <= max(support + 0.75 * A, support * 1.05) and rvol[j] <= 0.9:
                    add("ST", j, "מבחן משני: חזרה לאזור השפל בווליום נמוך — פחות מוכרים", 12)
                    break
            for j in range(ar + 1, n):
                if support - 1.5 * A <= l[j] < support and any(c[k] > support for k in range(j, min(j + 3, n))):
                    spring = j
            if spring is not None:
                fw = range(spring + 1, min(spring + 16, n))
                conf = 0
                if any(c[k] > support and c[k - 1] > support for k in fw if k - 1 > spring):
                    conf += 1
                if any(l[k] > l[spring] and rvol[k] <= 0.8 for k in fw):
                    conf += 1
                if any(c[k] > resistance and rvol[k] >= 1.5 for k in fw):
                    conf += 2
                if any(c[k] < l[spring] - 0.5 * A for k in range(spring + 1, n)):
                    add("SPRING_FAIL", spring, "ניעור שנכשל: המחיר ירד שוב מתחת לשפל של הניעור", -15)
                    spring = None
                elif conf >= 2:
                    add("SPRING", spring, "ניעור מאושר: ירידה רגעית מתחת לרצפה, חזרה מהירה פנימה, והמשך חיובי", 20)
                else:
                    add("SPRING?", spring, "ניעור אפשרי: ירידה רגעית מתחת לרצפה וחזרה מהירה — מחכה לאישור", 10)
            for j in range(ar + 1, n):
                if c[j] > resistance and rvol[j] >= 1.5 and cloc[j] >= 0.6:
                    sos = j
                    add("SOS", j, "סימן חוזק: פריצה מעל תקרת הטווח בווליום גבוה", 15)
                    for k in range(j + 2, n):
                        if resistance * 0.97 <= l[k] <= resistance * 1.04 and rvol[k] <= 0.9:
                            add("LPS", k, "נקודת תמיכה אחרונה: חזרה לתקרה הישנה בווליום נמוך, והיא החזיקה כרצפה", 10)
                            break
                    break
    elif dd >= 0.25 and (np.max(h[-30:]) - np.min(l[-30:])) <= 8 * A:
        support, resistance = float(np.min(l[-30:])), float(np.max(h[-30:]))
        add("BASE", n - 30, "בסיס: אחרי ירידה גדולה המניה דשדשה בטווח צר כחודש וחצי", 8)

    # --- שפלים עולים / יורדים (60 ימים) ---
    lows = [i for i in range(n - 60, n - 3) if l[i] == np.min(l[max(0, i - 4):i + 5])]
    if len(lows) >= 2:
        if l[lows[-1]] > l[lows[-2]] * 1.01:
            score += 8
        elif l[lows[-1]] < l[lows[-2]] * 0.99:
            score -= 10

    # --- שבירת הטווח ---
    broke = support is not None and c[-1] < support and any(c[k] < support * 0.97 and rvol[k] >= 1.5 for k in range(n - 15, n))
    if broke:
        add("SOW", n - 1, "שבירת רצפת הטווח בווליום גבוה — המבנה נכשל", -25)

    # --- פיזור (אחרי עלייה גדולה) ---
    dist = False
    if c[-1] >= hi52 * 0.85 and full[-1] / np.min(full[-252:]) >= 1.3:
        for i in range(n - 60, n - 3):
            if rvol[i] >= 2 and rng[i] >= 1.5 * a_at(i) and cloc[i] <= 0.5 and h[i] >= np.max(h[max(0, i - 20):i + 1]):
                top = float(h[i])
                ut = any(h[k] > top and c[k] < top for k in range(i + 3, n))
                weak = any(c[k] < np.min(l[i:i + 10]) and rvol[k] >= 1.5 for k in range(i + 3, n))
                if ut or weak:
                    dist = True
                    add("BC", i, "שיא קנייה: יום של ווליום חריג ליד השיא עם סגירה חלשה", -10)
                    add("UT" if ut else "SOW", n - 1, "פריצת שווא מעל השיא וחזרה מתחתיו" if ut else
                        "ירידה מתחת לרצפת הטווח בווליום — סימן חולשה", -10)
                break

    # --- חוזק יחסי וממוצע 50 ---
    if spy is not None and len(spy) > 25:
        rs = (full[-1] / full[-21] - 1) - float(spy.iloc[-1] / spy.iloc[-21] - 1)
        if rs > 0.02:
            score += 7
    if c[-1] > np.mean(full[-50:]):
        score += 5

    codes = {e["e"] for e in ev}
    sma200 = float(np.mean(full[-200:]))
    if broke:
        state = "MARKDOWN"
    elif dist:
        state = "DISTRIBUTION"
    elif sos is not None and c[-1] > (resistance or 0):
        state = "MARKUP"
    elif "SPRING" in codes:
        state = "SPRING_CONFIRMED"
    elif "SPRING?" in codes:
        state = "SPRING_POSSIBLE"
    elif sc is not None and "ST" in codes and c[-1] >= support:
        state = "ACCUMULATION"
    elif sc is not None or "BASE" in codes:
        state = "POTENTIAL_ACCUMULATION"
    elif full[-1] < sma200 and float(np.mean(full[-200:-180])) > sma200 and dd >= 0.2:
        state = "MARKDOWN"
        score -= 10
    else:
        state = "UNKNOWN"

    sc_score = max(0, min(100, 30 + score))
    if spy is not None and len(spy) > 200 and (spy.iloc[-1] < spy.iloc[-200:].mean() or spy.iloc[-1] / spy.iloc[-21] - 1 < -0.08):
        sc_score = round(sc_score * 0.8)
        flags.append("השוק כולו בלחץ — דפוסים כאלה מופיעים אז בהרבה מניות ופחות אמינים")
    if np.median(full[-60:] * v[-60:]) < 5_000_000:
        sc_score = round(sc_score * 0.7)
        flags.append("מחזור מסחר נמוך — אותות הווליום פחות אמינים")
    n_ev = len(codes & {"SC", "AR", "ST", "SPRING", "SOS", "LPS"})
    conf = "גבוהה" if n_ev >= 4 else ("בינונית" if n_ev >= 2 else "נמוכה")
    inval = l[spring] - 0.5 * A if spring is not None else (support - 0.5 * A if support is not None else None)
    return {"state": state, "label": WY_STATE[state], "score": int(sc_score), "conf": conf,
            "support": None if support is None else round(support, 2),
            "resistance": None if resistance is None else round(resistance, 2),
            "inval": None if inval is None else round(float(inval), 2), "events": ev[-8:], "flags": flags}


# ===================================================================
#  4ז. הייפ — לשונית עצמאית לגמרי. אזכורים ברדיט + קפיצות ווליום ועסקאות.
#  לא משפיע על שום ציון ולא קשור לשום לשונית אחרת.
# ===================================================================
def fetch_reddit_mentions(pages=4):
    out = []
    for pg in range(1, pages + 1):
        try:
            r = requests.get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{pg}", timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (personal stock screener)"})
            js = r.json()
            out += js.get("results") or []
            if pg >= int(js.get("pages") or 1):
                break
            time.sleep(1)
        except Exception as e:
            log("  שגיאה בטעינת אזכורי רדיט:", e)
            break
    log(f"רדיט: {len(out)} מניות מוזכרות")
    return out


def build_hype(closes, vols, trades, names, us_tickers):
    rows = {}
    for x in fetch_reddit_mentions():
        t = (x.get("ticker") or "").upper().replace(".", "-")
        if not t:
            continue
        try:
            m, m0 = int(x.get("mentions") or 0), int(x.get("mentions_24h_ago") or 0)
            rk = int(x["rank"]) if x.get("rank") else None
            rk0 = int(x["rank_24h_ago"]) if x.get("rank_24h_ago") else None
        except Exception:
            continue
        rows[t] = {"t": t, "name": names.get(t) or x.get("name") or t, "m": m, "m0": m0,
                   "mchg": None if not m0 else round(m / m0 - 1, 3), "rank": rk,
                   "rk_up": (rk0 - rk) if rk and rk0 else None, "up": int(x.get("upvotes") or 0)}
    days = sorted(trades)[-21:] if trades else []
    for t in us_tickers:
        if t not in closes or t not in vols:
            continue
        c, v = closes[t], vols[t]
        if len(v) < 25 or float(v.iloc[-21:-1].mean()) <= 0:
            continue
        rv = float(v.iloc[-1] / v.iloc[-21:-1].mean())
        txj = None
        if days:
            ns = [(trades[d].get(t) or [None, None])[1] for d in days]
            ns = [x for x in ns if x]
            if len(ns) >= 10 and np.mean(ns[:-1]) > 0:
                txj = round(ns[-1] / float(np.mean(ns[:-1])), 2)
        hot = rv >= 3 or (txj is not None and txj >= 3)
        if t in rows or hot:
            r = rows.setdefault(t, {"t": t, "name": names.get(t, t), "m": None, "m0": None, "mchg": None,
                                    "rank": None, "rk_up": None, "up": None})
            r["rvol"] = round(rv, 2)
            r["txj"] = txj
            r["chg1"] = round(float(c.iloc[-1] / c.iloc[-2] - 1), 4)
            r["chg5"] = round(float(c.iloc[-1] / c.iloc[-6] - 1), 4) if len(c) > 6 else None
            r["price"] = round(float(c.iloc[-1]), 2)
    out = []
    for r in rows.values():
        src = []
        if r.get("m"):
            src.append("reddit")
        if (r.get("rvol") or 0) >= 3 or (r.get("txj") or 0) >= 3:
            src.append("volume")
        if not src:
            continue
        r["src"] = src
        r["new"] = bool((r.get("m") or 0) >= 10 and r.get("m0") is not None and r["m"] >= 3 * max(r["m0"], 1)) \
            or (r.get("rvol") or 0) >= 5
        out.append(r)
    out.sort(key=lambda r: (-(r.get("m") or 0), -(r.get("rvol") or 0)))
    log(f"הייפ: {len(out)} מניות")
    return out[:400]


# ===================================================================
#  4ג. יומן ביצועים חודשי
# ===================================================================
def load_journal():
    path = DATA / "journal.json"
    if path.exists():
        return json.loads(path.read_text())
    # גיבוי: היומן מתפרסם גם באתר, אז אם המטמון נמחק משחזרים משם
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        try:
            r = requests.get(f"https://{owner.lower()}.github.io/{name}/journal.json", timeout=30)
            if r.status_code == 200:
                log("היומן שוחזר מהאתר")
                return r.json()
        except Exception:
            pass
    return {"snaps": []}


def update_journal(j, results, patterns, closes, spy, hype=None):
    today = dt.date.today()
    month = today.strftime("%Y-%m")
    if spy is not None and not any(sn["month"] == month for sn in j["snaps"]):
        j["snaps"].append({
            "month": month, "date": today.isoformat(), "spy": round(spy, 2),
            "stocks": [{"t": r["t"], "tier": r["tier"], "price": r["price"], "score": r["score"],
                        "bottom": [b["k"] for b in r.get("bottom", [])],
                        "light": (r.get("timing") or {}).get("light"), "vol": (r.get("vol") or {}).get("score"),
                        "wy": (r.get("wy") or {}).get("state")}
                       for r in results if r["tier"] in ("green", "yellow")],
            "hype": [{"t": h["t"], "price": h["price"], "m": h.get("m"), "new": h.get("new")}
                     for h in (hype or [])[:40] if h.get("price")],
            "pats": [{"t": p["t"], "type": p.get("type", "stock"), "vol": (p.get("vol") or {}).get("score"), "p": [x["k"] for x in p["pats"]], "st": [x["st"] for x in p["pats"]], "price": p["price"]}
                     for p in patterns],
        })
        log(f"נשמר צילום חודשי ליומן: {month}")
    tickers = {x["t"] for sn in j["snaps"] for x in sn["stocks"] + sn["pats"] + sn.get("hype", [])}
    j["now"] = {t: round(float(closes[t].iloc[-1]), 2) for t in tickers if t in closes}
    j["tier_now"] = {r["t"]: r["tier"] for r in results if r["t"] in tickers}
    j["spy_now"] = None if spy is None else round(spy, 2)
    j["updated"] = dt.datetime.utcnow().isoformat() + "Z"
    return j



def clean(o):
    """JSON תקין: NaN/אינסוף הופכים לריק."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if not math.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


# ===================================================================
#  5. הרצה
# ===================================================================
def main():
    t0 = time.time()
    uni = fetch_universe()
    if MAX_TICKERS:
        uni = uni[:MAX_TICKERS]
    fund = refresh_fundamentals(uni)
    journal = load_journal()
    tickers = [u["t"] for u in uni if u["t"] in fund]
    uni_t = {u["t"] for u in uni}
    etfs = fetch_etf_universe()
    if MAX_TICKERS:
        etfs = etfs[:MAX_TICKERS]
    etf_info = {e["t"]: e for e in etfs}
    side = set(etf_info) | set(COINS)
    extra = sorted({x["t"] for sn in journal["snaps"] for x in sn["stocks"] + sn["pats"] + sn.get("hype", [])} - set(tickers) - side)
    all_t = tickers + [t for t in uni_t if t not in fund] + extra + ["SPY"]
    log(f"מוריד מחירים עבור {len(all_t)} מניות...")
    closes, vols = fetch_prices(list(dict.fromkeys(all_t)))
    log(f"מוריד מחירים עבור {len(etfs)} תעודות סל ו-{len(COINS)} מטבעות...")
    side_list = list(dict.fromkeys(list(etf_info) + list(COINS) + list(SECTOR_ETF.values())))
    c2, v2 = fetch_prices([t for t in side_list if t not in closes], period="2y")
    for t in COINS:  # קריפטו נסחר 7 ימים בשבוע — משאירים ימי חול כדי שהתבניות יימדדו כמו במניות
        if t in c2:
            wk = c2[t].index.dayofweek < 5
            c2[t] = c2[t][wk]
            if t in v2:
                v2[t] = v2[t][wk]
    closes.update(c2)
    vols.update(v2)
    log("בורסות זרות:")
    intl = intl_tickers() if not MAX_TICKERS else {}
    if intl:
        c3, v3 = fetch_prices([t for t in intl if t not in closes], period="2y")
        closes.update(c3)
        vols.update(v3)
    spy = float(closes["SPY"].iloc[-1]) if "SPY" in closes else None
    trade_days = [x.strftime("%Y-%m-%d") for x in closes["SPY"].index[-61:-1]] if "SPY" in closes else []
    trades = fetch_transactions(trade_days, set(tickers) | uni_t | set(etf_info)) if trade_days else {}
    TX = lambda t: tx_stats(t, closes.get(t), trades) if trades else None

    ccys = {f.get("ccy", "USD") for f in fund.values()} - {"USD"}
    fx = fetch_fx(sorted(ccys)) if ccys else {"USD": 1.0}
    if ccys:
        log(f"שערי מטבע: {', '.join(f'{k}={v:.4f}' for k, v in fx.items() if k != 'USD')}")

    state_path = DATA / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    today = dt.date.today().isoformat()
    rank = {"green": 3, "yellow": 2, "watch": 1, "other": 0, "nodata": 0}

    results, errors = [], 0
    for u in uni:
        t = u["t"]
        if t not in fund or t not in closes:
            continue
        try:
            ccy = fund[t].get("ccy", "USD")
            if ccy not in fx:
                continue
            r = compute(u, fund[t], closes[t], fx[ccy])
        except Exception as e:
            errors += 1
            if errors <= 15:
                log("  שגיאת חישוב", t, repr(e))
            continue
        try:
            r["bottom"] = bottom_patterns(closes[t], vols.get(t))
        except Exception:
            r["bottom"] = []
        try:
            txs = TX(t)
            tm = timing(closes[t], vols.get(t), closes.get("SPY"), closes.get(SECTOR_ETF.get(u["sector"], "")), r.get("iv"), HL.get(t), txs)
            r["timing"] = tm
            r["vol"] = volume_score(closes[t], vols.get(t), HL.get(t), txs)
            r["risk"] = risk_stats(closes[t], closes.get("SPY"), HL.get(t))
            try:
                r["wy"] = wyckoff(closes[t], vols.get(t), HL.get(t), closes.get("SPY"))
            except Exception:
                r["wy"] = None
            r["yellow"].extend(tm["flags"])
            if tm["light"] == "red" and r["tier"] == "green":
                r["tier"] = "yellow"
                r["yellow"].append("אור אדום בתזמון — ירדה מהרמה הירוקה (נראית כמו סכין נופלת)")
        except Exception as e:
            r["timing"] = None
            if errors < 15:
                log("  שגיאת תזמון", t, repr(e))
        prev = state.get(t)
        if prev is None or prev["tier"] != r["tier"]:
            improved = prev is None or rank[r["tier"]] > rank[prev["tier"]]
            state[t] = {"tier": r["tier"], "since": today, "up": improved}
        r["since"] = state[t]["since"]
        r["new"] = bool(state[t].get("up")) and r["tier"] in ("green", "yellow") and \
            (dt.date.today() - dt.date.fromisoformat(state[t]["since"])).days <= 2
        results.append(r)
    results.sort(key=lambda x: -x["score"])

    # לשונית התבניות — כל המניות בביקום, בלי קשר לקריטריונים
    by_t = {r["t"]: r for r in results}
    patterns = []
    for u in uni:
        t = u["t"]
        if t not in closes:
            continue
        pats = tech_patterns(closes[t], vols.get(t))
        if not pats:
            continue
        r = by_t.get(t, {})
        patterns.append({"t": t, "type": "stock", "name": u["name"], "sector": u["sector"], "price": round(float(closes[t].iloc[-1]), 2),
                         "cap": round(u["cap"] / 1e9, 2), "tier": r.get("tier"), "score": r.get("score"),
                         "vr": vol_ratio(vols[t].values.astype(float)) if t in vols else None,
                         "pats": pats, "chart": chart_points(closes[t]), "vol": volume_score(closes[t], vols.get(t), HL.get(t), TX(t))})
        d150, up150, line150 = ma150_info(closes[t])
        patterns[-1].update({"ma150": d150, "ma150_up": up150, "ma150_line": line150, "rsi": rsi_status(closes[t])})
    # תעודות סל ומטבעות
    n_etf = 0
    for t in list(etf_info) + list(COINS):
        if t not in closes or t in uni_t:
            continue
        c = closes[t]
        if t in etf_info:
            if len(c) < 60 or t not in vols:
                continue
            dv = float((c.iloc[-50:] * vols[t].iloc[-50:]).mean())
            if dv < CONFIG["etf_min_dollar_volume"]:
                continue
            n_etf += 1
        pats = tech_patterns(c, vols.get(t))
        if not pats:
            continue
        is_coin = t in COINS
        typ = "coin" if is_coin else ("crypto_etf" if etf_info[t]["crypto"] else "etf")
        d150, up150, line150 = ma150_info(c)
        patterns.append({"t": t, "type": typ, "name": COINS[t] if is_coin else etf_info[t]["name"],
                         "sector": "קריפטו" if typ != "etf" else "תעודת סל", "price": round(float(c.iloc[-1]), 4 if c.iloc[-1] < 1 else 2),
                         "cap": None, "tier": None, "score": None,
                         "vr": vol_ratio(vols[t].values.astype(float)) if t in vols else None,
                         "pats": pats, "chart": chart_points(c), "ma150": d150, "ma150_up": up150, "ma150_line": line150,
                         "rsi": rsi_status(c), "vol": volume_score(c, vols.get(t), HL.get(t), TX(t) if t in etf_info else None)})
    log(f"תעודות סל נזילות שנסרקו: {n_etf}")
    for t, ex in intl.items():
        if t not in closes:
            continue
        c = closes[t]
        pats = tech_patterns(c, vols.get(t))
        if not pats:
            continue
        d150, up150, line150 = ma150_info(c)
        patterns.append({"t": t, "type": "intl", "ex": ex, "exn": INTL[ex]["n"], "ccy": INTL[ex]["ccy"], "name": t,
                         "sector": "", "price": round(float(c.iloc[-1]), 2), "cap": None, "tier": None, "score": None,
                         "vr": vol_ratio(vols[t].values.astype(float)) if t in vols else None,
                         "pats": pats, "chart": chart_points(c), "ma150": d150, "ma150_up": up150, "ma150_line": line150,
                         "rsi": rsi_status(c), "vol": volume_score(c, vols.get(t), HL.get(t))})
    for p in patterns:
        try:
            p["risk"] = risk_stats(closes[p["t"]], closes.get("SPY"), HL.get(p["t"]))
        except Exception:
            p["risk"] = None
        try:
            p["wy"] = wyckoff(closes[p["t"]], vols.get(p["t"]), HL.get(p["t"]), closes.get("SPY")) if p.get("type") != "coin" else None
        except Exception:
            p["wy"] = None
    patterns.sort(key=lambda p: (0 if any(x["st"] == "breakout" for x in p["pats"]) else 1, -(p["vr"] or 0)))
    log(f"תבניות: {len(patterns)} מניות")

    try:
        hype = build_hype(closes, vols, trades, {u["t"]: u["name"] for u in uni}, [u["t"] for u in uni])
    except Exception as e:
        log("שגיאה בהייפ:", e)
        hype = []
    state_path.write_text(json.dumps(state))
    out = {"hype": hype, "generated": dt.datetime.utcnow().isoformat() + "Z", "version": VERSION, "count": len(results), "config": CONFIG,
           "stocks": results, "patterns": patterns}
    (DATA / "results.json").write_text(json.dumps(clean(out), ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    journal = clean(update_journal(journal, results, patterns, closes, spy, hype))
    jtxt = json.dumps(journal, ensure_ascii=False, separators=(",", ":"))
    (DATA / "journal.json").write_text(jtxt)
    site = Path("site")
    site.mkdir(exist_ok=True)
    (site / "journal.json").write_text(jtxt)

    tiers = {k: sum(1 for r in results if r["tier"] == k) for k in rank}
    log(f"סיום: {len(results)} מניות, {tiers}, שגיאות: {errors}, זמן: {(time.time() - t0) / 60:.1f} דק'")
    if not results:
        sys.exit("לא נוצרו תוצאות")


if __name__ == "__main__":
    main()
