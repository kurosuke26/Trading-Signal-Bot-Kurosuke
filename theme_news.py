#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
theme_news.py — 政策・テーマ関連ニュースの収集と「銘柄別ニュース・モメンタムスコア」算出。

【狙い（2026-09-06のkurosukeさんとの合意事項）】
「国策に売りなし」という相場格言のとおり、政府の政策テーマ（半導体・防衛・量子など）に
連動する銘柄は中長期で値が伸びやすい傾向がある。これをGROWTHシグナルの1要素として
定量的に拾えるようにするため、以下の3段階を実装する:

  ①（実装済み・本ファイル以前から合意）重要度で重み付けしたテーマ・キーワードリスト
  ②本ファイルで追加：ニュース収集元に政策系の公的機関RSSフィードを追加
  ③本ファイルで追加：「テーマキーワード」と「銘柄名」が同一記事内に共起した場合のみ
     その銘柄への言及としてカウントする（銘柄名単体でのヒットは誤検知が多いため）
  ④本ファイルで追加：言及件数の増加率（直近ウィンドウ vs 直前ウィンドウ）でモメンタムスコア化

【設計上の注意点（正直に）】
- RSSフィードのURLは2026-09-06時点でWeb調査により確認したもの。防衛省・内閣府の2件は
  実際にfetchして中身を確認済み（内閣府はRSS1.0/RDF形式）。経済産業省のRSSは
  一覧ページの存在は確認できたが、このセッションの環境からは個別URLの直接確認が
  権限上できなかった（英語版のURLパターンは確認できたが日本語版は未確認）。
  METI_RSS_URLに正しいURLを入れ次第、RSS_FEEDSに追加すること（現状はコメントアウト）。
- 銘柄名マッチングは「テーマキーワードとの共起」を必須条件にすることで単純な社名検索
  よりは誤検知を抑えているが、社名が一般名詞に近い銘柄（例：「システム」を含む社名等）は
  依然として誤検知リスクが残る。COMPANY_NAME_MIN_LENGTH（既定2文字、法人格の接尾辞を
  除いた後の文字数）を調整することで多少の抑制はできるが、完全ではない点に注意。
- 同じ記事がRSS上に何日も残り続けるフィード仕様のため、「言及」は記事単位で一度だけ
  カウントする（`seen_articles`にlink/タイトルのキーを記録し、既知の記事は無視する）。
  記事自体の公開日（published/updated）が取得できればその日付でカウントし、
  取得できない場合のみ「初めて観測した日」（today）にフォールバックする。
- 政府機関の公式発表はそれ自体が一次情報であり、いわゆる「配信社が同じニュースを
  複数媒体に配信する」形の重複（シンジケーション）は起きにくいが、複数の省庁が
  同じ施策を相前後して発表するケースはあり得るため、タイトルの完全一致でも
  簡易的に重複除去する。

【実行方法】
  python theme_news.py
  （collector.pyと同じ実行ディレクトリを想定。data/配下に状態ファイルを保存する）
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta, date as date_cls

import requests
import feedparser

from util import env_int, env_float, json_default

