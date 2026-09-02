"""공통 설정: ffmpeg/ffprobe 경로 탐색, CapCut 드래프트 폴더 위치."""

import os
import shutil
from pathlib import Path

# CapCut이 실제로 쓰고 있는 드래프트 폴더 (전역설정 > 草稿位置 에서 확인한 경로).
# 사용자가 폴더를 옮긴 경우 CAPCUT_DRAFTS_ROOT 환경변수로 덮어쓸 수 있다.
DEFAULT_CAPCUT_DRAFTS_ROOT = (
    Path.home() / "AppData" / "Local" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
)

# winget install ffmpeg 직후에는 이번 세션 PATH에 아직 반영이 안 될 수 있어서
# (레지스트리에는 기록되지만 이미 떠 있는 프로세스는 PATH를 다시 읽지 않음),
# PATH에 없으면 winget이 실제로 설치한 위치를 직접 뒤져서 찾는다.
_WINGET_FFMPEG_GLOB = (
    Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
)


def _find_winget_binary(name: str) -> str:
    if not _WINGET_FFMPEG_GLOB.exists():
        return name
    matches = list(_WINGET_FFMPEG_GLOB.glob(f"Gyan.FFmpeg_*/**/bin/{name}.exe"))
    if matches:
        return str(matches[0])
    return name


def resolve_binary(name: str) -> str:
    """PATH에서 먼저 찾고, 없으면 winget 설치 경로를 직접 탐색한다."""
    on_path = shutil.which(name)
    if on_path:
        return on_path
    found = _find_winget_binary(name)
    if found == name:
        raise FileNotFoundError(
            f"'{name}'을(를) PATH에서도, winget 설치 경로에서도 찾지 못했습니다. "
            f"`winget install ffmpeg` 실행 후 터미널을 재시작했는지 확인하세요."
        )
    return found


FFMPEG = resolve_binary("ffmpeg")
FFPROBE = resolve_binary("ffprobe")

CAPCUT_DRAFTS_ROOT = Path(os.environ.get("CAPCUT_DRAFTS_ROOT", str(DEFAULT_CAPCUT_DRAFTS_ROOT)))
