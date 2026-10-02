"""
Napon belüli + swing jelzés-figyelő bot -> Telegram értesítés a telefonra.
NEM köt ügyletet, csak jelez. A döntés a tiéd.

Tartalma: EMA 9/21/200, RSI, trend, kitörés, gyertyaformációk,
bull/bear flag, automatikus trendvonal-törés, gazdasági naptár
(hírtilalom + előre figyelmeztetés + napi összefoglaló).

Telepítés:  pip install yfinance pandas numpy requests
Futtatás:   python jelzo_bot.py          (folyamatos)
            python jelzo_bot.py --once   (egy kör, felhős/ütemezett mód)
"""
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
USE_SWING = True        # swing jelzések (napi gyertyán) be/ki
TIMEFRAMES = [
    {"interval": "15m", "label": "NAPON BELÜLI", "min": 15, "period": "30d", "atr_mult": 1.5, "rr": 2.0, "news": True},
    {"interval": "1d", "label": "SWING", "min": 1440, "period": "1y", "atr_mult": 2.0, "rr": 3.0, "news": False},
]
INTERVAL = "15m"       # az állapotjelentés idősíkja
LOCAL_TZ = ZoneInfo("Europe/Budapest")

# Gazdasági naptár
USE_CALENDAR = True
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL_IMPACT = ("High",)          # ("High", "Medium") ha a közepeseket is kéred
BLACKOUT_BEFORE_MIN = 15        # hír előtt ennyi percig nem jelez
BLACKOUT_AFTER_MIN = 30         # hír után ennyi percig nem jelez
WARN_BEFORE_MIN = 30            # ennyivel előre figyelmeztet
BRIEF_HOUR = 7                  # napi összefoglaló ekkortól (magyar idő)
CUR_SYMBOLS = {"EUR": ["GER40", "EURHUF"], "HUF": ["USDHUF", "EURHUF"]}   # USD mindent érint
OVERVIEW_EVERY_MIN = 60         # óránként állapotjelentés minden eszközről (0 = ki)
NEWS_BLACKOUT = []              # kézi sáv (UTC), pl. [("2026-10-02 12:00", "2026-10-02 13:15")]
# =========================

