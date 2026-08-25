# Kurosuke割安チェッカー - GitHub Actions セットアップ完全ガイド

## 全体フロー

```
1️⃣ GitHub リポジトリを作成
   ↓
2️⃣ ローカルで Git セットアップ
   ↓
3️⃣ Discord Webhook URL を取得（10個）
   ↓
4️⃣ GitHub Secrets に登録
   ↓
5️⃣ Actions の書き込み権限を有効化（今回追加の手順・重要）
   ↓
6️⃣ ファイルを GitHub に Push
   ↓
7️⃣ データ収集ワークフローをテスト実行（まずは少数銘柄で）
   ↓
8️⃣ Discord投稿ワークフローをテスト実行
   ↓
✅ 本番運用開始（毎日 02:00 JST頃データ収集 → 08:00 JST投稿）
```

---

## ステップ 1️⃣ : GitHub リポジトリを作成

1. **GitHub にアクセス** → https://github.com/new

2. **Repository name：** `Trading-Signal-Bot-Kurosuke`

3. **Access:** Private に設定

4. **Create repository** をクリック

✅ 完了！

---

## ステップ 2️⃣ : Discord Webhook URL を取得（10個）

### テスト用サーバーの Webhook URL

**#ロング-シグナル（テスト）の URL を作成**
1. サーバー → チャネル設定 → 連携
2. ウェブフック → ウェブフックを新規作成
3. 名前：`Kurosuke-Long-Test`
4. URL をコピー
5. `DISCORD_WEBHOOK_URL_LONG_TEST` として保存

**#ショート-シグナル（テスト）の URL**
- 同じ方法で作成
- `DISCORD_WEBHOOK_URL_SHORT_TEST` として保存

**#マーケット警告（テスト）の URL**
- 同じ方法で作成
- `DISCORD_WEBHOOK_URL_WARNING_TEST` として保存

**#パフォーマンス（テスト）の URL**
- 同じ方法で作成
- `DISCORD_WEBHOOK_URL_PERFORMANCE_TEST` として保存

**#戦略通知（テスト）の URL**
- 同じ方法で作成
- `DISCORD_WEBHOOK_URL_SAKATA_TEST` として保存

### 本番用サーバーの Webhook URL（同じ方法で5個）

- `DISCORD_WEBHOOK_URL_LONG_PROD`
- `DISCORD_WEBHOOK_URL_SHORT_PROD`
- `DISCORD_WEBHOOK_URL_WARNING_PROD`
- `DISCORD_WEBHOOK_URL_PERFORMANCE_PROD`
- `DISCORD_WEBHOOK_URL_SAKATA_PROD`

✅ 合計 10個の Webhook URL を取得

---

## ステップ 3️⃣ : ローカルで Git セットアップ

### Windows の場合（PowerShell）

```powershell
# 作業フォルダを作成
mkdir Trading-Signal-Bot-Kurosuke
cd Trading-Signal-Bot-Kurosuke

# GitHub Actions 用フォルダを作成
mkdir .github
mkdir .github\workflows
mkdir data

# 以下のファイルを作成（コピー＆ペースト）
# - requirements.txt
# - .gitignore
# - .env.example
# - README.md
# - collector.py
# - poster.py
# - indicators.py
# - sakata.py
# - scoring.py
# - universe.py
# - util.py
# - .github/workflows/collect_data.yml
# - .github/workflows/post_signals.yml
```

### Mac / Linux の場合（Terminal）

```bash
# 作業フォルダを作成
mkdir Trading-Signal-Bot-Kurosuke
cd Trading-Signal-Bot-Kurosuke

# GitHub Actions 用フォルダを作成
mkdir -p .github/workflows
mkdir -p data

# ファイルを作成（テキストエディタで）
```

### Git を初期化

```bash
# Git を初期化
git init

# ユーザー情報を設定（初回のみ）
git config --global user.name "kurosuke26"
git config --global user.email "your-email@example.com"

# 全てのファイルをステージング
git add .

# コミット（PowerShell 注意：シングルクォート使用）
git commit -m 'Initial commit: Kurosuke Trading System'

# ブランチをリネーム
git branch -M main
```

✅ Git のセットアップ完了

---

## ステップ 4️⃣ : GitHub に Push

### リモートリポジトリを追加

```bash
git remote add origin https://github.com/kurosuke26/Trading-Signal-Bot-Kurosuke.git

# 確認
git remote -v
```

### GitHub に Push

```bash
git push -u origin main
```

❗ **認証情報を求められたら：**

**ユーザー名：** `kurosuke26`

**パスワード：** GitHub Personal Access Token（PAT）

### GitHub PAT を取得

1. ブラウザで https://github.com/settings/tokens を開く

