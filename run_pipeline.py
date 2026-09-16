#!/usr/bin/env python
"""
에이전트용 단일 진입점 (AGENT_INTERFACE.md 구현)

  python run_pipeline.py --request request.json [--out-json result.json]
  cat request.json | python run_pipeline.py

request.json:
  {
    "source": "https://www.youtube.com/watch?v=XXXX"  또는 로컬 파일 경로,
    "segments": [ {"start": "0:50", "end": "2:04"}, ... ],   # [] = 전체
    "edited": "편집본.mp4 또는 링크",  # (선택) 있으면 오디오 매칭으로 segments를 자동 계산 (segments는 [] 또는 생략)
    "options": { "scene_split": true, "scene_threshold": 10, "noise_db": -23,
                 "min_silence": 0.35, "pad": 0.12, "on_duplicate": "increment" },
    "project_name": "영상1"
  }

stdout: 결과 JSON 한 줄만 (성공/실패 모두). stderr: 진행 로그.
exit code: 0 성공 / 1 재시도 가능 에러 / 2 재시도 불가 에러
"""

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from silence_core import PipelineError
import link_to_capcut as ltc

RESULT_MARKER = "RESULT_JSON:"


def _emit(result, out_json):
    line = json.dumps(result, ensure_ascii=False)
    if out_json:
        Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # stdout에는 마커 + JSON 한 줄만
    sys.stdout.write(RESULT_MARKER + line + "\n")
    sys.stdout.flush()


def _error(code, message, retryable=False, suggested_action=None):
    return {
        "status": "error",
        "error_code": code,
        "message": message,
        "retryable": retryable,
        "suggested_action": suggested_action,
    }


def parse_request(req):
    if not isinstance(req, dict):
        raise PipelineError("INVALID_REQUEST", "요청은 JSON 객체여야 합니다.")
    source = req.get("source")
    if not source or not isinstance(source, str):
        raise PipelineError("INVALID_REQUEST", "'source' 필드(유튜브 URL 또는 파일 경로)가 필요합니다.")

    edited = req.get("edited")
    if edited is not None and not isinstance(edited, str):
        raise PipelineError("INVALID_REQUEST", "'edited'는 파일 경로 문자열이어야 합니다.")

    raw_segments = req.get("segments")
    if raw_segments is None:
        if edited is None:
            raise PipelineError("INVALID_REQUEST", "'segments' 필드가 필요합니다 (전체 구간이면 빈 배열 []).")
        raw_segments = []
    if edited and raw_segments:
        raise PipelineError("INVALID_REQUEST", "'edited'와 'segments'를 동시에 줄 수 없습니다 (edited면 segments는 []).")
    if not isinstance(raw_segments, list):
        raise PipelineError("INVALID_REQUEST", "'segments'는 배열이어야 합니다.")
    segments = []
    for i, seg in enumerate(raw_segments):
        if not isinstance(seg, dict) or "start" not in seg or "end" not in seg:
            raise PipelineError("INVALID_REQUEST", f"segments[{i}]에 start/end가 필요합니다.")
        s, e = ltc.parse_timestamp(seg["start"]), ltc.parse_timestamp(seg["end"])
        if e <= s:
            raise PipelineError("INVALID_REQUEST", f"segments[{i}]: end({seg['end']})가 start({seg['start']})보다 커야 합니다.")
        segments.append((s, e))

    options = req.get("options") or {}
    if not isinstance(options, dict):
        raise PipelineError("INVALID_REQUEST", "'options'는 객체여야 합니다.")
    unknown = set(options) - set(ltc.DEFAULT_OPTIONS)
    if unknown:
        raise PipelineError("INVALID_REQUEST", f"알 수 없는 옵션: {sorted(unknown)}")

    project_name = req.get("project_name")
    if project_name is not None and not isinstance(project_name, str):
        raise PipelineError("INVALID_REQUEST", "'project_name'은 문자열이어야 합니다.")

    return source, segments, options, project_name, edited


def run_with_audio_match(source, edited, options, project_name, draft_folder, work_dir):
    """편집본 오디오로 원본 구간을 찾은 뒤 그 segments로 파이프라인 실행."""
    import audio_match
    # 편집본도 유튜브 링크(쇼츠 등)일 수 있으므로 원본과 같은 캐시/다운로드 경로를 탄다
    edited_path, _, _ = ltc.resolve_source(edited, Path(work_dir))
    local, _, cache_hit = ltc.resolve_source(source, Path(work_dir))
    m = audio_match.match(edited_path, local)
    segments = [{"start": s["start"], "end": s["end"],
                 "reason": "audio_match_low_confidence" if s["low_confidence"] else "audio_match"}
                for s in m["segments"]]
    opts = {"silence_cut": False, "speed": m["speed"], **options}
    result = ltc.run(str(local), segments, opts, project_name, draft_folder=draft_folder, work_dir=work_dir)
    result["cache_hit"] = cache_hit
    result["audio_match"] = {"speed": m["speed"], "coverage": m["coverage"], "unmatched": m["unmatched"],
                             "edit_duration": m["edit_duration"]}
    result["warnings"] = m["warnings"] + result["warnings"]
    return result


def main():
    ap = argparse.ArgumentParser(description="에이전트용 파이프라인 래퍼")
    ap.add_argument("--request", help="request.json 경로 (생략 시 stdin)")
    ap.add_argument("--out-json", help="결과 JSON을 이 파일에도 저장")
    ap.add_argument("--draft-folder", default=ltc.DEFAULT_DRAFT_FOLDER)
    ap.add_argument("--work-dir", default=str(ltc.WORK_DIR))
    ap.add_argument("-v", "--verbose", action="store_true", help="stderr 로그를 DEBUG 레벨로")
    args = ap.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    try:
        try:
            raw = Path(args.request).read_text(encoding="utf-8") if args.request else sys.stdin.read()
            req = json.loads(raw)
        except FileNotFoundError:
            raise PipelineError("INVALID_REQUEST", f"요청 파일이 없습니다: {args.request}")
        except json.JSONDecodeError as e:
            raise PipelineError("INVALID_REQUEST", f"요청 JSON 파싱 실패: {e}")

        source, segments, options, project_name, edited = parse_request(req)
        if edited:
            result = run_with_audio_match(source, edited, options, project_name,
                                          args.draft_folder, args.work_dir)
        else:
            result = ltc.run(source, segments, options, project_name,
                             draft_folder=args.draft_folder, work_dir=args.work_dir)
        _emit({"status": "success", **result}, args.out_json)
        sys.exit(0)

    except PipelineError as e:
        _emit(_error(e.code, e.message, e.retryable, e.suggested_action), args.out_json)
        sys.exit(1 if e.retryable else 2)

    except MemoryError:
        _emit(_error("LOW_MEMORY", "메모리 부족", retryable=True,
                     suggested_action="다른 프로그램(크롬 탭 등) 종료 후 재시도"), args.out_json)
        sys.exit(1)

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _emit(_error("UNEXPECTED", f"{type(e).__name__}: {e}", retryable=False,
                     suggested_action="stderr의 traceback을 확인"), args.out_json)
        sys.exit(2)


if __name__ == "__main__":
    main()