sent = {}
warned = {}
state = {"brief_date": None, "overview_t": None}
last_info = {}
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
    try:
        r = requests.get(CAL_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        ev = []
        for e in r.json():
            if e.get("impact") not in CAL_IMPACT:
                continue
            t = pd.Timestamp(e["date"])
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            ev.append({"time": t, "cur": e.get("country", ""), "title": e.get("title", ""),
                       "fc": e.get("forecast", ""), "prev": e.get("previous", "")})
        cal_cache["events"] = ev
        cal_cache["t"] = now
        print(f"Naptár frissítve: {len(ev)} esemény")
    except Exception as ex:
        print("Naptár hiba:", ex)
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
    if state["brief_date"] != str(today) and loc(now).hour >= BRIEF_HOUR and cal_cache["t"] is not None:
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
    near = abs(last.Close - last.ema21) <= atr or abs(last.Close - last.ema200) <= atr

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
        if not aligned and len(base) < 2:
            continue                      # ellentrendben csak 2+ jelből
        risk = atr * tf["atr_mult"]
        entry = float(last.Close)
        stop = entry - risk if side == "bull" else entry + risk
        target = entry + tf["rr"] * risk if side == "bull" else entry - tf["rr"] * risk
        dist = (cur_price - entry) / atr if side == "bull" else (entry - cur_price) / atr
        out.append({"side": side, "reasons": rs, "aligned": aligned, "trend": trend,
                    "entry": entry, "stop": stop, "target": target, "rsi": float(last.rsi),
                    "cur": cur_price, "dist_atr": dist, "candle": last.name, "notes": notes})
    return out


def format_msg(name, s, tf):
    side = "LONG (vétel)" if s["side"] == "bull" else "SHORT (eladás)"
    late = ""
    if abs(s["dist_atr"]) > 0.5:
        late = "\nFIGYELEM: az ár már messze van a belépőtől, valószínűleg elkésett."
    trend_line = TREND_TXT[s["trend"]] + (" (trenddel egyező)" if s["aligned"] else " (ELLENTREND, óvatosan)")
    if tf["min"] >= 1440:
        candle = f"Lezárt napi gyertya: {loc(s['candle']).strftime('%Y-%m-%d')}"
        extra = "\nSwing: napokig/hetekig tartó pozíció, a stop és a cél szélesebb."
    else:
        open_t = loc(s["candle"])
        close_t = open_t + pd.Timedelta(minutes=tf["min"])
        candle = f"Lezárt gyertya: {open_t.strftime('%H:%M')}-{close_t.strftime('%H:%M')} (magyar idő)"
        extra = ""
    return (f"{name} [{tf['label']} {tf['interval']}] - {side}\n"
            f"Jelek ({len(s['reasons'])}): " + ", ".join(s["reasons"]) + "\n"
            f"Trend: {trend_line}\n"
            f"Belépő ~ {s['entry']:.5g}\n"
            f"Aktuális ár ~ {s['cur']:.5g}\n"
            f"Stop ~ {s['stop']:.5g}\n"
            f"Cél ~ {s['target']:.5g} (1:{tf['rr']:g})\n"
            + candle
            + (f"\nMegjegyzés: {', '.join(s['notes'])}" if s["notes"] else "")
            + extra + late +
            "\nEz csak jelzés, nem tanács. Ellenőrizd a grafikont az XTB-ben!")


def fetch(ticker, tf):
    df = yf.download(ticker, period=tf["period"], interval=tf["interval"], progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def overview(now):
    if OVERVIEW_EVERY_MIN <= 0 or not last_info:
        return
    t = state["overview_t"]
    if t is not None and (now - t).total_seconds() < OVERVIEW_EVERY_MIN * 60:
        return
    state["overview_t"] = now
    ic = {"bull": "LONG irány", "bear": "SHORT irány", "mixed": "nincs tiszta irány"}
    lines = [f"{n}: {ic[v[0]]} | RSI {v[1]:.0f} | ár ~ {v[2]:.5g}" for n, v in last_info.items()]
    telegram(f"Állapot ({loc(now).strftime('%H:%M')}, {INTERVAL}):\n" + "\n".join(lines)
             + "\n(Az irány a trendből jön, belépőt csak külön jelzés ad.)")


def run_once():
    now = pd.Timestamp.now(tz="UTC")
    calendar_messages(now)
    for tf in TIMEFRAMES:
        if tf["min"] >= 1440 and not USE_SWING:
            continue
        for name, ticker in SYMBOLS.items():
            try:
                if tf["news"]:
                    why = blackout_reason(name, now)
                    if why:
                        print(name, "hírtilalom:", why)
                        continue
                df = fetch(ticker, tf)
                if df.empty or len(df) < 40:
                    continue
                closed_at = df.index[-2] + pd.Timedelta(minutes=tf["min"])
                if now - closed_at > pd.Timedelta(minutes=3 * tf["min"]):
                    continue              # zárva a piac / régi adat
                if tf["interval"] == INTERVAL:
                    d_ = add_indicators(df).dropna()
                    if len(d_) > 2:
                        r_ = d_.iloc[-2]
                        last_info[name] = (trend_of(r_), float(r_.rsi), float(d_.iloc[-1].Close))
                for s in analyze(df, tf):
                    key = f"{name}|{tf['interval']}|{s['candle']}|{s['side']}"
                    if key in sent:
                        continue
                    sent[key] = 1
                    msg = format_msg(name, s, tf)
                    print(msg)
                    telegram(msg)
            except Exception as e:
                print(name, tf["interval"], "hiba:", e)
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
    state["brief_date"] = d.get("brief_date")
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
        "brief_date": state["brief_date"],
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
