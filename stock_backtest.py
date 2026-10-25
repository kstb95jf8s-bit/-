#!/usr/bin/env python3
"""
もしあの日に投資していたら? 個別株 投資シミュレーター

指定した銘柄に、指定した日に、指定した金額を投資していた場合、
現在いくらになっているかを計算します。

  使い方:
    python stock_backtest.py                       # 対話形式で入力
    python stock_backtest.py トヨタ 100万 2015-01-05
    python stock_backtest.py アップル 100万 2010-06-01 --plot
    python stock_backtest.py nvidia 5000 2020-03-16 --currency USD

  銘柄の指定 (銘柄名で検索):
    日本株 … 日本語の銘柄名の一部 (トヨタ, ソニー, 任天堂, とよた も可)
              → 東証(JPX)の上場銘柄一覧から検索
    米国株 … 英語の社名 (Apple, Nvidia, Microsoft など)
              → Yahoo Finance の検索を利用。アップル/エヌビディア等の主要銘柄はカタカナも可
    候補が複数あれば番号で選べます。証券コードやティッカーを直接入れても動きます。

  事前準備:
    pip install yfinance pandas matplotlib openpyxl xlrd
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime, timedelta

try:
    import pandas as pd
    import yfinance as yf
except ImportError:
    sys.exit("必要なライブラリがありません。次を実行してください:\n  pip install yfinance pandas matplotlib openpyxl xlrd")


CURRENCY_SYMBOL = {"JPY": "¥", "USD": "$", "EUR": "€", "GBP": "£"}


# ----------------------------------------------------------------------------
# 入力の整形
# ----------------------------------------------------------------------------
def normalize_ticker(raw: str) -> str:
    """'7203' → '7203.T'、'aapl' → 'AAPL'。"""
    t = raw.strip().upper()
    # 日本株: 4桁の数字、または新しい英数字コード(例 130A)
    if re.fullmatch(r"\d{4}|\d{3}[A-Z]", t):
        return f"{t}.T"
    return t


# ----------------------------------------------------------------------------
# 銘柄名検索
# ----------------------------------------------------------------------------
JPX_PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_FALLBACK_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]


def _jpx_list_urls() -> list[str]:
    """JPXのページから最新の一覧ファイルのURLを探す(形式やURLが変わっても追従)。"""
    urls = []
    try:
        req = urllib.request.Request(JPX_PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            html = res.read().decode("utf-8", "ignore")
        for href in re.findall(r'href="([^"]*data_j\.xlsx?)"', html):
            urls.append(urllib.parse.urljoin(JPX_PAGE_URL, href))
    except Exception:
        pass
    return urls + [u for u in JPX_FALLBACK_URLS if u not in urls]
CACHE_DIR = Path.home() / ".cache" / "stock_backtest"
JPX_CACHE = CACHE_DIR / "jpx_list_v2.csv"
JPX_CACHE_DAYS = 30

# よく使われる米国株のカタカナ表記 → 英語名 (Yahoo 検索は日本語に弱いため)
KANA_ALIASES = {
    "アップル": "Apple", "マイクロソフト": "Microsoft", "エヌビディア": "NVIDIA",
    "アマゾン": "Amazon", "グーグル": "Alphabet", "アルファベット": "Alphabet",
    "メタ": "Meta Platforms", "フェイスブック": "Meta Platforms", "テスラ": "Tesla",
    "ネットフリックス": "Netflix", "コカコーラ": "Coca-Cola", "コカ・コーラ": "Coca-Cola",
    "ディズニー": "Walt Disney", "マクドナルド": "McDonald's", "スターバックス": "Starbucks",
    "ナイキ": "Nike", "インテル": "Intel", "エーエムディー": "AMD", "ブロードコム": "Broadcom",
    "バークシャー": "Berkshire Hathaway", "ウォルマート": "Walmart", "ビザ": "Visa",
    "マスターカード": "Mastercard", "ジョンソンエンドジョンソン": "Johnson & Johnson",
    "ペプシコ": "PepsiCo", "オラクル": "Oracle", "セールスフォース": "Salesforce",
    "アドビ": "Adobe", "クアルコム": "Qualcomm", "ボーイング": "Boeing",
    "エクソンモービル": "Exxon Mobil", "ファイザー": "Pfizer", "イーライリリー": "Eli Lilly",
    "パランティア": "Palantir", "ティーエスエムシー": "Taiwan Semiconductor",
}


def _norm(text: str) -> str:
    """全角/半角・大小文字・ひらがな/カタカナ・空白の違いを吸収した比較用文字列。"""
    t = unicodedata.normalize("NFKC", str(text)).upper()
    t = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in t)  # ひらがな→カタカナ
    return re.sub(r"[\s・\.\-]", "", t)


def load_jpx_list() -> pd.DataFrame | None:
    """東証上場銘柄一覧(コード・銘柄名・市場)。30日キャッシュ。取得失敗時は None。"""
    if JPX_CACHE.exists() and time.time() - JPX_CACHE.stat().st_mtime < JPX_CACHE_DAYS * 86400:
        return pd.read_csv(JPX_CACHE, dtype=str)
    print("東証の上場銘柄一覧をダウンロードしています...", file=sys.stderr)
    err = None
    for url in _jpx_list_urls():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as res:
                data = res.read()
            raw = pd.read_excel(io.BytesIO(data), dtype=str)
            break
        except Exception as e:
            err = e
    else:
        raw = None
    try:
        if raw is None:
            raise RuntimeError(err)
        cols = {"コード": "code", "銘柄名": "name", "市場・商品区分": "market", "規模区分": "size"}
        df = raw[[c for c in cols if c in raw.columns]].rename(columns=cols)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(JPX_CACHE, index=False)
        return df
    except Exception as e:
        print(f"  (東証の銘柄一覧を取得できませんでした: {e})", file=sys.stderr)
        if JPX_CACHE.exists():
            return pd.read_csv(JPX_CACHE, dtype=str)
        return None


def search_japan(query: str) -> list[dict]:
    df = load_jpx_list()
    if df is None:
        return []
    q = _norm(query)
    keys = df["name"].map(_norm)
    hit = df[keys.str.contains(q, regex=False, na=False)].copy()
    if hit.empty:
        return []
    # 優先順: 完全一致 > 国内株式(ETF等より先) > 前方一致 > 規模(Core30→Large70→…)
    size_rank = {"TOPIX Core30": 0, "TOPIX Large70": 1, "TOPIX Mid400": 2,
                 "TOPIX Small 1": 3, "TOPIX Small 2": 4}
    sizes = hit["size"] if "size" in hit.columns else pd.Series("", index=hit.index)
    hit["rank"] = (
        (keys[hit.index] != q).astype(int) * 100
        + (~hit["market"].str.contains("内国株式", na=False)).astype(int) * 50
        + (~keys[hit.index].str.startswith(q)).astype(int) * 10
        + sizes.map(size_rank).fillna(5)
    )
    hit = hit.sort_values(["rank", "code"])
    return [{"symbol": f"{r.code}.T", "name": r.name, "market": r.market}
            for r in hit.itertuples()]


def search_yahoo(query: str) -> list[dict]:
    """Yahoo Finance の銘柄検索(株式のみ)。"""
    quotes = []
    try:
        quotes = yf.Search(query, max_results=10).quotes          # yfinance >= 0.2.44
    except Exception:
        try:
            url = ("https://query2.finance.yahoo.com/v1/finance/search?"
                   + urllib.parse.urlencode({"q": query, "quotesCount": 10, "newsCount": 0}))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as res:
                quotes = json.load(res).get("quotes", [])
        except Exception:
            return []
    out = []
    for q in quotes:
        if q.get("quoteType") != "EQUITY" or not q.get("symbol"):
            continue
        out.append({
            "symbol": q["symbol"],
            "name": q.get("longname") or q.get("shortname") or q["symbol"],
            "market": q.get("exchDisp") or q.get("exchange") or "",
        })
    # 米国の主要市場を優先
    us = {"NASDAQ", "NYSE", "NMS", "NYQ", "NGM", "NCM", "ASE", "NYSEArca"}
    out.sort(key=lambda c: 0 if c["market"] in us else 1)
    return out


def search_stocks(query: str) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    has_japanese = bool(re.search(r"[぀-ヿ一-鿿]", query))
    alias = KANA_ALIASES.get(unicodedata.normalize("NFKC", query))

    results = search_japan(query) if has_japanese else []
    if alias:
        results = search_yahoo(alias) + results
    elif not has_japanese:
        results = search_yahoo(query) + search_japan(query)   # 英字名の日本企業(SONY等)も拾う
    elif not results:
        results = search_yahoo(query)

    seen, uniq = set(), []
    for c in results:
        if c["symbol"] not in seen:
            seen.add(c["symbol"])
            uniq.append(c)
    return uniq


def resolve_ticker(query: str, interactive: bool = True, limit: int = 15) -> tuple[str, str]:
    """銘柄名(またはコード)から (シンボル, 銘柄名) を決める。"""
    q = query.strip()
    if re.fullmatch(r"\d{4}|\d{3}[A-Za-z]", q):                 # 証券コード直接指定
        return normalize_ticker(q), q

    cands = search_stocks(q)
    if not cands:
        # ティッカー直接指定(AAPL など)として扱う
        if re.fullmatch(r"[A-Za-z0-9\.\-\^=]{1,12}", q):
            return normalize_ticker(q), q.upper()
        raise ValueError(f"「{q}」に一致する銘柄が見つかりませんでした。別の名前で試してください")

    if len(cands) == 1 or not interactive:
        c = cands[0]
        if len(cands) > 1:
            others = "、".join(f"{x['name']}[{x['symbol']}]" for x in cands[1:6])
            print(f"「{q}」→ {c['name']} [{c['symbol']}] を選びました (他の候補: {others})",
                  file=sys.stderr)
        return c["symbol"], c["name"]

    shown = cands[:limit]
    print(f"「{q}」の検索結果:")
    for i, c in enumerate(shown, 1):
        print(f"  {i:>2}. {c['name']}  [{c['symbol']}]  {c['market']}")
    if len(cands) > limit:
        print(f"  ... ほか {len(cands) - limit} 件 (名前を詳しく入れると絞り込めます)")
    while True:
        sel = input("番号を選んでください (Enterで1番): ").strip() or "1"
        if sel.isdigit() and 1 <= int(sel) <= len(shown):
            c = shown[int(sel) - 1]
            return c["symbol"], c["name"]
        print("  → 一覧の番号を入力してください")


def parse_amount(raw: str) -> float:
    """'1,000,000' / '100万' / '1.5万' などを数値に。"""
    s = raw.strip().replace(",", "").replace("円", "").replace("$", "").replace("¥", "")
    mult = 1
    if s.endswith("億"):
        mult, s = 100_000_000, s[:-1]
    elif s.endswith("万"):
        mult, s = 10_000, s[:-1]
    value = float(s) * mult
    if value <= 0:
        raise ValueError("投資金額は正の数で入力してください")
    return value


def parse_date(raw: str) -> date:
    s = raw.strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"日付の形式が読み取れません: {raw}  (例: 2015-01-05)")


def fmt_money(value: float, ccy: str) -> str:
    sym = CURRENCY_SYMBOL.get(ccy, "")
    if ccy == "JPY":
        return f"{sym}{value:,.0f}"
    return f"{sym}{value:,.2f}" if sym else f"{value:,.2f} {ccy}"


# ----------------------------------------------------------------------------
# データ取得
# ----------------------------------------------------------------------------
def fetch_history(ticker: str, start: date) -> tuple[pd.DataFrame, str, str]:
    """株価履歴・取引通貨・銘柄名を返す。"""
    tk = yf.Ticker(ticker)
    hist = tk.history(start=start.isoformat(), auto_adjust=False, actions=True)
    if hist is None or hist.empty:
        raise RuntimeError(
            f"'{ticker}' の株価データが取得できませんでした。"
            "銘柄コードと日付(上場前・未来の日付でないか)を確認してください。"
        )
    hist.index = hist.index.tz_localize(None)

    currency, name = None, ticker
    try:
        currency = tk.fast_info.get("currency")
    except Exception:
        pass
    try:
        info = tk.info
        name = info.get("longName") or info.get("shortName") or ticker
        currency = currency or info.get("currency")
    except Exception:
        pass
    if not currency:
        currency = "JPY" if ticker.endswith(".T") else "USD"
    return hist, currency.upper(), name


def fetch_fx(from_ccy: str, to_ccy: str, start: date) -> pd.Series | None:
    """from_ccy → to_ccy の為替レート(終値)系列。同一通貨なら None。"""
    if from_ccy == to_ccy:
        return None
    pair = f"{from_ccy}{to_ccy}=X"
    fx = yf.Ticker(pair).history(start=(start - timedelta(days=10)).isoformat())
    if fx is None or fx.empty:
        raise RuntimeError(f"為替レート {pair} が取得できませんでした。")
    fx.index = fx.index.tz_localize(None)
    return fx["Close"]


def fx_at(fx: pd.Series | None, when: pd.Timestamp) -> float:
    if fx is None:
        return 1.0
    sub = fx[fx.index.normalize() <= when.normalize()]
    return float((sub if not sub.empty else fx).iloc[-1])


# ----------------------------------------------------------------------------
# 計算
# ----------------------------------------------------------------------------
@dataclass
class Result:
    ticker: str
    name: str
    stock_ccy: str
    base_ccy: str
    amount: float
    buy_date: pd.Timestamp
    buy_price: float
    shares: float
    last_date: pd.Timestamp
    last_price: float
    value_price_only: float        # 株価の値上がりのみ (配当は受け取らない)
    dividends_received: float      # 受取配当の累計 (税引前・再投資なし)
    value_with_dividends: float    # 株価 + 受取配当 (再投資なし)
    value_reinvested: float        # 配当を全額再投資した場合
    fx_buy: float
    fx_last: float
    years: float
    series: pd.DataFrame           # グラフ用: 日々の評価額


def simulate(ticker: str, amount: float, invest_date: date, base_ccy: str | None) -> Result:
    hist, stock_ccy, name = fetch_history(ticker, invest_date)
    base_ccy = (base_ccy or "JPY").upper()
    fx = fetch_fx(stock_ccy, base_ccy, invest_date)

    # 指定日当日、休場なら翌営業日の終値で買う
    buy_date = hist.index[0]
    buy_price = float(hist["Close"].iloc[0])           # 分割調整済みの終値
    fx_buy = fx_at(fx, buy_date)

    shares = (amount / fx_buy) / buy_price             # 端数株も許容する理論値

    last_date = hist.index[-1]
    last_price = float(hist["Close"].iloc[-1])
    fx_last = fx_at(fx, last_date)

    value_price_only = shares * last_price * fx_last

    # 受取配当(買付日の翌日以降の権利落ち分)。各支払日の為替で円換算。
    divs = hist["Dividends"]
    divs = divs[(divs > 0) & (divs.index > buy_date)]
    dividends_received = sum(shares * float(d) * fx_at(fx, ts) for ts, d in divs.items())
    value_with_dividends = value_price_only + dividends_received

    # 配当再投資: Adj Close(分割・配当調整済み)の騰落率を使う
    adj = hist["Adj Close"]
    growth_reinvested = float(adj.iloc[-1]) / float(adj.iloc[0])
    value_reinvested = (amount / fx_buy) * growth_reinvested * fx_last

    # グラフ用系列
    fx_daily = (
        fx.reindex(hist.index, method="ffill").bfill() if fx is not None
        else pd.Series(1.0, index=hist.index)
    )
    series = pd.DataFrame({
        "株価のみ": shares * hist["Close"] * fx_daily,
        "配当再投資": (amount / fx_buy) * (adj / float(adj.iloc[0])) * fx_daily,
    })

    years = (last_date - buy_date).days / 365.25
    return Result(
        ticker, name, stock_ccy, base_ccy, amount, buy_date, buy_price, shares,
        last_date, last_price, value_price_only, dividends_received,
        value_with_dividends, value_reinvested, fx_buy, fx_last, years, series,
    )


def cagr(start: float, end: float, years: float) -> float | None:
    if years < 1 or start <= 0 or end <= 0:   # 1年未満は年率を出さない
        return None
    return (end / start) ** (1 / years) - 1


# ----------------------------------------------------------------------------
# 表示
# ----------------------------------------------------------------------------
def report(r: Result) -> None:
    b, s = r.base_ccy, r.stock_ccy
    line = "─" * 56
    print()
    print(line)
    print(f"  {r.name}  ({r.ticker})")
    print(line)
    print(f"  投資金額      : {fmt_money(r.amount, b)}")
    print(f"  買付日        : {r.buy_date:%Y-%m-%d}  (終値 {fmt_money(r.buy_price, s)})")
    if s != b:
        print(f"  為替 {s}/{b}  : 買付時 {r.fx_buy:,.2f} → 現在 {r.fx_last:,.2f}")
    print(f"  取得株数      : {r.shares:,.4f} 株 (分割調整後・端数含む理論値)")
    print(f"  最新日        : {r.last_date:%Y-%m-%d}  (終値 {fmt_money(r.last_price, s)})")
    print(f"  経過期間      : {r.years:.1f} 年")
    print(line)

    rows = [
        ("株価の値上がりのみ", r.value_price_only),
        ("株価＋受取配当", r.value_with_dividends),
        ("配当を再投資", r.value_reinvested),
    ]
    for label, v in rows:
        gain = v - r.amount
        pct = gain / r.amount * 100
        c = cagr(r.amount, v, r.years)
        c_txt = f", 年率 {c*100:+.1f}%" if c is not None else ""
        print(f"  {label:<10}: {fmt_money(v, b):>16}   ({pct:+.1f}%{c_txt})")
    print(f"  (受取配当の累計: {fmt_money(r.dividends_received, b)} 税引前)")
    print(line)
    print("  ※ 手数料・税金は考慮していません。日本株の単元(100株)も無視した理論値です。")
    print("  ※ データは Yahoo Finance(yfinance)由来で、誤差や欠損がある場合があります。")
    print()


def plot(r: Result) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("グラフ表示には matplotlib が必要です: pip install matplotlib")
        return
    plt.rcParams["font.family"] = ["Hiragino Sans", "Yu Gothic", "Meiryo", "Noto Sans CJK JP",
                                   "IPAexGothic", "sans-serif"]
    ax = r.series.plot(figsize=(10, 5.5), linewidth=1.6)
    ax.axhline(r.amount, color="gray", linestyle="--", linewidth=1, label="投資元本")
    ax.set_title(f"{r.name} ({r.ticker}) に {fmt_money(r.amount, r.base_ccy)} 投資した場合")
    ax.set_ylabel(f"評価額 ({r.base_ccy})")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    ax.grid(alpha=0.3)
    ax.legend()
    plt.tight_layout()
    plt.show()


# ----------------------------------------------------------------------------
# エントリーポイント
# ----------------------------------------------------------------------------
def ask(prompt: str, parser):
    while True:
        try:
            return parser(input(prompt))
        except (ValueError, KeyboardInterrupt) as e:
            if isinstance(e, KeyboardInterrupt):
                sys.exit("\n中断しました")
            print(f"  → {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="もしあの日に投資していたら? 個別株シミュレーター")
    ap.add_argument("ticker", nargs="?", help="銘柄名 (例: トヨタ, ソニー, Apple)")
    ap.add_argument("amount", nargs="?", help="投資金額 (例: 1000000, 100万)")
    ap.add_argument("date", nargs="?", help="投資年月日 (例: 2015-01-05)")
    ap.add_argument("--currency", default="JPY",
                    help="投資金額・結果の通貨 (既定: JPY。米国株をドル建てで見るなら USD)")
    ap.add_argument("--plot", action="store_true", help="評価額の推移グラフを表示")
    args = ap.parse_args()

    interactive = sys.stdin.isatty()
    if args.ticker and args.amount and args.date:
        try:
            ticker, display_name = resolve_ticker(args.ticker, interactive)
        except ValueError as e:
            sys.exit(f"エラー: {e}")
        amount = parse_amount(args.amount)
        invest_date = parse_date(args.date)
        show_plot = args.plot
    else:
        print("=== もしあの日に投資していたら? ===")
        ticker, display_name = ask("銘柄名 (例: トヨタ / ソニー / Apple): ",
                                   lambda q: resolve_ticker(q, True))
        amount = ask(f"投資金額 [{args.currency}] (例: 100万): ", parse_amount)
        invest_date = ask("投資年月日 (例: 2015-01-05): ", parse_date)
        show_plot = args.plot or input("グラフを表示しますか? [y/N]: ").strip().lower() == "y"

    if invest_date >= date.today():
        sys.exit("投資年月日は過去の日付を指定してください。")

    try:
        result = simulate(ticker, amount, invest_date, args.currency)
    except Exception as e:
        sys.exit(f"エラー: {e}")

    if ticker.endswith(".T") and display_name and not display_name[:1].isdigit():
        result.name = display_name          # 日本株は東証一覧の日本語名で表示
    report(result)
    if show_plot:
        plot(result)


if __name__ == "__main__":
    main()
