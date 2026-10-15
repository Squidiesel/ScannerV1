import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import scanner as sc

st.set_page_config(page_title="Zone Ticket", layout="centered")

DEFAULT_WATCH = ("AAPL MSFT NVDA AMD TSLA META AMZN GOOGL NFLX AVGO PLTR SMCI COIN MSTR HOOD SOFI "
                 "RKLB ASTS LUNR IONQ RGTI AVAV KTOS MRNA CRSP")

# name -> (screen kind, default leverage limit, [(yahoo symbol, label)])
MARKETS = {
    "Forex": ("forex", 30.0, [
        ("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"), ("USDJPY=X", "USD/JPY"),
        ("AUDUSD=X", "AUD/USD"), ("NZDUSD=X", "NZD/USD"), ("USDCAD=X", "USD/CAD"),
        ("USDCHF=X", "USD/CHF"), ("EURJPY=X", "EUR/JPY"), ("GBPJPY=X", "GBP/JPY"),
        ("AUDJPY=X", "AUD/JPY"), ("EURGBP=X", "EUR/GBP"), ("EURAUD=X", "EUR/AUD")]),
    "Commodities": ("commodity", 10.0, [
        ("GC=F", "Gold"), ("SI=F", "Silver"), ("PL=F", "Platinum"), ("HG=F", "Copper"),
        ("CL=F", "WTI Oil"), ("BZ=F", "Brent Oil"), ("NG=F", "Natural Gas"),
        ("ZW=F", "Wheat"), ("ZC=F", "Corn"), ("KC=F", "Coffee")]),
    "Indices": ("index", 20.0, [
        ("ES=F", "S&P 500"), ("NQ=F", "Nasdaq 100"), ("YM=F", "Dow Jones"), ("RTY=F", "Russell 2000"),
        ("^GDAXI", "Germany 40"), ("^FTSE", "UK 100"), ("^N225", "Japan 225"),
        ("^AXJO", "Australia 200"), ("^HSI", "Hong Kong 50")]),
    "Crypto": ("crypto", 2.0, [
        ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
        ("XRP-USD", "XRP"), ("DOGE-USD", "Dogecoin"), ("ADA-USD", "Cardano")]),
    "ETFs": ("index", 5.0, [
        ("SPY", "SPY"), ("QQQ", "QQQ"), ("IWM", "IWM"), ("DIA", "DIA"), ("TQQQ", "TQQQ"),
        ("SQQQ", "SQQQ"), ("SOXL", "SOXL"), ("GLD", "GLD"), ("SLV", "SLV"), ("TLT", "TLT")]),
    "Stocks": ("stock", 5.0, []),
}
LOOKUP = {sym: (label, MARKETS[m][0], m) for m in MARKETS for sym, label in MARKETS[m][2]}
INDEX_CCY = {"^GDAXI": "EUR", "^FTSE": "GBP", "^N225": "JPY", "^AXJO": "AUD", "^HSI": "HKD"}

# ---------------- saved settings (kept in the page address) ----------------
DEFAULTS = {"acct": "AUD", "equity": 10000.0, "risk_pct": 2.0, "sel": ["Forex", "Commodities", "Indices"],
            "watch": DEFAULT_WATCH, "extras": "", "show_close": False, "auto": False}
for _m in MARKETS:
    DEFAULTS[f"lev_{_m}"] = MARKETS[_m][1]


def from_q(raw, default):
    if isinstance(default, bool):
        return raw == "1"
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [x for x in raw.split(",") if x in MARKETS]
    if default in ("AUD", "USD"):
        return raw if raw in ("AUD", "USD") else default
    return raw


def to_q(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, list):
        return ",".join(v)
    return str(v)


if "_init" not in st.session_state:
    q = st.query_params
    for k, d in DEFAULTS.items():
        val = d
        if k in q:
            try:
                val = from_q(q[k], d)
            except Exception:
                val = d
        st.session_state[k] = val
    st.session_state["_init"] = True


def reset_settings():
    for k, d in DEFAULTS.items():
        st.session_state[k] = d


# ---------------- helpers ----------------
def infer(sym):
    if sym in LOOKUP:
        return LOOKUP[sym]
    if sym.endswith("=X"):
        return sym, "forex", "Forex"
    if sym.endswith("=F"):
        return sym, "commodity", "Commodities"
    if sym.endswith("-USD"):
        return sym, "crypto", "Crypto"
    if sym.startswith("^"):
        return sym, "index", "Indices"
    return sym, "stock", "Stocks"