# ---------------------------------------------------------------------------
# ①テーマ・キーワード（重要度による重み付け、2026-09-06にkurosukeさんと合意した内容）
#   出典: https://media.rakuten-sec.net/articles/-/51058 ,
#         https://jioinc.jp/investleaders/news_2026tpolicy/ ,
#         https://www.jiji.com/jc/article?k=2025122200862&g=eco
# ---------------------------------------------------------------------------
THEMES = {
    '半導体・AI': {
        'weight': 3,
        'keywords': ['半導体', 'AI', '人工知能', '生成AI', 'エヌビディア', 'NVIDIA', 'ラピダス', 'Rapidus'],
    },
    '防衛': {
        'weight': 3,
        'keywords': ['防衛', '防衛費', '防衛装備', '自衛隊', '安全保障', '防衛産業'],
    },
    '量子コンピュータ': {
        'weight': 3,
        'keywords': ['量子コンピュータ', '量子技術', '量子暗号', '量子通信'],
    },
    '宇宙': {
        'weight': 2,
        'keywords': ['宇宙', '衛星', 'ロケット', 'JAXA', '宇宙開発'],
    },
    'サイバーセキュリティ': {
        'weight': 2,
        'keywords': ['サイバーセキュリティ', 'サイバー攻撃', 'サイバー防御', '情報セキュリティ'],
    },
    '造船・脱炭素船': {
        'weight': 2,
        'keywords': ['造船', '脱炭素船', '次世代船', '洋上風力'],
    },
    'インフラ強靭化・防災': {
        'weight': 2,
        'keywords': ['国土強靭化', '防災', 'インフラ整備', '老朽化対策'],
    },
    '合成生物学': {
        'weight': 2,
        'keywords': ['合成生物学', 'バイオものづくり', 'バイオテクノロジー'],
    },
    '海洋資源・レアアース': {
        'weight': 2,
        'keywords': ['レアアース', '海洋資源', '重要鉱物', 'レアメタル'],
    },
    '脱デフレ・内需': {
        'weight': 1,
        'keywords': ['脱デフレ', '内需拡大', '賃上げ', '物価高対策'],
    },
}

# ---------------------------------------------------------------------------
# ②ニュース収集元（政策系の公的機関RSS）
#   防衛省・内閣府は2026-09-06にfetch成功を確認済み。経済産業省は日本語版RSSの
#   正確なURLが未確認のためコメントアウト（実装時に確認して追加すること）。
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    {'source': '防衛省', 'url': 'https://www.mod.go.jp/j/rss/news.xml'},
    {'source': '内閣府', 'url': 'https://www.cao.go.jp/rss/news.rdf'},
    # {'source': '経済産業省', 'url': '<要確認：https://www.meti.go.jp/rss/ 配下のURL>'},
]

FEED_TIMEOUT_SECONDS = env_float('THEME_NEWS_FEED_TIMEOUT_SECONDS', 15)
FEED_USER_AGENT = 'Mozilla/5.0 (KurosukeBot theme_news.py)'

# ---------------------------------------------------------------------------
# ③銘柄名マッチングの設定
# ---------------------------------------------------------------------------
# 法人格・記号など、共起マッチングの邪魔になる部分を社名から取り除く。
# 前方一致・後方一致の両方をカバーするため、置換ではなく正規表現で一括除去する。
_CORP_SUFFIX_PATTERN = re.compile(
    r'(株式会社|（株）|\(株\)|ホールディングス|ＨＤ|HD|グループ本社|グループ)$'
)
_CORP_PREFIX_PATTERN = re.compile(r'^(株式会社|（株）|\(株\))')

COMPANY_NAME_MIN_LENGTH = env_int('THEME_NEWS_COMPANY_NAME_MIN_LENGTH', 2)


def normalize_company_name(raw_name):
    """
    社名から法人格の接頭辞・接尾辞を取り除いた「マッチング用のコア名」を作る。
    例：「株式会社ラピダス」「ラピダスホールディングス」→「ラピダス」
    取り除いた結果がCOMPANY_NAME_MIN_LENGTH未満になる場合はNone（マッチング対象外）を返す。
    短すぎる社名（1〜2文字）は一般名詞と衝突しやすく誤検知の原因になるため。
    """
    if not raw_name:
        return None
    name = str(raw_name).strip()
    name = _CORP_PREFIX_PATTERN.sub('', name)
    name = _CORP_SUFFIX_PATTERN.sub('', name)
    name = name.strip()
    if len(name) < COMPANY_NAME_MIN_LENGTH:
        return None
    return name


