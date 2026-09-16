#!/usr/bin/env python
"""유튜브 auto-caption VTT를 읽기 좋은 타임스탬프+텍스트로 정리해서 출력.
사용법: python vtt_read.py 파일.vtt [시작초] [끝초]
"""
import re
import sys


def parse_vtt(path):
    text = open(path, encoding="utf-8").read()
    blocks = text.split("\n\n")
    entries = []
    for b in blocks:
        m = re.search(r"(\d\d:\d\d:\d\d\.\d+) --> (\d\d:\d\d:\d\d\.\d+)", b)
        if not m:
            continue
        start = to_sec(m.group(1))
        lines = b.split("\n")[1:]
        content_lines = [l for l in lines if "-->" not in l]
        line = " ".join(content_lines)
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            entries.append((start, line))
    return entries


def to_sec(ts):
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def dedupe(entries):
    """같은 줄이 롤링 자막으로 반복되는 걸 제거하고, 새로 등장하는 마지막 줄만 남긴다."""
    out = []
    last_line = None
    for start, line in entries:
        final_line = line.split("\n")[-1] if "\n" in line else line
        if final_line != last_line and final_line:
            out.append((start, final_line))
            last_line = final_line
    return out


if __name__ == "__main__":
    path = sys.argv[1]
    s = float(sys.argv[2]) if len(sys.argv) > 2 else None
    e = float(sys.argv[3]) if len(sys.argv) > 3 else None
    entries = dedupe(parse_vtt(path))
    for t, line in entries:
        if s is not None and (t < s or t > e):
            continue
        mm, ss = divmod(t, 60)
        print(f"[{int(mm):02d}:{ss:05.2f}] {line}")