def fmt(v, nd):
    return f"{v:,.{nd}f}"


@st.cache_data(ttl=600, show_spinner=False)
def usd_rate(ccy):
    """USD value of one unit of ccy, or None if it cannot be read."""
    if ccy == "USD":
        return 1.0
    for sym, inv in ((f"{ccy}USD=X", False), (f"USD{ccy}=X", True)):
        try:
            import yfinance as yf
            c = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            if len(c):
                return 1 / float(c.iloc[-1]) if inv else float(c.iloc[-1])
        except Exception:
            pass
    return None


def acct_rate(ccy, acct):
    """Value of one unit of ccy in the account currency, or None."""
    a, b = usd_rate(ccy), usd_rate(acct)
    return None if a is None or b is None else a / b


def quote_ccy(p):
    sym = p["tkr"]
    if p.get("kind") == "forex" and len(sym) >= 6:
        return sym[3:6]
    return INDEX_CCY.get(sym, "USD")


def size(p, equity, risk_pct, lev_cap, acct="AUD", buf_pct=10.0):
    long = p["side"] == "long"
    height = abs(p["prox"] - p["dist"])
    stop = p["dist"] - height * buf_pct / 100 if long else p["dist"] + height * buf_pct / 100
    ccy = quote_ccy(p)
    f = acct_rate(ccy, acct)
    approx = f is None
    f = 1.0 if approx else f
    per_unit = abs(p["prox"] - stop) * f
    units = math.floor(equity * risk_pct / 100 / per_unit) if per_unit > 0 else 0
    notional = units * p["prox"] * f
    margin = notional / lev_cap if lev_cap > 0 else float("inf")
    return dict(stop=stop, units=units, notional=notional, margin=margin, approx=approx, ccy=ccy,
                acct=acct, cur="A$" if acct == "AUD" else "$",
                margin_pct=margin / equity * 100, lev=notional / equity,
                loss=units * per_unit, gain=units * abs(p["tgt"] - p["prox"]) * f)


def reasons(p, s, lev_cap):
    out = []
    if not all(p["screen"]):
        out.append("fails the screen")
    if p["score"] < 4:
        out.append(f"zone score {p['score']}/5, needs 4")
    if p["rr"] < 3:
        out.append(f"reward to risk {p['rr']:.1f}, needs 3")
    if s["units"] < 1:
        out.append("risk amount too small for one unit")
    if s["lev"] > lev_cap:
        out.append(f"needs {s['lev']:.1f}x leverage, limit {lev_cap:.0f}x")
    if s["margin_pct"] > 50:
        out.append(f"margin is {s['margin_pct']:.0f}% of equity, keep under 50%")
    return out


def play_body(p, price, s, why):
    long = p["side"] == "long"
    nd = p.get("nd", 2)
    dist = abs(price - p["prox"]) / price * 100
    st.markdown(f"### {'BUY' if long else 'SELL SHORT'} at {fmt(p['prox'], nd)}")
    st.caption(f"{'Demand' if long else 'Supply'} zone, {dist:.1f}% from price. Use a limit order.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Stop", fmt(s["stop"], nd))
    c2.metric("Sell target" if long else "Cover target", fmt(p["tgt"], nd))
    c3.metric("Reward : risk", f"{p['rr']:.1f} : 1")
    c4, c5, c6 = st.columns(3)
    c4.metric("Units", f"{s['units']:,}")
    cur = s["cur"]
    c5.metric("Margin", f"{cur}{s['margin']:,.0f}")
    c6.metric("Loss at stop", f"{cur}{s['loss']:,.0f}")
    extra = f" · about {s['units'] / 100000:.2f} standard lots" if p.get("kind") == "forex" else ""
    st.write(f"Zone score {p['score']}/5 · gain at target {cur}{s['gain']:,.0f} · leverage used {s['lev']:.1f}x{extra}")
    if s["approx"]:
        st.caption(f"Could not read the {s['ccy']} to {s['acct']} rate, so size and margin are approximate.")
    if not why:
        st.success("TAKE: every check passes.")
    else:
        st.warning("SKIP for now: " + "; ".join(why) + ".")