# ---------------------------------------------------------------------------
# ②RSS取得
# ---------------------------------------------------------------------------
def fetch_feed_entries(feed):
    """
    1フィード分の記事一覧を取得する。取得・パース失敗時は空リストを返す
    （collector.pyの他の取得処理と同様、1フィードの失敗で全体を落とさない設計）。
    戻り値: list[dict]（'source','title','summary','link','published_date'）
    """
    source = feed['source']
    url = feed['url']
    try:
        resp = requests.get(url, timeout=FEED_TIMEOUT_SECONDS, headers={'User-Agent': FEED_USER_AGENT})
        resp.raise_for_status()
    except Exception as e:
        print(f'[theme_news] {source} のRSS取得に失敗: {e}', file=sys.stderr)
        return []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        print(f'[theme_news] {source} のRSSパースに失敗: {e}', file=sys.stderr)
        return []

    if parsed.bozo and not parsed.entries:
        # bozo=True（フォーマット違反）でもentriesが取れていれば読み進める。
        # 完全にentriesが空の場合のみ失敗として扱う。
        print(f'[theme_news] {source} のRSSがパース不能な形式でした: {parsed.get("bozo_exception")}',
              file=sys.stderr)
        return []

    out = []
    for entry in parsed.entries:
        title = (entry.get('title') or '').strip()
        summary = (entry.get('summary') or entry.get('description') or '').strip()
        link = (entry.get('link') or '').strip()
        published_date = _entry_published_date(entry)
        if not title and not summary:
            continue
        out.append({
            'source': source,
            'title': title,
            'summary': summary,
            'link': link,
            'published_date': published_date,
        })
    print(f'[theme_news] {source}: {len(out)}件取得')
    return out


def _entry_published_date(entry):
    """記事の公開日をYYYY-MM-DD文字列で返す。取得できなければNone（呼び出し側でtoday扱い）。"""
    for key in ('published_parsed', 'updated_parsed'):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc).date().isoformat()
            except Exception:
                continue
    return None


def fetch_all_entries():
    """全フィードから記事を取得し、タイトル完全一致の重複を簡易的に除去する。"""
    all_entries = []
    seen_titles = set()
    for feed in RSS_FEEDS:
        for entry in fetch_feed_entries(feed):
            key = entry['title'] or entry['link']
            if key in seen_titles:
                continue
            seen_titles.add(key)
            all_entries.append(entry)
    return all_entries


# ---------------------------------------------------------------------------
# ③テーマ・銘柄の共起マッチング
# ---------------------------------------------------------------------------
def match_themes(text):
    """テキスト中に含まれるテーマとその重みを返す。戻り値: list[(theme_name, weight)]"""
    hits = []
    for theme_name, spec in THEMES.items():
        if any(kw in text for kw in spec['keywords']):
            hits.append((theme_name, spec['weight']))
    return hits


_KATAKANA_RANGE = ('゠', 'ヿ')  # 長音記号ーを含むカタカナのUnicodeブロック


def _is_katakana(ch):
    return _KATAKANA_RANGE[0] <= ch <= _KATAKANA_RANGE[1]


def _has_katakana_boundary_conflict(text, start, end, core_name):
    """
    「キング」が「ワーキンググループ」に含まれてしまうような、カタカナ語の内部に
    別のカタカナ社名がたまたま部分文字列として出現するケースを弾くための境界チェック。

    【2026-09-07判明】初回実行の実データで、防衛省の「日米サイバー防衛政策ワーキング
    グループ（ＣＤＰＷＧ）」という記事が、社名「キング」（8118.T）と誤って共起マッチ
    した（"ワーキング"の中に"キング"がまるごと含まれるため）。日本語は分かち書きが無く
    正規表現の\\bも効かないため、代わりに「マッチ箇所の直前・直後の文字がカタカナで
    あり、かつコア社名の先頭・末尾もカタカナである」場合はカタカナ語の内部に埋没した
    誤検知とみなして除外する。

    ひらがな・漢字の社名については適用しない：日本語の助詞（は・が・の等）は
    ひらがなであり、社名の直後にひらがなの助詞が来るのはごく普通の文（＝正しい
    マッチ）なので、同じ判定をひらがな社名に適用すると正しいマッチまで誤って
    除外してしまう。カタカナ語同士の連結にのみ起こりやすい問題のため、対象を
    カタカナに限定している（完全な解決ではないが、実際に観測できた誤検知パターンへの
    対処として妥当な範囲）。
    """
    if core_name and _is_katakana(core_name[0]) and start > 0 and _is_katakana(text[start - 1]):
        return True
    if core_name and _is_katakana(core_name[-1]) and end < len(text) and _is_katakana(text[end]):
        return True
    return False


