#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_sakata_accuracy.py — evaluate_sakata.py の測定結果JSONから、sakata.py の REFERENCE_ACCURACY を書き換える
（2026-09-16追加）。手で数字を写さないことで、どの測定結果を反映したかを再現できるようにする。

使う値：取引可能な銘柄（'liquid' スコープ）の、検出翌日の始値→20営業日後の終値の方向一致率（win_rate_pct）。
件数が MIN_COUNT 未満のパターンは None（未計測扱い）にする。
反映元のファイル名・データ指紋・コードのコミットを REFERENCE_ACCURACY_SOURCE に記録する。

使い方: python update_sakata_accuracy.py data/backtest_out/sakata_eval_final.json
"""

import json
import re
import sys

MIN_COUNT = 300


def main():
    path = sys.argv[1]
    with open(path, encoding='utf-8') as f:
        rep = json.load(f)
    table = {}
    for key, s in rep['patterns']['liquid'].items():
        name, direction = key.split('|')
        if s.get('n', 0) >= MIN_COUNT:
            table[(name, direction)] = round(s['20d']['win_rate_pct'])
    meta = rep.get('meta', {})
    source = {'file': path.replace('\\', '/'), 'data_fingerprint': meta.get('data_fingerprint_sha256', '')[:16],
              'code_commit': str((meta.get('code') or {}).get('git_commit'))[:8], 'min_count': MIN_COUNT,
              'metric': '取引可能銘柄・検出翌日始値→20営業日後終値の方向一致率(%)'}
    lines = ['REFERENCE_ACCURACY = {']
    for (name, direction), v in sorted(table.items()):
        lines.append(f"    ({name!r}, {direction!r}): {v},")
    lines.append('}')
    lines.append(f'REFERENCE_ACCURACY_SOURCE = {source!r}')
    block = '\n'.join(lines)
    with open('sakata.py', encoding='utf-8') as f:
        src = f.read()
    new = re.sub(r'(# --- REFERENCE_ACCURACY_BEGIN ---\n).*?(\n# --- REFERENCE_ACCURACY_END ---)',
                 lambda m: m.group(1) + block + m.group(2), src, flags=re.S)
    with open('sakata.py', 'w', encoding='utf-8') as f:
        f.write(new)
    print(block)


if __name__ == '__main__':
    main()
