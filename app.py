import math

import streamlit as st

import scanner as sc

st.set_page_config(page_title="Zone Ticket", layout="centered")


@st.cache_data(ttl=300, show_spinner=False)
def load(tk):
    return sc.fetch(tk)


def snapshot(d15, d1h, dd, spread):
    price = float(d15.Close.iloc[-1])
    adv = float(dd.Volume.iloc[-21:-1].mean())
    rvol = float(dd.Volume.iloc[-1] / adv) if adv else 0.0
    atr_pct = float(sc.atr_series(dd).iloc[-1] / dd.Close.iloc[-1] * 100)
    rows = [
        ("Price above $5", f"${price:,.2f}", price > 5),
        ("Avg volume 1M+", f"{adv:,.0f}", adv >= 1e6),
        ("Relative volume 1.5x+", f"{rvol:.1f}x", rvol >= 1.5),
        ("Daily range 2-8%", f"{atr_pct:.1f}%", 2 <= atr_pct <= 8),
        ("Spread 0.1% or less", "n/a" if spread is None else f"{spread:.2f}%", None if spread is None else spread <= 0.1),
    ]
    return price, rows


def size(p, equity, risk_pct, lev_cap, buf_pct=10.0):
    long = p["side"] == "long"
    height = abs(p["prox"] - p["dist"])
    stop = p["dist"] - height * buf_pct / 100 if long else p["dist"] + height * buf_pct / 100
    per_unit = abs(p["prox"] - stop)
    units = math.floor(equity * risk_pct / 100 / per_unit) if per_unit > 0 else 0
    notional = units * p["prox"]
    margin = notional / lev_cap if lev_cap > 0 else float("inf")
    return dict(stop=stop, units=units, notional=notional, margin=margin,
                margin_pct=margin / equity * 100, lev=notional / equity,
                loss=units * per_unit, gain=units * abs(p["tgt"] - p["prox"]))


def reasons(p, s, lev_cap):
    out = []
    if not all(p["screen"]):
        out.append("stock fails the screen")
    if p["score"] < 4:
        out.append(f"zone score {p['score']}/5, needs 4")
    if p["rr"] < 3:
        out.append(f"reward to risk {p['rr']:.1f}, needs 3")
    if s["units"] < 1:
        out.append("risk amount too small for one unit")
    if s["lev"] > lev_cap:
        out.append(f"needs {s['lev']:.1f}x leverage, you have {lev_cap:.1f}x")
    if s["margin_pct"] > 50:
        out.append(f"margin is {s['margin_pct']:.0f}% of equity, keep under 50%")
    return out


def card(p, price, equity, risk_pct, lev_cap):
    long = p["side"] == "long"
    s = size(p, equity, risk_pct, lev_cap)
    why = reasons(p, s, lev_cap)
    take = not why
    dist = abs(price - p["prox"]) / price * 100
    with st.container(border=True):
        head = "BUY" if long else "SELL SHORT"
        st.markdown(f"### {head} at ${p['prox']:,.2f}")
        st.caption(f"{'Demand' if long else 'Supply'} zone, {dist:.1f}% from price. Use a limit order.")
        c1, c2, c3 = st.columns(3)
        c1.metric("Stop", f"${s['stop']:,.2f}")
        c2.metric("Sell target" if long else "Cover target", f"${p['tgt']:,.2f}")
        c3.metric("Reward : risk", f"{p['rr']:.1f} : 1")
        c4, c5, c6 = st.columns(3)
        c4.metric("Units", f"{s['units']:,}")
        c5.metric("Margin", f"${s['margin']:,.0f}")
        c6.metric("Loss at stop", f"${s['loss']:,.0f}")
        st.write(f"Zone score {p['score']}/5 · gain at target ${s['gain']:,.0f} · leverage used {s['lev']:.1f}x")
        if take:
            st.success("TAKE: every check passes.")
        else:
            st.warning("SKIP for now: " + "; ".join(why) + ".")


st.title("Zone Ticket")
st.caption("Type a ticker. Get supply and demand entry, stop and target levels for a day trade.")

tk = st.text_input("Ticker", placeholder="e.g. NVDA").strip().upper()
with st.expander("Account settings"):
    equity = st.number_input("Account equity ($)", min_value=100.0, value=10000.0, step=500.0)
    risk_pct = st.number_input("Risk per trade (%)", min_value=0.1, max_value=10.0, value=2.0, step=0.25)
    lev_cap = st.number_input("Max leverage (x)", min_value=1.0, max_value=30.0, value=5.0, step=1.0)

if tk:
    with st.spinner(f"Reading {tk}..."):
        try:
            d15, d1h, dd, spread = load(tk)
            ok = len(d15) >= 60 and len(d1h) >= 10 and len(dd) >= 25
        except Exception as e:
            ok, err = False, str(e)
    if not ok:
        st.error(f"Could not get enough data for {tk}. Check the ticker.")
        st.stop()

    price, rows = snapshot(d15, d1h, dd, spread)
    st.subheader(f"{tk}  ${price:,.2f}")
    st.caption("Prices are delayed. Confirm on your chart before ordering.")

    with st.expander("Stock screen", expanded=not all(r[2] is not False for r in rows)):
        for label, val, res in rows:
            mark = ":gray[n/a]" if res is None else (":green[PASS]" if res else ":red[FAIL]")
            st.write(f"{mark}  {label}: **{val}**")

    plays = sc.build_plays(tk, d15, d1h, dd, spread, max_dist=15)
    longs = [p for p in plays if p["side"] == "long"][:2]
    shorts = [p for p in plays if p["side"] == "short"][:2]

    st.markdown("## Buy levels")
    if longs:
        for p in longs:
            card(p, price, equity, risk_pct, lev_cap)
    else:
        st.info("No demand zone below price with a usable target right now.")
    st.markdown("## Sell short levels")
    if shorts:
        for p in shorts:
            card(p, price, equity, risk_pct, lev_cap)
    else:
        st.info("No supply zone above price with a usable target right now.")

st.divider()
st.caption("Leveraged products lose money for most retail traders. Test on a demo account first. "
           "This is a calculator, not financial advice.")
