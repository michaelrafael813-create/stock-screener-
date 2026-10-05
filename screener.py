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
}

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

FULL = os.environ.get("FULL", "").lower() == "true"
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "0") or 0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ===================================================================
#  1. רשימת המניות
# ===================================================================
def fetch_universe():
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
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


def d(s):
    return dt.date.fromisoformat(s)


def collect(facts, tags, unit):
    out = []
    g = facts.get("facts", {}).get("us-gaap", {})
    for prio, tag in enumerate(tags):
        node = g.get(tag)
        if not node:
            continue
        for f in node.get("units", {}).get(unit, []):
            form = f.get("form", "")
            if not (form.startswith("10-K") or form.startswith("10-Q")):
                continue
            out.append((prio, f.get("start"), f["end"], f["val"], f.get("filed", ""), form))
    return out


def annual_flow(entries):
    best = {}
    for prio, s, e, v, filed, form in entries:
        if not s or not form.startswith("10-K"):
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
        a = {e: v for (pp, s, e, v, f, fm) in ent if fm.startswith("10-K") and 330 <= (d(e) - d(s)).days <= 400}
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
        if annual_only and not form.startswith("10-K"):
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


def extract(facts):
    """מחזיר סדרות שנתיות + TTM + מאזן אחרון, בפורמט קומפקטי לשמירה."""
    flows, ttm, yoy, ttm_end = {}, {}, {}, None
    for k, tags in FLOW.items():
        ent = collect(facts, tags, UNIT.get(k, "USD"))
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
    for k, tags in INSTANT.items():
        ent = collect(facts, tags, "USD")
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
    for k in FLOW:
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
    return {"s": series, "ttm": ttm, "yoy": yoy, "ttm_end": ttm_end, "bal": bal}


def refresh_fundamentals(universe):
    path = DATA / "fundamentals.json"
    old = json.loads(path.read_text()) if path.exists() else {"updated": None, "by_ticker": {}}
    age_h = 1e9
    if old.get("updated"):
        age_h = (dt.datetime.utcnow() - dt.datetime.fromisoformat(old["updated"])).total_seconds() / 3600
    missing = [u for u in universe if u["t"] not in old["by_ticker"]]
    if not FULL and age_h < CONFIG["fundamentals_max_age_hours"] and not missing:
        log(f"דוחות עדכניים ({age_h:.1f} שעות) — מדלג על רענון")
        return old["by_ticker"]
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
        if facts:
            try:
                x = extract(facts)
                if x:
                    out[u["t"]] = x
            except Exception as e:
                log("  שגיאה בפענוח", u["t"], e)
        if (i + 1) % 100 == 0:
            log(f"  {i + 1}/{len(todo)}")
    path.write_text(json.dumps(clean({"updated": dt.datetime.utcnow().isoformat(), "by_ticker": out}), separators=(",", ":")))
    return out


# ===================================================================
#  3. מחירים
# ===================================================================
def fetch_prices(tickers):
    """מחזיר (מחירי סגירה, מחזורי מסחר) לכל מניה."""
    import yfinance as yf
    out, vols = {}, {}
    for i in range(0, len(tickers), 80):
        chunk = tickers[i:i + 80]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="5y", interval="1d", auto_adjust=False, progress=False,
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
            except Exception:
                pass
        time.sleep(1.5)
        log(f"  מחירים: {min(i + 80, len(tickers))}/{len(tickers)}")
    return out, vols


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


def compute(u, F, s):
    C = CONFIG
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
    if fcf_t and fcf_t > 0 and g_in:
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
        check("Margin of Safety (DCF שמרני)", None if mos is None else (1 if mos >= C["mos_good"] else (0.5 if mos >= C["mos_min"] else 0)),
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
    fin = u["sector"] in ("Finance", "Real Estate")
    if fin:
        yellow.append("חברה פיננסית/נדל\"ן — FCF, ROIC ו-EBITDA פחות מתאימים לסקטור")
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
    elif score >= 70 and not red and dd52 >= C["dip_min"] and (qs or 0) >= 65 and not fin:
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


def chart_points(close, days=160, pts=80):
    c = close.values[-days:]
    idx = np.linspace(0, len(c) - 1, min(pts, len(c))).astype(int)
    return [round(float(c[i]), 2) for i in idx]


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


def update_journal(j, results, patterns, closes, spy):
    today = dt.date.today()
    month = today.strftime("%Y-%m")
    if spy is not None and not any(sn["month"] == month for sn in j["snaps"]):
        j["snaps"].append({
            "month": month, "date": today.isoformat(), "spy": round(spy, 2),
            "stocks": [{"t": r["t"], "tier": r["tier"], "price": r["price"], "score": r["score"],
                        "bottom": [b["k"] for b in r.get("bottom", [])]}
                       for r in results if r["tier"] in ("green", "yellow")],
            "pats": [{"t": p["t"], "p": [x["k"] for x in p["pats"]], "st": [x["st"] for x in p["pats"]], "price": p["price"]}
                     for p in patterns],
        })
        log(f"נשמר צילום חודשי ליומן: {month}")
    tickers = {x["t"] for sn in j["snaps"] for x in sn["stocks"] + sn["pats"]}
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
    extra = sorted({x["t"] for sn in journal["snaps"] for x in sn["stocks"] + sn["pats"]} - set(tickers))
    all_t = tickers + [t for t in uni_t if t not in fund] + extra + ["SPY"]
    log(f"מוריד מחירים עבור {len(all_t)} מניות...")
    closes, vols = fetch_prices(list(dict.fromkeys(all_t)))
    spy = float(closes["SPY"].iloc[-1]) if "SPY" in closes else None

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
            r = compute(u, fund[t], closes[t])
        except Exception as e:
            errors += 1
            if errors <= 15:
                log("  שגיאת חישוב", t, repr(e))
            continue
        try:
            r["bottom"] = bottom_patterns(closes[t], vols.get(t))
        except Exception:
            r["bottom"] = []
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
        patterns.append({"t": t, "name": u["name"], "sector": u["sector"], "price": round(float(closes[t].iloc[-1]), 2),
                         "cap": round(u["cap"] / 1e9, 2), "tier": r.get("tier"), "score": r.get("score"),
                         "vr": vol_ratio(vols[t].values.astype(float)) if t in vols else None,
                         "pats": pats, "chart": chart_points(closes[t])})
    patterns.sort(key=lambda p: (0 if any(x["st"] == "breakout" for x in p["pats"]) else 1, -(p["vr"] or 0)))
    log(f"תבניות: {len(patterns)} מניות")

    state_path.write_text(json.dumps(state))
    out = {"generated": dt.datetime.utcnow().isoformat() + "Z", "count": len(results), "config": CONFIG,
           "stocks": results, "patterns": patterns}
    (DATA / "results.json").write_text(json.dumps(clean(out), ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    journal = clean(update_journal(journal, results, patterns, closes, spy))
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
