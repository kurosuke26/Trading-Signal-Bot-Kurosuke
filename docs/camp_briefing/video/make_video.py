# -*- coding: utf-8 -*-
"""
説明会スライド＋読み上げ音声から、会員向けの動画（mp4）を作る。PCで実行する想定。

  使い方（詳しくは「動画の作り方.md」）:
    pip install edge-tts
    python make_video.py                     # Microsoftの読み上げ音声（男性・Keita）で作成
    python make_video.py --voice ja-JP-NanamiNeural   # 女性の声
    python make_video.py --engine voicevox --speaker 13  # VOICEVOX（アプリ起動が必要）
    python make_video.py --engine silent     # 音声なしの試し作り（文字数から長さを決める）

  フォルダ構成（このファイルと同じ場所）:
    slides/     slide01.png …（PowerPointの「エクスポート→PNG」で出た スライド1.PNG 等でもOK）
    narration/  slide01.txt …（build_deck.py が台本から自動生成。読み方を直したい時はここを編集）
    audio/      生成した音声の置き場。自分で録音した slide05.mp3 / .wav / .m4a を置くとそれを優先
    output/     完成した動画（camp_briefing_video.mp4）

  必要なもの: Python 3.9以上、ffmpeg（コマンドプロンプトで ffmpeg -version が通ること）
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SLIDES_DIR = os.path.join(BASE, 'slides')
NARRATION_DIR = os.path.join(BASE, 'narration')
AUDIO_DIR = os.path.join(BASE, 'audio')
OUT_DIR = os.path.join(BASE, 'output')
WORK_DIR = os.path.join(BASE, 'output', '_work')
OWN_AUDIO_EXTS = ('.mp3', '.wav', '.m4a')


def die(msg):
    print(f'\n[エラー] {msg}')
    sys.exit(1)


def slide_number(filename):
    m = re.search(r'(\d+)', filename)
    return int(m.group(1)) if m else None


def list_slides():
    if not os.path.isdir(SLIDES_DIR):
        die(f'slides フォルダがありません: {SLIDES_DIR}')
    slides = {}
    for f in os.listdir(SLIDES_DIR):
        if f.lower().endswith(('.png', '.jpg', '.jpeg')):
            n = slide_number(f)
            if n is not None:
                slides[n] = os.path.join(SLIDES_DIR, f)
    if not slides:
        die('slides フォルダにPNG画像がありません')
    return dict(sorted(slides.items()))


def read_narration(n):
    path = os.path.join(NARRATION_DIR, f'slide{n:02d}.txt')
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read().strip()


def own_audio(n):
    for ext in OWN_AUDIO_EXTS:
        path = os.path.join(AUDIO_DIR, f'slide{n:02d}{ext}')
        if os.path.exists(path):
            return path
    return None


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        die('ffmpegの実行に失敗しました:\n' + ' '.join(cmd) + '\n' + r.stderr[-1500:])
    return r.stdout


def duration_of(path):
    out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', path])
    return float(json.loads(out)['format']['duration'])


# ---------------------------------------------------------------------------
# 音声の作り方（engine）
# ---------------------------------------------------------------------------
def tts_edge(text, path, voice, rate):
    try:
        import edge_tts
    except ImportError:
        die('edge-tts が入っていません。コマンドプロンプトで  pip install edge-tts  を実行してください')

    async def _go():
        await edge_tts.Communicate(text, voice, rate=rate).save(path)
    asyncio.run(_go())


def tts_voicevox(text, path, speaker, host):
    # 長文は1回で合成できないことがあるので、段落ごとに合成してつなぐ
    parts = [p.strip() for p in re.split(r'\n\s*\n|\n', text) if p.strip()]
    wavs = []
    for i, part in enumerate(parts):
        q = urllib.parse.urlencode({'text': part, 'speaker': speaker})
        try:
            req = urllib.request.Request(f'{host}/audio_query?{q}', method='POST')
            query = urllib.request.urlopen(req, timeout=60).read()
            req = urllib.request.Request(f'{host}/synthesis?speaker={speaker}', data=query,
                                         headers={'Content-Type': 'application/json'}, method='POST')
            wav = urllib.request.urlopen(req, timeout=300).read()
        except Exception as e:
            die(f'VOICEVOXに接続できませんでした（アプリを起動していますか？）: {e}')
        wp = os.path.join(WORK_DIR, f'vv_{i:03d}.wav')
        with open(wp, 'wb') as f:
            f.write(wav)
        wavs.append(wp)
    concat_audio(wavs, path)


def concat_audio(paths, out_path):
    lst = os.path.join(WORK_DIR, 'audio_list.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for p in paths:
            f.write(f"file '{p.replace(os.sep, '/')}'\n")
    run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c:a', 'libmp3lame', '-b:a', '192k', out_path])


def silent_audio(text, path, chars_per_sec=7.0):
    sec = max(4.0, len(text) / chars_per_sec) if text else 4.0
    run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', f'{sec:.2f}',
         '-c:a', 'libmp3lame', '-b:a', '128k', path])


def make_audio(n, text, args):
    mine = own_audio(n)
    if mine and args.engine != 'silent':
        print(f'  スライド{n:02d}: 録音ファイルを使用（{os.path.basename(mine)}）')
        return mine
    if args.engine == 'silent':
        path = os.path.join(WORK_DIR, f'silent{n:02d}.mp3')
        silent_audio(text, path)
        return path
    path = os.path.join(AUDIO_DIR, f'tts_slide{n:02d}_{args.engine}.mp3')
    if os.path.exists(path) and not args.force:
        print(f'  スライド{n:02d}: 作成済みの音声を再利用')
        return path
    if not text:
        silent_audio('', path)
        return path
    print(f'  スライド{n:02d}: 音声を作成中…（{len(text)}文字）')
    if args.engine == 'edge':
        tts_edge(text, path, args.voice, args.rate)
    else:
        tts_voicevox(text, path, args.speaker, args.voicevox_host)
    return path


# ---------------------------------------------------------------------------
# 動画
# ---------------------------------------------------------------------------
def make_segment(n, image, audio, pad):
    seg = os.path.join(WORK_DIR, f'seg{n:02d}.mp4')
    dur = duration_of(audio) + pad
    run(['ffmpeg', '-y', '-loop', '1', '-framerate', '30', '-i', image, '-i', audio,
         '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:white,format=yuv420p',
         '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'medium', '-r', '30',
         '-af', f'apad=pad_dur={pad}', '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
         '-t', f'{dur:.3f}', seg])
    return seg, dur


def main():
    ap = argparse.ArgumentParser(description='スライド＋読み上げ音声から動画を作る')
    ap.add_argument('--engine', choices=['edge', 'voicevox', 'silent'], default='edge',
                    help='edge=Microsoftの読み上げ（無料・要ネット） / voicevox / silent=音声なしの試し作り')
    ap.add_argument('--voice', default='ja-JP-KeitaNeural', help='edgeの声（女性: ja-JP-NanamiNeural）')
    ap.add_argument('--rate', default='+5%', help='edgeの話す速さ（例: +0%%, +10%%, -5%%）')
    ap.add_argument('--speaker', type=int, default=13, help='VOICEVOXの話者ID（13=青山龍星 など）')
    ap.add_argument('--voicevox-host', default='http://127.0.0.1:50021')
    ap.add_argument('--pad', type=float, default=1.0, help='各スライドの話し終わりの余白（秒）')
    ap.add_argument('--only', type=str, default='', help='一部だけ作る（例: 1-5 や 3,7）')
    ap.add_argument('--force', action='store_true', help='作成済みの音声も作り直す')
    ap.add_argument('--output', default='camp_briefing_video.mp4')
    args = ap.parse_args()

    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        die('ffmpeg が見つかりません。「動画の作り方.md」の手順でインストールしてください')
    for d in (AUDIO_DIR, OUT_DIR, WORK_DIR):
        os.makedirs(d, exist_ok=True)

    slides = list_slides()
    if args.only:
        wanted = set()
        for part in args.only.split(','):
            if '-' in part:
                a, b = part.split('-')
                wanted.update(range(int(a), int(b) + 1))
            else:
                wanted.add(int(part))
        slides = {n: p for n, p in slides.items() if n in wanted}

    print(f'スライド {len(slides)}枚 から動画を作ります（音声: {args.engine}）')
    segs, total = [], 0.0
    for n, image in slides.items():
        audio = make_audio(n, read_narration(n), args)
        seg, dur = make_segment(n, image, audio, args.pad)
        segs.append(seg)
        total += dur

    lst = os.path.join(WORK_DIR, 'video_list.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for s in segs:
            f.write(f"file '{s.replace(os.sep, '/')}'\n")
    out = os.path.join(OUT_DIR, args.output)
    run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', '-movflags', '+faststart', out])
    print(f'\n完成: {out}（約{int(total // 60)}分{int(total % 60)}秒）')

    # YouTube概要欄に貼れるチャプター（目次）
    chapters, t = [], 0.0
    narr_titles = _slide_titles()
    for (n, _), s in zip(slides.items(), segs):
        title = narr_titles.get(n)
        if title and (title.startswith('第') or n == min(slides)):
            chapters.append(f'{int(t // 60)}:{int(t % 60):02d} {title}')
        t += duration_of(s)
    if chapters:
        if not chapters[0].startswith('0:00'):
            chapters.insert(0, '0:00 オープニング')
        cp = os.path.join(OUT_DIR, 'chapters.txt')
        with open(cp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(chapters) + '\n')
        print(f'チャプター（概要欄用）: {cp}')


def _slide_titles():
    path = os.path.join(BASE, 'slide_titles.json')
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return {int(k): v for k, v in json.load(f).items()}


if __name__ == '__main__':
    main()
