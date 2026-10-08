# data/daily_bars — 日足の保存（検証用）

J-Quants無料プランは12週間遅れのため、直近の株価を検証に使えるよう、botが取得した日足をここに残す（2026-10-08開始）。

## 毎晩の保存分：`YYYY/YYYY-MM-DD.csv.gz`
- `collector.py` の `save_daily_bars()` が毎晩保存する。ファイル名は**収集日**。
- 各銘柄の直近3営業日分を重ねて保存している（収集が失敗した日を翌日の分で埋めるため）。
  読むときは、同じ銘柄・同じ日付の行が複数あれば、**後の収集日のファイルの行**を使う。
- 株価はbotと同じ yfinance `auto_adjust=True`（分割・配当とも調整済み）で、**保存した日時点の調整値**。
- 最新の日の行にだけ、その日にyfinanceから取った `per`・`pbr`・`dividend_yield`・`market_cap`（円）が入る。

## 過去分の補完：`backfill/2026-06-10_2026-10-06_yahoo_split_adjusted.csv.gz`
- 日足の保存を始める前の期間（J-Quantsの最終日2026-06-09の翌日〜2026-10-06）を、`extend_bars_yfinance.py` で
  Yahoo! Financeから取り直したもの（2026-10-08作成。3,593銘柄・80営業日）。
- 株価は `auto_adjust=False`（**分割は調整済み・配当は未調整**＝J-Quantsの生の終値に相当）。毎晩の保存分とは調整方法が違う。
- `market_cap`（円）は、J-Quantsの2026-06-09前後の時価総額に株価の変化率を掛けた近似（株数は変わらない前提）。
- 確認済み：J-Quantsと重なる期間の終値のずれが平均1%以内の銘柄だけを入れた（ずれが大きかった107銘柄と、
  Yahoo!で取れなかった40銘柄は含まない）。botが毎日gitに保存した `data/latest_scan.json` の終値とも照合し、
  2026-08-26〜10-07で中央値のずれ0.000%（2%超のずれはほぼ0%）。
- PER・PBRは含まない（当時のyfinanceの値は再取得できない）。2026-08-25以降は `data/latest_scan.json` のgit履歴に
  全銘柄のPER×PBR・配当利回りが残っている。

## それより前の期間
`data/jquants_cache/<銘柄>/bars.json`（J-Quants、2024-06〜2026-06-09）を使う。