2. **「Generate new token」** → **「Generate new token (classic)」**

3. **Token name：** `git-push-token`

4. **Scopes：** `repo` にチェック

5. **「Generate token」** をクリック

6. **トークンをコピー**（二度と表示されません）

7. PowerShell の Password に貼り付け

✅ GitHub に Push 完了

---

## ステップ 5️⃣ : GitHub Secrets に登録

1. GitHub Repository ページを開く

2. **Settings** → **Secrets and variables** → **Actions**

3. **New repository secret** をクリック

### 以下の 10個を登録

#### テスト用（5個）

| Name | Value |
|------|-------|
| DISCORD_WEBHOOK_URL_LONG_TEST | テスト用 #ロング-シグナル の URL |
| DISCORD_WEBHOOK_URL_SHORT_TEST | テスト用 #ショート-シグナル の URL |
| DISCORD_WEBHOOK_URL_WARNING_TEST | テスト用 #マーケット警告 の URL |
| DISCORD_WEBHOOK_URL_PERFORMANCE_TEST | テスト用 #パフォーマンス の URL |
| DISCORD_WEBHOOK_URL_SAKATA_TEST | テスト用 #戦略通知 の URL |

#### 本番用（5個）

| Name | Value |
|------|-------|
| DISCORD_WEBHOOK_URL_LONG_PROD | 本番用 #ロング-シグナル の URL |
| DISCORD_WEBHOOK_URL_SHORT_PROD | 本番用 #ショート-シグナル の URL |
| DISCORD_WEBHOOK_URL_WARNING_PROD | 本番用 #マーケット警告 の URL |
| DISCORD_WEBHOOK_URL_PERFORMANCE_PROD | 本番用 #パフォーマンス の URL |
| DISCORD_WEBHOOK_URL_SAKATA_PROD | 本番用 #戦略通知 の URL |

✅ Secrets 登録完了（Webhookが必要なのは投稿側の post_signals.yml のみです）

---

## ステップ 6️⃣ : Actions の書き込み権限を有効化（重要・今回追加の手順）

データ収集ワークフロー（`collect_data.yml`）は、取得結果 `data/latest_scan.json` を
リポジトリへ自動コミットします。そのためにはActionsのデフォルトトークンに書き込み権限が
必要です。

1. GitHub Repository → **Settings** → **Actions** → **General**
2. 一番下までスクロールし「Workflow permissions」を探す
3. **Read and write permissions** を選択
4. **Save** をクリック

✅ この設定を忘れると、データ収集は成功してもコミットに失敗し、
翌朝のDiscord投稿にデータが渡りません。

---

## ステップ 7️⃣ : ファイルを GitHub に Push

（ステップ4で既にPush済みの場合はスキップしてください）

```bash
git add .
git commit -m 'Add collector.py / poster.py (2-stage architecture)'
git push
```

---

## ステップ 8️⃣ : データ収集ワークフローをテスト実行（少数銘柄から）

全銘柄（約3,900）をいきなり回さず、まずは少数銘柄で動作確認します。

1. GitHub Repository ページを開く
2. **Actions** タブをクリック
3. 左側に **「Kurosuke Data Collection」** が表示される
4. **Run workflow** をクリック
5. `max_tickers` に `50` などの小さい数値を入力し、**Run workflow** をクリック（確認）

⏳ **50銘柄程度なら数分で完了します**

✅ **緑色のチェックマーク** が出たら成功！ Actions のログも必ず確認してください
（JPX銘柄一覧の取得件数、ファンダメンタルズ・株価取得の成功/失敗件数など）。

実行後、リポジトリの `data/latest_scan.json` を開き、`kurosuke-bot` によるコミットが
入っているか確認してください。

---

## ステップ 9️⃣ : Discord投稿ワークフローをテスト実行

1. **Actions** タブ → **「Kurosuke Discord Posting」**
2. **Run workflow** をクリック（`max_tickers` のような入力欄は無く、保存済みデータを
   読むだけなのですぐ完了します）

Discord のテスト用サーバーを開き、各チャネルを確認：

- ✅ #ロング-シグナル（テスト）に投稿されたか（複合スコア・テクニカル・酒田五法・
  エントリー目安が表示されているか）
- ✅ #ショート-シグナル（テスト）に投稿されたか
- ✅ #マーケット警告（テスト）に投稿されたか
- ✅ #パフォーマンス（テスト）に投稿されたか（対象銘柄数・成功/失敗件数・データ収集時間）
- ✅ #戦略通知（テスト）に投稿されたか

✅ **全てのチャネルに想定通り投稿されていたら、次はデータ収集を全銘柄で実行してみましょう**
（ステップ8を `max_tickers` 空欄で再度実行 → 完了後ステップ9で投稿確認）。
実行時間が想定より長い場合は `time_budget_minutes` を調整してください。

