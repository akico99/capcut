"""ffmpeg silencedetect로 무음 구간을 찾고, 그 여집합인 '남길 구간'(keep_ranges)을 계산한다.

전체 단위는 초(float). pycapcut의 마이크로초 변환은 build_draft.py에서만 다룬다.
"""

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from config import FFMPEG, FFPROBE


@dataclass
class SilenceRange:
    start: float
    end: float


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")


def get_duration(video_path: str) -> float:
    """ffprobe로 영상 총 길이(초)를 구한다."""
    result = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    )
    return float(result.stdout.strip())


def get_resolution(video_path: str) -> Tuple[int, int]:
    """ffprobe로 영상 해상도(width, height)를 구한다."""
    result = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video_path,
        ],
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    )
    width_str, height_str = result.stdout.strip().split("x")
    return int(width_str), int(height_str)


def get_mean_volume(video_path: str) -> float:
    """ffmpeg -af volumedetect로 영상 전체 평균 음량(dB)을 구한다."""
    cmd = [FFMPEG, "-i", video_path, "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    match = _MEAN_VOLUME_RE.search(result.stderr)
    if not match:
        raise RuntimeError(f"mean_volume을 찾지 못했습니다:\n{result.stderr[-1000:]}")
    return float(match.group(1))


def _detect_silence_at(
    video_path: str, noise_db: float, min_silence_duration: float
) -> List[SilenceRange]:
    """ffmpeg -af silencedetect를 실제로 한 번 돌려서 무음 구간을 파싱한다."""
    cmd = [
        FFMPEG, "-i", video_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_duration}",
        "-f", "null", "-",
    ]
    # ffmpeg는 silencedetect 결과를 stderr로 출력한다 (stdout이 아님).
    # text=True만 쓰면 한글 Windows 로케일(cp949)로 디코딩을 시도하다 깨지므로 encoding 명시.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(result.stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(result.stderr)]

    # 영상이 무음으로 끝나면 ffmpeg가 silence_end를 안 찍는 경우가 있어
    # start만 있고 end가 없는 마지막 구간은 영상 끝까지로 처리한다.
    if len(starts) > len(ends):
        duration = get_duration(video_path)
        ends.append(duration)

    if len(starts) != len(ends):
        raise RuntimeError(
            f"silence_start({len(starts)}개)와 silence_end({len(ends)}개) 개수가 안 맞습니다. "
            f"ffmpeg stderr를 확인하세요:\n{result.stderr[-2000:]}"
        )

    return [SilenceRange(s, e) for s, e in zip(starts, ends)]


def detect_silence(
    video_path: str,
    noise_db: Optional[float] = None,
    min_silence_duration: float = 0.35,
    target_removed_ratio: Tuple[float, float] = (0.05, 0.35),
    max_attempts: int = 3,
) -> List[SilenceRange]:
    """ffmpeg -af silencedetect로 무음 구간을 감지한다.

    noise_db: 이 dB 이하를 무음으로 판단. 명시하면 그 값으로 딱 한 번만 감지한다.
        None(기본값)이면 영상마다 자동으로 보정한다 - 녹음 환경(마이크 게인, 방음, 거리)에
        따라 "적당한" noise_db가 완전히 다르다는 게 실측으로 확인됐다: 어떤 영상은
        mean_volume -21.9dB에 noise_db -18dB가 맞았고, 다른 영상은 mean_volume -14.0dB라
        같은 -18dB를 쓰면 거의 아무것도 안 잘렸다(171초 영상에 4컷). 고정값 하나로는 안 되고
        영상별로 다시 잡아야 한다.

        자동 보정 방식: mean_volume + 4dB를 시작점으로 감지해보고, 제거되는 비율이
        target_removed_ratio 범위 밖이면(너무 안 잘렸거나 너무 많이 잘렸으면) noise_db를
        4dB씩 조정해가며 최대 max_attempts번 재시도한다. 그래도 범위 밖이면 마지막 시도
        결과를 그대로 쓴다 - 이건 추정치일 뿐이니 최종 판단은 항상 CapCut에서 직접
        재생해서 확인해야 한다.
    min_silence_duration: 이 길이(초) 이상 지속돼야 무음으로 인정 (기본 0.35s)
    target_removed_ratio: 자동 보정 시 목표로 하는 '제거 비율' 범위 (기본 5%~35%).
        너무 낮으면(<5%) 사실상 안 잘린 것과 다름없고, 너무 높으면(>35%) 말소리 자체를
        잘라먹고 있을 위험이 있다고 보고 반대 방향으로 조정한다.
    max_attempts: noise_db가 None일 때 자동 보정 재시도 최대 횟수
    """
    if noise_db is not None:
        return _detect_silence_at(video_path, noise_db, min_silence_duration)

    mean_volume = get_mean_volume(video_path)
    duration = get_duration(video_path)
    low, high = target_removed_ratio
    candidate = mean_volume + 4.0

    silences: List[SilenceRange] = []
    for attempt in range(max_attempts):
        silences = _detect_silence_at(video_path, candidate, min_silence_duration)
        removed = sum(s.end - s.start for s in silences)
        ratio = removed / duration if duration else 0.0

        if low <= ratio <= high or attempt == max_attempts - 1:
            break
        candidate += 4.0 if ratio < low else -4.0

    return silences


