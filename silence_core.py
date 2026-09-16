"""무음 구간 감지 공용 로직 (silence_jumpcut.py, link_to_capcut.py, run_pipeline.py 공용)"""

import json
import re
import shutil
import subprocess
from pathlib import Path


class PipelineError(Exception):
    """파이프라인 전 단계에서 쓰는 공용 예외. run_pipeline.py가 그대로 result JSON으로 변환한다."""

    def __init__(self, code, message, retryable=False, suggested_action=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.suggested_action = suggested_action


# winget 설치 직후 PATH가 이번 세션에 반영 안 됐을 때를 대비해 설치 폴더를 직접 뒤진다.
# 사용자명/버전에 의존하지 않도록 Path.home() + glob 사용.
_WINGET_PACKAGES = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
_WINGET_GLOBS = {
    "ffmpeg": ["Gyan.FFmpeg_*/**/bin/ffmpeg.exe", "yt-dlp.yt-dlp_*/**/bin/ffmpeg.exe"],
    "ffprobe": ["Gyan.FFmpeg_*/**/bin/ffprobe.exe", "yt-dlp.yt-dlp_*/**/bin/ffprobe.exe"],
    "yt-dlp": ["yt-dlp.yt-dlp_*/yt-dlp.exe", "yt-dlp.yt-dlp_*/**/yt-dlp.exe"],
}


def find_binary(name, extra_paths=None):
    found = shutil.which(name)
    if found:
        return found
    if _WINGET_PACKAGES.exists():
        for pattern in _WINGET_GLOBS.get(name, []):
            for candidate in sorted(_WINGET_PACKAGES.glob(pattern), reverse=True):
                if candidate.exists():
                    return str(candidate)
    for p in extra_paths or []:
        if Path(p).exists():
            return str(p)
    raise PipelineError(
        "BINARY_NOT_FOUND",
        f"{name} 실행파일을 PATH에서도 winget 설치 경로에서도 찾을 수 없습니다.",
        retryable=False,
        suggested_action=f"winget install 로 {name} 설치 후 터미널을 재시작하세요.",
    )


def find_ytdlp():
    return find_binary("yt-dlp")


def probe(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,width,height,avg_frame_rate",
         str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    duration = float(data["format"]["duration"])
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    fps_str = vstream["avg_frame_rate"]
    num, den = fps_str.split("/")
    fps = float(num) / float(den) if float(den) != 0 else float(num)
    width, height = vstream["width"], vstream["height"]
    return duration, fps, width, height


def _range_args(start, duration):
    """start/duration이 주어지면 -i 앞에 넣을 -ss/-t 인자를 만든다.
    입력측 시크(-ss가 -i보다 앞)라 그 구간만 디코딩해서 훨씬 빠르고,
    결과 타임스탬프는 자동으로 그 구간 기준 0초부터 시작한다."""
    if start is None or duration is None:
        return []
    return ["-ss", str(start), "-t", str(duration)]


def detect_silences(ffmpeg, path, noise_db, min_silence, start=None, duration=None):
    result = subprocess.run(
        [ffmpeg, *_range_args(start, duration), "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = result.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", log)]
    return list(zip(starts, ends))


def detect_scene_changes(ffmpeg, path, threshold=10.0, start=None, duration=None):
    """장면전환 지점(초, 분석 구간 기준 0초부터) 리스트를 반환한다."""
    result = subprocess.run(
        [ffmpeg, *_range_args(start, duration), "-i", str(path), "-vf",
         f"scdet=threshold={threshold},metadata=print:key=lavfi.scd.time",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    times = [float(m) for m in re.findall(r"lavfi\.scd\.time=([\d.]+)", result.stderr)]
    return sorted(set(times))


def split_at_scene_changes(keep_segments, scene_times, min_piece=0.3):
    """무음컷으로 남은 구간들을 장면전환 지점 기준으로 더 잘게 쪼갠다."""
    result = []
    for a, b in keep_segments:
        points = [a] + [t for t in scene_times if a + min_piece < t < b - min_piece] + [b]
        for i in range(len(points) - 1):
            result.append((points[i], points[i + 1]))
    return result


def build_keep_segments(duration, silences, pad, min_gap=0.05):
    cuts = []
    for s, e in silences:
        remove_start = s + pad
        remove_end = e - pad
        if remove_end - remove_start >= min_gap:
            cuts.append((remove_start, remove_end))

    keep = []
    cursor = 0.0
    for rs, re_ in cuts:
        if rs > cursor + min_gap:
            keep.append((cursor, rs))
        cursor = max(cursor, re_)
    if duration - cursor >= min_gap:
        keep.append((cursor, duration))
    return keep


def download_youtube(url, out_template, ytdlp=None, log_stream=None):
    """yt-dlp로 다운로드. 진행 출력은 log_stream(기본 stderr)으로 보낸다."""
    ytdlp = ytdlp or find_ytdlp()
    result = subprocess.run(
        [ytdlp, "--no-playlist", "-f",
         "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
         "--merge-output-format", "mp4",
         "-o", out_template,
         "--retries", "10", "--fragment-retries", "10",
         url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if log_stream is not None:
        log_stream.write(result.stdout)
        log_stream.write(result.stderr)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        if "403" in result.stderr or "Forbidden" in result.stderr:
            raise PipelineError("YTDLP_FORBIDDEN", f"HTTP 403: yt-dlp 버전이 오래되어 발생했을 가능성이 높습니다.\n{tail}",
                                retryable=True, suggested_action="yt-dlp -U 실행 후 재시도")
        raise PipelineError("YTDLP_FAILED", f"yt-dlp 다운로드 실패 (exit {result.returncode}):\n{tail}",
                            retryable=True, suggested_action="링크 확인 또는 yt-dlp -U 후 재시도")


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")