# ---------------- scanning ----------------
def do_scan(selected, watchlist, extras, max_dist=8.0):
    jobs, seen = [], set()

    def add(sym, label, m, kind):
        if sym and sym not in seen:
            seen.add(sym)
            jobs.append((sym, label, m, kind))

    for m in selected:
        kind = MARKETS[m][0]
        if m == "Stocks":
            for sym in watchlist.replace(",", " ").upper().split():
                add(sym, sym, m, kind)
        else:
            for sym, label in MARKETS[m][2]:
                add(sym, label, m, kind)
    for sym in extras.replace(",", " ").upper().split():
        label, kind, m = infer(sym)
        add(sym, label, m, kind)

    def work(job):
        sym, label, m, kind = job
        try:
            d15, d1h, dd, spr = sc.fetch(sym, with_info=(kind == "stock"))
            if len(d15) < 60 or len(d1h) < 10 or len(dd) < 25:
                return sym, [], "not enough data"
            ps = sc.build_plays(sym, d15, d1h, dd, spr, max_dist, kind=kind)[:2]
            for p in ps:
                p["label"], p["market"] = label, m
            return sym, ps, None
        except Exception as e:
            return sym, [], str(e)[:80]

    plays, errors = [], []
    bar = st.progress(0.0, text=f"Scanning {len(jobs)} instruments...")
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            sym, ps, err = fu.result()
            plays += ps
            if err:
                errors.append(f"{sym}: {err}")
            bar.progress(i / len(jobs), text=f"Scanned {i} of {len(jobs)}")
    bar.empty()
    st.session_state["scan"] = dict(ts=time.time(), plays=plays, errors=errors, n=len(jobs))


def render_scan(equity, risk_pct, levs, show_close, acct):
    s = st.session_state.get("scan")
    if not s:
        st.info("Pick your markets and tap Scan now.")
        return
    when = datetime.fromtimestamp(s["ts"], ZoneInfo("Australia/Sydney")).strftime("%a %H:%M")
    st.caption(f"Last scan {when} Sydney time · {s['n']} instruments · prices delayed")
    rows = []
    for p in s["plays"]:
        lev = levs.get(p["market"], 5.0)
        sz = size(p, equity, risk_pct, lev, acct)
        rows.append((p, sz, reasons(p, sz, lev)))
    rows.sort(key=lambda r: (bool(r[2]), -r[0]["score"], -r[0]["rr"]))
    takes = [r for r in rows if not r[2]]
    close = [r for r in rows if r[2] and r[0]["score"] >= 3 and r[0]["rr"] >= 2]

    def show(items, tag):
        for p, sz, why in items:
            long = p["side"] == "long"
            title = (f"{tag} · {'BUY' if long else 'SELL SHORT'} {p['label']} @ {fmt(p['prox'], p['nd'])}"
                     f" · {p['rr']:.1f}R · {p['score']}/5")
            with st.expander(title):
                play_body(p, p["px"], sz, why)

    st.subheader(f"Take ({len(takes)})")
    if takes:
        show(takes, "TAKE")
    else:
        st.info("Nothing passes every check right now. That is normal. Waiting is part of the strategy.")
    if show_close:
        st.subheader(f"Close calls ({len(close)})")
        show(close, "WATCH")
    if s["errors"]:
        with st.expander(f"{len(s['errors'])} instruments could not load"):
            st.write("\n".join(s["errors"]))


# ---------------- page ----------------
st.title("Zone Ticket")
st.caption("Supply and demand day-trade levels. Scan the markets or look up one ticker.")

with st.expander("Settings"):
    st.selectbox("Account currency", ["AUD", "USD"], key="acct")
    st.number_input("Account equity", min_value=100.0, step=500.0, key="equity")
    st.number_input("Risk per trade (%)", min_value=0.1, max_value=10.0, step=0.25, key="risk_pct")
    st.caption("Leverage limits. Defaults are typical Australian retail limits. Use your broker's real numbers.")
    for m in MARKETS:
        st.number_input(f"{m} max leverage (x)", min_value=1.0, max_value=500.0, step=1.0, key=f"lev_{m}")
    st.text_area("Stock watchlist (tickers separated by spaces)", key="watch")
    st.text_area("Extra symbols to scan every time", key="extras",
                 help="Any Yahoo Finance symbol, for example AUDNZD=X, NG=F, BTC-USD or a stock like UBER.")
    st.button("Reset to defaults", on_click=reset_settings)
    st.caption("Settings save in this page's web address. After you change them, bookmark the page "
               "or add it to your home screen again so it opens with your settings.")