def get_keep_ranges(
    video_path: str,
    silences: List[SilenceRange],
    pad_seconds: float = 0.15,
    min_keep_duration: float = 0.05,
    bounds: Optional[Tuple[float, float]] = None,
) -> List[Tuple[float, float]]:
    """무음 구간의 여집합(=남길 구간)을 계산한다.

    pad_seconds: 각 무음 구간의 양 끝을 이만큼 줄여서(=자연스러운 여백을 남기고) 자른다.
                 컷이 너무 딱딱 끊기지 않도록 하는 용도. 줄인 뒤 길이가 min_keep_duration보다
                 짧아지면 그 무음은 아예 자르지 않는다(원래 있던 그대로 남긴다).
    min_keep_duration: 이보다 짧은 keep 구간은 버린다(잡음성 미세 구간 제거).
    bounds: (start, end)를 주면 전체 영상이 아니라 그 구간 안에서만 남길 구간을 계산한다.
        화면 전환으로 나눈 후보 구간 중 사용자가 고른 것만 처리할 때 쓴다 - silences는
        영상 전체에 대해 한 번만 감지해두고, 여기서 구간별로 잘라 쓰면 된다.
    """
    range_start, range_end = bounds if bounds is not None else (0.0, get_duration(video_path))

    effective_silences: List[Tuple[float, float]] = []
    for s in silences:
        eff_start = max(s.start + pad_seconds, range_start)
        eff_end = min(s.end - pad_seconds, range_end)
        if eff_end - eff_start < min_keep_duration:
            continue  # 패딩 적용하면 너무 짧아지는(또는 bounds 밖으로 밀려나는) 무음은 무시
        effective_silences.append((eff_start, eff_end))

    effective_silences.sort()

    keep_ranges: List[Tuple[float, float]] = []
    cursor = range_start
    for eff_start, eff_end in effective_silences:
        if eff_start > cursor:
            keep_ranges.append((cursor, eff_start))
        cursor = max(cursor, eff_end)
    if cursor < range_end:
        keep_ranges.append((cursor, range_end))

    return [(s, e) for s, e in keep_ranges if e - s >= min_keep_duration]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python silence_detect.py <video_path>")
        sys.exit(1)

    path = sys.argv[1]
    silences = detect_silence(path)
    print(f"무음 구간 {len(silences)}개 감지:")
    for s in silences:
        print(f"  {s.start:.2f}s ~ {s.end:.2f}s ({s.end - s.start:.2f}s)")

    keeps = get_keep_ranges(path, silences)
    total_keep = sum(e - s for s, e in keeps)
    total_dur = get_duration(path)
    print(f"\n남길 구간 {len(keeps)}개 (원본 {total_dur:.2f}s -> {total_keep:.2f}s, "
          f"{total_dur - total_keep:.2f}s 제거):")
    for s, e in keeps:
        print(f"  {s:.2f}s ~ {e:.2f}s ({e - s:.2f}s)")