def match_companies(text, company_index):
    """
    テキスト中に含まれる銘柄（コア社名がヒットしたもの）を返す。
    company_index: [(core_name, ticker, full_name), ...]（normalize_company_nameでNoneに
    なった銘柄は事前に除外済みのインデックスを渡す想定）
    戻り値: list[(ticker, full_name)]
    """
    hits = []
    for core_name, ticker, full_name in company_index:
        idx = text.find(core_name)
        if idx == -1:
            continue
        if _has_katakana_boundary_conflict(text, idx, idx + len(core_name), core_name):
            continue
        hits.append((ticker, full_name))
    return hits


def build_company_index(meta_df):
    """
    universe.get_all_tse_tickers()が返すmeta_df（'ticker','name'列を持つ）から、
    共起マッチング用のインデックス（コア社名・ticker・元の社名のタプルのリスト）を作る。
    """
    index = []
    for row in meta_df.to_dict('records'):
        ticker = row.get('ticker')
        full_name = row.get('name')
        core_name = normalize_company_name(full_name)
        if ticker and core_name:
            index.append((core_name, ticker, full_name))
    return index


def scan_entries(entries, company_index, today_str):
    """
    記事一覧から「テーマ×銘柄の共起」を抽出する。
    戻り値: (mentions: dict[ticker] -> {date: weight}の加算用list, article_records: list)
      article_records は seen_articles 登録・ログ表示用に、記事ごとの結果を保持する。
    """
    article_records = []
    for entry in entries:
        text = f"{entry['title']} {entry['summary']}"
        theme_hits = match_themes(text)
        if not theme_hits:
            continue  # テーマに該当しない記事は銘柄マッチングも行わない（無駄な走査を避ける）

        company_hits = match_companies(text, company_index)
        if not company_hits:
            continue  # 銘柄名との共起が無ければカウント対象外（③の核心部分）

        max_weight = max(w for _, w in theme_hits)
        theme_names = [name for name, _ in theme_hits]
        entry_date = entry['published_date'] or today_str

        article_records.append({
            'date': entry_date,
            'source': entry['source'],
            'title': entry['title'],
            'link': entry['link'],
            'themes': theme_names,
            'weight': max_weight,
            'tickers': [t for t, _ in company_hits],
            'company_names': [n for _, n in company_hits],
        })
    return article_records


# ---------------------------------------------------------------------------
# 状態管理（記事の既読管理・日次言及カウントの永続化）
# ---------------------------------------------------------------------------
HISTORY_PATH = os.getenv('THEME_NEWS_HISTORY_PATH') or 'data/theme_news_history.json'
SNAPSHOT_PATH = os.getenv('THEME_NEWS_SNAPSHOT_PATH') or 'data/theme_news_snapshot.json'
HISTORY_RETENTION_DAYS = env_int('THEME_NEWS_HISTORY_RETENTION_DAYS', 120)
RECENT_WINDOW_DAYS = env_int('THEME_NEWS_RECENT_WINDOW_DAYS', 7)
BASELINE_WINDOW_DAYS = env_int('THEME_NEWS_BASELINE_WINDOW_DAYS', 30)
MOMENTUM_SMOOTHING = env_float('THEME_NEWS_MOMENTUM_SMOOTHING', 0.5)
# 【2026-09-07判明】運用初日は「ベースライン期間中にデータがある日」が1日しかない
# ケースがあり、その1日だけでbaseline_avg_per_dayを計算すると「その1日にたまたま
# 何件あったか」に結果が丸ごと引きずられ、モメンタム比が実態以上に振れる。
# 実績日数がこの値未満のときはlow_confidence=Trueを立て、呼び出し側（Discord表示や
# GROWTHスコアリング）で「参考値」であることを分かるようにする。
MIN_BASELINE_DAYS_FOR_CONFIDENCE = env_int('THEME_NEWS_MIN_BASELINE_DAYS', 14)


