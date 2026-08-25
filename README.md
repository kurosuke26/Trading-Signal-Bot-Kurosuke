# Kurosuke割安チェッカー

日本株スイングトレーダー向けの完全自動分析システムです。

## 今回のアップデート概要

- **対象銘柄を東証プライム・スタンダード・グロースの内国株式 全銘柄（約3,900銘柄）に拡大**
  （`universe.py`。JPXが公開している東証上場銘柄一覧を実行時に取得。取得に失敗した場合は
  従来の5銘柄にフォールバックします）
- **テクニカル指標を追加**：移動平均線（MA5/25/75）・RSI（14）・MACD（12,26,9）・ATR（14）
  （`indicators.py`）
- **酒田五法10パターンの自動検出を追加**：三山・三川・三兵・三空・三法・たすき線・毛抜き
  （資料内「かい値」に相当）・段違い・N字・V字（`sakata.py`）
- **ファンダメンタルズ＋テクニカル＋酒田五法を統合した複合スコア**を実装
  （`scoring.py`。配点はREADME従来記載の テクニカル25%／酒田五法25%／配当15%／
  PER×PBR20%／EPS10%／グロース5% をそのまま採用）
- **ATRベースのトレーリングストップ目安**：エントリー・初期損切り・トレーリングルールを
  LONG銘柄の投稿に表示（実際のポジション追跡・自動決済はまだ行いません＝シグナル生成のみ）
- **処理を「データ収集」と「Discord投稿」の2段階に分割**（下記「アーキテクチャ」参照）。
  データ収集は 02:00 JST 頃から約4時間かけてゆっくりペースで行い、Discord投稿は
  08:00 JST に保存済みデータを読んで即座に行う

**正直な注意点**：このアップデートを開発したサンドボックス環境はネットワークが制限されて
おり、Yahoo! Finance にも JPX にも直接アクセスできませんでした。そのため合成データでの
単体テスト（`test_synthetic.py` / `test_patterns_targeted.py`）とモックを使った結合テスト
（`test_integration.py`）は実施済みですが、**実データに対する動作確認はまだ行っていません**。
必ずテスト用サーバーで、かつ最初は `MAX_TICKERS` で少数に絞って実行し、ログとDiscordの
投稿内容を確認してから本番へ進めてください（詳しくは下記「テスト方法」参照）。

## アーキテクチャ（データ収集とDiscord投稿を分離した理由）

以前は「データ取得」と「Discord投稿」を1本のスクリプトで毎朝まとめて行っていましたが、
全銘柄（約3,900）が対象になると、短時間に大量のリクエストをYahoo! Financeへ送ることに
なり、レート制限（ブロック）を受けやすくなります。そこで以下の2段階に分割しました。

```
02:00 JST頃                                          08:00 JST
   │                                                     │
   ▼                                                     ▼
┌─────────────────────────┐                    ┌──────────────────┐
│ collect_data.yml          │  data/latest_scan  │ post_signals.yml   │
│  → collector.py           │ ------.json------> │  → poster.py       │
│  全銘柄のデータ取得・判定  │  (リポジトリに     │  Discordの5チャネル │
│  ペースを抑えて約4時間     │   コミット)         │  へ投稿するだけ     │
│  かけて実行                │                    │  （数秒〜数十秒）    │
└─────────────────────────┘                    └──────────────────┘
```

- **collector.py**：JPX銘柄一覧の取得、Yahoo Financeからのファンダメンタルズ・株価履歴取得、
  テクニカル指標計算、酒田五法パターン検出、複合スコア算出までを行い、結果を
  `data/latest_scan.json` に保存する。Discordへは投稿しない。
  `collect_data.yml` が実行後にこのファイルをリポジトリへ自動コミットする。
- **poster.py**：`data/latest_scan.json` を読み込み、5チャネルへ整形して投稿するだけ。
  Yahoo Financeへのアクセスは一切行わないため、実行は数秒〜数十秒で終わる。

**データが「古くならない」理由**：東証の取引時間は 9:00〜15:30 JST。前営業日の取引終了
（15:30 JST）から当日の寄り付き（9:00 JST）までは株価・出来高等のデータは更新されない
ため、2:00に取得しても8:00に取得しても、Yahoo! Finance側の値は基本的に同じ（＝前営業日
終値ベース）になります。したがって収集タイミングを深夜に早めても、情報が古くなる
デメリットは実質的に生じません。

