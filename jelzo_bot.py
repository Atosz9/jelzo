"""
Napon belüli + swing, többidősíkos jelzés-figyelő bot -> Telegram értesítés a telefonra.
NEM köt ügyletet, csak jelez. A döntés a tiéd.

Tartalma: EMA 9/21/200, RSI, trend, kitörés, gyertyaformációk,
bull/bear flag, automatikus trendvonal-törés, gazdasági naptár
(hírtilalom + előre figyelmeztetés + napi összefoglaló).

Telepítés:  pip install yfinance pandas numpy requests
Futtatás:   python jelzo_bot.py          (folyamatos)
            python jelzo_bot.py --once   (egy kör, felhős/ütemezett mód)
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ====== BEÁLLÍTÁSOK ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "IDE_A_BOT_TOKEN")      # @BotFather adja
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "IDE_A_CHAT_ID")    # @userinfobot adja
STATE_FILE = "state.json"

SYMBOLS = {                              # név -> Yahoo ticker
    "ARANY": "GC=F",
    "EZUST": "SI=F",
    "US100": "^NDX",
    "GER40": "^GDAXI",
    "MODERNA": "MRNA",
    "APPLE": "AAPL",
    "USDHUF": "HUF=X",
    "EURHUF": "EURHUF=X",
}
CHECK_EVERY = 120       # másodperc két ellenőrzés között (csak folyamatos módban)
USE_SWING = True        # swing jelzések be/ki

# Idősíkok. A "4h" az órás adatból készül (a Yahoo nem ad 4 órásat).
TF = {
    "1mo": {"label": "MN", "yf": "1mo", "period": "max", "min": 43200},
    "1wk": {"label": "W1", "yf": "1wk", "period": "10y", "min": 10080},
    "1d":  {"label": "D1", "yf": "1d", "period": "2y", "min": 1440},
    "4h":  {"label": "H4", "yf": "1h", "period": "180d", "min": 240, "resample": "4h"},
    "1h":  {"label": "H1", "yf": "1h", "period": "180d", "min": 60},
    "15m": {"label": "M15", "yf": "15m", "period": "30d", "min": 15},
}
TF_ORDER = ["1mo", "1wk", "1d", "4h", "1h", "15m"]       # fentről lefelé
REFRESH_MIN = {"1mo": 1440, "1wk": 720, "1d": 180, "4h": 20, "1h": 20, "15m": 0}   # ennyi percenként tölti újra
# entry = ezen az idősíkon keres belépőt; context = ezek trendje erősíti meg (min_align db-nak egyeznie kell)
MODES = [
    {"name": "NAPON BELÜLI", "entry": "15m", "context": ["1d", "4h", "1h"],
     "atr_mult": 1.5, "rr": 2.0, "news": True, "min_align": 2,
     "lookback": 300, "fib_lookback": 120},
    {"name": "SWING", "entry": "4h", "context": ["1mo", "1wk", "1d"],
     "atr_mult": 2.0, "rr": 3.0, "news": False, "min_align": 2,
     "lookback": 250, "fib_lookback": 120},
]
LOCAL_TZ = ZoneInfo("Europe/Budapest")

# Gazdasági naptár
USE_CALENDAR = True
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
CAL_IMPACT = ("High",)          # ("High", "Medium") ha a közepeseket is kéred
BLACKOUT_BEFORE_MIN = 15        # hír előtt ennyi percig nem jelez
BLACKOUT_AFTER_MIN = 30         # hír után ennyi percig nem jelez
WARN_BEFORE_MIN = 30            # ennyivel előre figyelmeztet
BRIEF_HOUR = 7                  # napi összefoglaló ekkortól (magyar idő)
CUR_SYMBOLS = {"EUR": ["GER40", "EURHUF"], "HUF": ["USDHUF", "EURHUF"]}   # USD mindent érint
OVERVIEW_EVERY_MIN = 60         # óránként állapotjelentés minden eszközről (0 = ki)
SKIP_SATURDAY = True            # szombaton nincs üzenet
SUNDAY_OUTLOOK_HOUR = 18        # vasárnap ekkortól (magyar idő) jön a heti kép és a jövő heti naptár
SUNDAY_RESUME_HOUR = 23         # vasárnap ekkortól (forex/arany nyitás) indul a normál működés
NEWS_BLACKOUT = []              # kézi sáv (UTC), pl. [("2026-10-02 12:00", "2026-10-02 13:15")]
# =========================

sent = {}
warned = {}
state = {"brief_date": None, "overview_t": None, "outlook_date": None}
last_info = {}
fetched = {}
ctx = {}
cal_cache = {"t": None, "events": []}


def telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram hiba:", e)


def loc(ts):
    return ts.tz_convert(LOCAL_TZ)


# ---------- Gazdasági naptár ----------
def load_calendar():
    now = pd.Timestamp.now(tz="UTC")
    if cal_cache["t"] is not None and (now - cal_cache["t"]).total_seconds() < 3600:
        return cal_cache["events"]
    ev, ok = {}, 0
    for url in CAL_URLS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            for e in r.json():
                if e.get("impact") not in CAL_IMPACT:
                    continue
                t = pd.Timestamp(e["date"])
                t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
                item = {"time": t, "cur": e.get("country", ""), "title": e.get("title", ""),
                        "fc": e.get("forecast", ""), "prev": e.get("previous", "")}
                ev[(item["title"], str(t), item["cur"])] = item
            ok += 1
        except Exception as ex:
            print("Naptár hiba:", url, ex)
    if ok:
        cal_cache["events"] = list(ev.values())
        cal_cache["t"] = now
        print(f"Naptár frissítve: {len(ev)} esemény")
    else:
        cal_cache["t"] = now - pd.Timedelta(minutes=50)   # 10 perc múlva újrapróbál
    return cal_cache["events"]


def affects(cur, name):
    return cur == "USD" or name in CUR_SYMBOLS.get(cur, [])


def blackout_reason(name, now):
    for a, b in NEWS_BLACKOUT:
        if pd.Timestamp(a, tz="UTC") <= now <= pd.Timestamp(b, tz="UTC"):
            return "kézi hírtilalom"
    for e in load_calendar() if USE_CALENDAR else []:
        if not affects(e["cur"], name):
            continue
        m = (now - e["time"]).total_seconds() / 60
        if -BLACKOUT_BEFORE_MIN <= m <= BLACKOUT_AFTER_MIN:
            return f'{e["cur"]} {e["title"]} ({loc(e["time"]).strftime("%H:%M")})'
    return None


def calendar_messages(now):
    if not USE_CALENDAR:
        return
    events = load_calendar()
    today = loc(now).date()
    if state["brief_date"] != str(today) and loc(now).weekday() < 5 and loc(now).hour >= BRIEF_HOUR and cal_cache["t"] is not None:
        todays = sorted([e for e in events if loc(e["time"]).date() == today], key=lambda x: x["time"])
        if todays:
            lines = [f'{loc(e["time"]).strftime("%H:%M")} {e["cur"]} {e["title"]}'
                     + (f' (várt: {e["fc"]}, előző: {e["prev"]})' if e["fc"] or e["prev"] else "")
                     for e in todays]
            telegram("Mai fontos gazdasági hírek (magyar idő):\n" + "\n".join(lines)
                     + f"\n\nA bot a hírek előtt {BLACKOUT_BEFORE_MIN} és után {BLACKOUT_AFTER_MIN} percig nem jelez.")
        else:
            telegram("Ma nincs fontos gazdasági hír a naptárban.")
        state["brief_date"] = str(today)
    for e in events:
        m = (e["time"] - now).total_seconds() / 60
        key = f'{e["title"]}|{e["time"]}'
        if 0 < m <= WARN_BEFORE_MIN and key not in warned:
            warned[key] = 1
            telegram(f'Figyelem: {loc(e["time"]).strftime("%H:%M")} {e["cur"]} {e["title"]} '
                     f'({int(m)} perc múlva). Nagy mozgás és szélesedő spread jöhet.')


# ---------- Indikátorok ----------
def add_indicators(df):
    df = df.copy()
    c = df["Close"]
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / down.replace(0, 1e-9))
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - c.shift()).abs(),
                    (df["Low"] - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["hh20"] = df["High"].rolling(20).max().shift(1)
    df["ll20"] = df["Low"].rolling(20).min().shift(1)
    return df


def trend_of(r):
    if r.ema9 > r.ema21 > r.ema200 and r.Close > r.ema21:
        return "bull"
    if r.ema9 < r.ema21 < r.ema200 and r.Close < r.ema21:
        return "bear"
    return "mixed"


TREND_TXT = {"bull": "emelkedő", "bear": "csökkenő", "mixed": "oldalazó / vegyes"}


# ---------- Gyertyaformációk ----------
def candle_patterns(c1, c0):
    bull, bear, notes = [], [], []
    rng = c1.High - c1.Low
    if rng <= 0:
        return bull, bear, notes
    body = abs(c1.Close - c1.Open)
    upper = c1.High - max(c1.Open, c1.Close)
    lower = min(c1.Open, c1.Close) - c1.Low
    if c0.Close < c0.Open and c1.Close > c1.Open and c1.Close >= c0.Open and c1.Open <= c0.Close:
        bull.append("Bullish engulfing")
    if c0.Close > c0.Open and c1.Close < c1.Open and c1.Close <= c0.Open and c1.Open >= c0.Close:
        bear.append("Bearish engulfing")
    if lower >= 2 * max(body, 1e-12) and lower >= 0.55 * rng and upper <= max(body, 0.1 * rng):
        bull.append("Kalapács / bull pin bar")
    if upper >= 2 * max(body, 1e-12) and upper >= 0.55 * rng and lower <= max(body, 0.1 * rng):
        bear.append("Hullócsillag / bear pin bar")
    if body <= 0.1 * rng:
        notes.append("Doji (bizonytalanság)")
    return bull, bear, notes


# ---------- Bull / bear flag ----------
def detect_flag(d, atr):
    """d: lezárt gyertyák. Rúd (10 gyertya) + szűkülő konszolidáció (6 gyertya) + kitörés az utolsó gyertyával."""
    pole_n, flag_n = 10, 6
    if len(d) < pole_n + flag_n + 2:
        return None
    last = d.iloc[-1]
    flag = d.iloc[-(flag_n + 1):-1]
    pole = d.iloc[-(flag_n + pole_n + 1):-(flag_n + 1)]
    move = pole.Close.iloc[-1] - pole.Close.iloc[0]
    slope = np.polyfit(range(flag_n), flag.Close.values, 1)[0]
    flag_range = flag.High.max() - flag.Low.min()
    if move > 3 * atr:
        retr = (pole.Close.iloc[-1] - flag.Low.min()) / move
        if retr <= 0.5 and flag_range < 0.6 * move and slope <= 0 and last.Close > flag.High.max():
            return "bull"
    if move < -3 * atr:
        retr = (flag.High.max() - pole.Close.iloc[-1]) / (-move)
        if retr <= 0.5 and flag_range < 0.6 * (-move) and slope >= 0 and last.Close < flag.Low.min():
            return "bear"
    return None


# ---------- Trendvonal-törés ----------
def pivots(H, L, w=3):
    hi, lo = [], []
    for i in range(w, len(H) - w):
        if H[i] == H[i - w:i + w + 1].max():
            hi.append(i)
        if L[i] == L[i - w:i + w + 1].min():
            lo.append(i)
    return hi, lo


def trendline_break(d):
    """Az utolsó két csúcsra (ereszkedő) / mélypontra (emelkedő) illesztett vonal áttörése."""
    d = d.iloc[-100:]
    H, L, C = d.High.values, d.Low.values, d.Close.values
    n = len(d) - 1
    hi, lo = pivots(H, L)
    if len(hi) >= 2 and hi[-1] - hi[-2] >= 5:
        i1, i2 = hi[-2], hi[-1]
        s = (H[i2] - H[i1]) / (i2 - i1)
        line = lambda x: H[i2] + s * (x - i2)
        if s < 0 and C[n - 1] <= line(n - 1) and C[n] > line(n):
            return "bull"
    if len(lo) >= 2 and lo[-1] - lo[-2] >= 5:
        i1, i2 = lo[-2], lo[-1]
        s = (L[i2] - L[i1]) / (i2 - i1)
        line = lambda x: L[i2] + s * (x - i2)
        if s > 0 and C[n - 1] >= line(n - 1) and C[n] < line(n):
            return "bear"
    return None


# ---------- Elemzés ----------
def find_levels(d, atr, lookback, w=3):
    """Támasz/ellenállás szintek: a csúcsok és mélypontok csoportosítása (0,5 ATR-en belül), legalább 2 érintéssel."""
    d2 = d.iloc[-lookback:]
    H, L = d2["High"].values, d2["Low"].values
    hi, lo = pivots(H, L, w)
    pts = sorted([float(H[i]) for i in hi] + [float(L[i]) for i in lo])
    zones = []
    for p in pts:
        if zones and abs(p - zones[-1][0] / zones[-1][1]) <= 0.5 * atr:
            zones[-1][0] += p
            zones[-1][1] += 1
        else:
            zones.append([p, 1])
    return [(z[0] / z[1], z[1]) for z in zones if z[1] >= 2]


def fib_legs(d, atr, lookback=120):
    """Fibonacci alap-hullámok: felfelé (mélypont -> csúcs) és lefelé (csúcs -> mélypont) a lookback ablak legnagyobb szélsőértékei alapján."""
    d2 = d.iloc[-lookback:]
    H, L = d2["High"].values, d2["Low"].values
    legs = {}
    ih = int(np.argmax(H))
    if ih >= 4:
        il = int(np.argmin(L[:ih]))
        if H[ih] - L[il] >= 3 * atr:
            legs["bull"] = (float(L[il]), float(H[ih]))
    il = int(np.argmin(L))
    if il >= 4:
        ih2 = int(np.argmax(H[:il]))
        if H[ih2] - L[il] >= 3 * atr:
            legs["bear"] = (float(L[il]), float(H[ih2]))
    return legs


def fib_levels(side, low, high):
    rng = high - low
    if side == "bull":
        ret = {r: high - r * rng for r in (0.382, 0.5, 0.618)}
        ext = {1.272: high + 0.272 * rng, 1.618: high + 0.618 * rng}
    else:
        ret = {r: low + r * rng for r in (0.382, 0.5, 0.618)}
        ext = {1.272: low - 0.272 * rng, 1.618: low - 0.618 * rng}
    return ret, ext


def fib_touch(side, low, high, prev, last, atr):
    """Visszapattanás a 61,8 / 50 / 38,2%-os Fibonacci szintről a hullám irányában."""
    ret, _ = fib_levels(side, low, high)
    for r in (0.618, 0.5, 0.382):
        lvl = ret[r]
        if side == "bull":
            lo = min(prev.Low, last.Low)
            if lvl - 0.5 * atr <= lo <= lvl + 0.25 * atr and last.Close > lvl and last.Close > last.Open and last.Close > low:
                return r
        else:
            hi = max(prev.High, last.High)
            if lvl - 0.25 * atr <= hi <= lvl + 0.5 * atr and last.Close < lvl and last.Close < last.Open and last.Close < high:
                return r
    return None


def analyze(df, tf):
    """Visszaad egy listát a jelzésekről az utolsó LEZÁRT gyertyán (az utolsó sor a még nyitott gyertya)."""
    df = add_indicators(df).dropna()
    if len(df) < 30:
        return []
    d = df.iloc[:-1]
    last, prev = d.iloc[-1], d.iloc[-2]
    cur_price = float(df.iloc[-1].Close)
    trend = trend_of(last)
    atr = float(last.atr)

    levels = find_levels(d, atr, tf.get("lookback", 250))
    sups = sorted([x for x in levels if x[0] < last.Close], key=lambda x: -x[0])   # legközelebbi elöl
    ress = sorted([x for x in levels if x[0] > last.Close], key=lambda x: x[0])
    near_lvl = any(abs(last.Low - p) <= 0.5 * atr or abs(last.High - p) <= 0.5 * atr for p, _ in levels)
    near = near_lvl or abs(last.Close - last.ema21) <= atr or abs(last.Close - last.ema200) <= atr

    reasons = {"bull": [], "bear": []}
    if trend == "bull":
        if prev.Low <= prev.ema21 and last.Close > last.ema21 and last.rsi > 50:
            reasons["bull"].append("EMA21 visszahúzás")
        if last.Close > last.hh20:
            reasons["bull"].append("20 gyertyás kitörés")
    if trend == "bear":
        if prev.High >= prev.ema21 and last.Close < last.ema21 and last.rsi < 50:
            reasons["bear"].append("EMA21 visszahúzás")
        if last.Close < last.ll20:
            reasons["bear"].append("20 gyertyás letörés")

    pb, pr, notes = candle_patterns(last, prev)
    if near:
        reasons["bull"] += pb
        reasons["bear"] += pr

    fl = detect_flag(d, atr)
    if fl:
        reasons[fl].append("Bull flag kitörés" if fl == "bull" else "Bear flag letörés")
    tl = trendline_break(d)
    if tl:
        reasons[tl].append("Trendvonal áttörés (felfelé)" if tl == "bull" else "Trendvonal áttörés (lefelé)")

    # Támasz / ellenállás
    for p, n in sorted(levels, key=lambda x: abs(x[0] - last.Close)):
        if (abs(last.Low - p) <= 0.5 * atr or abs(prev.Low - p) <= 0.5 * atr) and last.Close > p and last.Close > last.Open:
            reasons["bull"].append(f"Támasz visszapattanás ({n}x)")
            break
    for p, n in sorted(levels, key=lambda x: abs(x[0] - last.Close)):
        if (abs(last.High - p) <= 0.5 * atr or abs(prev.High - p) <= 0.5 * atr) and last.Close < p and last.Close < last.Open:
            reasons["bear"].append(f"Ellenállás visszafordulás ({n}x)")
            break
    for p, n in levels:
        if prev.Close <= p and last.Close > p + 0.1 * atr:
            reasons["bull"].append(f"Ellenállás kitörés ({n}x)")
            break
    for p, n in levels:
        if prev.Close >= p and last.Close < p - 0.1 * atr:
            reasons["bear"].append(f"Támasz letörés ({n}x)")
            break

    # Fibonacci
    legs = {k: v for k, v in fib_legs(d, atr, tf.get("fib_lookback", 120)).items() if v[0] <= last.Close <= v[1]}
    for side, (lo_, hi_) in legs.items():
        r = fib_touch(side, lo_, hi_, prev, last, atr)
        if r:
            reasons[side].append(f"Fibonacci {r * 100:.1f}% visszapattanás")

    out = []
    for side in ("bull", "bear"):
        base = reasons[side]
        if not base:
            continue
        rs = list(base)
        rsi_ok = last.rsi > 50 if side == "bull" else last.rsi < 50
        if rsi_ok:
            rs.append(f"RSI megerősít ({last.rsi:.0f})")
        aligned = trend == side
        core = [r for r in base if any(k in r for k in ("EMA21", "20 gyertyás", "flag", "Trendvonal"))]
        if not ((aligned and (core or len(base) >= 2)) or (not aligned and len(base) >= 3)):
            continue                      # gyenge jel önmagában nem elég; ellentrendben legalább 3 jel kell
        risk = atr * tf["atr_mult"]
        entry = float(last.Close)
        stop = entry - risk if side == "bull" else entry + risk
        target = entry + tf["rr"] * risk if side == "bull" else entry - tf["rr"] * risk
        dist = (cur_price - entry) / atr if side == "bull" else (entry - cur_price) / atr
        blocker = None                    # szint a cél előtt
        if side == "bull":
            c = [x for x in ress if entry < x[0] < target]
            blocker = c[0] if c else None
        else:
            c = [x for x in sups if target < x[0] < entry]
            blocker = c[0] if c else None
        fib = None
        if side in legs:
            lo_, hi_ = legs[side]
            ret, ext = fib_levels(side, lo_, hi_)
            fib = {"low": lo_, "high": hi_, "ret": ret, "ext": ext}
        out.append({"side": side, "reasons": rs, "aligned": aligned, "trend": trend,
                    "entry": entry, "stop": stop, "target": target, "rsi": float(last.rsi),
                    "cur": cur_price, "dist_atr": dist, "candle": last.name, "notes": notes,
                    "sup": sups[:2], "res": ress[:2], "blocker": blocker, "fib": fib,
                    "close": float(last.Close)})
    return out


ARROW = {"bull": "↑", "bear": "↓", "mixed": "→"}


def ctx_trend(df, closed_last=False):
    """Trend az utolsó LEZÁRT gyertyán: EMA9 > EMA21 > lassú EMA. Rövid előzménynél EMA50 az EMA200 helyett."""
    if len(df) < 30:
        return None
    c = df["Close"]
    slow = 200 if len(df) >= 250 else 50
    i = -1 if closed_last else -2
    e9 = c.ewm(span=9, adjust=False).mean().iloc[i]
    e21 = c.ewm(span=21, adjust=False).mean().iloc[i]
    es = c.ewm(span=slow, adjust=False).mean().iloc[i]
    p = c.iloc[i]
    if e9 > e21 > es and p > e21:
        return "bull"
    if e9 < e21 < es and p < e21:
        return "bear"
    return "mixed"


def ctx_line(name, mode, side):
    parts, ok = [], 0
    for k in mode["context"]:
        t = ctx.get(f"{name}|{k}")
        parts.append(f"{TF[k]['label']} {ARROW.get(t, '?')}")
        if t == side:
            ok += 1
    return " | ".join(parts), ok


def levels_text(s):
    """Támasz/ellenállás és Fibonacci sorok az üzenethez."""
    t = ""
    parts = []
    if s["sup"]:
        parts.append("támasz " + ", ".join(f"{p:.5g} ({n}x)" for p, n in s["sup"]))
    if s["res"]:
        parts.append("ellenállás " + ", ".join(f"{p:.5g} ({n}x)" for p, n in s["res"]))
    if parts:
        t += "Szintek: " + " | ".join(parts) + "\n"
    f = s.get("fib")
    if f:
        r = f["ret"]
        t += (f"Fibo ({f['low']:.5g}-{f['high']:.5g}): 38.2% {r[0.382]:.5g} | 50% {r[0.5]:.5g} | 61.8% {r[0.618]:.5g}"
              f" | cél 1.272: {f['ext'][1.272]:.5g}\n")
    return t


def format_msg(name, s, mode, tfc, ctx_text, ok):
    side = "LONG (vétel)" if s["side"] == "bull" else "SHORT (eladás)"
    late = ""
    if abs(s["dist_atr"]) > 0.5:
        late = "\nFIGYELEM: az ár már messze van a belépőtől, valószínűleg elkésett."
    trend_line = TREND_TXT[s["trend"]] + (" (trenddel egyező)" if s["aligned"] else " (ELLENTREND, óvatosan)")
    if tfc["min"] >= 1440:
        candle = f"Lezárt gyertya: {loc(s['candle']).strftime('%Y-%m-%d')}"
    else:
        open_t = loc(s["candle"])
        close_t = open_t + pd.Timedelta(minutes=tfc["min"])
        candle = f"Lezárt gyertya: {open_t.strftime('%H:%M')}-{close_t.strftime('%H:%M')} (magyar idő)"
    extra = "\nSwing: napokig/hetekig tartó pozíció, a stop és a cél szélesebb." if mode["name"] == "SWING" else ""
    if s.get("blocker"):
        extra += f"\nFIGYELEM: {'ellenállás' if s['side'] == 'bull' else 'támasz'} a cél előtt ({s['blocker'][0]:.5g}, {s['blocker'][1]}x), a cél nehezebben érhető el."
    return (f"{name} [{mode['name']} {tfc['label']}] - {side}\n"
            f"Jelek ({len(s['reasons'])}): " + ", ".join(s["reasons"]) + "\n"
            f"Trend ({tfc['label']}): {trend_line}\n"
            f"Idősíkok: {ctx_text} -> {ok}/{len(mode['context'])} egyezik\n"
            + levels_text(s) +
            f"Belépő ~ {s['entry']:.5g}\n"
            f"Aktuális ár ~ {s['cur']:.5g}\n"
            f"Stop ~ {s['stop']:.5g}\n"
            f"Cél ~ {s['target']:.5g} (1:{mode['rr']:g})\n"
            + candle
            + (f"\nMegjegyzés: {', '.join(s['notes'])}" if s["notes"] else "")
            + extra + late +
            "\nEz csak jelzés, nem tanács. Ellenőrizd a grafikont az XTB-ben!")


def fetch_raw(ticker, yf_interval, period):
    df = yf.download(ticker, period=period, interval=yf_interval, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def get_frame(tfk, ticker, raw):
    cfg = TF[tfk]
    key = (ticker, cfg["yf"], cfg["period"])
    if key not in raw:
        raw[key] = fetch_raw(ticker, cfg["yf"], cfg["period"])
    df = raw[key]
    if df is None or df.empty:
        return None
    if cfg.get("resample"):
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        if "Volume" in df.columns:
            agg["Volume"] = "sum"
        df = df.resample(cfg["resample"], origin="start_day").agg(agg).dropna()
    return df


def overview(now):
    if OVERVIEW_EVERY_MIN <= 0 or not last_info:
        return
    t = state["overview_t"]
    if t is not None and (now - t).total_seconds() < OVERVIEW_EVERY_MIN * 60:
        return
    state["overview_t"] = now
    lines = []
    for n in SYMBOLS:
        if n not in last_info:
            continue
        rsi, price, stale = last_info[n]
        arrows = " ".join(f"{TF[k]['label']}{ARROW.get(ctx.get(f'{n}|{k}'), '?')}" for k in TF_ORDER)
        lines.append(f"{n}: {arrows} | RSI {rsi:.0f} | ár ~ {price:.5g}" + (" (zárva)" if stale else ""))
    telegram(f"Állapot ({loc(now).strftime('%H:%M')}):\n" + "\n".join(lines)
             + "\n(↑ emelkedő, ↓ csökkenő, → vegyes. Az RSI az M15-ös. Belépőt csak külön jelzés ad.)")


HU_DAYS = ["Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek", "Szombat", "Vasárnap"]


def send_long(text):
    while text:
        chunk = text[:3500]
        if len(text) > 3500 and "\n" in chunk:
            chunk = chunk[:chunk.rfind("\n")]
        telegram(chunk)
        text = text[len(chunk):].lstrip("\n")


def outlook_line(name, ticker, now):
    w = fetch_raw(ticker, "1wk", "5y")
    d = fetch_raw(ticker, "1d", "1y")
    if w.empty or d.empty or len(w) < 30 or len(d) < 30:
        return None
    if not w.index[-1] + pd.Timedelta(days=4) <= now:      # még alakuló heti gyertya -> el
        w = w.iloc[:-1]
    if not d.index[-1] + pd.Timedelta(days=1) <= now:
        d = d.iloc[:-1]
    wt, dtr = ctx_trend(w, True), ctx_trend(d, True)
    last, prev = w.iloc[-1], w.iloc[-2]
    chg = (last.Close / prev.Close - 1) * 100
    rsi = float(add_indicators(d).dropna().iloc[-1].rsi)
    atr_w = float(add_indicators(w).dropna().iloc[-1].atr)
    if wt == dtr == "bull":
        bias = "LONG oldalra dől"
    elif wt == dtr == "bear":
        bias = "SHORT oldalra dől"
    else:
        bias = "vegyes, nincs tiszta irány"
    atr_d = float(add_indicators(d).dropna().iloc[-1].atr)
    lv = find_levels(d, atr_d, 250)
    px = float(d.Close.iloc[-1])
    sup = sorted([x for x in lv if x[0] < px], key=lambda x: -x[0])[:2]
    res = sorted([x for x in lv if x[0] > px], key=lambda x: x[0])[:2]
    lv_txt = ""
    if sup or res:
        lv_txt = ("\n  D1 szintek: " + " | ".join(filter(None, [
            ("támasz " + ", ".join(f"{p:.5g} ({n}x)" for p, n in sup)) if sup else "",
            ("ellenállás " + ", ".join(f"{p:.5g} ({n}x)" for p, n in res)) if res else ""])))
    return (f"{name}: W1{ARROW.get(wt, '?')} D1{ARROW.get(dtr, '?')} | {bias}\n"
            f"  RSI(D1) {rsi:.0f} | múlt hét {chg:+.1f}% | H {last.High:.5g} L {last.Low:.5g} Z {last.Close:.5g} | heti ATR {atr_w:.4g}"
            + lv_txt)


def weekly_outlook(now):
    lt = loc(now)
    if state["outlook_date"] == str(lt.date()) or lt.hour < SUNDAY_OUTLOOK_HOUR:
        return
    state["outlook_date"] = str(lt.date())
    if USE_CALENDAR:
        cal_cache["t"] = None
        events = load_calendar()
        end = lt.date() + dt.timedelta(days=5)
        evs = sorted([e for e in events if e["time"] > now and loc(e["time"]).date() <= end], key=lambda x: x["time"])
        lines, cur_day, counts = [], None, {}
        for e in evs:
            ld = loc(e["time"])
            if ld.date() != cur_day:
                cur_day = ld.date()
                lines.append(f"\n{HU_DAYS[ld.weekday()]} ({ld.strftime('%m.%d')}):")
            lines.append(f"  {ld.strftime('%H:%M')} {e['cur']} {e['title']}"
                         + (f" (várt: {e['fc']}, előző: {e['prev']})" if e["fc"] or e["prev"] else ""))
            counts[e["cur"]] = counts.get(e["cur"], 0) + 1
        head = "Jövő heti fontos gazdasági hírek (magyar idő):"
        if not evs:
            head += "\nNem találtam eseményt a naptár-feedben (lehet, hogy még nem frissült)."
        if counts:
            top = sorted(counts.items(), key=lambda x: -x[1])[:3]
            lines.append("\nLegtöbb hír: " + ", ".join(f"{c} ({n})" for c, n in top))
        send_long(head + "\n".join(lines))
    out = []
    for name, ticker in SYMBOLS.items():
        try:
            line = outlook_line(name, ticker, now)
            if line:
                out.append(line)
        except Exception as ex:
            print(name, "heti kép hiba:", ex)
    if out:
        send_long("Heti kép piacnyitás előtt (heti és napi trend):\n" + "\n".join(out)
                  + "\n(↑ emelkedő, ↓ csökkenő, → vegyes. H/L/Z = múlt heti csúcs/mélypont/zárás. A heti ATR az átlagos heti mozgás.)"
                  + "\nForex és arany kb. 23:00-kor nyit (magyar idő), a normál jelzések ekkortól indulnak. Hétfőn 7:00 után jön a napi hírösszefoglaló.")


def run_once(now=None):
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    lt = loc(now)
    if SKIP_SATURDAY and lt.weekday() == 5:
        print("szombat: nincs jelzés")
        return
    if lt.weekday() == 6:
        weekly_outlook(now)
        if lt.hour < SUNDAY_RESUME_HOUR:
            return
    calendar_messages(now)
    modes = [m for m in MODES if USE_SWING or m["name"] != "SWING"]
    needed = set()
    for m in modes:
        needed.add(m["entry"])
        needed.update(m["context"])
    order = [k for k in TF_ORDER if k in needed]
    for name, ticker in SYMBOLS.items():
        try:
            raw, frames = {}, {}
            for tfk in order:                                  # fentről lefelé: előbb a kontextus
                lf = fetched.get(f"{name}|{tfk}")
                age = (now - pd.Timestamp(lf)).total_seconds() / 60 if lf else 1e9
                if age < REFRESH_MIN[tfk]:
                    continue
                df = get_frame(tfk, ticker, raw)
                if df is None or len(df) < 30:
                    continue
                fetched[f"{name}|{tfk}"] = str(now)
                frames[tfk] = df
                t = ctx_trend(df)
                if t:
                    ctx[f"{name}|{tfk}"] = t
                if tfk == "15m":
                    d_ = add_indicators(df).dropna()
                    if len(d_) > 2:
                        stale = now - (df.index[-2] + pd.Timedelta(minutes=15)) > pd.Timedelta(minutes=45)
                        last_info[name] = (float(d_.iloc[-2].rsi), float(d_.iloc[-1].Close), bool(stale))
            for mode in modes:
                e = mode["entry"]
                if e not in frames:
                    continue
                tfc = TF[e]
                if mode["news"]:
                    why = blackout_reason(name, now)
                    if why:
                        print(name, "hírtilalom:", why)
                        continue
                df = frames[e]
                closed_at = df.index[-2] + pd.Timedelta(minutes=tfc["min"])
                win = 3 * tfc["min"] if tfc["min"] <= 15 else 2 * tfc["min"]
                if now - closed_at > pd.Timedelta(minutes=win):
                    continue                                   # zárva a piac / régi adat
                for s in analyze(df, mode):
                    ctx_text, ok = ctx_line(name, mode, s["side"])
                    if ok < mode["min_align"]:
                        continue                               # a magasabb idősíkok nem erősítik meg
                    key = f"{name}|{mode['name']}|{e}|{s['candle']}|{s['side']}"
                    if key in sent:
                        continue
                    sent[key] = 1
                    msg = format_msg(name, s, mode, tfc, ctx_text, ok)
                    print(msg)
                    telegram(msg)
        except Exception as ex:
            print(name, "hiba:", ex)
    overview(now)


def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except Exception:
        return False
    for k in d.get("sent", []):
        sent[k] = 1
    for k in d.get("warned", []):
        warned[k] = 1
    fetched.update(d.get("fetched", {}))
    ctx.update(d.get("ctx", {}))
    state["brief_date"] = d.get("brief_date")
    state["outlook_date"] = d.get("outlook_date")
    if d.get("overview_t"):
        state["overview_t"] = pd.Timestamp(d["overview_t"])
    c = d.get("cal")
    if c:
        cal_cache["t"] = pd.Timestamp(c["t"])
        cal_cache["events"] = [dict(e, time=pd.Timestamp(e["time"])) for e in c["events"]]
    return True


def save_state():
    d = {
        "sent": list(sent)[-400:],
        "warned": list(warned)[-200:],
        "fetched": fetched,
        "ctx": ctx,
        "brief_date": state["brief_date"],
        "outlook_date": state["outlook_date"],
        "overview_t": str(state["overview_t"]) if state["overview_t"] is not None else None,
        "cal": {"t": str(cal_cache["t"]),
                "events": [dict(e, time=str(e["time"])) for e in cal_cache["events"]]} if cal_cache["t"] is not None else None,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(d, f)


if __name__ == "__main__":
    if "--once" in sys.argv:          # felhős mód: egy kör, az állapot fájlban marad
        if not load_state():
            telegram("Jelző bot elindult (felhős mód).")
        run_once()
        save_state()
    else:                             # folyamatos mód (számítógépen)
        telegram("Jelző bot elindult. Naptár: " + ("be" if USE_CALENDAR else "ki") + ".")
        while True:
            run_once()
            time.sleep(CHECK_EVERY)