def load_history(path=None):
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return {'seen_articles': {}, 'daily_mentions': {}}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'[theme_news] 履歴ファイルの読み込みに失敗したため、履歴なしで再開します: {e}', file=sys.stderr)
        return {'seen_articles': {}, 'daily_mentions': {}}
    data.setdefault('seen_articles', {})
    data.setdefault('daily_mentions', {})
    return data


def save_history(history, path=None):
    path = path or HISTORY_PATH
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=json_default)
    os.replace(tmp_path, path)


def prune_history(history, today):
    """HISTORY_RETENTION_DAYSより古い日次カウント・既読記事レコードを削除し、ファイル肥大化を防ぐ。"""
    cutoff = (today - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    history['daily_mentions'] = {
        d: v for d, v in history['daily_mentions'].items() if d >= cutoff
    }
    history['seen_articles'] = {
        k: d for k, d in history['seen_articles'].items() if d >= cutoff
    }


def update_history_with_articles(history, article_records):
    """
    未読の記事のみを履歴に反映する。既読（seen_articles登録済み）の記事はスキップすることで、
    「同じ記事がRSSに残り続けて何日も言及カウントされ続ける」ことを防ぐ（設計メモ参照）。
    戻り値: 今回新規に加算された記事数
    """
    new_count = 0
    for rec in article_records:
        key = rec['link'] or rec['title']
        if not key or key in history['seen_articles']:
            continue
        history['seen_articles'][key] = rec['date']

        day_bucket = history['daily_mentions'].setdefault(rec['date'], {})
        for ticker in rec['tickers']:
            day_bucket[ticker] = day_bucket.get(ticker, 0) + rec['weight']
        new_count += 1
    return new_count


# ---------------------------------------------------------------------------
# ④モメンタムスコア算出
# ---------------------------------------------------------------------------
def compute_momentum(history, today):
    """
    銘柄ごとに「直近RECENT_WINDOW_DAYS日の言及量」と「その前BASELINE_WINDOW_DAYS日間の
    1日あたり平均言及量」を比較し、モメンタムを算出する。

    momentum_ratio = 直近合計 / (直近日数換算のベースライン期待値 + スムージング定数)
      1.0前後＝平常、1.0より大きいほど「直近で言及が急増している」ことを示す。
      ベースライン期間にまったく言及が無かった銘柄（新規に話題化した銘柄）でも
      ゼロ除算にならないよう、分母にMOMENTUM_SMOOTHINGを加える。

    戻り値: dict[ticker] -> {'recent_mentions': int, 'baseline_avg_per_day': float,
                              'momentum_ratio': float}
    """
    recent_start = today - timedelta(days=RECENT_WINDOW_DAYS - 1)
    baseline_end = recent_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=BASELINE_WINDOW_DAYS - 1)

    recent_totals = {}
    baseline_totals = {}
    baseline_days_with_data = 0

    for d_str, mentions in history['daily_mentions'].items():
        try:
            d = date_cls.fromisoformat(d_str)
        except ValueError:
            continue
        if recent_start <= d <= today:
            for ticker, cnt in mentions.items():
                recent_totals[ticker] = recent_totals.get(ticker, 0) + cnt
        elif baseline_start <= d <= baseline_end:
            baseline_days_with_data += 1
            for ticker, cnt in mentions.items():
                baseline_totals[ticker] = baseline_totals.get(ticker, 0) + cnt

    baseline_days = max(baseline_days_with_data, 1)  # 実績日数で割る（未稼働期間を過大評価しないため）
    all_tickers = set(recent_totals) | set(baseline_totals)

    out = {}
    for ticker in all_tickers:
        recent = recent_totals.get(ticker, 0)
        baseline_avg = baseline_totals.get(ticker, 0) / baseline_days
        baseline_expected = baseline_avg * RECENT_WINDOW_DAYS
        momentum_ratio = round((recent + MOMENTUM_SMOOTHING) / (baseline_expected + MOMENTUM_SMOOTHING), 2)
        out[ticker] = {
            'recent_mentions': recent,
            'baseline_avg_per_day': round(baseline_avg, 2),
            'momentum_ratio': momentum_ratio,
            'baseline_days_with_data': baseline_days_with_data,
            'low_confidence': baseline_days_with_data < MIN_BASELINE_DAYS_FOR_CONFIDENCE,
        }
    return out


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def build_snapshot(article_records, momentum_by_ticker, today_str):
    """
    今回の実行結果を1ファイルにまとめる。collector.py（将来的にGROWTHシグナルの
    ニュース・モメンタム要素として参照する想定）やDiscord投稿側からはこのファイルを読む。
    """
    per_ticker = {}
    for rec in article_records:
        for ticker, name in zip(rec['tickers'], rec['company_names']):
            entry = per_ticker.setdefault(ticker, {'name': name, 'themes': set(), 'headlines': []})
            entry['themes'].update(rec['themes'])
            if len(entry['headlines']) < 3:  # 表示用に代表的な見出しだけ残す
                entry['headlines'].append({'title': rec['title'], 'source': rec['source'], 'link': rec['link']})

    tickers_out = {}
    for ticker, momentum in momentum_by_ticker.items():
        base = per_ticker.get(ticker, {})
        tickers_out[ticker] = {
            'name': base.get('name'),
            'themes_today': sorted(base.get('themes', [])),
            'headlines': base.get('headlines', []),
            **momentum,
        }

    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'target_date': today_str,
        'recent_window_days': RECENT_WINDOW_DAYS,
        'baseline_window_days': BASELINE_WINDOW_DAYS,
        'tickers': tickers_out,
    }