**収集ジョブが失敗・間に合わなかった場合**：`poster.py` は `data/latest_scan.json` の
生成時刻を確認し、
- ファイル自体が存在しない → マーケット警告チャネルにその旨を投稿して終了
- 生成から `MAX_SNAPSHOT_AGE_HOURS`（既定12時間）以上経過している → 投稿はするが、
  ロング/ショート/戦略通知の本文とマーケット警告に「データが古い可能性がある」旨を明記

という扱いをします。無言でスキップしたり、古いデータをさも最新のように投稿したりしません。

## 機能

- データ収集：毎日 02:00 JST 頃に自動実行（約4時間かけてゆっくり取得、GitHub Actions）
- Discord投稿：毎日 08:00 JST に自動実行（保存済みデータを読むだけの軽い処理）
- JPX公開の東証上場銘柄一覧から対象銘柄（プライム・スタンダード・グロースの内国株式、
  約3,900銘柄）を取得
- Yahoo Finance から配当利回り・PER・PBR・EPS推移・日足OHLCVを取得
- kurosuke割安チェッカーの基本条件（配当3.5〜5.8%・PER×PBR≦22.5）でLONG/SHORTを一次判定
- MA/RSI/MACD・酒田五法パターン・ファンダメンタルズを統合した複合スコアで「買い時」を判定
- ATRベースの初期損切り・トレーリングストップ目安を提示
- 複数チャネルに自動投稿（LONG/SHORTはDiscordの10 Embed上限を超える分をCSV添付で補完）

## 対応銘柄

東証プライム・スタンダード・グロース市場の内国株式、約3,900銘柄（動的取得のため日々変動）。
JPXの銘柄一覧が取得できなかった場合のみ、以下5銘柄にフォールバックします：

