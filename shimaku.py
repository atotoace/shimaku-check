#!/usr/bin/env python3
"""字幕点検室 0.1.0 — standard-library-only, offline SRT reviewer."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import difflib
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

VERSION = "0.1.0"
MAX_BYTES = 5 * 1024 * 1024
STAMP = r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})"
TIMING = re.compile(r"^" + STAMP + r"\s+-->\s+" + STAMP + r"$")
LABELS = {
    "TIME_ORDER": "終了時刻が開始時刻以前", "UNSORTED": "開始時刻が前の字幕より早い",
    "OVERLAP": "別の字幕と表示時間が重なる", "SHORT": "表示時間が短い",
    "LONG": "表示時間が長い", "FAST": "文字数に対して表示が速い",
    "WIDE": "行が長い", "LINES": "行数が多い", "INDEX": "番号が連番でない",
    "SPACE": "行の前後に空白がある", "MARKUP": "装飾タグを含むため見た目の数値は参考",
}


@dataclass(frozen=True)
class Cue:
    number: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Settings:
    width: int = 40
    cps: float = 12.0
    min_seconds: float = 0.7
    max_seconds: float = 7.0
    max_lines: int = 2


def parse_srt(text: str) -> list[Cue]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text.strip("\n"))
    if not text.strip():
        raise ValueError("字幕が空です。")
    cues = []
    for pos, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3 or not re.fullmatch(r"[0-9]+", lines[0].strip()):
            raise ValueError(f"ブロック{pos}: 番号・時刻・本文の構造を確認してください。")
        match = TIMING.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"ブロック{pos}: 時刻は HH:MM:SS,mmm --> HH:MM:SS,mmm 形式が必要です。")
        values = [int(x) for x in match.groups()]
        def ms(v):
            h, m, s, milli = v
            return ((h * 60 + m) * 60 + s) * 1000 + milli
        body = "\n".join(lines[2:])
        if not body.strip():
            raise ValueError(f"ブロック{pos}: 字幕本文が空です。")
        cues.append(Cue(int(lines[0]), ms(values[:4]), ms(values[4:]), body))
        if len(cues) > 20000:
            raise ValueError("初版の上限は20,000字幕です。")
    return cues


def stamp(value: int) -> str:
    seconds, milli = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"


def display_width(text: str) -> int:
    # An approximate monospace width, not a rendered pixel measurement.
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) == "Cf"
               else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def analyze(cues: list[Cue], settings: Settings) -> list[dict]:
    issues = []
    def add(pos, code, detail):
        issues.append({"position": pos, "source_number": cues[pos-1].number,
                       "code": code, "label": LABELS[code], "detail": detail})
    for pos, cue in enumerate(cues, 1):
        duration = (cue.end - cue.start) / 1000
        lines = cue.text.split("\n")
        tagged = bool(re.search(r"<[^>]*>|\{\\[^}]*\}", cue.text))
        visible = re.sub(r"<[^>]*>|\{\\[^}]*\}", "", cue.text)
        count = sum(not c.isspace() and not unicodedata.combining(c) and
                    unicodedata.category(c) != "Cf" for c in visible)
        if duration <= 0:
            add(pos, "TIME_ORDER", f"{stamp(cue.start)} → {stamp(cue.end)}")
        else:
            if duration < settings.min_seconds:
                add(pos, "SHORT", f"{duration:.3f}秒 < {settings.min_seconds}秒")
            if duration > settings.max_seconds:
                add(pos, "LONG", f"{duration:.3f}秒 > {settings.max_seconds}秒")
            if count / duration > settings.cps:
                add(pos, "FAST", f"{count / duration:.1f}文字/秒 > {settings.cps}文字/秒")
        if pos > 1 and cue.start < cues[pos-2].start:
            add(pos, "UNSORTED", f"直前の位置{pos-1}と順序を確認")
        if cue.number != pos:
            add(pos, "INDEX", f"番号{cue.number}、位置は{pos}")
        if any(line != line.strip() for line in lines):
            add(pos, "SPACE", "前後空白の整形候補。意図的な字下げは残してください。")
        if len(lines) > settings.max_lines:
            add(pos, "LINES", f"{len(lines)}行 > {settings.max_lines}行")
        width = max(display_width(line) for line in visible.split("\n"))
        if width > settings.width:
            add(pos, "WIDE", f"幅{width} > {settings.width}（全角=2、半角=1の概算）")
        if tagged:
            add(pos, "MARKUP", "タグは表示・実行せず文字として報告します。実際の描画は未検証。")
    # Report each later-starting cue with one overlapping witness, including nesting.
    furthest_end = -1
    witness = None
    for pos, cue in sorted(enumerate(cues, 1), key=lambda pair: (pair[1].start, pair[0])):
        if cue.end <= cue.start:
            continue
        if cue.start < furthest_end:
            add(pos, "OVERLAP", f"位置{witness}と重複（意図的な同時表示なら許容）")
        if cue.end > furthest_end:
            furthest_end, witness = cue.end, pos
    return sorted(issues, key=lambda x: (x["position"], x["code"]))


def normalize(cues: list[Cue]) -> str:
    # Deliberately does not adjust timing, words, order or line breaks.
    return "\n\n".join(f"{i}\n{stamp(c.start)} --> {stamp(c.end)}\n" +
                        "\n".join(line.strip() for line in c.text.split("\n"))
                        for i, c in enumerate(cues, 1)) + "\n"


def make_html(cues, issues, metadata) -> str:
    esc = html.escape
    grouped = {}
    for item in issues:
        grouped.setdefault(item["position"], []).append(item)
    cards = []
    for pos, cue in enumerate(cues, 1):
        rows = grouped.get(pos, [])
        details = "".join(f'<li><b>{esc(x["label"])}</b> — {esc(x["detail"])}</li>' for x in rows)
        cards.append(f'<article class="{"flagged" if rows else "clean"}"><header>位置 {pos} · 元番号 {cue.number}'
                     f'<span>{esc(stamp(cue.start))} → {esc(stamp(cue.end))}</span></header>'
                     f'<pre>{esc(cue.text)}</pre><ul>{details or "<li>この設定での指摘なし</li>"}</ul></article>')
    counts = Counter(x["code"] for x in issues)
    badges = "".join(f'<span class="badge">{esc(LABELS[k])} {v}</span>' for k, v in counts.items())
    return '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>字幕点検室 — 点検レポート</title><style>
body{background:#f3f5f7;color:#182c3c;font:16px/1.65 system-ui,sans-serif;margin:0;padding:30px}
main{max-width:1000px;margin:auto}h1{font-size:34px;margin:6px 0}.eyebrow{color:#187b72;font-weight:700}
.intro,.summary,article{background:white;border-radius:14px;padding:22px;margin:18px 0}
.summary{background:#143b42;color:white}.stats{display:flex;gap:34px;flex-wrap:wrap}.stat b{font-size:32px;display:block}
.badge{display:inline-block;background:#285059;padding:4px 11px;border-radius:6px;margin:5px 5px 0 0;font-size:13px}
article{border-left:5px solid #e6a33b}article.clean{border-left-color:#28a48c}header{font-weight:700}
header span{display:block;color:#5a6c79;font-weight:400;font-size:14px}pre{font:18px/1.6 system-ui,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f9;padding:14px;border-radius:8px}
li{margin:6px 0}small{color:#596976;overflow-wrap:anywhere}ul{padding-left:22px}.note{color:#566775}
@media(max-width:600px){body{padding:14px}h1{font-size:27px}.intro,.summary,article{padding:16px}}
</style><main><div class="eyebrow">PROJECT ¥10,000 / LOCAL TOOL 0.1.0</div>
<h1>字幕点検室</h1><p class="note">自動字幕を、公開する前に見直すための点検レポート。</p>''' + f'''
<section class="summary"><div class="stats"><div class="stat"><b>{len(cues)}</b>字幕</div>
<div class="stat"><b>{len(grouped)}</b>要確認の字幕</div><div class="stat"><b>{len(issues)}</b>指摘</div></div>{badges}</section>
<section class="intro"><b>入力：{esc(metadata['input_name'])}</b><p>元ファイルは変更しません。指摘は確認候補です。意図的な重複や改行もあり、指摘ゼロでも内容・音声との一致を保証しません。</p>
<p>設定：行幅 {metadata['settings']['width']}（全角約{metadata['settings']['width']/2:g}文字）、速度 {metadata['settings']['cps']}文字/秒、表示 {metadata['settings']['min_seconds']}〜{metadata['settings']['max_seconds']}秒、最大 {metadata['settings']['max_lines']}行。すべて初版の仮設定で、規格ではありません。</p>
<p>番号・行前後の空白だけを整える候補は、指定した場合のみ別ファイルに保存します。時間・文言・字幕順は変更しません。</p>
<small>入力SHA-256：{esc(metadata['sha256'])} · 文字コード：{esc(metadata['encoding'])}</small></section>
''' + "".join(cards) + '<p class="note">この報告はローカル処理で作成しました。外部通信・解析用送信・追跡コードは含みません。制作：Project ¥10,000 運営AI。</p></main></html>'


def positive_float(value):
    value = float(value)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("有限の正の数を指定してください")
    return value


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("正の整数を指定してください")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="字幕点検室: SRTをローカル点検し、元ファイルを変えず日本語レポートを保存")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path, help="まだ存在しない出力フォルダー。省略時は入力名_review")
    parser.add_argument("--encoding", default="utf-8-sig", help="既定:utf-8-sig。必要に応じcp932/utf-16を明示")
    parser.add_argument("--normalize", action="store_true", help="連番・行前後空白の整形候補と差分も出力")
    parser.add_argument("--width", type=positive_int, default=40)
    parser.add_argument("--cps", type=positive_float, default=12.0)
    parser.add_argument("--min-seconds", type=positive_float, default=0.7)
    parser.add_argument("--max-seconds", type=positive_float, default=7.0)
    parser.add_argument("--max-lines", type=positive_int, default=2)
    args = parser.parse_args(argv)
    if args.min_seconds > args.max_seconds:
        parser.error("min-secondsはmax-seconds以下にしてください")
    out = args.out if args.out is not None else args.input.with_name(args.input.stem + "_review")
    temp = None
    try:
        if out.exists() or out.is_symlink():
            raise ValueError("出力先が既に存在します。別の--outを指定してください。上書きしません。")
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("初版の入力上限は5 MiBです。")
        original = raw.decode(args.encoding)
        cues = parse_srt(original)
        settings = Settings(args.width, args.cps, args.min_seconds, args.max_seconds, args.max_lines)
        issues = analyze(cues, settings)
        metadata = {"version": VERSION, "input_name": args.input.name,
                    "sha256": hashlib.sha256(raw).hexdigest(), "encoding": args.encoding,
                    "settings": asdict(settings), "cue_count": len(cues), "issue_count": len(issues),
                    "issues": issues, "normalization_requested": args.normalize,
                    "normalization_changes_timing": False}
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".shimaku-", dir=out.parent))
        (temp / "report.html").write_text(make_html(cues, issues, metadata), encoding="utf-8")
        (temp / "report.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.normalize:
            normalized = normalize(cues)
            (temp / "format_candidate.srt").write_text(normalized, encoding="utf-8")
            diff = "".join(difflib.unified_diff(original.replace("\r\n", "\n").replace("\r", "\n").splitlines(True),
                                               normalized.splitlines(True), fromfile="original.srt", tofile="format_candidate.srt"))
            (temp / "changes.diff").write_text(diff, encoding="utf-8")
        # Exclusive directory creation protects existing destinations, including concurrent runs.
        out.mkdir()
        for file in temp.iterdir():
            shutil.move(str(file), str(out / file.name))
        temp.rmdir()
        temp = None
        print(f"字幕{len(cues)}件 / 指摘{len(issues)}件 / 要確認{len({x['position'] for x in issues})}件")
        print(f"レポート: {out / 'report.html'}")
        if args.normalize:
            print("format_candidate.srt は書式整形候補です。時刻や読みやすさの問題は自動修正していません。")
        return 0
    except (OSError, ValueError, LookupError) as exc:
        print(f"点検できません: {exc}", file=sys.stderr)
        return 2
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
