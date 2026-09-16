# 유튜브 링크 → CapCut 프로젝트 자동 컷편집 — 설치/사용 가이드

유튜브 링크 + 타임라인 구간만 주면: 다운로드 → 무음구간 자동 점프컷 → 장면전환 지점 분할 →
CapCut에서 바로 열리는 프로젝트(드래프트)로 생성해주는 로컬 워크플로우.

**원칙**: 유료 API 일체 사용 안 함(자막/TTS/AI 영상생성 없음). ffmpeg, yt-dlp, pycapcut(무료 PyPI
패키지)만으로 동작. 렌더링된 mp4가 아니라 CapCut 프로젝트(각 컷이 개별 수정 가능한 타임라인 클립)로
출력하는 게 핵심.

---

## 1. 요구 사항

- Windows 10/11
- CapCut 데스크톱 앱 설치 및 최소 1회 실행 (드래프트 폴더가 생성되어 있어야 함)
- Python **3.10** (버전 고정 권장 — 아래 "주의사항" 참고)
- ffmpeg / ffprobe
- yt-dlp

## 2. 설치

### 2-1. Python 3.10
[python.org](https://www.python.org/downloads/) 에서 3.10.x 설치.

> ⚠️ **중요**: 이 PC에 Python 3.12 등 다른 버전이 나중에 추가로 설치되면 `python` 명령이 그쪽으로
> 바뀌면서 아래에서 설치한 패키지(pycapcut 등)를 못 찾는 문제가 실제로 발생했다. 스크립트를 돌릴 때는
> `python` 대신 **3.10 실행파일 전체 경로를 직접 지정**하는 걸 권장한다:
> `C:\Users\<user>\AppData\Local\Programs\Python\Python310\python.exe`

### 2-2. ffmpeg + yt-dlp
PowerShell(관리자 불필요)에서 winget으로 설치:
```powershell
winget install --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements -e
winget install --id yt-dlp.yt-dlp --accept-source-agreements --accept-package-agreements -e
```
설치 직후엔 새로 연 터미널에서 `ffmpeg -v`, `yt-dlp --version`이 바로 인식 안 될 수 있음(PATH 갱신
지연) — 터미널을 완전히 새로 열거나, 아래처럼 winget 설치 경로를 직접 참조해도 된다:
```
%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe
%LOCALAPPDATA%\Microsoft\WinGet\Packages\yt-dlp.yt-dlp_Microsoft.Winget.Source_8wekyb3d8bbwe\yt-dlp.exe
```
(패키지 버전에 따라 폴더명 끝자리 숫자는 다를 수 있음 — `Get-ChildItem`으로 실제 경로 확인)

이 문서 작성 시점 기준 버전: yt-dlp `2026.08.19`, ffmpeg `9.0-full_build`.

### 2-3. pycapcut (CapCut 프로젝트 생성용 파이썬 패키지)
```powershell
C:\Users\<user>\AppData\Local\Programs\Python\Python310\python.exe -m pip install pycapcut
```
설치되는 패키지: `pycapcut==0.0.3`, `imageio`, `pymediainfo`, `comtypes`, `uiautomation`.
(GitHub: https://github.com/GuanYixuan/pyCapCut — 활발히 관리되는 PyPI 정식 배포 패키지)

### 2-4. 파이프라인 스크립트
아래 3개 파일을 작업 폴더(예: `Desktop\편집`)에 그대로 복사:

- `silence_core.py` — 공용 로직 (ffprobe/ffmpeg 바이너리 탐색, 무음감지, 장면전환감지, yt-dlp 다운로드)
- `silence_jumpcut.py` — 무음컷 결과를 **mp4로 렌더링**하는 버전 (CapCut 없이 결과물만 필요할 때)
- `link_to_capcut.py` — 무음컷+장면전환 결과를 **CapCut 프로젝트로 생성**하는 메인 스크립트

스크립트 상단의 `KNOWN_FFMPEG_DIRS`, `KNOWN_YTDLP_PATHS`, `DEFAULT_DRAFT_FOLDER` 상수를 새 PC의
실제 경로로 맞춰줘야 한다(사용자명이 다르면 `C:\Users\<user>\...` 부분이 달라짐).

## 3. CapCut 드래프트 폴더 찾기

Windows 기본 경로:
```
%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft
```
CapCut 앱을 최소 1번 실행해서 빈 프로젝트라도 만들어봐야 이 폴더가 생성된다. `link_to_capcut.py`의
`DEFAULT_DRAFT_FOLDER` 상수를 이 경로로 맞추면 됨(대부분 그대로 사용 가능하나 사용자명은 확인).

## 4. 사용법

```powershell
$PY = "C:\Users\<user>\AppData\Local\Programs\Python\Python310\python.exe"

# 기본: 링크 + 구간 1개 → CapCut 프로젝트
& $PY link_to_capcut.py "https://www.youtube.com/watch?v=XXXX" --start 0:50 --end 2:04 --scene --name "영상1"

# 여러 구간을 한 프로젝트에 순서대로 이어붙이기
& $PY link_to_capcut.py "https://www.youtube.com/watch?v=XXXX" --start 0:32 --end 0:41 --start 1:54 --end 2:17 --scene --name "영상2"

# 로컬 파일도 동일하게 가능
& $PY link_to_capcut.py "내영상.mp4" --scene --name "영상3"

# mp4로 바로 렌더링하고 싶을 때 (CapCut 없이)
& $PY silence_jumpcut.py "내영상.mp4"
```

**주요 옵션**
| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--start` / `--end` | (전체) | 구간 지정, 반복 사용 시 여러 구간을 순서대로 이어붙임 |
| `--scene` | 꺼짐 | 장면전환 지점에서 클립 추가 분할 |
| `--scene-threshold` | 10 | 장면전환 민감도 (낮을수록 민감) |
| `--noise` | -23 (dB) | 무음 판단 기준 — BGM 있는 예능 콘텐츠 기준 실측 조정값 |
| `--min-silence` | 0.35 (초) | 무음으로 판단할 최소 길이 |
| `--pad` | 0.12 (초) | 컷 경계에 남길 여유 시간 (너무 뚝뚝 끊기지 않게) |
| `--name` | 자동생성 | CapCut 프로젝트 이름 |

다운로드된 원본 파일명은 `영상_YYYYMMDD.mp4`(같은 날 여러 개면 `_1`, `_2`...) 형식으로 자동 저장됨.

## 5. 동작 원리 (요약)

1. `yt-dlp`로 지정 구간 근처만 필요한 만큼 다운로드
2. `ffmpeg -ss/-t`로 **지정 구간만** 디코딩해서 `silencedetect`, `scdet` 필터로 무음구간/장면전환
   지점을 빠르게 탐지 (전체 파일을 안 훑어서 긴 원본도 몇 초면 분석 끝남)
3. 남길 구간(무음 제외 + 장면전환 경계)을 계산
4. `pycapcut`으로 CapCut 드래프트 JSON을 직접 생성 — 각 구간이 타임라인 위 개별 클립(원본 화질
   그대로, 컷마다 CapCut에서 자유롭게 재수정 가능)으로 배치됨

## 6. 트러블슈팅 (실제로 겪은 문제들)

- **`ModuleNotFoundError: No module named 'pycapcut'`**
  다른 Python 버전이 PATH 우선순위를 가져간 경우. `python` 대신 3.10 전체 경로로 직접 실행.

- **한글이 `??`나 깨진 문자로 출력됨**
  실행 전에 환경변수 설정: `$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"`

- **`pycapcut.exceptions.SegmentOverlap`**
  클립 경계를 초 단위 문자열(`"12.34s"`)로 넘기면 반올림 오차로 겹침 발생 가능. **마이크로초 정수**로
  계산해서 `cc.trange(정수, 정수)`에 넘길 것 (숫자를 그냥 넘기면 pycapcut은 이를 **마이크로초**로
  해석함 — 초 단위 float를 그대로 넘기면 안 됨. 문자열로 넘길 때만 `"1.5s"`처럼 단위를 붙여야 초로
  해석됨).

- **마지막 클립에서 "범위 초과" 에러**
  pycapcut이 내부적으로 재는 소재 길이가 ffprobe 값과 수십ms 다를 수 있음. 마지막 구간 끝을
  `duration - 0.05` 정도로 살짝 당겨서 저장.

- **CapCut에 같은 이름 프로젝트가 2개로 보임 / 유령 프로젝트가 보임**
  CapCut은 실제 드래프트 폴더와 별개로 `com.lveditor.draft\root_meta_info.json`에 자체 인덱스를
  캐싱한다. 폴더를 셸에서 직접 삭제/재생성하면(CapCut 자체 UI를 거치지 않으면) 이 인덱스에 예전
  draft_id가 고아로 남아 중복/유령 항목으로 보인다. 해결: 각 폴더 안의 `draft_meta_info.json`에서
  현재 유효한 `draft_id`를 확인한 뒤, `root_meta_info.json`의 `all_draft_store` 배열에서 그 외의
  중복/실체 없는 항목을 제거. **수정 후 CapCut을 완전히 재시작**해야 반영됨.

- **드래프트 폴더 이름 변경/삭제가 `Permission denied`로 실패**
  CapCut 앱이 해당 프로젝트를 열어놓은 상태라 폴더가 잠김. `Copy-Item -Recurse`로 새 이름 폴더에
  복사 후 원본 삭제하는 방식으로 우회 가능(복사는 잠긴 상태에서도 대체로 됨).

- **유튜브 다운로드 중 `HTTP Error 403: Forbidden`**
  yt-dlp가 구버전일 때 자주 발생. `yt-dlp -U`로 최신 버전 업데이트하면 대부분 해결.

- **`silencedetect` 기본값(-30dB)이 아무것도 못 찾음**
  배경음악/환경음이 있는 예능·브이로그 콘텐츠는 오디오가 거의 항상 -20dB 안팎을 유지해서 -30dB
  기준으로는 무음이 안 잡힘. 실측 후 `-23dB / 0.35초`를 기본값으로 조정함 — 콘텐츠 성격에 따라
  `--noise`, `--min-silence`로 추가 조정 가능.

- **긴 원본 영상 처리 시 몇 분씩 걸림**
  구간 지정 없이 전체 파일에 `silencedetect`/`scdet`를 돌리면 느림. `--start/--end`를 지정하면
  `ffmpeg -ss/-t`로 해당 구간만 디코딩해서 수 초 내로 끝남 (전체 분석 대비 대폭 단축).

- **저사양 PC에서 ffmpeg 인코딩 중 `x264 malloc failed`**
  (mp4 렌더링 경로인 `silence_jumpcut.py` 사용 시) 실제 물리메모리가 거의 소진된 상태에서 발생.
  다른 프로그램(특히 크롬 탭 다수) 종료 후 재시도.

## 7. 자막 대본(실제 발화) 참고가 필요할 때

화면에 박힌 자막만으로는 내용 파악이 부족할 때, 유튜브 자체 자막 트랙(자동생성 포함)을 무료로 받아
실제 대사를 텍스트로 읽을 수 있다(유료 API 불필요, 자막이 아예 없는 영상은 제외):

```powershell
& "<yt-dlp 경로>" --skip-download --write-auto-sub --write-sub --sub-langs "ko,ko-orig" --sub-format "vtt" -o "subs\%(id)s.%(ext)s" "<유튜브 링크>"
```
받은 `.vtt`는 `vtt_read.py`로 타임스탬프별 텍스트만 깔끔하게 정리해서 읽을 수 있음:
```powershell
& $PY vtt_read.py subs/영상ID.ko.vtt 32 41   # 32초~41초 구간만 출력
```