acct = st.session_state["acct"]
equity = st.session_state["equity"]
risk_pct = st.session_state["risk_pct"]
levs = {m: st.session_state[f"lev_{m}"] for m in MARKETS}

tab_scan, tab_one = st.tabs(["Scan markets", "One ticker"])

with tab_scan:
    selected = st.multiselect("Markets", list(MARKETS), key="sel")
    watchlist, extras = st.session_state["watch"], st.session_state["extras"]
    show_close = st.toggle("Show close calls", key="show_close")
    auto = st.toggle("Rescan every hour while this page is open", key="auto")
    can_scan = bool(selected) or bool(extras.strip())
    if st.button("Scan now", type="primary", use_container_width=True, disabled=not can_scan):
        do_scan(selected, watchlist, extras)

    @st.fragment(run_every=3600 if auto else None)
    def results():
        s = st.session_state.get("scan")
        if auto and s and time.time() - s["ts"] > 3300 and can_scan:
            do_scan(selected, watchlist, extras)
        render_scan(equity, risk_pct, levs, show_close, acct)

    results()
    if auto:
        st.caption("Auto scan only runs while this page is open on your phone. It does not run in the background.")


@st.cache_data(ttl=300, show_spinner=False)
def load(tk):
    return sc.fetch(tk)


def snapshot(d15, d1h, dd, spread, kind):
    price = float(d15.Close.iloc[-1])
    atr_pct = float(sc.atr_series(dd).iloc[-1] / dd.Close.iloc[-1] * 100)
    lo, hi = sc.ATR_RANGE[kind]
    nd = sc.decimals(price)
    if kind != "stock":
        return price, nd, [(f"Daily range {lo}-{hi}%", f"{atr_pct:.2f}%", lo <= atr_pct <= hi)]
    adv = float(dd.Volume.iloc[-21:-1].mean())
    rvol = float(dd.Volume.iloc[-1] / adv) if adv else 0.0
    return price, nd, [
        ("Price above $5", f"${price:,.2f}", price > 5),
        ("Avg volume 1M+", f"{adv:,.0f}", adv >= 1e6),
        ("Relative volume 1.5x+", f"{rvol:.1f}x", rvol >= 1.5),
        ("Daily range 2-8%", f"{atr_pct:.1f}%", lo <= atr_pct <= hi),
        ("Spread 0.1% or less", "n/a" if spread is None else f"{spread:.2f}%", None if spread is None else spread <= 0.1),
    ]


with tab_one:
    tk = st.text_input("Ticker", placeholder="NVDA, EURUSD=X, GC=F, BTC-USD").strip().upper()
    if tk:
        label, kind, market = infer(tk)
        with st.spinner(f"Reading {tk}..."):
            try:
                d15, d1h, dd, spread = load(tk)
                ok = len(d15) >= 60 and len(d1h) >= 10 and len(dd) >= 25
            except Exception:
                ok = False
        if not ok:
            st.error(f"Could not get enough data for {tk}. Check the ticker.")
        else:
            price, nd, rows = snapshot(d15, d1h, dd, spread, kind)
            st.subheader(f"{label}  {fmt(price, nd)}")
            st.caption("Prices are delayed. Confirm on your chart before ordering.")
            with st.expander("Screen", expanded=any(r[2] is False for r in rows)):
                for lab, val, res in rows:
                    mark = ":gray[n/a]" if res is None else (":green[PASS]" if res else ":red[FAIL]")
                    st.write(f"{mark}  {lab}: **{val}**")
            plays = sc.build_plays(tk, d15, d1h, dd, spread, max_dist=15, kind=kind)
            for head, side in (("Buy levels", "long"), ("Sell short levels", "short")):
                st.markdown(f"## {head}")
                items = [p for p in plays if p["side"] == side][:2]
                if not items:
                    st.info("No zone with a usable target right now.")
                for p in items:
                    sz = size(p, equity, risk_pct, levs.get(market, 5.0), acct)
                    with st.container(border=True):
                        play_body(p, price, sz, reasons(p, sz, levs.get(market, 5.0)))

st.divider()
st.caption("Leveraged products lose money for most retail traders. Test on a demo account first. "
           "This is a calculator, not financial advice.")

# Save the current settings into the page address.
try:
    st.query_params.from_dict({k: to_q(st.session_state[k]) for k in DEFAULTS})
except Exception:
    pass
