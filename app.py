"""
もしあの日に投資していたら? ─ アプリ版

起動:  streamlit run app.py   (Mac は「起動.command」をダブルクリック)
計算ロジックは同じフォルダの stock_backtest.py を使います。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

import stock_backtest as sb

st.set_page_config(page_title="もしあの日に投資していたら?", page_icon="📈", layout="centered")


# ----------------------------------------------------------------------------
# キャッシュ付きのデータ取得
# ----------------------------------------------------------------------------
@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_search(query: str) -> list[dict]:
    return sb.search_stocks(query)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_simulate(symbol: str, amount: float, invest_date: date, currency: str) -> dict:
    r = sb.simulate(symbol, amount, invest_date, currency)
    return r.__dict__


def money(v: float, ccy: str) -> str:
    return sb.fmt_money(v, ccy)


# ----------------------------------------------------------------------------
# 画面
# ----------------------------------------------------------------------------
st.title("📈 もしあの日に投資していたら?")
st.caption("銘柄・金額・日付を選ぶと、その日に買っていた場合の今の評価額を計算します。")

query = st.text_input("銘柄名", placeholder="例: トヨタ、ソニー、任天堂、アップル、Nvidia")

symbol, display_name = None, None
if query.strip():
    with st.spinner("銘柄を検索しています…"):
        try:
            cands = cached_search(query.strip())
        except Exception as e:
            cands = []
            st.error(f"検索中にエラーが発生しました: {e}")

    if not cands and sb.re.fullmatch(r"[A-Za-z0-9\.\-]{1,12}", query.strip()):
        # 見つからなければコード/ティッカーとして扱う
        cands = [{"symbol": sb.normalize_ticker(query), "name": query.strip().upper(), "market": ""}]

    if cands:
        labels = [f"{c['name']}  [{c['symbol']}]  {c['market']}" for c in cands[:30]]
        idx = st.selectbox("候補から選択", range(len(labels)), format_func=lambda i: labels[i])
        symbol, display_name = cands[idx]["symbol"], cands[idx]["name"]
    else:
        st.warning("一致する銘柄が見つかりませんでした。別の名前で試してください。")

col1, col2 = st.columns([3, 2])
with col1:
    amount = st.number_input("投資金額", min_value=1.0, value=1_000_000.0, step=100_000.0,
                             format="%.0f")
with col2:
    currency = st.selectbox("通貨", ["JPY", "USD"],
                            help="米国株も円で投資した想定なら JPY。ドル建てで見るなら USD")

st.caption(f"= {money(amount, currency)}")

invest_date = st.date_input(
    "投資年月日",
    value=date(2015, 1, 5),
    min_value=date(1970, 1, 1),
    max_value=date.today() - timedelta(days=1),
    format="YYYY/MM/DD",
)

run = st.button("計算する", type="primary", use_container_width=True, disabled=symbol is None)

# ----------------------------------------------------------------------------
# 結果
# ----------------------------------------------------------------------------
if run and symbol:
    with st.spinner("株価データを取得しています…"):
        try:
            r = cached_simulate(symbol, float(amount), invest_date, currency)
        except Exception as e:
            st.error(f"計算できませんでした: {e}")
            st.stop()

    name = display_name if symbol.endswith(".T") else r["name"]
    b, s = r["base_ccy"], r["stock_ccy"]
    st.divider()
    st.subheader(f"{name}  ({symbol})")

    def delta(v: float) -> str:
        pct = (v - r["amount"]) / r["amount"] * 100
        c = sb.cagr(r["amount"], v, r["years"])
        return f"{pct:+.1f}%" + (f" (年率 {c*100:+.1f}%)" if c is not None else "")

    m1, m2, m3 = st.columns(3)
    m1.metric("株価の値上がりのみ", money(r["value_price_only"], b), delta(r["value_price_only"]))
    m2.metric("株価＋受取配当", money(r["value_with_dividends"], b),
              delta(r["value_with_dividends"]))
    m3.metric("配当を再投資", money(r["value_reinvested"], b), delta(r["value_reinvested"]))

    chart = r["series"].copy()
    chart["投資元本"] = r["amount"]
    st.line_chart(chart, y_label=f"評価額 ({b})", height=360)

    details = {
        "投資金額": money(r["amount"], b),
        "買付日": f"{r['buy_date']:%Y/%m/%d}（終値 {money(r['buy_price'], s)}）",
        "最新日": f"{r['last_date']:%Y/%m/%d}（終値 {money(r['last_price'], s)}）",
        "経過期間": f"{r['years']:.1f} 年",
        "取得株数": f"{r['shares']:,.2f} 株（分割調整後・端数含む理論値）",
        "受取配当の累計": f"{money(r['dividends_received'], b)}（税引前）",
    }
    if s != b:
        details[f"為替 {s}/{b}"] = f"買付時 {r['fx_buy']:,.2f} → 現在 {r['fx_last']:,.2f}"
    st.table(pd.DataFrame(details.items(), columns=["項目", "内容"]).set_index("項目"))

    if invest_date.isoformat() != f"{r['buy_date']:%Y-%m-%d}":
        st.info(f"{invest_date:%Y/%m/%d} は休場日のため、翌営業日 {r['buy_date']:%Y/%m/%d} の終値で計算しています。")

st.caption("※ 手数料・税金・日本株の100株単元は考慮していない理論値です。"
           "データは Yahoo Finance（yfinance）と東証の銘柄一覧に基づきます。")