---

## ステップ 🔟 : 本番用サーバーでも同じテストを実行

1. 本番用Webhook（`_PROD`）をSecretsに登録

2. 「Kurosuke Discord Posting」を手動実行

3. 本番用サーバーの各チャネルで投稿を確認

✅ テスト用・本番用の両方で動作確認完了

---

## ステップ 1️⃣1️⃣ : 本番運用開始

```
データ収集：cron '0 17 * * *'（UTC）= 02:00 JST（翌日）頃開始・約4時間かけて実行
Discord投稿：cron '0 23 * * *'（UTC）= 08:00 JST 実行
```

どちらも毎日自動実行されます。手動実行も引き続き可能です（Actions → 各ワークフロー →
Run workflow）。

---

## トラブルシューティング

### ❌ データ収集ワークフローが実行されない

**確認項目：**
1. Secrets が全て登録されているか？
2. collect_data.yml / post_signals.yml のファイルパスは正確か？
3. ファイルが main ブランチに Push されているか？

### ❌ data/latest_scan.json がコミットされない

**確認項目：**
1. ステップ6の「Read and write permissions」を有効化したか？
2. collect_data.yml の `permissions: contents: write` が残っているか？
3. Actions のログで「Commit and push snapshot」ステップのエラーを確認

### ❌ Discord投稿ワークフローが「データが見つかりません」で失敗する

**確認項目：**
1. データ収集ワークフローが少なくとも1回成功しているか？
2. `data/latest_scan.json` がリポジトリに存在するか？
3. `SCAN_OUTPUT_PATH` を独自に設定している場合、collector.py側とposter.py側で
   同じ値になっているか？

### ❌ Discord に投稿されない

**確認項目：**
1. Webhook URL が正確にコピーされているか？
2. Webhook URL の期限が切れていないか？
3. チャネルに対する権限があるか？

### ❌ Python エラーが出ている

**確認項目：**
1. requirements.txt に全ての依存関係が含まれているか？
2. collector.py / poster.py / indicators.py / sakata.py / scoring.py / universe.py /
   util.py がすべて同じディレクトリに存在するか？

### ❌ データ収集が時間切れになる／Yahoo!からエラーが大量に出る

**確認項目：**
1. `TIME_BUDGET_MINUTES`（既定230分）を確認。ジョブのtimeout-minutes（280分）より
   十分小さいか
2. `FUNDAMENTALS_WINDOW_MINUTES` / `HISTORY_WINDOW_MINUTES` の配分を見直す
3. `FETCH_WORKERS` を下げてみる（並列数を減らすとレート制限を回避しやすい）
4. ログの [fundamentals] / [history] 進捗表示から、どの段階で時間がかかっているか確認

### 詳細ログを確認

GitHub Actions の実行結果から詳細ログを確認：
1. Actions → 該当ワークフロー
2. 実行結果をクリック
3. 各ステップのログをクリック
4. エラーメッセージを確認

---

## 環境変数の設定

### ローカル実行用（.env ファイル）

```bash
# .env.example をコピーして .env を作成
cp .env.example .env

# .env を編集
# DISCORD_WEBHOOK_URL_LONG_TEST=https://...
# DISCORD_WEBHOOK_URL_SHORT_TEST=https://...
# etc.
```

### GitHub Actions 用（Secrets）

GitHub Secrets に登録した値が自動的に使用されます（post_signals.yml のみ）。
`max_tickers` / `time_budget_minutes` は Secrets ではなく、collect_data.yml の
手動実行（workflow_dispatch）の入力欄から指定します。

---

## 本番運用時の注意事項

1. **毎日 02:00 JST頃データ収集 → 08:00 JST投稿の自動実行**
   - 寝坊しても自動実行される
   - GitHub のサーバーが動作していれば確実に実行

2. **テスト用・本番用の同時運用**
   - テスト用で検証してから本番用に投稿
   - トラブル時はテスト用で確認

3. **Discord Webhook URL の管理**
   - Secrets に登録した値は GitHub に表示されない
   - .env ファイルは .gitignore で除外

4. **定期的な監視**
   - GitHub Actions の実行ログを確認（データ収集・投稿の両方）
   - Discord の投稿内容を確認
   - 全銘柄実行時のActions消費時間を定期的にチェック（プライベートリポジトリの
     無料枠に注意。消費時間の大部分はデータ収集側）

---

## サポート

問題が発生した場合：
1. GitHub の Issues を開く
2. エラーログを貼り付け
3. 実行環境（Windows/Mac/Linux）を記載

---

**最終更新日：** 2026年8月25日