def write_snapshot(snapshot, path=None):
    path = path or SNAPSHOT_PATH
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=json_default)
    os.replace(tmp_path, path)
    print(f'[theme_news] スナップショットを書き込みました: {path}')


def run(meta_df=None):
    """
    メイン処理。meta_dfを渡さない場合はuniverse.get_all_tse_tickers()を呼んで取得する
    （collector.py側で既に取得済みのmeta_dfがあれば、それを渡すことで二重取得を避けられる）。
    """
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()

    if meta_df is None:
        from universe import get_all_tse_tickers
        _, meta_df = get_all_tse_tickers()
        if meta_df is None:
            print('[theme_news] 銘柄一覧が取得できなかったため処理を中断します', file=sys.stderr)
            return None

    company_index = build_company_index(meta_df)
    print(f'[theme_news] 共起マッチング対象：{len(company_index)}銘柄'
          f'（社名が短すぎて除外：{len(meta_df) - len(company_index)}銘柄）')

    entries = fetch_all_entries()
    print(f'[theme_news] 全フィード合計：{len(entries)}件（重複除去後）')

    article_records = scan_entries(entries, company_index, today_str)
    print(f'[theme_news] テーマ×銘柄の共起がヒットした記事：{len(article_records)}件')

    history = load_history()
    new_count = update_history_with_articles(history, article_records)
    prune_history(history, today)
    save_history(history)
    print(f'[theme_news] 新規に履歴へ追加した記事：{new_count}件'
          f'（既読スキップ：{len(article_records) - new_count}件）')

    momentum_by_ticker = compute_momentum(history, today)
    snapshot = build_snapshot(article_records, momentum_by_ticker, today_str)
    write_snapshot(snapshot)

    hot = sorted(momentum_by_ticker.items(), key=lambda kv: kv[1]['momentum_ratio'], reverse=True)[:10]
    if hot:
        print('[theme_news] モメンタムスコア上位10銘柄:')
        for ticker, m in hot:
            name = snapshot['tickers'][ticker]['name']
            confidence_note = '（参考値：ベースライン実績日数がまだ少ない）' if m['low_confidence'] else ''
            print(f"  {ticker} {name}: ratio={m['momentum_ratio']} "
                  f"(直近{m['recent_mentions']}件 / 平常{m['baseline_avg_per_day']}件/日"
                  f"・実績{m['baseline_days_with_data']}日分){confidence_note}")
    else:
        print('[theme_news] 本日はテーマ×銘柄の共起ヒットがありませんでした')

    return snapshot


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        print(f'Error: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