- 6758.T（ソニー）
- 7203.T（トヨタ自動車）
- 9984.T（ソフトバンクグループ）
- 6861.T（キーエンス）
- 8306.T（三菱UFJフィナンシャル・グループ）

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/kurosuke26/Trading-Signal-Bot-Kurosuke.git
cd Trading-Signal-Bot-Kurosuke
```

### 2. 環境変数を設定

```bash
cp .env.example .env
# .env を編集して Discord Webhook URLs を設定
```

### 3. GitHub Secrets に登録

GitHub Repository → Settings → Secrets and variables → Actions

以下の10個の Webhook URL を登録（Secrets名は従来と同じで変更ありません。**poster.py側の
ワークフローにのみ**設定すればOKです。collector.py側はDiscordへ投稿しないためWebhookは
不要です）：

**テスト用：**
- DISCORD_WEBHOOK_URL_LONG_TEST
- DISCORD_WEBHOOK_URL_SHORT_TEST
- DISCORD_WEBHOOK_URL_WARNING_TEST
- DISCORD_WEBHOOK_URL_PERFORMANCE_TEST
- DISCORD_WEBHOOK_URL_SAKATA_TEST

**本番用：**
- DISCORD_WEBHOOK_URL_LONG_PROD
- DISCORD_WEBHOOK_URL_SHORT_PROD
- DISCORD_WEBHOOK_URL_WARNING_PROD
- DISCORD_WEBHOOK_URL_PERFORMANCE_PROD
- DISCORD_WEBHOOK_URL_SAKATA_PROD

### 4. リポジトリの「Actions の権限」を確認（重要・今回追加の手順）

`collect_data.yml` は取得結果（`data/latest_scan.json`）をリポジトリに自動コミットします。
このためにはGitHub Actionsのデフォルトトークンに書き込み権限が必要です。

1. GitHub Repository → **Settings** → **Actions** → **General**
2. 「Workflow permissions」で **Read and write permissions** を選択して保存
   （ワークフローファイル内でも `permissions: contents: write` を明示していますが、
   リポジトリ側の設定がRead onlyのままだと上書きされず失敗するため、必ず確認してください）

### 5. 依存関係をインストール（ローカル実行時）

```bash
pip install -r requirements.txt
```

## テスト方法（重要）

全銘柄（約3,900）をいきなり毎日回す前に、必ずテスト用サーバー・小規模銘柄数で動作確認して
ください。

1. Actions タブ → **「Kurosuke Data Collection」** → **Run workflow**
2. `max_tickers` に `50` のような小さい数値を入力して実行
3. 実行ログで、JPX銘柄一覧の取得件数・区分内訳、ファンダメンタルズ/株価取得の成功件数を確認
4. 実行後、リポジトリに `data/latest_scan.json` がコミットされているか確認
5. Actions タブ → **「Kurosuke Discord Posting」** → **Run workflow** で手動実行
6. テスト用Discordサーバーの5チャネルに想定通り投稿されているか確認
   （特にLONG/SHORTのEmbedの見た目、複合スコア・テクニカル・酒田五法の表示内容）
7. 問題なければ「Kurosuke Data Collection」を `max_tickers` 空欄（＝全銘柄）で再度実行し、
   実行時間・失敗件数を確認
8. 全銘柄実行が安定したら、本番用Webhook（`_PROD`）をSecretsに登録して同様に確認

ローカルでの単体テスト（ネットワーク不要・合成データ）：

```bash
python test_synthetic.py          # indicators/sakata/scoringが例外なく動くか
python test_patterns_targeted.py  # 各酒田五法パターンが狙った形で検出できるか
python test_integration.py        # collector→poster のend-to-endテスト（ネットワークはモック）
```

ローカルで実際に2段階を通して試す場合：

```bash
MAX_TICKERS=20 python collector.py   # data/latest_scan.json が作られる
python poster.py                     # それを読んでDiscordに投稿（.envにWebhook URLが必要）
```

## 実行時間・GitHub Actions分についての注意

データ収集（collector.py）は全銘柄（約3,900）を対象にするため、意図的にペースを抑えて
（既定：ファンダメンタルズ取得に150分、株価履歴取得に65分ほど配分）実行しても、
Yahoo! Finance側の状況によっては非常停止ライン（`TIME_BUDGET_MINUTES`、既定230分）に
達することがあります。その場合は残りの銘柄をスキップし、そこまでの結果を保存します
（未処理銘柄数はログとパフォーマンスチャネルに表示されます）。

プライベートリポジトリの場合、GitHub Actionsの無料実行時間枠（プランにより異なります）を
消費するため、最初の数回の全銘柄実行で所要時間を確認してください。データ投稿
（poster.py）側はほぼ一瞬で終わるため、Actions分の大部分はデータ収集側で消費されます。

## 環境変数（実行パラメータ）

### collector.py（データ収集）

| 変数名 | 既定値 | 説明 |
|---|---|---|
| MAX_TICKERS | 0（無制限） | テスト用：対象銘柄数の上限 |
| TIME_BUDGET_MINUTES | 230 | 非常停止ライン（分）。超過分の銘柄はスキップ |
| FUNDAMENTALS_WINDOW_MINUTES | 150 | ファンダメンタルズ取得を均等に引き延ばす目標時間（分） |
| HISTORY_WINDOW_MINUTES | 65 | 株価履歴取得を均等に引き延ばす目標時間（分） |
| HISTORY_PERIOD | 9mo | 株価履歴の取得期間（MA75計算に必要な日数を確保） |
| FETCH_WORKERS | 4 | ファンダメンタルズ取得の並列数（控えめな既定値） |
| HISTORY_CHUNK_SIZE | 100 | 株価履歴取得のチャンクサイズ |
| INFO_CHUNK_SIZE | 30 | ファンダメンタルズ取得のチャンクサイズ |
| TOP_NEUTRAL_KEEP | 20 | ニュートラル銘柄のうち詳細を保存する上位件数（JSON肥大化防止） |
| SCAN_OUTPUT_PATH | data/latest_scan.json | 出力先パス |

### poster.py（Discord投稿）

| 変数名 | 既定値 | 説明 |
|---|---|---|
| SCAN_OUTPUT_PATH | data/latest_scan.json | 読み込み元パス（collector.pyと合わせる） |
| MAX_SNAPSHOT_AGE_HOURS | 12 | この時間を超えて古いデータは「古い」警告付きで投稿 |

## 投稿チャネル

### テスト用サーバー

- #ロング-シグナル（テスト）
- #ショート-シグナル（テスト）
- #マーケット警告（テスト）
- #パフォーマンス（テスト）
- #戦略通知（テスト）

### 本番用サーバー

- #ロング-シグナル
- #ショート-シグナル
- #マーケット警告
- #パフォーマンス
- #戦略通知

## 判定ロジック

### 一次フィルタ（kurosuke割安チェッカーの基本条件・従来通り）

- LONG候補：配当利回り 3.5%〜5.8% かつ PER×PBR ≦ 22.5
- SHORT候補：PER×PBR > 30、または MA配列が弱気かつ精度70%以上の弱気酒田五法パターンを検出

### 複合スコア（0〜100点、取得できなかった項目は母数から除外して再配分）

| 項目 | 配点 | 内容 |
|---|---|---|
| テクニカル | 25% | MA5/25/75の配列・ゴールデンクロス・MACD・RSI |
| 酒田五法 | 25% | 10パターンの検出結果（参考精度で重み付け） |
| 配当利回り | 15% | 3.5〜5.8%で満点 |
| PER×PBR | 20% | 15未満で満点、22.5まで部分点 |
| EPS推移 | 10% | 直近3期が維持〜増加で満点（LONG候補のみ追加取得） |
| グロース | 5% | 直近の増益率（取得できる場合のみ） |

スコア評価：80点以上＝割安度が極めて高い／70-79＝割安度が高い／60-69＝やや割安／
50-59＝中立・監視対象／50点未満＝割高

### エントリー・トレーリングストップ

LONG候補には、ATR×1.5を基準にした初期損切り目安と、トレーリングストップの運用ルール
（直近高値からATR×1.5を引いた水準を目安に切り上げる「シャンデリア・ストップ」方式）を
表示します。**実際のポジションを記録して自動的に決済判定する機能はまだありません**
（ロードマップのGoogle Sheets連携フェーズで対応予定）。

## 酒田五法パターン（10パターン、参考精度は引き継ぎ資料に基づく参考値）

1. 三山（さんざん）：売りシグナル - 参考精度80%
2. 三川（さんせん）：買いシグナル - 参考精度85%
3. 三兵（さんぺい）：トレンド転換 - 参考精度70%
4. 三空（さんくう）：強いトレンド継続（行き過ぎ）- 参考精度75%
5. 三法（さんぽう）：トレンド継続 - 参考精度65%
6. たすき線：転換シグナル - 参考精度60%
7. 毛抜き（資料内の「かい値」に相当）：反発シグナル - 参考精度70%
8. 段違い：トレンド継続 - 参考精度55%
9. N字：買い継続 - 参考精度75%
10. V字：強気反発 - 参考精度80%

※検出はヒューリスティック（経験則）による近似で、参考精度は実測のバックテスト値では
ありません。詳細は `sakata.py` 冒頭のコメントを参照してください。

## ディレクトリ構造

```
Trading-Signal-Bot-Kurosuke/
├── .github/
│   └── workflows/
│       ├── collect_data.yml   # 02:00 JST頃起動。データ収集→data/latest_scan.jsonをコミット
│       └── post_signals.yml   # 08:00 JST起動。data/latest_scan.jsonを読んでDiscordに投稿
├── .gitignore
├── .env.example
├── README.md
├── IMPLEMENTATION_STRATEGY.md
├── SETUP_GUIDE.md
├── requirements.txt
├── collector.py               # データ収集・判定（Discordへは投稿しない）
├── poster.py                  # Discord投稿のみ（保存済みデータを読むだけ）
├── indicators.py              # テクニカル指標（MA/RSI/MACD/ATR）
├── sakata.py                  # 酒田五法10パターンの検出
├── scoring.py                 # 複合スコア・トレーリングストップ計算
├── universe.py                # 東証全銘柄ティッカー一覧の取得
├── util.py                    # 環境変数パース・JSON変換・ペーシングの共通処理
├── data/
│   └── latest_scan.json       # collector.pyの出力（Git管理・毎日上書き）
├── test_synthetic.py          # 合成データでの単体テスト
├── test_patterns_targeted.py  # 酒田五法パターンの狙い撃ちテスト
└── test_integration.py        # collector→poster のend-to-endテスト
```

## ライセンス

MIT

## サポート

問題が発生した場合は GitHub Issues を開いてください。

## 今後の拡張機能（ロードマップ）

- Google Sheets 連携によるポジション追跡・自動成績集計
- LINE 通知機能
- Web ダッシュボード
- メール自動レポート

---

**作成者:** Kurosuke
**更新日:** 2026年8月25日
