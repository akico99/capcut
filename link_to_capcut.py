#!/usr/bin/env python
"""
유튜브 링크(또는 로컬 파일) -> 다운로드 -> 무음구간 자동 점프컷 -> CapCut 프로젝트 생성

렌더링된 MP4가 아니라 CapCut에서 바로 열리는 "드래프트(프로젝트)"로 출력한다.
클립을 자르고 잇는 원본 화질 그대로 유지되며, CapCut에서 후속 작업(자막/타이틀 등)을
직접 진행할 수 있다.

사람용 CLI:
  python link_to_capcut.py "https://www.youtube.com/watch?v=XXXX"
  python link_to_capcut.py 로컬영상.mp4 --name "내프로젝트"
  python link_to_capcut.py URL --start 0:50 --end 2:04   (특정 구간만 먼저 자른 뒤 무음컷)
  python link_to_capcut.py URL --start 0:32 --end 0:41 --start 1:54 --end 2:17  (여러 구간을 순서대로 이어붙임)

에이전트용 진입점은 run_pipeline.py — 이 파일의 run()을 import해서 쓴다.
"""

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

from silence_core import (
    PipelineError, find_binary, probe, detect_silences,
    build_keep_segments, download_youtube, is_url,
    detect_scene_changes, split_at_scene_changes,
)

log = logging.getLogger("link_to_capcut")

DEFAULT_DRAFT_FOLDER = str(
    Path.home() / "AppData" / "Local" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
)
WORK_DIR = Path(__file__).resolve().parent
CACHE_INDEX = "download_cache.json"   # url -> 날짜명 파일 매핑 (WORK_DIR 안)

DEFAULT_OPTIONS = {
    "scene_split": False,
    "scene_threshold": 10.0,
    "noise_db": -23.0,
    "min_silence": 0.35,
    "pad": 0.12,
    "on_duplicate": "increment",   # "increment" | "overwrite"
    "silence_cut": True,           # False면 무음 감지 없이 segments만 그대로 클립으로
    "speed": 1.0,                  # 클립 재생 속도 (편집본 매칭 시 판별된 배속을 그대로 적용)
    "caption_placeholders": True,  # 클립마다 하단에 "n-k" 번호 자막 자리를 넣어둔다 (CapCut에서 텍스트만 교체)
    "caption_prefix": None,        # "n-k"의 n. None이면 project_name 끝 숫자(영상3 → 3), 없으면 1
    "caption_groups": None,        # 자막 자리를 컷 묶음 단위로: [[1,2],[3,4],[5]] (1-based 컷 번호). None이면 컷마다 1개
}
# 자막 자리 스타일 — "캡컷 작업시 주의사항.txt" 3번 기준 (흰색 / 크기 10 / 하단). 글꼴은 CapCut에서 코트라 볼드로 교체.
CAPTION_STYLE = {"size": 10.0, "color": (1.0, 1.0, 1.0), "bold": True, "transform_y": -0.8}

# 마지막 구간 끝을 이만큼 당긴다 (pycapcut 소재 길이가 ffprobe와 수십ms 다를 수 있음)
TAIL_TRIM_SEC = 0.05
# 장면전환 threshold가 낮아 과분할됐다고 경고할 기준 (분당 클립 수)
OVERSPLIT_CLIPS_PER_MIN = 40

_TS_RE = re.compile(r"^(\d+:)?(\d+:)?\d+(\.\d+)?$")


def parse_timestamp(ts):
    """'0:50' / '1:02:03' / '50' / '50.0' 만 허용. 그 외는 INVALID_REQUEST."""
    ts = str(ts).strip()
    if not _TS_RE.match(ts):
        raise PipelineError("INVALID_REQUEST", f"시간 형식을 해석할 수 없습니다: {ts!r} (예: 0:50, 50, 50.0)")
    seconds = 0.0
    for p in ts.split(":"):
        seconds = seconds * 60 + float(p)
    return seconds


