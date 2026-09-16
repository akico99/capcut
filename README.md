# 캡컷 에이전트

한국어 토킹 영상을 자동으로 편집해서 CapCut 드래프트로 만들어주는 도구.
입력: mp4/mov 원본 → 출력: 무음·잔말·NG 컷이 빠지고 단어별 자막이 붙은 CapCut 드래프트.

지금은 1단계(무음 감지 + 점프컷)까지만 만들어졌고, UI 없이 스크립트로 돌아간다.
아래는 지금 시점 기준 사용법이고, 단계가 추가될 때마다 이 문서도 같이 갱신한다.

## 지금 뭐가 되나 (1단: 점프컷)

`촬영본.mov` 같은 원본 영상에서 ffmpeg로 무음 구간을 찾고, 그 구간을 뺀 나머지를
이어붙여서 CapCut 드래프트를 만든다. 추가로 화면 전환(장면이 바뀌는 지점)이 있으면
무음이 없어도 그 지점에서 클립을 나눠서, CapCut 타임라인에서 장면별로 따로 다룰 수 있게
한다(`scene_detect.split_at_scene_changes`). 자막·잔말 제거·NG 컷은 아직 없음.

## 사용법

```bash
# 1) 무음 구간만 미리 확인하고 싶을 때
python silence_detect.py 촬영본.mov

# 2) 실제로 CapCut 드래프트까지 만들 때
python build_draft.py 촬영본.mov [드래프트_이름]
```

`build_draft.py`를 돌리면 `C:\Users\<user>\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft\<드래프트_이름>` 폴더가 생긴다.
CapCut을 열면 그 이름의 드래프트가 보인다 — **직접 재생해서 컷이 자연스러운지 확인하는 게 검증의 전부다.**
스크립트가 에러 없이 끝났다고 해서 결과가 맞다는 뜻이 아니다.

### 무음 감지 임계값 튜닝

`silence_detect.py`의 `detect_silence()`는 두 파라미터를 받는다:

- `noise_db`: 이 dB 이하를 무음으로 판단. 기본 `-18.0`.
- `min_silence_duration`: 이 길이(초) 이상 지속돼야 무음으로 인정. 기본 `0.35`.

기본값 `-30dB`는 책에 흔히 나오는 값이지만, 방음이 안 된 실내 녹화에서는 배경 잡음이
항상 그보다 커서 무음이 하나도 안 잡힐 수 있다. 실제로 `촬영본.mov`(mean_volume -21.9dB)에서
`-30dB`는 0개, `-18dB`에서야 133개가 잡혔다. 새 영상을 테스트할 땐 먼저 이걸로 감을 잡으면 좋다:

```bash
ffmpeg -i 영상.mp4 -af volumedetect -f null - 2>&1 | grep volume
```

`mean_volume`보다 살짝 높은(즉 절대값이 작은) `noise_db`부터 시도해보는 식.
컷이 너무 잦으면(문장 사이 자연스러운 숨쉬기까지 다 잘림) `noise_db`를 낮추거나
`min_silence_duration`을 늘리고, 컷이 안 잡히면 반대로.

## 프로젝트 구조

```
config.py           # ffmpeg/ffprobe 경로 탐색, CapCut 드래프트 폴더 위치
silence_detect.py    # ffmpeg silencedetect -> 무음 구간 -> 남길 구간(keep_ranges) 계산
build_draft.py        # keep_ranges -> pycapcut으로 CapCut 드래프트 생성
```

`config.py`는 `winget install ffmpeg` 직후 PATH가 아직 이번 세션에 반영 안 됐을 때를 대비해서
winget 설치 경로를 직접 뒤지는 fallback이 들어있다. 터미널을 한 번 재시작하면 필요 없어진다.

## 앞으로 나올 단계 (원 계획)

| 단계 | 내용 |
|---|---|
| 2단 | FastAPI + 정적 HTML (드래그앤드롭 업로드 + SSE로 진행 상황 표시) |
| 3단 | Whisper로 대본 추출(세그먼트+단어) + 세그먼트 단위 자막 |
| 4단 | 잔말/NG 컷 통합, 결과 화면에 대본 노출 |
| 5단 | 영상 프리뷰 + 사용자가 직접 보존 구간 마킹([/] 단축키) |

