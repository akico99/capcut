"""keep_ranges(남길 구간, 초 단위)를 받아 CapCut 점프컷 드래프트를 만든다.

핵심 주의사항 (pycapcut-mac 스킬 참고):
- 최하단 비디오 트랙의 첫 세그먼트는 반드시 0초부터 시작해야 함. 그래서 target_timerange는
  원본 타임스탬프가 아니라 '누적된 출력 타임라인 길이'로 계산한다.
- source_timerange는 원본 파일에서 잘라올 위치, target_timerange는 결과 타임라인 위치.
  이 둘을 섞으면 안 됨.
"""

from typing import List, Optional, Tuple

import pycapcut as cc

from config import CAPCUT_DRAFTS_ROOT
from silence_detect import get_resolution


def build_jumpcut_draft(
    draft_name: str,
    video_path: str,
    keep_ranges: List[Tuple[float, float]],
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: int = 30,
) -> str:
    """점프컷 CapCut 드래프트를 생성하고 저장한다. 저장된 드래프트 폴더 경로를 반환.

    width/height를 지정하지 않으면 원본 영상 해상도를 그대로 사용한다.
    """
    if not keep_ranges:
        raise ValueError("keep_ranges가 비어 있습니다 - 남길 구간이 없습니다.")

    if width is None or height is None:
        width, height = get_resolution(video_path)

    draft_folder = cc.DraftFolder(str(CAPCUT_DRAFTS_ROOT))
    script = draft_folder.create_draft(draft_name, width, height, fps=fps, allow_replace=True)

    script.add_track(cc.TrackType.video)

    # VideoMaterial을 한 번만 만들어 재사용한다. 세그먼트마다 경로(str)를 넘기면 pycapcut이
    # 매번 새로 프로브하는데, 그러면 (a) 134개 세그먼트에 134번 프로브하는 비효율이 생기고
    # (b) ffprobe의 컨테이너 duration(format=duration)과 pycapcut이 실제로 쓰는
    # 비디오 트랙 duration이 수십ms 단위로 어긋날 수 있어, 마지막 keep_range의 끝이
    # material.duration을 살짝 넘어서는 ValueError가 날 수 있다. 여기서 material을 한 번
    # 만들어서 그 실제 duration에 맞춰 클램프하면 두 문제가 한꺼번에 해결된다.
    material = cc.VideoMaterial(video_path)

    timeline_cursor = 0  # 마이크로초, 출력 타임라인 상의 누적 위치 (0부터 시작해야 함)
    for src_start, src_end in keep_ranges:
        src_start_us = min(round(src_start * 1_000_000), material.duration)
        src_end_us = min(round(src_end * 1_000_000), material.duration)
        src_duration_us = src_end_us - src_start_us
        if src_duration_us <= 0:
            continue  # 소재 길이 클램프로 인해 완전히 사라진 구간은 건너뜀

        seg = cc.VideoSegment(
            material,
            target_timerange=cc.Timerange(timeline_cursor, src_duration_us),
            source_timerange=cc.Timerange(src_start_us, src_duration_us),
        )
        script.add_segment(seg)
        timeline_cursor += src_duration_us

    script.save()

    draft_path = str(CAPCUT_DRAFTS_ROOT / draft_name)
    return draft_path


if __name__ == "__main__":
    import sys
    import time

    from scene_detect import detect_scene_changes, split_at_scene_changes
    from silence_detect import detect_silence, get_keep_ranges

    if len(sys.argv) < 2:
        print("Usage: python build_draft.py <video_path> [draft_name]")
        sys.exit(1)

    video_path = sys.argv[1]
    draft_name = sys.argv[2] if len(sys.argv) > 2 else f"jumpcut_{int(time.time())}"

    print(f"[1/3] 무음 감지 중: {video_path}")
    silences = detect_silence(video_path)
    keep_ranges = get_keep_ranges(video_path, silences)
    print(f"  -> {len(silences)}개 무음 구간 감지, {len(keep_ranges)}개 구간 남김")

    print("[2/3] 장면 전환 감지 중")
    scene_changes = detect_scene_changes(video_path)
    keep_ranges = split_at_scene_changes(keep_ranges, scene_changes)
    print(f"  -> 장면 전환 {len(scene_changes)}개 감지, 분할 후 {len(keep_ranges)}개 구간")

    print(f"[3/3] CapCut 드래프트 생성 중: {draft_name}")
    draft_path = build_jumpcut_draft(draft_name, video_path, keep_ranges)
    print(f"완료: {draft_path}")
    print("CapCut을 열어서 이 드래프트를 재생해보고 컷이 자연스러운지 확인해주세요.")