def sanitize_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:60]


def next_dated_name(work_dir, prefix="영상"):
    """오늘 처음이면 '영상_20260902', 같은 날 여러 개면 '영상_20260902_1', '_2'..."""
    today = date.today().strftime("%Y%m%d")
    base = f"{prefix}_{today}"
    if not (work_dir / f"{base}.mp4").exists() and not list(work_dir.glob(f"{base}_*.mp4")):
        return base
    n = 1
    while (work_dir / f"{base}_{n}.mp4").exists():
        n += 1
    return f"{base}_{n}"


# ---------------------------------------------------------------------------
# 다운로드 캐시: 같은 URL을 다시 받지 않고 기존 날짜명 파일을 재사용
# ---------------------------------------------------------------------------
def _cache_key(url):
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def _load_cache(work_dir):
    p = work_dir / CACHE_INDEX
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("캐시 인덱스가 손상되어 새로 만듭니다: %s", p)
    return {}


def _save_cache(work_dir, cache):
    (work_dir / CACHE_INDEX).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_source(source, work_dir):
    """URL이면 (캐시 확인 후) 다운로드, 로컬이면 존재 확인. (local_path, default_name, cache_hit) 반환."""
    if not is_url(source):
        local_path = Path(source)
        if not local_path.is_absolute():
            local_path = work_dir / local_path
        if not local_path.exists():
            raise PipelineError("SOURCE_NOT_FOUND", f"파일을 찾을 수 없습니다: {local_path}")
        log.info("[1/4] 로컬 파일 사용: %s", local_path.name)
        return local_path, local_path.stem, False

    cache = _load_cache(work_dir)
    key = _cache_key(source)
    entry = cache.get(key)
    if entry and (work_dir / entry["file"]).exists():
        local_path = work_dir / entry["file"]
        log.info("[1/4] 캐시 재사용: %s (%s)", local_path.name, source)
        return local_path, local_path.stem, True

    dated_name = next_dated_name(work_dir)
    out_template = str(work_dir / f"{dated_name}.%(ext)s")
    local_path = work_dir / f"{dated_name}.mp4"
    log.info("[1/4] 다운로드 중: %s -> %s.mp4", source, dated_name)
    download_youtube(source, out_template, log_stream=sys.stderr)
    if not local_path.exists():
        raise PipelineError("YTDLP_FAILED", f"다운로드는 끝났지만 결과 파일이 없습니다: {local_path}", retryable=True)
    cache[key] = {"url": source, "file": local_path.name}
    _save_cache(work_dir, cache)
    return local_path, dated_name, False


# ---------------------------------------------------------------------------
# 컷 계산
# ---------------------------------------------------------------------------
def compute_cuts(ffmpeg, path, duration, ranges, opts):
    """ranges: [(start, end, first_reason)] (절대 초). 무음/장면전환 분석 -> [{'in','out','reason'}] 리스트."""
    cuts = []
    for s, e, first_reason in ranges:
        local_duration = e - s
        if opts["silence_cut"]:
            silences = detect_silences(ffmpeg, path, opts["noise_db"], opts["min_silence"],
                                       start=s, duration=local_duration)
            keep_local = build_keep_segments(local_duration, silences, opts["pad"])
            log.info("      구간 %.2f~%.2f: 무음 %d개, 유지 %d개", s, e, len(silences), len(keep_local))
        else:
            keep_local = [(0.0, local_duration)]
        range_keep = [(a + s, b + s) for a, b in keep_local]

        scene_times = set()
        if opts["scene_split"]:
            local_scene = detect_scene_changes(ffmpeg, path, opts["scene_threshold"],
                                               start=s, duration=local_duration)
            scene_times = {round(t + s, 3) for t in local_scene}
            range_keep = split_at_scene_changes(range_keep, sorted(scene_times))
            log.info("      장면전환 %d개 반영 -> 유지 %d개", len(scene_times), len(range_keep))

        for i, (a, b) in enumerate(range_keep):
            if i == 0:
                reason = first_reason
            elif round(a, 3) in scene_times:
                reason = "scene_split"
            else:
                reason = "silence_removed"
            cuts.append({"in": a, "out": b, "reason": reason})
    return cuts


