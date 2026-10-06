"""
Napon belüli + swing, többidősíkos jelzés-figyelő bot -> Telegram értesítés a telefonra.
NEM köt ügyletet, csak jelez. A döntés a tiéd.

Tartalma: EMA 9/21/200, RSI, trend, kitörés, gyertyaformációk,
bull/bear flag, automatikus trendvonal-törés, gazdasági naptár
(hírtilalom + előre figyelmeztetés + napi összefoglaló).

Telepítés:  pip install yfinance pandas numpy requests
Futtatás:   python jelzo_bot.py          (folyamatos)
            python jelzo_bot.py --loop 55   (felhős, percenként ~55 percig)
            python jelzo_bot.py --once   (egy kör, felhős/ütemezett mód)
"""
import datetime as dt
import html
import json
import os
import re
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

SYMBOLS = {                              # név -> Yahoo ticker (az üzenetekben is ebben a sorrendben)
    "ARANY": "GC=F",
    "EZUST": "SI=F",
    "US100": "^NDX",
    "GER40": "^GDAXI",
    "BTC": "BTC-USD",
    "USD INDEX": "DX-Y.NYB",
    "OLAJ": "CL=F",
    "MODERNA": "MRNA",
    "APPLE": "AAPL",
    "INTEL": "INTC",
    "NVIDIA": "NVDA",
    "USDHUF": "HUF=X",
    "EURHUF": "EURHUF=X",
}
CHECK_EVERY = 120       # másodperc két ellenőrzés között (csak folyamatos módban)
USE_SWING = True        # swing jelzések be/ki
USE_FAST = True         # GYORS mód (M5 belépő) be/ki
USE_SCALP = False       # SCALP mód (M1 belépő) be/ki (zajos, a spread felfalja; alapból ki)
FAST_SYMBOLS = None     # None = az M5/M1 mód minden eszközre fut; pl. ["MODERNA", "NVIDIA"] = csak ezekre
PREPOST_SYMBOLS = ["MODERNA", "APPLE", "INTEL", "NVIDIA"]   # ezeknél a tőzsdenyitás előtti/utáni (pre/post-market) adat is számít
USE_SPIKE = True        # "erős mozgás" riasztás be/ki (belépő jelzés nélkül is szól, ha az ár megindul)
SPIKE_ATR = 3.0         # ennyi ATR-nyi mozgás 3 gyertya alatt = erős mozgás
SPIKE_TFS = ["5m", "15m"]
SPIKE_COOLDOWN_MIN = 60
USE_PLANS = True        # előre elemzés: napi és swing terv (belépő zónák, stop, célok) + figyelés
DAY_PLAN_TIMES = [(6, 15), (15, 0)]   # napi terv küldése (magyar idő): reggel teljes, délután csak ami változott
WEEKEND_PLAN_TIME = (9, 0)            # hétvégén csak a WEEKEND_SYMBOLS kap napi tervet
PLAN_ZONE_ATR = 0.35    # ennyi ATR-en belül számít, hogy az ár "zónába ért"
USE_SETUP = False       # "készülő jel" előzetes figyelmeztetés: az ár egy belépő zóna közelében jár, de a jel még nem jött
SETUP_MODES = ["NAPON BELÜLI", "GYORS"]
SETUP_COOLDOWN_MIN = 120
LOOP_SLEEP = 60         # folyamatos módban ennyi másodpercenként fut egy kör
EXPIRE_DAYS = {"NAPON BELÜLI": 2, "SWING": 21, "GYORS": 1, "SCALP": 1}   # eredménykövetés: ennyi nap után "lejárt"

# Idősíkok. A "4h" az órás adatból készül (a Yahoo nem ad 4 órásat).
TF = {
    "1mo": {"label": "MN", "yf": "1mo", "period": "max", "min": 43200},
    "1wk": {"label": "W1", "yf": "1wk", "period": "10y", "min": 10080},
    "1d":  {"label": "D1", "yf": "1d", "period": "2y", "min": 1440},
    "4h":  {"label": "H4", "yf": "1h", "period": "180d", "min": 240, "resample": "4h"},
    "1h":  {"label": "H1", "yf": "1h", "period": "180d", "min": 60},
    "15m": {"label": "M15", "yf": "15m", "period": "30d", "min": 15},
    "5m":  {"label": "M5", "yf": "5m", "period": "5d", "min": 5},
    "1m":  {"label": "M1", "yf": "1m", "period": "2d", "min": 1},
}
TF_ORDER = ["1mo", "1wk", "1d", "4h", "1h", "15m", "5m", "1m"]       # fentről lefelé
REFRESH_MIN = {"1mo": 1440, "1wk": 720, "1d": 180, "4h": 20, "1h": 20, "15m": 3, "5m": 0, "1m": 0}   # ennyi percenként tölti újra
# entry = ezen az idősíkon keres belépőt; context = ezek trendje erősíti meg (min_align db-nak egyeznie kell)
MODES = [
    {"name": "NAPON BELÜLI", "entry": "15m", "context": ["1d", "4h", "1h"],
     "atr_mult": 1.5, "rr": 2.0, "news": True, "min_align": 2,
     "lookback": 300, "fib_lookback": 120},
    {"name": "SWING", "entry": "4h", "context": ["1mo", "1wk", "1d"],
     "atr_mult": 2.0, "rr": 3.0, "news": False, "min_align": 2,
     "lookback": 250, "fib_lookback": 120},
    {"name": "GYORS", "entry": "5m", "context": ["1h", "15m"],
     "atr_mult": 1.5, "rr": 2.0, "news": True, "min_align": 2,
     "lookback": 300, "fib_lookback": 120, "cooldown_min": 30},
    {"name": "SCALP", "entry": "1m", "context": ["1h", "15m", "5m"],
     "atr_mult": 1.5, "rr": 1.5, "news": True, "min_align": 3,
     "lookback": 300, "fib_lookback": 120, "cooldown_min": 20},
]
LOCAL_TZ = ZoneInfo("Europe/Budapest")

# Gazdasági naptár
USE_CALENDAR = True
CAL_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
CAL_IMPACT = ("High",)          # ("High", "Medium") ha a közepeseket is kéred
BLACKOUT_BEFORE_MIN = 15        # hír előtt ennyi percig nem jelez
BLACKOUT_AFTER_MIN = 30         # hír után ennyi percig nem jelez
WARN_BEFORE_MIN = 75            # ennyivel előre figyelmeztet (a GitHub késése miatt bő előzetes)
BRIEF_TIME = (6, 15)            # napi összefoglaló ekkortól (óra, perc, magyar idő)
CUR_SYMBOLS = {"EUR": ["GER40", "EURHUF"], "HUF": ["USDHUF", "EURHUF"]}   # USD mindent érint
OVERVIEW_EVERY_MIN = 60         # óránként állapotjelentés minden eszközről (0 = ki)
SKIP_SATURDAY = True            # szombaton nincs üzenet (a WEEKEND_SYMBOLS kivételével)
INTRADAY_EXPIRE_DAYS = 2        # eredménykövetés: napon belüli jelzés ennyi nap után "lejárt"
SWING_EXPIRE_DAYS = 21          # swing jelzés ennyi nap után "lejárt"
SIGNAL_KEEP_DAYS = 35           # ennyi napig őrzi a jelzéseket a heti összesítőhöz
WEEKEND_SYMBOLS = ["BTC"]       # ezekre hétvégén (szombat, vasárnap a nyitásig) is jön jelzés
SUNDAY_OUTLOOK_TIME = (17, 15)  # vasárnap ekkortól jön a heti összesítő, a jövő heti naptár és a heti kép
SUNDAY_RESUME_TIME = (20, 15)   # vasárnap ekkortól indul a normál működés (a forex/arany kb. 23:00-kor nyit)
NEWS_BLACKOUT = []              # kézi sáv (UTC), pl. [("2026-10-02 12:00", "2026-10-02 13:15")]
# =========================

sent = {}
warned = {}
state = {"brief_date": None, "overview_t": None, "outlook_date": None}
last_info = {}
signals = []
cool = {}
spike = {}
setup = {}
plans = {}
plan_slots = {}
live = {}
fetched = {}
ctx = {}
cal_cache = {"t": None, "events": []}


def fp(x):
    """Ár szépen: 5 értékes jegy, de nagy számoknál (pl. BTC) egész szám szóközzel."""
    x = float(x)
    return format(x, ".5g") if abs(x) < 99999.5 else f"{x:,.0f}".replace(",", " ")