핵심 원칙(변하지 않음): 전체 대본을 먼저 뽑고 그걸로 NG/자막을 결정하지, 단어 하나하나 보고
즉흥적으로 판단하지 않는다. 그리고 빌드가 성공했다고 검증된 게 아니다 — 항상 CapCut에서
직접 재생해봐야 한다.

## Claude Code(이 에이전트)와 같이 작업할 때 팁

- **CapCut 드래프트 JSON 관련 질문/구현은 `pycapcut-mac` 스킬이 이미 있다.** draft_content.json
  구조, `tm_duration`(마이크로초 단위), `transform_y`(자막 위치, 기본 -0.8), 최하단 비디오
  트랙은 반드시 0초부터 시작해야 한다는 규칙 등이 정리돼 있음. 새 세션에서도 "CapCut 드래프트"
  관련 작업이면 알아서 참조하게 되어 있지만, 안 하는 것 같으면 명시적으로 스킬 이름을 언급하면 됨.
- **"됐다"고 하기 전에 항상 CapCut에서 직접 재생해서 확인해달라고 요청받게 될 것이다.** 이건
  게으름이 아니라 원 계획서의 핵심 원칙이라서 그렇다 — 스크립트가 안 터졌다고 결과물이 맞다는
  보장이 없는 포맷이기 때문.
- 무음 감지 임계값처럼 "정답이 코드로 판단 안 되는" 튜닝 값은, 실행해보고 사용자 피드백을
  받아 조정하는 루프로 다룬다. 값을 임의로 정하고 넘어가지 않는다.
- 이 폴더에 테스트용 원본 영상을 넣어두면(`촬영본.mov`처럼) 그걸로 각 단계를 바로 검증한다.

## 에이전트 모드 (2026-09 추가) — 링크/편집본 → CapCut 드래프트

cut_sim(`silence_core.py` / `link_to_capcut.py`)을 AGENT_INTERFACE.md대로 래핑한 진입점. 자세한 설치는 `SETUP.md`.

```powershell
$PY = "C:\Users\leeyongsoo\AppData\Local\Programs\Python\Python310\python.exe"
& $PY run_pipeline.py --request request.json --out-json result.json
```

`request.json` 두 가지 형태:

```jsonc
// A) 링크 + 타임라인 구간 → 무음컷(+장면분할)
{ "source": "https://www.youtube.com/watch?v=XXXX",
  "segments": [ {"start": "0:50", "end": "2:04"} ],      // [] = 전체
  "options": { "scene_split": true }, "project_name": "영상1" }

// B) 편집본 + 원본 → 편집본이 쓴 원본 구간을 오디오 매칭으로 자동 추출 (audio_match.py)
{ "source": "원본.mp4 또는 링크", "edited": "편집본.mp4", "project_name": "영상2" }
```

- stdout에 `RESULT_JSON:{...}` 한 줄, stderr에 로그. exit 0 / 1(재시도 가능) / 2(불가).
- B는 배속(1.2x 등)을 자동 판별해 클립 speed로 그대로 적용하고, 무음컷은 생략(`silence_cut: false`).
  `cuts[].reason`이 `audio_match_low_confidence`인 클립과 `audio_match.unmatched` 구간은 CapCut에서 직접 확인.
- 같은 링크는 `download_cache.json`으로 재사용, 같은 프로젝트명은 `_v2`로 증가(`on_duplicate`).
- 단독 실행: `& $PY audio_match.py 편집본.mp4 원본.mp4` (JSON 출력), `--emit-request req.json`으로 A형 요청 생성.

### 자막 워크플로우 (드래프트에 번호 자리만, 문구는 텍스트로)

- 드래프트 생성 시 클립마다 하단에 `n-k`(영상 번호-클립 번호) 텍스트 자리가 자동으로 들어감
  (`options.caption_placeholders`, 스타일은 주의사항 3번 기준 흰색/크기10/하단. 글꼴은 CapCut에서 코트라 볼드로).
- `caption_sheet.py --result result.json --out sheets/영상2` → 클립별 프레임 3장 + Whisper 대사 시트.
  에이전트가 시트를 보고 하단 자막(예능 3인칭 내레이션) + 상단 후킹 제목 후보를 `자막/*.md`로 출력.
- 사용자가 텍스트를 골라 CapCut에서 `n-k` 자리에 붙여넣기.