def normalize_segments(segments):
    """(s, e) 튜플 또는 {'start','end','reason'?,'low_confidence'?} dict -> (s, e, reason) 튜플."""
    out = []
    for seg in segments or []:
        if isinstance(seg, dict):
            reason = seg.get("reason") or ("audio_match_low_confidence" if seg.get("low_confidence") else "manual_segment")
            out.append((float(seg["start"]), float(seg["end"]), reason))
        else:
            s, e = seg[0], seg[1]
            reason = seg[2] if len(seg) > 2 else "manual_segment"
            out.append((float(s), float(e), reason))
    return out


def _unique_project_name(draft_folder, name):
    """increment 정책: 이미 있으면 _v2, _v3 ... 붙인다."""
    root = Path(draft_folder)
    if not (root / name).exists():
        return name
    n = 2
    while (root / f"{name}_v{n}").exists():
        n += 1
    return f"{name}_v{n}"


def _stamp_draft_meta(draft_path, name, duration_us):
    """pycapcut은 템플릿을 복사하므로 모든 드래프트가 같은 draft_id/빈 이름을 갖는다.
    CapCut 인덱스(root_meta_info.json)가 꼬이지 않도록 고유 id·이름·경로·시간을 채운다."""
    import time
    import uuid
    draft_path = Path(draft_path)
    meta = draft_path / "draft_meta_info.json"
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    now_us = int(time.time() * 1_000_000)
    d["draft_id"] = str(uuid.uuid4()).upper()
    d["draft_name"] = name
    d["draft_fold_path"] = draft_path.as_posix()
    d["draft_root_path"] = draft_path.parent.as_posix()
    d["tm_draft_create"] = d["tm_draft_modified"] = now_us
    d["tm_duration"] = duration_us
    meta.write_text(json.dumps(d, ensure_ascii=False, indent=4), encoding="utf-8")
    return d["draft_id"]


