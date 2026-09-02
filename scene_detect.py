"""ffmpeg scene detection filter로 화면이 급격히 바뀌는(카메라 전환 등) 지점을 찾고,
그 지점들을 기준으로 후보 구간(candidate segments)을 나눈다.

주의: threshold 하나로 모든 영상에 맞는 값을 자동으로 찾을 수 없다는 게 실측으로 확인됐다
(무음 감지의 dB 임계값과 같은 문제). 그래서 여기서는 오디오처럼 자동 보정을 시도하지 않고,
대신 각 구간의 대표 썸네일을 뽑아서 사람이 웹 UI에서 직접 보고 고르게 한다 - '어느 게 좋은
컷인지'는 샷 타입만으로 판단할 수 없는 문제라(맥락이 중요), 자동 분류보다 이 방식이 낫다.
"""

import re
import subprocess
from pathlib import Path
from typing import List, Tuple

from config import FFMPEG
from silence_detect import get_duration

_PTS_TIME_RE = re.compile(r"pts_time:([\d.]+)")

# 손떨림/빠른 움직임만으로도 0.2~0.4 근처에서 오탐지가 쏟아지는 걸 실측으로 확인했다.
# 0.45는 그보다 확실히 큰 차이(실제 컷/전환)만 잡으려는 보수적인 기본값.
DEFAULT_THRESHOLD = 0.45


def detect_scene_changes(video_path: str, threshold: float = DEFAULT_THRESHOLD) -> List[float]:
    """화면이 threshold(0~1) 이상 급격히 바뀌는 지점들의 타임스탬프(초)를 반환한다.

    threshold가 낮을수록 민감하지만(손떨림 등도 잡음), 높을수록 확실한 전환만 잡는다.
    """
    cmd = [
        FFMPEG, "-i", video_path,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return [float(m.group(1)) for m in _PTS_TIME_RE.finditer(result.stderr)]


def get_candidate_segments(
    video_path: str, threshold: float = DEFAULT_THRESHOLD
) -> List[Tuple[float, float]]:
    """화면 전환 지점들을 기준으로 영상을 구간(segment)들로 나눈다.

    전환이 하나도 없으면 영상 전체가 구간 하나가 된다.
    """
    duration = get_duration(video_path)
    changes = detect_scene_changes(video_path, threshold)
    boundaries = [0.0] + sorted(t for t in changes if 0 < t < duration) + [duration]
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def split_at_scene_changes(
    ranges: List[Tuple[float, float]], changes: List[float]
) -> List[Tuple[float, float]]:
    """ranges 각각을 changes(화면 전환 타임스탬프)로 추가 분할한다.

    무음 제거로 이미 나뉜 keep_ranges라도, 무음 없이 장면만 바뀌는 지점(예: 컷 편집이
    붙어있는 구간)에서는 안 나뉘어 있을 수 있다. 그런 지점도 별도 클립으로 쪼개고 싶을 때 쓴다.
    """
    result: List[Tuple[float, float]] = []
    for start, end in ranges:
        cuts = sorted(t for t in changes if start < t < end)
        cursor = start
        for t in cuts:
            result.append((cursor, t))
            cursor = t
        result.append((cursor, end))
    return result


def extract_thumbnail(video_path: str, timestamp: float, out_path: str, width: int = 320) -> None:
    """주어진 시각의 프레임을 썸네일 jpg로 저장한다."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{timestamp}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={width}:-1",
        out_path,
    ]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python scene_detect.py <video_path>")
        sys.exit(1)

    path = sys.argv[1]
    segments = get_candidate_segments(path)
    print(f"화면 전환 기준 구간 {len(segments)}개:")
    for i, (s, e) in enumerate(segments):
        print(f"  [{i}] {s:.2f}s ~ {e:.2f}s ({e - s:.2f}s)")
