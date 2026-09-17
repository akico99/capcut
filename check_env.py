#!/usr/bin/env python
"""
설치 현황 점검 — 새 PC에서 첫 작업 전에 실행

  python check_env.py          # 사람용 표
  python check_env.py --json   # 에이전트용 JSON 한 줄

항목: Python 버전 / ffmpeg / ffprobe / yt-dlp / pycapcut / numpy / faster-whisper / Whisper 모델 캐시 / CapCut 드래프트 폴더
"""

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path


def _ver(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        line = (out.stdout or out.stderr).strip().splitlines()[0]
        return line[:60]
    except Exception as e:
        return f"실행 실패: {e}"


def check():
    items = []

    py = sys.version_info
    items.append({"name": "Python 3.10", "ok": (py.major, py.minor) == (3, 10),
                  "detail": f"{py.major}.{py.minor}.{py.micro} ({sys.executable})",
                  "fix": "3.10 실행파일 전체 경로로 실행 (SETUP.md 2-1)"})

    try:
        from silence_core import find_binary, PipelineError
    except Exception as e:
        items.append({"name": "silence_core", "ok": False, "detail": str(e), "fix": "레포 폴더에서 실행"})
        return items

    for name, fix in (("ffmpeg", "winget install Gyan.FFmpeg"), ("ffprobe", "winget install Gyan.FFmpeg"),
                      ("yt-dlp", "winget install yt-dlp.yt-dlp")):
        try:
            p = find_binary(name)
            items.append({"name": name, "ok": True, "detail": _ver([p, "--version"]) + f"  @ {p}", "fix": fix})
        except PipelineError as e:
            items.append({"name": name, "ok": False, "detail": e.message, "fix": fix})

    for mod, pkg in (("pycapcut", "pycapcut"), ("numpy", "numpy"), ("faster_whisper", "faster-whisper")):
        try:
            m = importlib.import_module(mod)
            items.append({"name": pkg, "ok": True, "detail": getattr(m, "__version__", "설치됨"),
                          "fix": f"pip install {pkg}"})
        except ModuleNotFoundError:
            items.append({"name": pkg, "ok": False, "detail": "없음", "fix": f"pip install {pkg}"})

    # Whisper 모델 캐시 (없어도 첫 실행 때 자동 다운로드 — 경고만)
    hf = Path.home() / ".cache" / "huggingface" / "hub"
    cached = list(hf.glob("models--Systran--faster-whisper-medium*")) if hf.exists() else []
    items.append({"name": "Whisper medium 모델", "ok": bool(cached), "warn_only": True,
                  "detail": "캐시됨" if cached else "없음 (첫 caption_sheet 실행 때 약 1.5GB 자동 다운로드)",
                  "fix": "자동"})

    draft = Path.home() / "AppData" / "Local" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    items.append({"name": "CapCut 드래프트 폴더", "ok": draft.exists(), "detail": str(draft),
                  "fix": "CapCut 설치 후 1회 실행"})
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    items = check()
    required_ok = all(i["ok"] or i.get("warn_only") for i in items)
    if args.json:
        print(json.dumps({"ready": required_ok, "items": items}, ensure_ascii=False))
        sys.exit(0 if required_ok else 2)
    for i in items:
        mark = "OK " if i["ok"] else ("-- " if i.get("warn_only") else "XX ")
        print(f"[{mark}] {i['name']:<20} {i['detail']}")
        if not i["ok"]:
            print(f"        -> {i['fix']}")
    print("\n준비 완료" if required_ok else "\n미설치 항목이 있습니다. 위 -> 안내대로 설치 후 다시 실행하세요.")
    sys.exit(0 if required_ok else 2)


if __name__ == "__main__":
    main()
