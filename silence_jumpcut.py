#!/usr/bin/env python
"""
무음 구간 자동 점프컷 도구 (CUSTOM CUT-EDIT MODE, 렌더링된 MP4로 출력)

사용법:
  python silence_jumpcut.py 입력.mp4
  python silence_jumpcut.py 입력.mp4 -o 출력.mp4 --noise -30 --min-silence 0.5 --pad 0.12
"""

import argparse
import sys
from pathlib import Path

from silence_core import find_binary, probe, detect_silences, build_keep_segments
import subprocess


def build_filter_complex(keep_segments, fade=0.015):
    v_labels, a_labels = [], []
    parts = []
    for i, (a, b) in enumerate(keep_segments):
        dur = b - a
        fo_start = max(dur - fade, 0)
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(
            f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={fo_start:.3f}:d={fade}[a{i}]"
        )
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")

    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    parts.append(f"{concat_inputs}concat=n={len(keep_segments)}:v=1:a=1[vout][aout]")
    return "; ".join(parts)


def main():
    ap = argparse.ArgumentParser(description="무음구간 자동 점프컷 (MP4 렌더링)")
    ap.add_argument("input", help="입력 영상 파일")
    ap.add_argument("-o", "--output", help="출력 파일 (기본: <입력명>_jumpcut.mp4)")
    ap.add_argument("--noise", type=float, default=-23, help="무음 판단 기준 dB (기본 -23, BGM 있는 영상 기준 실측 조정값)")
    ap.add_argument("--min-silence", type=float, default=0.35, help="무음 최소 길이 초 (기본 0.35)")
    ap.add_argument("--pad", type=float, default=0.12, help="컷 경계 여유 시간 초 (기본 0.12)")
    ap.add_argument("--crf", type=int, default=16, help="인코딩 품질 (기본 16)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"[error] 파일을 찾을 수 없습니다: {src}")

    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_jumpcut.mp4")

    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")

    print("[1/3] 원본 정보 확인 중...")
    duration, fps, width, height = probe(ffprobe, src)
    print(f"      길이 {duration:.1f}s, 해상도 {width}x{height}, fps {fps}")

    print("[2/3] 무음 구간 감지 중...")
    silences = detect_silences(ffmpeg, src, args.noise, args.min_silence)
    print(f"      무음 구간 {len(silences)}개 발견")

    keep_segments = build_keep_segments(duration, silences, args.pad)
    if not keep_segments:
        sys.exit("[error] 남길 구간이 없습니다. --noise/--min-silence 값을 조정하세요.")
    removed = duration - sum(b - a for a, b in keep_segments)
    print(f"      유지 구간 {len(keep_segments)}개, 제거되는 총 길이 약 {removed:.1f}s")

    print("[3/3] 점프컷 렌더링 중 (원본 해상도/비율 유지, 자막·크롭·TTS 없음)...")
    filter_complex = build_filter_complex(keep_segments)
    cmd = [
        ffmpeg, "-y", "-i", str(src),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    print(f"[완료] {out}")


if __name__ == "__main__":
    main()
