#!/usr/bin/env python
"""
컷편집 결과(result.json) → 자막 작성용 재료 시트 생성

  python caption_sheet.py --result result.json --out sheets/영상2

각 클립마다:
  - 프레임 3장(시작/중간/끝)을 가로로 붙인 이미지 (방송 자막·표정·상황 확인용)
  - Whisper(로컬, 무료)로 받아쓴 대사
를 뽑아 sheet.md 한 장으로 정리한다. 자막 문구 자체는 만들지 않는다 — 그건 에이전트/사람이 시트를 보고 쓴다.
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from silence_core import PipelineError, find_binary

log = logging.getLogger("caption_sheet")

FRAME_H = 480          # 타일 한 장 높이
FRAMES_PER_CLIP = 3
WHISPER_MODEL = "medium"   # 한국어는 small 이하 정확도가 떨어짐


def extract_tile(ffmpeg, src, t_in, t_out, out_png, n=FRAMES_PER_CLIP):
    """클립 구간에서 n장을 균등 추출해 가로로 이어붙인 PNG 생성."""
    dur = t_out - t_in
    margin = min(0.25, dur / 4)
    times = [t_in + margin + (dur - 2 * margin) * i / max(1, n - 1) for i in range(n)]
    inputs = []
    for t in times:
        inputs += ["-ss", f"{t:.3f}", "-i", str(src)]
    filt = "".join(f"[{i}:v]scale=-2:{FRAME_H}[v{i}];" for i in range(n))
    filt += "".join(f"[v{i}]" for i in range(n)) + f"hstack=inputs={n}"
    subprocess.run([ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", filt,
                    "-frames:v", "1", str(out_png)], check=True)
    return times


def load_whisper():
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError:
        raise PipelineError("WHISPER_MISSING", "faster-whisper가 없습니다.",
                            suggested_action="pip install faster-whisper")
    log.info("Whisper 모델 로드 중 (%s, 첫 실행은 다운로드로 수 분)...", WHISPER_MODEL)
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribe_range(model, ffmpeg, src, t_in, t_out, tmp_wav):
    subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{t_in:.3f}", "-t", f"{t_out - t_in:.3f}",
                    "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(tmp_wav)], check=True)
    segments, _ = model.transcribe(str(tmp_wav), language="ko", vad_filter=True, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip()


def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:05.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True, help="run_pipeline.py --out-json 결과 파일")
    ap.add_argument("--out", required=True, help="시트 출력 폴더")
    ap.add_argument("--no-whisper", action="store_true", help="대사 받아쓰기 생략(프레임만)")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")

    r = json.loads(Path(args.result).read_text(encoding="utf-8"))
    if r.get("status") != "success":
        sys.exit("result.json이 성공 결과가 아닙니다.")
    src = Path(r["source_resolved"])
    cuts = r["cuts"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_binary("ffmpeg")
    model = None if args.no_whisper else load_whisper()

    lines = [f"# {r['project_name']} 자막 시트", "",
             f"- 원본: `{src.name}`  클립 {len(cuts)}개  총 {r['total_duration_sec']}s  배속 {r.get('speed', 1.0)}x",
             "- 각 행: 클립 번호 / 원본 시간 / 길이 / 대사(Whisper) / 프레임 이미지", ""]
    timeline = 0.0
    speed = float(r.get("speed", 1.0))
    labels = {}
    for ph in r.get("caption_placeholders", []):
        for ci in ph.get("clips", [len(labels) + 1]):
            labels[ci] = ph["label"]
    for i, c in enumerate(cuts, 1):
        t_in, t_out = c["in"], c["out"]
        dur = t_out - t_in
        png = out / f"clip{i:02d}.png"
        extract_tile(ffmpeg, src, t_in, t_out, png)
        text = ""
        if model:
            text = transcribe_range(model, ffmpeg, src, t_in, t_out, out / "_tmp.wav")
        log.info("clip %02d  %s~%s (%.1fs)  %s", i, fmt(t_in), fmt(t_out), dur, text[:40])
        lines += [f"## clip {i:02d}  자막자리 [{labels.get(i, '-')}]  원본 {fmt(t_in)}~{fmt(t_out)}  ({dur:.1f}s)  결과 타임라인 {fmt(timeline)}~  [{c['reason']}]",
                  f"- 대사: {text or '(없음)'}",
                  f"- 프레임: `{png.name}`", ""]
        timeline += dur / speed
    (out / "_tmp.wav").unlink(missing_ok=True)
    (out / "sheet.md").write_text("\n".join(lines), encoding="utf-8")
    log.info("[완료] %s", out / "sheet.md")


if __name__ == "__main__":
    main()