def before(lt, hm):
    """Igaz, ha a helyi idő még a megadott (óra, perc) előtt van."""
    return lt.hour * 60 + lt.minute < hm[0] * 60 + hm[1]


def esc(x):
    return html.escape(str(x), quote=False)


def telegram(text):
    """HTML formázással küld; ha a Telegram elutasítja, formázás nélkül újrapróbálja."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": plain}, timeout=15)
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


def event_lines(evs):
    out = []
    for e in evs:
        out.append(f'<b>{loc(e["time"]).strftime("%H:%M")}</b> · {esc(e["cur"])} · {esc(e["title"])}')
        if e["fc"] or e["prev"]:
            out.append(f'      várt: {esc(e["fc"]) or "-"} · előző: {esc(e["prev"]) or "-"}')
    return out


def calendar_messages(now):
    if not USE_CALENDAR:
        return
    events = load_calendar()
    today = loc(now).date()
    if state["brief_date"] != str(today) and loc(now).weekday() < 5 and not before(loc(now), BRIEF_TIME) and cal_cache["t"] is not None:
        todays = sorted([e for e in events if loc(e["time"]).date() == today], key=lambda x: x["time"])
        if todays:
            telegram("📅 <b>Mai fontos gazdasági hírek</b> (magyar idő)\n\n" + "\n".join(event_lines(todays))
                     + f"\n\n<i>A bot a hírek előtt {BLACKOUT_BEFORE_MIN} és után {BLACKOUT_AFTER_MIN} percig nem jelez.</i>")
        else:
            telegram("📅 Ma nincs fontos gazdasági hír a naptárban.")
        state["brief_date"] = str(today)
    for e in events:
        m = (e["time"] - now).total_seconds() / 60
        key = f'{e["title"]}|{e["time"]}'
        if 0 < m <= WARN_BEFORE_MIN and key not in warned:
            warned[key] = 1
            telegram(f'⏰ <b>Figyelem!</b> {loc(e["time"]).strftime("%H:%M")} · {esc(e["cur"])} · {esc(e["title"])}\n'
                     f'{int(m)} perc múlva. Nagy mozgás és szélesedő spread jöhet.')


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
    """Támasz/ellenállás és Fibonacci blokk az üzenethez."""
    t = ""
    if s["sup"] or s["res"]:
        t += "\n<b>Szintek</b>\n"
        if s["sup"]:
            t += "  Támasz: " + ", ".join(f"{fp(p)} ({n}x)" for p, n in s["sup"]) + "\n"
        if s["res"]:
            t += "  Ellenállás: " + ", ".join(f"{fp(p)} ({n}x)" for p, n in s["res"]) + "\n"
    f = s.get("fib")
    if f:
        r = f["ret"]
        t += (f"\n<b>Fibonacci</b> (hullám: {fp(f['low'])} – {fp(f['high'])})\n"
              f"  38.2%: {fp(r[0.382])} · 50%: {fp(r[0.5])} · 61.8%: {fp(r[0.618])}\n"
              f"  Cél (1.272): {fp(f['ext'][1.272])}\n")
    return t


def format_msg(name, s, mode, tfc, ctx_text, ok, now=None):
    icon = "🟢" if s["side"] == "bull" else "🔴"
    side = "LONG (vétel)" if s["side"] == "bull" else "SHORT (eladás)"
    trend_line = TREND_TXT[s["trend"]] + (" ✓ trenddel egyező" if s["aligned"] else " ⚠️ ELLENTREND, óvatosan")
    if tfc["min"] >= 1440:
        candle = f"Lezárt gyertya: {loc(s['candle']).strftime('%Y-%m-%d')}"
    else:
        open_t = loc(s["candle"])
        close_t = open_t + pd.Timedelta(minutes=tfc["min"])
        candle = f"Lezárt gyertya: {open_t.strftime('%H:%M')} – {close_t.strftime('%H:%M')} (magyar idő)"
    msg = (f"{icon} <b>{esc(name)}</b> · {side}\n"
           f"<i>{esc(mode['name'].capitalize())} · {tfc['label']}</i>\n\n"
           f"<b>Jelek ({len(s['reasons'])}):</b> {esc(', '.join(s['reasons']))}\n"
           f"<b>Trend ({tfc['label']}):</b> {trend_line}\n"
           f"<b>Idősíkok:</b> {ctx_text} ({ok}/{len(mode['context'])} egyezik)\n"
           + levels_text(s) +
           f"\n<b>Ügylet terv</b>\n"
           f"  Belépő: ~ {fp(s['entry'])}\n"
           f"  Aktuális ár: ~ {fp(s['cur'])}\n"
           f"  Stop: ~ {fp(s['stop'])}\n"
           f"  Cél: ~ {fp(s['target'])} (1:{mode['rr']:g})\n\n"
           f"🕒 {candle}")
    if now is not None and tfc["min"] < 1440:
        dly = (now - (s["candle"] + pd.Timedelta(minutes=tfc["min"]))).total_seconds() / 60
        msg += f"\n⏱ Késés a gyertya zárásától: {max(0, int(round(dly)))} perc"
    if s["notes"]:
        msg += f"\nMegjegyzés: {esc(', '.join(s['notes']))}"
    if mode["name"] == "SWING":
        msg += "\n📌 Swing: napokig/hetekig tartó pozíció, a stop és a cél szélesebb."
    if s.get("blocker"):
        msg += (f"\n⚠️ <b>Figyelem:</b> {'ellenállás' if s['side'] == 'bull' else 'támasz'} a cél előtt "
                f"({fp(s['blocker'][0])}, {s['blocker'][1]}x), a cél nehezebben érhető el.")
    if abs(s["dist_atr"]) > 0.5:
        msg += "\n⚠️ <b>Figyelem:</b> az ár már messze van a belépőtől, valószínűleg elkésett."
    return msg + "\n\n<i>Ez csak jelzés, nem tanács. Ellenőrizd a grafikont az XTB-ben!</i>"


def fetch_raw(ticker, yf_interval, period, prepost=False):
    kw = {"prepost": True} if prepost else {}
    df = yf.download(ticker, period=period, interval=yf_interval, progress=False, auto_adjust=True, **kw)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def get_frame(tfk, ticker, raw, prepost=False):
    cfg = TF[tfk]
    prepost = prepost and cfg["yf"] in ("1m", "5m", "15m")
    key = (ticker, cfg["yf"], cfg["period"], prepost)
    if key not in raw:
        raw[key] = fetch_raw(ticker, cfg["yf"], cfg["period"], prepost)
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
    blocks = []
    for n in SYMBOLS:
        if n not in last_info:
            continue
        rsi, price, stale = last_info[n]
        def fresh_k(k):
            if TF[k]["min"] > 5:
                return True
            lf = fetched.get(f"{n}|{k}")
            return bool(lf) and (now - pd.Timestamp(lf)).total_seconds() < 1800
        arrows = "  ".join(f"{TF[k]['label']}{ARROW.get(ctx.get(f'{n}|{k}'), '?')}" for k in TF_ORDER if fresh_k(k))
        blocks.append(f"<b>{esc(n)}</b>" + (" (zárva)" if stale else "") + f"\n  {arrows}\n  RSI {rsi:.0f} · ár ~ {fp(price)}")
    send_long(f"📋 <b>Állapot</b> ({loc(now).strftime('%H:%M')})\n\n" + "\n\n".join(blocks)
             + "\n\n<i>↑ emelkedő · ↓ csökkenő · → vegyes. Az RSI az M15-ös. Belépőt csak külön jelzés ad.</i>")


HU_DAYS = ["Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek", "Szombat", "Vasárnap"]


def send_long(text):
    while text:
        chunk = text[:3500]
        if len(text) > 3500:
            cut = chunk.rfind("\n\n")
            if cut <= 0:
                cut = chunk.rfind("\n")
            if cut > 0:
                chunk = chunk[:cut]
        telegram(chunk)
        text = text[len(chunk):].lstrip("\n")


def completed_trend(df, days, now):
    """Trend a legutóbbi LEZÁRT gyertyán; az még alakuló gyertyát eldobja."""
    if df is None or df.empty or len(df) < 30:
        return None, df
    if not df.index[-1] + pd.Timedelta(days=days) <= now:
        df = df.iloc[:-1]
    return ctx_trend(df, True), df


def outlook_data(name, ticker, now):
    w = fetch_raw(ticker, "1wk", "5y")
    d = fetch_raw(ticker, "1d", "1y")
    m = fetch_raw(ticker, "1mo", "max")
    if w.empty or d.empty or len(w) < 30 or len(d) < 30:
        return None
    wt, w = completed_trend(w, 4, now)
    dtr, d = completed_trend(d, 1, now)
    mt, m = completed_trend(m, 27, now)
    last, prev = w.iloc[-1], w.iloc[-2]
    di = add_indicators(d).dropna()
    dl = di.iloc[-1]
    rsi, atr_d, ema21_d, px = float(dl.rsi), float(dl.atr), float(dl.ema21), float(dl.Close)
    atr_w = float(add_indicators(w).dropna().iloc[-1].atr)
    lv = find_levels(d, atr_d, 250)
    sup = sorted([x for x in lv if x[0] < px], key=lambda x: -x[0])[:2]
    res = sorted([x for x in lv if x[0] > px], key=lambda x: x[0])[:2]
    trends = (mt, wt, dtr)
    side = None
    for sd, opp in (("bull", "bear"), ("bear", "bull")):
        if sum(t == sd for t in trends) >= 2 and wt != opp and dtr != opp:
            side = sd
    votes = sum(t == side for t in trends) if side else 0
    zones = []
    if side:
        if abs(px - ema21_d) <= atr_d:
            zones.append(f"D1 EMA21 közelében ({fp(ema21_d)})")
        legs = fib_legs(d, atr_d, 120)
        if side in legs:
            ret, _ = fib_levels(side, *legs[side])
            lo_, hi_ = sorted((ret[0.382], ret[0.618]))
            if lo_ - 0.25 * atr_d <= px <= hi_ + 0.25 * atr_d:
                zones.append(f"Fibonacci zóna ({fp(lo_)} – {fp(hi_)})")
        if side == "bull":
            near = [p for p, n in lv if 0 <= px - p <= atr_d]
            if near:
                zones.append(f"támasz közelében ({fp(max(near))})")
        else:
            near = [p for p, n in lv if 0 <= p - px <= atr_d]
            if near:
                zones.append(f"ellenállás közelében ({fp(min(near))})")
    rsi_ok = (40 <= rsi <= 65) if side == "bull" else ((35 <= rsi <= 60) if side == "bear" else False)
    score = votes + len(zones) + (1 if rsi_ok else 0)
    risk = 2 * atr_d
    return {"name": name, "mt": mt, "wt": wt, "dt": dtr, "rsi": rsi, "px": px, "ema21": ema21_d,
            "chg": (last.Close / prev.Close - 1) * 100, "high": float(last.High), "low": float(last.Low),
            "close": float(last.Close), "atr_w": atr_w, "sup": sup, "res": res,
            "side": side, "votes": votes, "zones": zones, "score": score,
            "stop": px - risk if side == "bull" else px + risk,
            "target": px + 3 * risk if side == "bull" else px - 3 * risk}


def outlook_block(x):
    A = lambda t: ARROW.get(t, "?")
    if x["wt"] == x["dt"] == "bull":
        bias = "LONG oldalra dől"
    elif x["wt"] == x["dt"] == "bear":
        bias = "SHORT oldalra dől"
    else:
        bias = "vegyes, nincs tiszta irány"
    t = (f"<b>{esc(x['name'])}</b> · {bias}\n"
         f"  Trend: MN{A(x['mt'])} · W1{A(x['wt'])} · D1{A(x['dt'])}\n"
         f"  RSI (D1): {x['rsi']:.0f} · múlt hét: {x['chg']:+.1f}%\n"
         f"  Múlt heti csúcs: {fp(x['high'])} · mélypont: {fp(x['low'])} · zárás: {fp(x['close'])}\n"
         f"  Átlagos heti mozgás (ATR): {fp(x['atr_w'])}")
    if x["sup"]:
        t += "\n  D1 támasz: " + ", ".join(f"{fp(p)} ({n}x)" for p, n in x["sup"])
    if x["res"]:
        t += "\n  D1 ellenállás: " + ", ".join(f"{fp(p)} ({n}x)" for p, n in x["res"])
    return t


def swing_watchlist(datas):
    cands = sorted([x for x in datas if x["side"] and x["zones"]], key=lambda x: -x["score"])
    wait = [x for x in datas if x["side"] and not x["zones"]]
    none = [x for x in datas if not x["side"]]
    icon = lambda x: "🟢" if x["side"] == "bull" else "🔴"
    sd = lambda x: "LONG" if x["side"] == "bull" else "SHORT"
    t = "🎯 <b>Heti swing figyelőlista</b>\n<i>Trendben van, és visszahúzási zónában áll. A konkrét belépőt hét közben a H4 jelzés adja.</i>\n"
    if cands:
        for i, x in enumerate(cands, 1):
            t += (f"\n{i}. {icon(x)} <b>{esc(x['name'])}</b> · {sd(x)} · pontszám {x['score']}\n"
                  f"   Idősíkok: MN{ARROW.get(x['mt'], '?')} W1{ARROW.get(x['wt'], '?')} D1{ARROW.get(x['dt'], '?')} ({x['votes']}/3 egyezik)\n"
                  f"   Zóna: {esc(', '.join(x['zones']))}\n"
                  f"   RSI (D1): {x['rsi']:.0f}\n"
                  f"   Tájékoztató: stop ~ {fp(x['stop'])} · cél ~ {fp(x['target'])} (1:3)\n")
    else:
        t += "\nNincs ilyen jelölt a héten.\n"
    if wait:
        t += "\n⏳ <b>Trendben, visszahúzásra vár</b>\n"
        for x in wait:
            t += f" • {icon(x)} {esc(x['name'])} · {sd(x)} · figyeld a D1 EMA21-et: ~ {fp(x['ema21'])}\n"
    if none:
        t += "\n⛔ <b>Nincs tiszta swing-irány:</b> " + esc(", ".join(x["name"] for x in none)) + "\n"
    return t


def weekly_outlook(now):
    lt = loc(now)
    if state["outlook_date"] == str(lt.date()) or before(lt, SUNDAY_OUTLOOK_TIME):
        return
    state["outlook_date"] = str(lt.date())
    send_long(weekly_review(now))
    if USE_CALENDAR:
        cal_cache["t"] = None
        events = load_calendar()
        end = lt.date() + dt.timedelta(days=5)
        evs = sorted([e for e in events if e["time"] > now and loc(e["time"]).date() <= end], key=lambda x: x["time"])
        lines, counts = ["📅 <b>Jövő heti fontos gazdasági hírek</b> (magyar idő)"], {}
        if not evs:
            lines.append("\nNem találtam eseményt a naptár-feedben (lehet, hogy még nem frissült).")
        for day in sorted({loc(e["time"]).date() for e in evs}):
            de = [e for e in evs if loc(e["time"]).date() == day]
            lines.append(f"\n<b>{HU_DAYS[day.weekday()]}</b> ({day.strftime('%m.%d.')})")
            lines += event_lines(de)
            for e in de:
                counts[e["cur"]] = counts.get(e["cur"], 0) + 1
        if counts:
            top = sorted(counts.items(), key=lambda x: -x[1])[:3]
            lines.append("\n<i>Legtöbb hír: " + ", ".join(f"{esc(c)} ({n})" for c, n in top) + "</i>")
        send_long("\n".join(lines))
    datas = []
    for name, ticker in SYMBOLS.items():
        try:
            x = outlook_data(name, ticker, now)
            if x:
                datas.append(x)
        except Exception as ex:
            print(name, "heti kép hiba:", ex)
    if datas:
        send_long(swing_watchlist(datas))
        if USE_PLANS:
            blocks = []
            for name, ticker in SYMBOLS.items():
                try:
                    pl = build_plans(name, ticker, now, ("swing",)).get("swing")
                    if pl:
                        plans[f"{name}|swing"] = pl
                        blocks.append(plan_block(pl))
                    time.sleep(0.3)
                except Exception as ex:
                    print(name, "heti terv hiba:", ex)
            if blocks:
                send_long(f"🗺 <b>Heti swing terv</b>\n{PLAN_NOTE}\n\n" + PLAN_SEP.join(blocks) + PLAN_LEGEND)
        send_long("📊 <b>Heti kép piacnyitás előtt</b>\n\n" + "\n\n".join(outlook_block(x) for x in datas)
                  + "\n\n<i>↑ emelkedő · ↓ csökkenő · → vegyes. Az átlagos heti mozgás a 14 hetes ATR."
                  + " Forex és arany kb. 23:00-kor nyit (magyar idő), a normál jelzések ekkortól indulnak."
                  + " Hétfőn 7:00 után jön a napi hírösszefoglaló.</i>")


def record_signal(name, mode, tfc, s):
    """Elmenti a kiküldött jelzést az utólagos eredménykövetéshez."""
    t_close = s["candle"] + pd.Timedelta(minutes=tfc["min"])
    signals.append({"name": name, "mode": mode["name"], "tf": tfc["label"], "side": s["side"],
                    "t": str(t_close), "entry": float(s["entry"]), "stop": float(s["stop"]),
                    "target": float(s["target"]), "rr": mode["rr"], "reasons": list(s["reasons"]),
                    "aligned": bool(s["aligned"]), "tfk": mode["entry"], "status": "open", "outcome": None, "r": None, "resolved": None})


def resolve_signals(name, frames, now):
    """A nyitott jelzéseket a jelzés utáni gyertyákon kiértékeli (M1/M5 jelzésnél azok a gyertyák, egyébként M15): cél, stop vagy lejárat.
    Ha egy gyertyában a stop és a cél is érintve van, óvatosan stopnak számít."""
    for sg in signals:
        if sg["name"] != name or sg["status"] != "open":
            continue
        rk = sg.get("tfk") if sg.get("tfk") in ("1m", "5m") else "15m"
        dfr = frames.get(rk)
        if dfr is None or dfr.empty:
            continue
        t0 = pd.Timestamp(sg["t"])
        sub = dfr[dfr.index >= t0]
        long_ = sg["side"] == "bull"
        out = None
        for ts, c in sub.iterrows():
            hit_stop = c.Low <= sg["stop"] if long_ else c.High >= sg["stop"]
            hit_tgt = c.High >= sg["target"] if long_ else c.Low <= sg["target"]
            if hit_stop:
                out = ("stop", -1.0, ts)
                break
            if hit_tgt:
                out = ("target", float(sg["rr"]), ts)
                break
        if out:
            sg.update(status="closed", outcome=out[0], r=out[1], resolved=str(out[2]))
            continue
        limit = EXPIRE_DAYS.get(sg["mode"], 2)
        if (now - t0).total_seconds() / 86400 >= limit:
            risk = abs(sg["entry"] - sg["stop"]) or 1e-9
            last = float(dfr.Close.iloc[-1])
            r = (last - sg["entry"]) / risk if long_ else (sg["entry"] - last) / risk
            sg.update(status="closed", outcome="expired", r=round(r, 2), resolved=str(now))


def summarize(sgs):
    closed = [x for x in sgs if x["status"] == "closed"]
    wins = [x for x in closed if x["outcome"] == "target"]
    losses = [x for x in closed if x["outcome"] == "stop"]
    dec = len(wins) + len(losses)
    return {"n": len(sgs), "closed": len(closed), "open": len(sgs) - len(closed), "wins": len(wins),
            "losses": len(losses), "exp": len(closed) - dec, "hit": (len(wins) / dec * 100) if dec else None,
            "tot": sum(x["r"] for x in closed)}


def stat_line(label, sgs):
    m = summarize(sgs)
    if not m["closed"]:
        return f"  {esc(label)}: {m['n']} jelzés · mind nyitott"
    hit = f"{m['hit']:.0f}%" if m["hit"] is not None else "-"
    return f"  {esc(label)}: {m['n']} jelzés · találat {hit} · {m['tot']:+.1f}R"


def weekly_review(now):
    wk = [x for x in signals if pd.Timestamp(x["t"]) >= now - pd.Timedelta(days=7)]
    head = "📈 <b>Heti eredmény-összesítő</b> (elmúlt 7 nap)\n"
    if not wk:
        return head + "\nAz elmúlt 7 napban nem volt jelzés."
    m = summarize(wk)
    t = head + ("<i>Szimulált eredmény: belépő = a jelzés záróára, a stop és a cél a gyertyák csúcsa/mélypontja alapján. "
                "Nem valós ügylet, a költségeket (spread) nem tartalmazza. Az R a kockázat egysége: -1R = stop, +2R vagy +3R = cél.</i>\n\n")
    t += f"Jelzések: {m['n']} · lezárt: {m['closed']} · nyitott: {m['open']}\n"
    t += f"✅ Cél: {m['wins']} · ❌ Stop: {m['losses']} · ⌛ Lejárt: {m['exp']}\n"
    if m["hit"] is not None:
        t += f"Találati arány: {m['hit']:.0f}% · összesen: {m['tot']:+.1f}R"
        if m["closed"]:
            t += f" · átlag: {m['tot'] / m['closed']:+.2f}R"
        t += "\n"
    t += "\n<b>Módonként</b>\n"
    for md in [m["name"] for m in MODES]:
        g = [x for x in wk if x["mode"] == md]
        if g:
            t += stat_line(md.capitalize(), g) + "\n"
    t += "\n<b>Irány szerint</b>\n"
    for sd, lb in (("bull", "LONG"), ("bear", "SHORT")):
        g = [x for x in wk if x["side"] == sd]
        if g:
            t += stat_line(lb, g) + "\n"
    names = sorted({x["name"] for x in wk})
    rows = []
    for n in names:
        g = [x for x in wk if x["name"] == n]
        rows.append((summarize(g)["tot"], n, g))
    t += "\n<b>Eszközönként</b> (legjobbtól)\n"
    for _, n, g in sorted(rows, key=lambda r: -r[0]):
        t += stat_line(n, g) + "\n"
    by = {}
    for x in wk:
        if x["status"] != "closed":
            continue
        for r in x["reasons"]:
            k = re.sub(r"\s*\(.*?\)", "", r)
            k = re.sub(r"\s*\d+(\.\d+)?%", "", k).strip()
            if k.startswith("RSI"):
                continue
            by.setdefault(k, []).append(x)
    rs = [(k, v) for k, v in by.items() if len(v) >= 3]
    if rs:
        t += "\n<b>Jelek szerint</b> (min. 3 lezárt jelzés; egy jelzés több jelhez is számít)\n"
        for k, v in sorted(rs, key=lambda kv: -summarize(kv[1])["tot"]):
            t += stat_line(k, v) + "\n"
    closed = [x for x in wk if x["status"] == "closed"]
    if closed:
        b = max(closed, key=lambda x: x["r"])
        w = min(closed, key=lambda x: x["r"])
        tag = lambda x: f"{esc(x['name'])} {'LONG' if x['side'] == 'bull' else 'SHORT'} {x['r']:+.1f}R"
        t += f"\n🏆 Legjobb: {tag(b)}\n📉 Legrosszabb: {tag(w)}\n"
    allm = summarize(signals)
    if allm["closed"]:
        hit = f"{allm['hit']:.0f}%" if allm["hit"] is not None else "-"
        t += (f"\n<i>Az elmúlt ~{SIGNAL_KEEP_DAYS} nap: {allm['closed']} lezárt jelzés · találat {hit} · {allm['tot']:+.1f}R. "
              "Kevés jelzésnél az arányok nem megbízhatók.</i>")
    return t


def check_spikes(name, frames, now):
    """Erős mozgás riasztás: ha az ár 3 gyertya alatt SPIKE_ATR × ATR-t mozdul, szól, akkor is, ha a trendszűrők még nem adnának belépőt."""
    for tfk in SPIKE_TFS:
        df = frames.get(tfk)
        if df is None:
            continue
        ai = add_indicators(df).dropna()
        closed = ai.iloc[:-1]
        if len(closed) < 10:
            continue
        tfm = TF[tfk]["min"]
        if now - (closed.index[-1] + pd.Timedelta(minutes=tfm)) > pd.Timedelta(minutes=max(3 * tfm, 5)):
            continue                                    # régi adat / zárva
        n = 3
        c0, c1 = float(closed.Close.iloc[-1 - n]), float(closed.Close.iloc[-1])
        atr0 = float(closed.atr.iloc[-1 - n])
        move = c1 - c0
        if atr0 <= 0 or abs(move) < SPIKE_ATR * atr0:
            continue
        side = "bull" if move > 0 else "bear"
        key = f"{name}|{side}"
        last = spike.get(key)
        if last and (now - pd.Timestamp(last)).total_seconds() / 60 < SPIKE_COOLDOWN_MIN:
            return
        spike[key] = str(now)
        pct = move / c0 * 100
        ks = ["1d", "1h", "15m", "5m"]
        arrows = " | ".join(f"{TF[k]['label']} {ARROW.get(ctx.get(f'{name}|{k}'), '?')}" for k in ks)
        agree = sum(ctx.get(f"{name}|{k}") == side for k in ks)
        verdict = ("✓ az idősíkok többsége a mozgás irányába áll" if agree >= 3 else
                   "⚠️ az idősíkok még nem egyeznek, ezért nincs belépő jelzés (a mozgás gyorsabb a trendszűrőknél)")
        telegram(f"⚡ <b>{esc(name)}</b> · erős mozgás {'felfelé 🟢' if side == 'bull' else 'lefelé 🔴'}\n"
                 f"<i>{TF[tfk]['label']} · utolsó {n * tfm} perc</i>\n\n"
                 f"Mozgás: {pct:+.2f}% ({abs(move) / atr0:.1f} × ATR)\n"
                 f"Ár: ~ {fp(c1)} (korábban ~ {fp(c0)})\n"
                 f"Idősíkok: {arrows}\n{verdict}\n\n"
                 f"<i>Ez nem belépő jelzés, csak figyelmeztetés. Nézd meg a grafikont az XTB-ben!</i>")
        return


def check_setups(name, frames, modes, now):
    """Előzetes figyelmeztetés: a trend és az idősíkok egyeznek, az ár belépő zóna közelében jár (EMA21, szint, Fibonacci, kitörési szint),
    de a lezárt gyertyás jel még nem jött. Nem jóslat: csak jelzi, hová figyelj."""
    for mode in modes:
        if mode["name"] not in SETUP_MODES:
            continue
        e = mode["entry"]
        df = frames.get(e)
        if df is None:
            continue
        tfc = TF[e]
        ai = add_indicators(df).dropna()
        if len(ai) < 40:
            continue
        d = ai.iloc[:-1]
        last = d.iloc[-1]
        if now - (d.index[-1] + pd.Timedelta(minutes=tfc["min"])) > pd.Timedelta(minutes=max(3 * tfc["min"], 5)):
            continue
        trend = trend_of(last)
        if trend == "mixed":
            continue
        if mode["news"] and blackout_reason(name, now):
            continue
        ctx_text, ok = ctx_line(name, mode, trend)
        if ok < mode["min_align"]:
            continue
        key = f"{name}|{trend}"
        lt_ = setup.get(key)
        if lt_ and (now - pd.Timestamp(lt_)).total_seconds() / 60 < SETUP_COOLDOWN_MIN:
            continue
        atr = float(last.atr)
        px = float(ai.iloc[-1].Close)
        levels = find_levels(d, atr, mode.get("lookback", 250))
        near = []
        if trend == "bull":
            ema = float(last.ema21)
            if 0 <= px - ema <= 0.5 * atr and float(d.High.iloc[-10:].max()) - px >= atr:
                near.append((f"EMA21 ~ {fp(ema)}", (px - ema) / atr, "visszahúzás után ott a trend újraindulhat"))
            for p, n in levels:
                if 0 <= px - p <= 0.5 * atr:
                    near.append((f"támasz ~ {fp(p)} ({n}x)", (px - p) / atr, "visszapattanhat"))
        else:
            ema = float(last.ema21)
            if 0 <= ema - px <= 0.5 * atr and px - float(d.Low.iloc[-10:].min()) >= atr:
                near.append((f"EMA21 ~ {fp(ema)}", (ema - px) / atr, "visszahúzás után ott a trend újraindulhat"))
            for p, n in levels:
                if 0 <= p - px <= 0.5 * atr:
                    near.append((f"ellenállás ~ {fp(p)} ({n}x)", (p - px) / atr, "visszafordulhat"))
        legs = fib_legs(d, atr, mode.get("fib_lookback", 120))
        if trend in legs and legs[trend][0] <= px <= legs[trend][1]:
            ret, _ = fib_levels(trend, *legs[trend])
            for r in (0.382, 0.5, 0.618):
                if abs(px - ret[r]) <= 0.3 * atr:
                    near.append((f"Fibonacci {r * 100:.1f}% ~ {fp(ret[r])}", abs(px - ret[r]) / atr, "visszapattanhat"))
        if not near:
            continue
        near.sort(key=lambda x: x[1])
        setup[key] = str(now)
        icon = "🟢" if trend == "bull" else "🔴"
        lines = "\n".join(f"  • {esc(w)} · {dd:.1f} ATR-re · {esc(h)}" for w, dd, h in near[:3])
        telegram(f"👀 <b>{esc(name)}</b> · készülő jel {icon} {'LONG' if trend == 'bull' else 'SHORT'}\n"
                 f"<i>{esc(mode['name'].capitalize())} · {tfc['label']}</i>\n\n"
                 f"<b>Közel van:</b>\n{lines}\n\n"
                 f"<b>Trend ({tfc['label']}):</b> {TREND_TXT[trend]} ✓\n"
                 f"<b>Idősíkok:</b> {ctx_text} ({ok}/{len(mode['context'])} egyezik)\n"
                 f"Ár: ~ {fp(px)}\n\n"
                 f"<i>Ez még nem belépő jelzés, csak előzetes figyelmeztetés: a belépő jel akkor jön, ha az ár a zónából a trend irányába visszafordul, vagy lezár a szint fölött/alatt. Nem garancia.</i>")
        return


# ---------- Előre elemzés: napi és swing terv ----------
def merge_levels(lv, atr):
    zones = []
    for p, n in sorted(lv):
        if zones and abs(p - zones[-1][0] / zones[-1][1]) <= 0.5 * atr:
            zones[-1][0] += p * n
            zones[-1][1] += n
        else:
            zones.append([p * n, n])
    return [(z[0] / z[1], z[1]) for z in zones]


def detect_flag_forming(d, atr, pole_n=10, flag_n=6):
    """Alakuló bull/bear flag: erős rúd után szűkülő, enyhén visszahúzó konszolidáció, a kitörés még nem történt meg."""
    if len(d) < pole_n + flag_n + 2:
        return None
    flag = d.iloc[-flag_n:]
    pole = d.iloc[-(flag_n + pole_n):-flag_n]
    move = float(pole.Close.iloc[-1] - pole.Close.iloc[0])
    slope = float(np.polyfit(range(flag_n), flag.Close.values, 1)[0])
    rng = float(flag.High.max() - flag.Low.min())
    last_close = float(d.Close.iloc[-1])
    if move > 3 * atr:
        retr = (float(pole.Close.iloc[-1]) - float(flag.Low.min())) / move
        if retr <= 0.5 and rng < 0.6 * move and slope <= 0 and last_close <= float(flag.High.max()):
            return {"side": "bull", "level": float(flag.High.max()), "stop": float(flag.Low.min()), "target": float(flag.High.max()) + move}
    if move < -3 * atr:
        retr = (float(flag.High.max()) - float(pole.Close.iloc[-1])) / (-move)
        if retr <= 0.5 and rng < 0.6 * (-move) and slope >= 0 and last_close >= float(flag.Low.min()):
            return {"side": "bear", "level": float(flag.Low.min()), "stop": float(flag.High.max()), "target": float(flag.Low.min()) + move}
    return None


def trendline_values(d, lookback=100):
    """A legutóbbi két mélypontra illesztett emelkedő támasz és a két csúcsra illesztett ereszkedő ellenállás értéke most."""
    d2 = d.iloc[-lookback:]
    H, L = d2.High.values, d2.Low.values
    hi, lo = pivots(H, L)
    n = len(d2) - 1
    sup = res = None
    if len(lo) >= 2 and lo[-1] - lo[-2] >= 5:
        i1, i2 = lo[-2], lo[-1]
        sl = (L[i2] - L[i1]) / (i2 - i1)
        if sl > 0:
            sup = float(L[i2] + sl * (n - i2))
    if len(hi) >= 2 and hi[-1] - hi[-2] >= 5:
        i1, i2 = hi[-2], hi[-1]
        sl = (H[i2] - H[i1]) / (i2 - i1)
        if sl < 0:
            res = float(H[i2] + sl * (n - i2))
    return sup, res


def make_plan(kind, name, trends, side, px, ref, now):
    """Forgatókönyv-terv: belépő zónák (szint, EMA, Fibonacci, trendvonal), kitörés, alakuló flag, stop, célok, érvénytelenítés."""
    atr, lv = ref["atr"], ref["levels"]
    bull = side == "bull"
    tl_sup, tl_res = ref.get("tl", (None, None))

    def targets(entry, risk, up, extra=()):
        sg = 1 if up else -1
        cand = [p for p, n in lv] + list(extra)
        pool = sorted({round(p, 10) for p in cand if 1.2 * risk <= sg * (p - entry) <= 4 * risk}, key=lambda p: sg * p)[:2]
        defaults = [entry + sg * 2 * risk, entry + sg * 3 * risk]
        while len(pool) < 2:
            pool.append(defaults[len(pool)])
        if sg * (pool[1] - pool[0]) < 0.5 * risk:          # a két cél legalább fél kockázatnyira legyen egymástól
            pool[1] = pool[0] + sg * risk
        return pool[0], pool[1]

    def entry_dict(kind_, label, level, lo, hi, stop, up, extra=()):
        risk = abs(level - stop)
        t1, t2 = targets(level, risk, up, extra)
        return {"kind": kind_, "label": label, "level": level, "lo": lo, "hi": hi, "stop": stop, "t1": t1, "t2": t2,
                "r1": abs(t1 - level) / risk, "r2": abs(t2 - level) / risk, "up": up, "zone_hit": False, "t1_hit": False}

    entries = []
    if side:
        cands = []
        if (bull and px > ref["ema21"]) or (not bull and px < ref["ema21"]):
            cands.append((f"EMA21 ({ref['ref_label']})", ref["ema21"]))
        for p, n in lv:
            if (bull and p < px) or (not bull and p > px):
                cands.append((("támasz" if bull else "ellenállás") + f" ({n}x)", p))
        if bull and tl_sup is not None and tl_sup < px:
            cands.append(("emelkedő trendvonal", tl_sup))
        if (not bull) and tl_res is not None and tl_res > px:
            cands.append(("ereszkedő trendvonal", tl_res))
        leg = ref["legs"].get(side)
        if leg and leg[0] <= px <= leg[1]:
            ret, _ = fib_levels(side, *leg)
            for r in (0.382, 0.5, 0.618):
                if (bull and ret[r] < px) or (not bull and ret[r] > px):
                    cands.append((f"Fibonacci {r * 100:.1f}%", ret[r]))
        cands = sorted([c for c in cands if 0 < abs(px - c[1]) <= 3 * atr], key=lambda c: abs(px - c[1]))
        clusters = []
        for lab, p in cands:
            for cl in clusters:
                if abs(cl["level"] - p) <= 0.5 * atr:
                    cl["labels"].append(lab)
                    cl["ps"].append(p)
                    cl["level"] = sum(cl["ps"]) / len(cl["ps"])
                    break
            else:
                clusters.append({"labels": [lab], "ps": [p], "level": p})
        for cl in clusters[:2]:
            lvl = cl["level"]
            stop = lvl - 0.75 * atr if bull else lvl + 0.75 * atr
            entries.append(entry_dict("pullback", " + ".join(cl["labels"]), lvl, lvl - 0.25 * atr, lvl + 0.25 * atr, stop, bull))

    def breakout(up):
        opts = []
        if up:
            above = sorted(p for p, n in lv if p > px)
            level = above[0] if above else (ref["hh"] if ref["hh"] > px else None)
            if level is not None:
                opts.append(("szint áttörése felfelé", level))
            if tl_res is not None and tl_res > px:
                opts.append(("ereszkedő trendvonal áttörése", tl_res))
        else:
            below = sorted((p for p, n in lv if p < px), reverse=True)
            level = below[0] if below else (ref["ll"] if ref["ll"] < px else None)
            if level is not None:
                opts.append(("szint áttörése lefelé", level))
            if tl_sup is not None and tl_sup < px:
                opts.append(("emelkedő trendvonal letörése", tl_sup))
        opts = [o for o in opts if abs(o[1] - px) <= 3 * atr]
        if not opts:
            return None
        label, level = min(opts, key=lambda o: abs(o[1] - px))
        trig = level + 0.1 * atr if up else level - 0.1 * atr
        stop = level - atr if up else level + atr
        return entry_dict("breakout", label, trig, trig, trig, stop, up)

    for up in ([bull] if side else [True, False]):
        b = breakout(up)
        if b:
            entries.append(b)

    for fl in ref.get("flags", []):
        up = fl["side"] == "bull"
        if side and up != bull:
            continue
        if abs(fl["level"] - px) > 3 * atr:
            continue
        trig = fl["level"] + 0.1 * atr if up else fl["level"] - 0.1 * atr
        stop = fl["stop"] - 0.25 * atr if up else fl["stop"] + 0.25 * atr
        if abs(trig - stop) <= 0.2 * atr:
            continue
        entries.append(entry_dict("flag", f"alakuló {'bull' if up else 'bear'} flag ({fl['tf']})", trig, trig, trig, stop, up, (fl["target"],)))
        break

    inv = None
    pull = [e for e in entries if e["kind"] == "pullback"]
    if side:
        if pull:
            inv = min(e["stop"] for e in pull) if bull else max(e["stop"] for e in pull)
        else:
            near = sorted((p for p, n in lv if (p < px if bull else p > px)), reverse=bull)
            inv = (near[0] - 0.5 * atr if bull else near[0] + 0.5 * atr) if near else (px - 2 * atr if bull else px + 2 * atr)
    below = sorted((p for p, n in lv if p < px), reverse=True)
    above = sorted(p for p, n in lv if p > px)
    return {"name": name, "kind": kind, "side": side, "trends": trends, "px": px, "atr": atr, "ref_label": ref["ref_label"],
            "inv": inv, "entries": entries, "status": "active", "created": str(now), "date": str(loc(now).date()),
            "rsi": ref.get("rsi"), "pattern": ref.get("pattern", ""),
            "range": (below[0] if below else None, above[0] if above else None)}


def build_plans(name, ticker, now, kinds=("day", "swing")):
    """Letölti az adatot és elkészíti a napi / swing tervet."""
    pp = name in PREPOST_SYMBOLS
    out = {"day": None, "swing": None}
    d = fetch_raw(ticker, "1d", "2y")
    if d.empty or len(d) < 60:
        return out
    dt_, d = completed_trend(d, 1, now)
    dd = add_indicators(d).dropna()
    if len(dd) < 30:
        return out
    atr_d = float(dd.iloc[-1].atr)
    m15 = fetch_raw(ticker, "15m", "5d", pp)
    px = float(m15.Close.iloc[-1]) if not m15.empty else float(dd.iloc[-1].Close)

    def pat(last, prev):
        b, r, _ = candle_patterns(last, prev)
        return ", ".join(b + r)

    if "day" in kinds:
        h = fetch_raw(ticker, "1h", "180d")
        if not h.empty and len(h) > 60:
            h4 = h.resample("4h", origin="start_day").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            tr = {"1d": dt_, "4h": ctx_trend(h4), "1h": ctx_trend(h)}
            hi = add_indicators(h).dropna()
            if len(hi) > 30:
                hc = hi.iloc[:-1]
                last, prev = hc.iloc[-1], hc.iloc[-2]
                atr_h = float(last.atr)
                lvl = merge_levels(find_levels(hc, atr_h, 250) + find_levels(d, atr_d, 250), atr_h)
                votes = list(tr.values())
                side = "bull" if votes.count("bull") >= 2 else ("bear" if votes.count("bear") >= 2 else None)
                flags = []
                if not m15.empty:
                    mi = add_indicators(m15).dropna()
                    if len(mi) > 40:
                        mc = mi.iloc[:-1]
                        f_ = detect_flag_forming(mc, float(mc.iloc[-1].atr))
                        if f_:
                            flags.append(dict(f_, tf="M15"))
                f_ = detect_flag_forming(hc, atr_h)
                if f_:
                    flags.append(dict(f_, tf="H1"))
                ref = {"atr": atr_h, "ema21": float(last.ema21), "levels": lvl, "legs": fib_legs(hc, atr_h, 120),
                       "hh": float(hc.High.iloc[-20:].max()), "ll": float(hc.Low.iloc[-20:].min()), "ref_label": "H1",
                       "tl": trendline_values(hc), "flags": flags, "rsi": float(last.rsi), "pattern": pat(last, prev)}
                out["day"] = make_plan("day", name, tr, side, px, ref, now)
    if "swing" in kinds:
        w = fetch_raw(ticker, "1wk", "10y")
        mo = fetch_raw(ticker, "1mo", "max")
        wt, w = completed_trend(w, 4, now) if not w.empty else (None, w)
        mt, mo = completed_trend(mo, 27, now) if not mo.empty else (None, mo)
        tr = {"1mo": mt, "1wk": wt, "1d": dt_}
        last, prev = dd.iloc[-1], dd.iloc[-2]
        big = []
        if not w.empty and len(w) > 30:
            wi = add_indicators(w).dropna()
            if len(wi) > 20:
                big = find_levels(w, float(wi.iloc[-1].atr), 150)
        lvl = merge_levels(find_levels(d, atr_d, 250) + big, atr_d)
        votes = list(tr.values())
        side = "bull" if votes.count("bull") >= 2 and wt != "bear" and dt_ != "bear" else (
            "bear" if votes.count("bear") >= 2 and wt != "bull" and dt_ != "bull" else None)
        f_ = detect_flag_forming(dd, atr_d)
        ref = {"atr": atr_d, "ema21": float(last.ema21), "levels": lvl, "legs": fib_legs(d, atr_d, 120),
               "hh": float(d.High.iloc[-20:].max()), "ll": float(d.Low.iloc[-20:].min()), "ref_label": "D1",
               "tl": trendline_values(dd), "flags": ([dict(f_, tf="D1")] if f_ else []),
               "rsi": float(last.rsi), "pattern": pat(last, prev)}
        out["swing"] = make_plan("swing", name, tr, side, px, ref, now)
    return out


NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def plan_block(p):
    icon = {"bull": "🟢", "bear": "🔴", None: "⚪"}[p["side"]]
    dtxt = {"bull": "LONG", "bear": "SHORT", None: "nincs tiszta irány"}[p["side"]]
    kt = "napi terv" if p["kind"] == "day" else "swing terv"
    tl = " ".join(f"{TF[k]['label']}{ARROW.get(v, '?')}" for k, v in p["trends"].items())
    rsi = p.get("rsi")
    rsi_txt = ""
    if rsi is not None:
        rsi_txt = f"\nRSI ({p['ref_label']}): {rsi:.0f} · " + ("túlvett" if rsi >= 70 else ("túladott" if rsi <= 30 else "nincs szélsőség"))
        if p.get("pattern"):
            rsi_txt += f" · gyertya: {esc(p['pattern'])}"
    t = f"{icon} <b>{esc(p['name'])}</b> · {dtxt} · <i>{kt}</i>\nÁr: ~ {fp(p['px'])} · Trend: {tl}{rsi_txt}"
    if p["side"] is None and p.get("range") and (p["range"][0] or p["range"][1]):
        lo_, hi_ = p["range"]
        t += "\nTartomány: " + (f"támasz {fp(lo_)}" if lo_ else "") + (" · " if lo_ and hi_ else "") + (f"ellenállás {fp(hi_)}" if hi_ else "")
    for i, e in enumerate(p["entries"]):
        sd = "vétel" if e["up"] else "eladás"
        if e["kind"] == "pullback":
            head, line = f"Visszahúzás – {sd} · {esc(e['label'])}", f"📍 Zóna: {fp(e['lo'])} – {fp(e['hi'])}"
        elif e["kind"] == "flag":
            head, line = f"{esc(e['label'])[:1].upper() + esc(e['label'])[1:]} – {sd}", f"🚀 Belépő: {fp(e['level'])} {'fölött' if e['up'] else 'alatt'} lezáró gyertya"
        else:
            head, line = f"Kitörés – {sd} · {esc(e['label'])}", f"🚀 Belépő: {fp(e['level'])} {'fölött' if e['up'] else 'alatt'} lezáró gyertya"
        t += (f"\n\n{NUMS[min(i, 4)]} <b>{head}</b>\n   {line}\n"
              f"   Stop: {fp(e['stop'])} · Cél: {fp(e['t1'])} ({e['r1']:.1f}R) → {fp(e['t2'])} ({e['r2']:.1f}R)")
    if not p["entries"]:
        t += "\n\nNincs közeli, érdemi zóna: ne kergesd az árat, várj visszahúzásra."
    if p["side"] and p["inv"] is not None:
        t += f"\n\n⛔ Érvénytelen: {fp(p['inv'])} {'alatt' if p['side'] == 'bull' else 'fölött'} lezárva"
    first = p["entries"][0] if p["entries"] else None
    if first and p["side"]:
        t += f"\n🧭 Várható mozgás: {fp(first['level'])} → {fp(first['t2'])}"
    return t


PLAN_SEP = "\n\n──────────\n\n"
PLAN_NOTE = "<i>Forgatókönyv, nem garancia. A zónákra érdemes árriasztást vagy függő megbízást beállítani az XTB-ben.</i>"
PLAN_LEGEND = ("\n\n<i>📍 visszahúzásos belépő · 🚀 kitörés / alakuló formáció · ⛔ érvénytelenítés · "
               "R = a kockázat egysége (2R = a stop távolság kétszerese) · ↑ emelkedő ↓ csökkenő → vegyes</i>")


def plan_changed(old, new):
    if old is None or new is None:
        return old is not new
    if old["side"] != new["side"] or len(old["entries"]) != len(new["entries"]):
        return True
    return any(abs(a["level"] - b["level"]) > 0.5 * new["atr"] for a, b in zip(old["entries"], new["entries"]))


def plans_schedule(now, active):
    """Időzített tervküldés: reggel teljes napi terv, délután csak a megváltozottak; swing frissítés csak változáskor."""
    if not USE_PLANS:
        return
    lt = loc(now)
    wd = lt.weekday()
    slots = DAY_PLAN_TIMES if wd < 5 else [WEEKEND_PLAN_TIME]
    for i, hm in enumerate(slots):
        key = f"{lt.date()}|{hm[0]:02d}:{hm[1]:02d}"
        if key in plan_slots or before(lt, hm):
            continue
        plan_slots[key] = 1
        day_blocks, swing_blocks = [], []
        for name, ticker in active.items():
            try:
                pl = build_plans(name, ticker, now)
                time.sleep(0.3)
            except Exception as ex:
                print(name, "terv hiba:", ex)
                continue
            for kind, blocks in (("day", day_blocks), ("swing", swing_blocks)):
                new = pl.get(kind)
                if not new:
                    continue
                k = f"{name}|{kind}"
                old = plans.get(k)
                fresh_day = kind == "day" and (i == 0 or not old or old.get("date") != str(lt.date()))
                if fresh_day or plan_changed(old, new):
                    plans[k] = new
                    blocks.append(plan_block(new))
        if day_blocks:
            head = "🗺 <b>Napi terv</b>" if i == 0 else "🗺 <b>Napi terv frissítés</b> (csak ami változott)"
            send_long(f"{head} ({lt.strftime('%H:%M')}, magyar idő)\n{PLAN_NOTE}\n\n" + PLAN_SEP.join(day_blocks) + PLAN_LEGEND)
        if swing_blocks and wd < 5:
            send_long(f"🗺 <b>Swing terv frissítés</b> (csak ami változott)\n{PLAN_NOTE}\n\n" + PLAN_SEP.join(swing_blocks) + PLAN_LEGEND)


def plans_monitor(name, frames, now):
    """Folyamatos figyelés: zónába érés, kitörés, 1. cél, érvénytelenné válás -> értesítés (egyszer / terv)."""
    m15 = frames.get("15m")
    if m15 is None or len(m15) < 3:
        return
    cur = float(m15.Close.iloc[-1])
    today = str(loc(now).date())
    for kind in ("day", "swing"):
        k = f"{name}|{kind}"
        p = plans.get(k)
        if not p or p["status"] != "active" or (kind == "day" and p["date"] != today):
            continue
        if kind == "swing":
            h4 = frames.get("4h")
            conf = float(h4.Close.iloc[-2]) if h4 is not None and len(h4) > 3 else None
        else:
            conf = float(m15.Close.iloc[-2])
        atr = p["atr"]
        kt = "napi" if kind == "day" else "swing"
        icon = "🟢" if p["side"] == "bull" else ("🔴" if p["side"] == "bear" else "⚪")
        for e in p["entries"]:
            dirtxt = "LONG" if e["up"] else "SHORT"
            if e["kind"] == "pullback" and not e["zone_hit"] and abs(cur - e["level"]) <= PLAN_ZONE_ATR * atr:
                e["zone_hit"] = True
                telegram(f"📍 <b>{esc(name)}</b> · zónába ért ({kt} terv) {icon} {dirtxt}\n\n"
                         f"Ár: ~ {fp(cur)} · zóna: {fp(e['lo'])} – {fp(e['hi'])} ({esc(e['label'])})\n"
                         f"Stop: {fp(e['stop'])} · Cél 1: {fp(e['t1'])} · Cél 2: {fp(e['t2'])}\n\n"
                         f"<i>Ez a terv szerinti zóna, nem automatikus belépő: várd meg, hogy az ár innen a terv irányába visszaforduljon, "
                         f"és nézd meg a grafikont az XTB-ben.</i>")
            if e["kind"] in ("breakout", "flag") and not e["zone_hit"] and conf is not None and ((conf > e["level"]) if e["up"] else (conf < e["level"])):
                e["zone_hit"] = True
                telegram(f"🚀 <b>{esc(name)}</b> · kitörés: {esc(e['label'])} ({kt} terv) {icon} {dirtxt}\n\n"
                         f"Lezáró gyertya: {fp(conf)} · szint: {fp(e['level'])}\n"
                         f"Stop: {fp(e['stop'])} · Cél 1: {fp(e['t1'])} · Cél 2: {fp(e['t2'])}\n"
                         f"Ár most: ~ {fp(cur)}\n\n<i>A kitörés megerősítve, de az ár már mozoghatott: nézd meg a távolságot a szinttől.</i>")
            if e["zone_hit"] and not e["t1_hit"] and ((cur >= e["t1"]) if e["up"] else (cur <= e["t1"])):
                e["t1_hit"] = True
                telegram(f"🎯 <b>{esc(name)}</b> · 1. cél elérve ({kt} terv) {dirtxt}: {fp(e['t1'])}\n\n"
                         f"<i>Ilyenkor szokás a stopot a belépőre húzni vagy részben zárni. A döntés a tiéd.</i>")
        if p["side"] and p["inv"] is not None and conf is not None and ((conf < p["inv"]) if p["side"] == "bull" else (conf > p["inv"])):
            p["status"] = "invalid"
            telegram(f"⛔ <b>{esc(name)}</b> · a {kt} terv érvénytelen\n\n"
                     f"Az ár lezárt {fp(p['inv'])} {'alatt' if p['side'] == 'bull' else 'fölött'}: a régi belépők már nem érvényesek.")
            try:
                new = build_plans(name, SYMBOLS[name], now, (kind,)).get(kind)
            except Exception as ex:
                print(name, "újraterv hiba:", ex)
                new = None
            if new:
                plans[k] = new
                send_long("🗺 <b>Új terv</b>\n\n" + plan_block(new))


def run_once(now=None):
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    lt = loc(now)
    silent = False                    # hétvégi csend: csak a WEEKEND_SYMBOLS jelezhet
    if SKIP_SATURDAY and lt.weekday() == 5:
        silent = True
    if lt.weekday() == 6:
        weekly_outlook(now)
        if before(lt, SUNDAY_RESUME_TIME):
            silent = True
    if silent and not WEEKEND_SYMBOLS:
        print("hétvégi csend")
        return
    if not silent:
        calendar_messages(now)
    active = {n: t for n, t in SYMBOLS.items() if not silent or n in WEEKEND_SYMBOLS}
    plans_schedule(now, active)
    enabled = {"SWING": USE_SWING, "GYORS": USE_FAST, "SCALP": USE_SCALP}
    modes = [m for m in MODES if enabled.get(m["name"], True)]
    needed = set()
    for m in modes:
        needed.add(m["entry"])
        needed.update(m["context"])
    order = [k for k in TF_ORDER if k in needed]
    for name, ticker in active.items():
        try:
            raw, frames = {}, {}
            for tfk in order:                                  # fentről lefelé: előbb a kontextus
                lf = fetched.get(f"{name}|{tfk}")
                age = (now - pd.Timestamp(lf)).total_seconds() / 60 if lf else 1e9
                if age < REFRESH_MIN[tfk]:
                    continue
                if tfk in ("5m", "1m") and (not live.get(name, True) or (FAST_SYMBOLS is not None and name not in FAST_SYMBOLS)):
                    continue                                   # zárt piacnál / kihagyott eszköznél nem tölt gyors adatot
                df = get_frame(tfk, ticker, raw, name in PREPOST_SYMBOLS)
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
                        live[name] = not stale
                        last_info[name] = (float(d_.iloc[-2].rsi), float(d_.iloc[-1].Close), bool(stale))
            resolve_signals(name, frames, now)
            if USE_SPIKE:
                check_spikes(name, frames, now)
            if USE_SETUP:
                check_setups(name, frames, modes, now)
            if USE_PLANS:
                plans_monitor(name, frames, now)
            for mode in modes:
                e = mode["entry"]
                if e not in frames:
                    continue
                if e in ("5m", "1m") and FAST_SYMBOLS is not None and name not in FAST_SYMBOLS:
                    continue
                tfc = TF[e]
                if mode["news"]:
                    why = blackout_reason(name, now)
                    if why:
                        print(name, "hírtilalom:", why)
                        continue
                df = frames[e]
                closed_at = df.index[-2] + pd.Timedelta(minutes=tfc["min"])
                win = max(3 * tfc["min"], 5) if tfc["min"] <= 15 else min(2 * tfc["min"], 90)   # régi jelet nem küld
                if now - closed_at > pd.Timedelta(minutes=win):
                    continue                                   # zárva a piac / régi adat
                for s in analyze(df, mode):
                    ctx_text, ok = ctx_line(name, mode, s["side"])
                    if ok < mode["min_align"]:
                        continue                               # a magasabb idősíkok nem erősítik meg
                    key = f"{name}|{mode['name']}|{e}|{s['candle']}|{s['side']}"
                    if key in sent:
                        continue
                    ck = f"{name}|{mode['name']}|{s['side']}"
                    cd = mode.get("cooldown_min", 0)
                    if cd and ck in cool and (now - pd.Timestamp(cool[ck])).total_seconds() / 60 < cd:
                        continue                               # gyors módoknál nem ismétel túl sűrűn
                    cool[ck] = str(now)
                    sent[key] = 1
                    msg = format_msg(name, s, mode, tfc, ctx_text, ok, now)
                    print(msg)
                    telegram(msg)
                    record_signal(name, mode, tfc, s)
        except Exception as ex:
            print(name, "hiba:", ex)
    if not silent:
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
    signals[:] = d.get("signals", [])
    cool.update(d.get("cool", {}))
    spike.update(d.get("spike", {}))
    setup.update(d.get("setup", {}))
    plans.update(d.get("plans", {}))
    plan_slots.update(d.get("plan_slots", {}))
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
        "cool": cool,
        "spike": spike,
        "setup": setup,
        "plans": {k: v for k, v in plans.items() if v.get("status") == "active"},
        "plan_slots": dict(list(plan_slots.items())[-20:]),
        "signals": [x for x in signals if pd.Timestamp(x["t"]) >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=SIGNAL_KEEP_DAYS)],
        "ctx": ctx,
        "brief_date": state["brief_date"],
        "outlook_date": state["outlook_date"],
        "overview_t": str(state["overview_t"]) if state["overview_t"] is not None else None,
        "cal": {"t": str(cal_cache["t"]),
                "events": [dict(e, time=str(e["time"])) for e in cal_cache["events"]]} if cal_cache["t"] is not None else None,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(d, f)


def loop_mode(minutes):
    """Folyamatos felhős mód: a megadott percig percenként fut egy kör (a GitHub-os munkafolyamat így gyors jelzést tud adni)."""
    deadline = time.time() + minutes * 60
    if not load_state():
        telegram("Jelző bot elindult (folyamatos felhős mód).")
    while True:
        t0 = time.time()
        try:
            run_once()
        except Exception as ex:
            print("kör hiba:", ex)
        save_state()
        if time.time() + LOOP_SLEEP > deadline:
            break
        time.sleep(max(5, LOOP_SLEEP - (time.time() - t0)))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        loop_mode(float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 55)
    elif "--once" in sys.argv:          # felhős mód: egy kör, az állapot fájlban marad
        if not load_state():
            telegram("Jelző bot elindult (felhős mód).")
        run_once()
        save_state()
    else:                             # folyamatos mód (számítógépen)
        telegram("Jelző bot elindult. Naptár: " + ("be" if USE_CALENDAR else "ki") + ".")
        while True:
            run_once()
            time.sleep(CHECK_EVERY)