# ---------------------------------------------------------------------------
# 메인 로직 (사람용 CLI와 run_pipeline.py가 공유)
# ---------------------------------------------------------------------------
def run(source, segments=None, options=None, project_name=None,
        draft_folder=DEFAULT_DRAFT_FOLDER, work_dir=WORK_DIR):
    """
    source:   유튜브 URL 또는 로컬 파일 경로
    segments: [(start_sec, end_sec[, reason]), ...] 또는 dict 목록. 빈 리스트/None이면 전체 구간
    options:  DEFAULT_OPTIONS의 키 중 일부 (누락은 기본값)
    반환:     AGENT_INTERFACE.md 4-1 포맷의 dict (status 제외)
    """
    try:
        import pycapcut as cc
    except ModuleNotFoundError:
        raise PipelineError(
            "PYTHON_VERSION_MISMATCH",
            "pycapcut 모듈을 찾을 수 없습니다. 다른 Python 버전으로 실행됐을 가능성이 큽니다.",
            suggested_action="Python 3.10 실행파일 전체 경로로 다시 실행하세요.",
        )

    opts = {**DEFAULT_OPTIONS, **(options or {})}
    if opts["on_duplicate"] not in ("increment", "overwrite"):
        raise PipelineError("INVALID_REQUEST", f"on_duplicate 값이 잘못됨: {opts['on_duplicate']!r}")
    speed = float(opts["speed"])
    if not 0.1 <= speed <= 10:
        raise PipelineError("INVALID_REQUEST", f"speed 값이 범위를 벗어남: {speed}")
    work_dir = Path(work_dir)
    warnings = []

    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")

    # 1) 소스 확보
    local_path, default_name, cache_hit = resolve_source(source, work_dir)

    # 2) 분석 구간
    duration, fps, width, height = probe(ffprobe, local_path)
    segments = normalize_segments(segments)
    if segments:
        ranges = []
        for s, e, reason in segments:
            s2, e2 = max(0.0, s), min(duration, e)
            if e2 <= s2:
                raise PipelineError("INVALID_REQUEST", f"구간이 비어 있습니다: {s}~{e} (영상 길이 {duration:.1f}s)")
            if e > duration:
                warnings.append(f"구간 끝 {e:.1f}s가 영상 길이 {duration:.1f}s를 넘어 잘라냈습니다.")
            ranges.append((s2, e2, reason))
        log.info("[2/4] 지정 구간 %d개 사용: %s", len(ranges),
                 ", ".join(f"{a:.1f}~{b:.1f}" for a, b, _ in ranges))
    else:
        ranges = [(0.0, duration, "manual_segment")]
        log.info("[2/4] 전체 구간(%.1fs)을 분석 대상으로 사용", duration)

    # 3) 컷 계산
    log.info("[3/4] 무음 구간 감지 중...")
    cuts = compute_cuts(ffmpeg, local_path, duration, ranges, opts)
    if not cuts:
        raise PipelineError(
            "NO_SILENCE_DETECTED",
            f"남길 구간이 없습니다 (noise_db={opts['noise_db']}, min_silence={opts['min_silence']}).",
            suggested_action="noise_db를 더 낮추거나(예: -30) min_silence를 늘려서 재시도",
        )
    if opts["silence_cut"] and not opts["scene_split"] and all(c["reason"] != "silence_removed" for c in cuts):
        warnings.append(f"무음 구간이 하나도 감지되지 않았습니다 (noise_db={opts['noise_db']}). 값을 조정해 보세요.")

    # pycapcut이 재는 소재 길이는 ffprobe보다 수십~수백ms 짧을 수 있다.
    # pycapcut 기준 길이로 직접 클램프해야 "范围超出" 에러가 안 난다.
    abs_path = str(local_path.resolve())
    material = cc.VideoMaterial(abs_path)
    material_dur = material.duration / 1_000_000 - TAIL_TRIM_SEC
    clamped = [c for c in cuts if c["in"] < material_dur]
    if len(clamped) < len(cuts):
        warnings.append(f"소재 끝({material_dur:.2f}s)을 넘는 클립 {len(cuts) - len(clamped)}개를 버렸습니다.")
    cuts = clamped
    if not cuts:
        raise PipelineError("RANGE_EXCEEDED", f"모든 클립이 소재 길이({material_dur:.2f}s)를 벗어났습니다.")
    if cuts[-1]["out"] > material_dur:
        delta = cuts[-1]["out"] - material_dur
        cuts[-1]["out"] = material_dur
        warnings.append(f"마지막 클립 끝을 {delta:.3f}초 당겼습니다 (pycapcut 소재 길이 {material_dur + TAIL_TRIM_SEC:.3f}s 기준).")

    total_in = sum(b - a for a, b, _ in ranges)
    total_out = sum(c["out"] - c["in"] for c in cuts)
    if total_out > 0 and len(cuts) / (total_out / 60) > OVERSPLIT_CLIPS_PER_MIN:
        warnings.append(f"클립이 {len(cuts)}개로 많이 분할됐습니다. scene_threshold를 높이거나 min_silence를 늘려 보세요.")
    log.info("      유지 구간 %d개, 제거 약 %.1fs", len(cuts), total_in - total_out)

    # 4) CapCut 드래프트 생성
    base_name = sanitize_name(project_name or f"{default_name}_jumpcut")
    if opts["on_duplicate"] == "increment":
        final_name = _unique_project_name(draft_folder, base_name)
        if final_name != base_name:
            warnings.append(f"같은 이름의 프로젝트가 있어 '{final_name}'으로 생성했습니다.")
    else:
        final_name = base_name
    log.info("[4/4] CapCut 프로젝트 생성 중: %s", final_name)

    placeholders, clip_spans = [], []
    try:
        df = cc.DraftFolder(draft_folder)
        script = df.create_draft(final_name, width, height, round(fps), allow_replace=True)
        script.add_track(cc.TrackType.video, "video_1")
        if opts["caption_placeholders"]:
            script.add_track(cc.TrackType.text, "caption")
            m = re.search(r"(\d+)\s*$", project_name or "")
            cap_prefix = str(opts["caption_prefix"] or (m.group(1) if m else 1))

        # 마이크로초 정수로 계산해야 경계가 정확히 맞물린다 (아니면 SegmentOverlap)
        cursor_us = 0
        for c in cuts:
            dur_us = round((c["out"] - c["in"]) * 1_000_000)
            src_us = round(c["in"] * 1_000_000)
            if dur_us <= 0:
                continue
            # speed 지정 시 pycapcut이 target 길이를 round(src/speed)로 다시 계산하므로 같은 식으로 커서를 옮긴다
            target_us = round(dur_us / speed)
            seg = cc.VideoSegment(material, cc.trange(cursor_us, target_us),
                                  source_timerange=cc.trange(src_us, dur_us),
                                  speed=speed if speed != 1.0 else None)
            script.add_segment(seg, "video_1")
            clip_spans.append((cursor_us, target_us, c))
            cursor_us += target_us

        if opts["caption_placeholders"]:
            groups = opts["caption_groups"] or [[i] for i in range(1, len(clip_spans) + 1)]
            for gi, g in enumerate(groups, 1):
                idx = sorted(int(x) for x in g)
                if not idx or idx[0] < 1 or idx[-1] > len(clip_spans):
                    raise PipelineError("INVALID_REQUEST", f"caption_groups 컷 번호 범위 오류: {g} (컷 1~{len(clip_spans)})")
                start_us = clip_spans[idx[0] - 1][0]
                end_us = clip_spans[idx[-1] - 1][0] + clip_spans[idx[-1] - 1][1]
                label = f"{cap_prefix}-{gi}"
                txt = cc.TextSegment(
                    label, cc.trange(start_us, end_us - start_us),
                    style=cc.TextStyle(size=CAPTION_STYLE["size"], color=CAPTION_STYLE["color"],
                                       bold=CAPTION_STYLE["bold"], align=1),
                    clip_settings=cc.ClipSettings(transform_y=CAPTION_STYLE["transform_y"]),
                )
                script.add_segment(txt, "caption")
                placeholders.append({"label": label, "clips": idx,
                                     "in": round(clip_spans[idx[0] - 1][2]["in"], 3),
                                     "out": round(clip_spans[idx[-1] - 1][2]["out"], 3),
                                     "timeline_start": round(start_us / 1e6, 2),
                                     "timeline_end": round(end_us / 1e6, 2)})
        script.save()
    except PermissionError as e:
        raise PipelineError("DRAFT_LOCKED", f"드래프트 폴더에 쓸 수 없습니다 (CapCut이 열어둔 상태?): {e}",
                            retryable=True, suggested_action="CapCut 앱을 완전히 종료한 뒤 재시도")
    except Exception as e:  # pycapcut 예외 매핑
        name = type(e).__name__
        if name == "SegmentOverlap":
            raise PipelineError("SEGMENT_OVERLAP", f"클립 경계가 겹칩니다: {e}",
                                suggested_action="지정 구간이 서로 겹치지 않는지 확인")
        if "超出" in str(e) or "range" in str(e).lower() or "exceed" in str(e).lower():
            raise PipelineError("RANGE_EXCEEDED", f"클립이 소재 길이를 벗어났습니다 (내부 보정 실패, 버그 가능성): {e}")
        raise

    draft_path = str(Path(draft_folder) / final_name)
    draft_id = _stamp_draft_meta(draft_path, final_name, cursor_us)
    log.info("[완료] CapCut에서 '%s' 프로젝트를 열어 확인하세요. 총 길이 약 %.1fs", final_name, cursor_us / 1e6)

    return {
        "project_name": final_name,
        "draft_id": draft_id,
        "draft_path": draft_path,
        "source_resolved": str(local_path.resolve()),
        "cache_hit": cache_hit,
        "clip_count": len(cuts),
        "speed": speed,
        "total_duration_sec": round(cursor_us / 1e6, 2),
        "removed_sec": round(total_in - total_out, 2),
        "cuts": [{"in": round(c["in"], 3), "out": round(c["out"], 3), "reason": c["reason"]} for c in cuts],
        "caption_placeholders": placeholders,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 사람용 CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="링크/로컬파일 -> 무음컷 -> CapCut 프로젝트")
    ap.add_argument("source", help="유튜브 URL 또는 로컬 영상 파일 경로")
    ap.add_argument("--start", action="append", help="구간 시작 (예: 0:50). 여러 구간이면 --start/--end를 반복 사용. 생략 시 전체")
    ap.add_argument("--end", action="append", help="구간 끝 (예: 2:04)")
    ap.add_argument("--name", help="CapCut 프로젝트 이름 (기본: 자동 생성)")
    ap.add_argument("--noise", type=float, default=DEFAULT_OPTIONS["noise_db"], help="무음 판단 기준 dB (기본 -23)")
    ap.add_argument("--min-silence", type=float, default=DEFAULT_OPTIONS["min_silence"], help="무음 최소 길이 초 (기본 0.35)")
    ap.add_argument("--pad", type=float, default=DEFAULT_OPTIONS["pad"], help="컷 경계 여유 시간 초 (기본 0.12)")
    ap.add_argument("--draft-folder", default=DEFAULT_DRAFT_FOLDER, help="CapCut 드래프트 폴더 경로")
    ap.add_argument("--scene", action="store_true", help="장면전환 지점에서도 클립을 추가로 분할")
    ap.add_argument("--scene-threshold", type=float, default=DEFAULT_OPTIONS["scene_threshold"], help="장면전환 민감도 (기본 10)")
    ap.add_argument("--overwrite", action="store_true", help="같은 이름 프로젝트가 있으면 덮어쓰기 (기본: _v2 붙여 새로 생성)")
    ap.add_argument("--no-silence", action="store_true", help="무음 감지 생략 (지정 구간을 그대로 클립으로)")
    ap.add_argument("--speed", type=float, default=1.0, help="클립 재생 속도 (기본 1.0)")
    args = ap.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")

    try:
        if bool(args.start) != bool(args.end) or (args.start and len(args.start) != len(args.end)):
            raise PipelineError("INVALID_REQUEST", "--start와 --end 개수가 서로 다릅니다.")
        segments = [(parse_timestamp(s), parse_timestamp(e)) for s, e in zip(args.start or [], args.end or [])]
        result = run(
            args.source, segments,
            options={
                "scene_split": args.scene, "scene_threshold": args.scene_threshold,
                "noise_db": args.noise, "min_silence": args.min_silence, "pad": args.pad,
                "on_duplicate": "overwrite" if args.overwrite else "increment",
                "silence_cut": not args.no_silence, "speed": args.speed,
            },
            project_name=args.name, draft_folder=args.draft_folder,
        )
    except PipelineError as e:
        sys.exit(f"[error:{e.code}] {e.message}" + (f"\n -> {e.suggested_action}" if e.suggested_action else ""))

    for w in result["warnings"]:
        print(f"[주의] {w}", file=sys.stderr)
    print(f"{result['project_name']}: 클립 {result['clip_count']}개, 총 {result['total_duration_sec']}s "
          f"(무음 {result['removed_sec']}s 제거) -> {result['draft_path']}")


if __name__ == "__main__":
    main()
