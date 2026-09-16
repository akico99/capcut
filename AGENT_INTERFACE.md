# 에이전트(Claude Code) 연동 인터페이스 설계 가이드

`SETUP.md`의 파이프라인(`silence_core.py` / `link_to_capcut.py` / `silence_jumpcut.py`)을
사람이 PowerShell에서 직접 돌리는 게 아니라, **에이전트가 "링크 + 타임라인"만 받아서 스크립트를
호출하고 결과를 해석**하는 구조로 쓸 때 필요한 설계 원칙과 구체적인 입출력 스펙 정리.

---

## 1. 문제의식 — 사람용 CLI와 에이전트용 CLI는 요구사항이 다르다

기존 `link_to_capcut.py`는 사람이 눈으로 보고 옵션을 조합해 쓰는 걸 전제로 설계됨:
- 진행 상황을 stdout에 사람이 읽기 좋은 문장으로 출력
- 에러가 나면 traceback을 그대로 던짐
- 결과 확인은 "CapCut 앱을 열어서 눈으로 본다"

에이전트가 호출자가 되면 요구사항이 바뀐다:
- **에이전트는 화면을 못 본다** — CapCut 프로젝트가 실제로 만들어졌는지, 몇 개 클립인지,
  어느 구간이 매칭됐는지를 **텍스트로** 알아야 다음 판단(사용자에게 뭐라고 보고할지, 재시도할지)을 함
- **파싱 가능한 출력**이 필요 — "성공했습니다" 같은 자연어 문장이 아니라 구조화된 데이터
- **stdout/stderr 분리**가 중요 — 로그(진행상황, 디버그)와 결과(최종 산출물)가 섞이면 에이전트가
  결과만 골라내기 어려움
- **에러가 재시도 가능한지 아닌지**를 에이전트가 구분할 수 있어야 함 (예: PATH 문제는 재시도 무의미,
  다운로드 403은 `yt-dlp -U` 후 재시도 가능)

즉, 기존 스크립트를 그대로 두고 **그 위에 "에이전트 전용 얇은 래퍼 1개"** 를 추가하는 방향을 권장.

---

## 2. 전체 구조 제안

```
사용자 → (클로드코드에게) "이 링크, 0:50~2:04 컷편집해줘"
              │
              ▼
        [에이전트]
              │  ① 자연어 → 구조화된 요청 JSON으로 변환
              ▼
        run_pipeline.py  ← 신규 추가: 단일 진입점 래퍼
              │  ② 기존 스크립트들 내부 호출 (subprocess 아니라 함수 import 권장)
              │     silence_core → link_to_capcut
              ▼
        result.json (stdout 마지막 줄, 또는 --out-json 파일)
              │
              ▼
        [에이전트가 파싱] → 사용자에게 결과 요약 보고
```

핵심은 **에이전트가 호출하는 커맨드는 하나**로 고정하고, 그 안에서 기존 스크립트 로직을 함수로
재사용하는 것. 여러 스크립트를 순서대로 호출하게 만들면 중간 실패 시 상태 추적이 에이전트 쪽 책임이
되어버려서 복잡해짐.

---

## 3. 입력 스펙

### 3-1. 에이전트 → 래퍼: JSON 한 덩어리로 전달

사람이 쓰는 `--start 0:50 --end 2:04` 반복 옵션 방식은 에이전트가 생성하기엔 번거롭고 실수하기
쉬움(특히 구간이 여러 개일 때). 대신 **하나의 JSON을 stdin 또는 `--request` 파일로 전달**하는 방식을
권장.

```json
{
  "source": "https://www.youtube.com/watch?v=XXXX",
  "segments": [
    { "start": "0:50", "end": "2:04" }
  ],
  "options": {
    "scene_split": true,
    "scene_threshold": 10,
    "noise_db": -23,
    "min_silence": 0.35,
    "pad": 0.12
  },
  "project_name": "영상1"
}
```

- `source`: 유튜브 링크든 로컬 파일 경로든 동일 필드로 통일 (기존처럼 스크립트가 자동 판별)
- `segments`: 빈 배열이면 "전체 구간" (기존 `--start/--end` 생략과 동일 의미) — **명시적으로 빈 배열을
  넣게 해서** "옵션 누락"과 "의도적으로 전체 선택"을 구분되게 함
- `options`: 전부 기본값이 있으므로 에이전트는 사용자가 명시적으로 언급한 값만 채우고 나머지는
  생략 가능 → 래퍼가 기존 스크립트의 default와 동일한 값으로 채움

호출:
```powershell
& $PY run_pipeline.py --request request.json --out-json result.json
```

### 3-2. 자연어 타임라인 → 구조화 변환은 에이전트 쪽 책임으로 명확히 분리

"1분부터 2분 4초까지", "초반 1분 컷하고 중간에 지루한 부분 빼줘" 같은 표현을 초 단위로 바꾸는 건
래퍼 스크립트가 할 일이 아니라 **에이전트가 request.json을 만드는 단계에서** 끝내야 함. 스크립트 쪽은
`0:50` / `50` / `50.0` 정도의 명확한 시간 포맷만 받고, 모호하면 바로 에러를 던지는 게 맞음 (스크립트가
자연어를 추측하려고 하면 실패 원인 추적이 어려워짐).

---

## 4. 출력 스펙 — 에이전트가 읽을 결과

### 4-1. 성공 시

`result.json` (또는 stdout 마지막 줄에 `RESULT_JSON:{...}` 형태로 마커를 붙여 stdout에서도 바로
grep 가능하게):

```json
{
  "status": "success",
  "project_name": "영상1",
  "draft_id": "3F8A9B2C-...",
  "draft_path": "C:\\Users\\<user>\\AppData\\Local\\CapCut\\User Data\\Projects\\com.lveditor.draft\\영상1",
  "source_resolved": "C:\\...\\영상_20260916.mp4",
  "clip_count": 14,
  "total_duration_sec": 74.3,
  "cuts": [
    { "in": 50.12, "out": 58.44, "reason": "manual_segment" },
    { "in": 58.44, "out": 61.02, "reason": "scene_split" }
  ],
  "warnings": []
}
```

- `clip_count`, `total_duration_sec`: 에이전트가 사용자에게 "14개 클립, 총 1분 14초로 만들어졌어요"
  라고 바로 보고할 수 있게 숫자로 제공 (CapCut을 열어보지 않아도 결과를 설명 가능해야 함)
- `cuts`: 각 컷이 왜 생겼는지(`manual_segment`/`silence_removed`/`scene_split`) 명시 — 나중에
  "왜 여기서 끊겼지?" 질문에 에이전트가 답할 근거
- `warnings`: 치명적이진 않지만 사용자가 알아야 할 사항 (예: "마지막 클립 끝을 0.05초 당김",
  "장면전환 threshold가 낮아 컷이 26개로 과도하게 분할됨")

### 4-2. 실패 시 — 재시도 가능 여부를 코드로 구분

```json
{
  "status": "error",
  "error_code": "YTDLP_FORBIDDEN",
  "message": "HTTP 403: yt-dlp 버전이 오래되어 발생하는 문제로 보임",
  "retryable": true,
  "suggested_action": "yt-dlp -U 실행 후 재시도"
}
```

`SETUP.md` 6번 트러블슈팅 항목을 그대로 에러 코드 테이블로 옮기면 됨:

| error_code | retryable | 설명 |
|---|---|---|
| `PYTHON_VERSION_MISMATCH` | false | pycapcut 못 찾음 — 3.10 경로 재확인 필요, 자동 재시도 무의미 |
| `YTDLP_FORBIDDEN` | true | 403 — `yt-dlp -U` 후 재시도 |
| `SEGMENT_OVERLAP` | false | 내부 로직 버그 가능성 — 사용자에게 구간 재확인 요청 |
| `RANGE_EXCEEDED` | false (자동 보정됨) | 정상적으로는 내부에서 자동 조정되므로 외부에 노출되면 버그 |
| `DRAFT_LOCKED` | true | CapCut이 해당 프로젝트를 열어놓은 상태 — 앱 종료 후 재시도 |
| `LOW_MEMORY` | true | x264 malloc 실패 — 다른 프로그램 종료 후 재시도 |
| `NO_SILENCE_DETECTED` | false (자동 보정 시도) | `-23dB` 기본값으로도 안 잡히면 `noise_db`를 더 조정해야 함 |

에이전트는 `retryable: true`인 경우에만 `suggested_action`을 자동으로 실행해보고, `false`면 바로
사용자에게 물어보는 식으로 분기 가능.

---

## 5. 로그와 결과의 물리적 분리

- **stderr**: 사람이 디버깅할 때 보는 상세 로그 (ffmpeg 진행률, silencedetect raw 출력 등) — 에이전트는
  기본적으로 무시하되, 에러 발생 시 마지막 N줄을 함께 사용자에게 보여줄 수 있음
- **stdout**: 오직 최종 결과 JSON 한 줄만 — 에이전트가 `subprocess.run(..., capture_output=True)`
  했을 때 `stdout`을 바로 `json.loads()` 하면 끝나야 함 (중간에 print 섞이면 파싱 깨짐)
- 기존 스크립트들의 진행상황 print문은 전부 stderr로 리다이렉트하거나 `logging` 모듈(핸들러를
  stderr로)로 통일 권장

---

## 6. 멱등성(idempotency) — 같은 요청 반복 호출 대비

에이전트는 사용자가 "다시 해줘" / 중간에 실패한 걸 재실행할 가능성이 높음. 아래 두 가지를 권장:

1. **다운로드 캐시**: `source` + 구간 정보를 해시해서 이미 받은 파일이 있으면 재다운로드 스킵.
   `result.json`의 `source_resolved`에 캐시 히트 여부(`"cache_hit": true`)도 같이 반환하면 에이전트가
   "다시 다운로드했어요" vs "기존 파일 재사용했어요"를 구분해 보고 가능.
2. **동일 project_name 재실행 시 정책 명시**: 덮어쓸지, `_v2`를 붙여 새로 만들지를 `options`에
   `"on_duplicate": "overwrite" | "increment"` 필드로 받아서 에이전트가 매번 판단하지 않고 요청에
   포함시키게 함. (기존 "유령 프로젝트" 트러블슈팅 이슈 재발 방지 차원에서도 덮어쓰기보다
   `increment` 기본값 권장)

---

## 7. run_pipeline.py 구현 시 체크리스트

- [ ] 기존 `silence_core.py`의 함수들을 subprocess 재호출이 아니라 **직접 import**해서 사용
      (subprocess 체이닝은 에러 전파·타입 보존이 지저분해짐)
- [ ] 모든 예외를 최상위에서 캐치해서 위 4-2 포맷의 JSON으로 변환 후 종료 (raw traceback을 stdout에
      흘리지 않기)
- [ ] `sys.exit()` 코드도 `status`와 맞춰서: 성공 0, `retryable` 에러는 1, `retryable=false`는 2 —
      에이전트가 JSON 파싱 전에 exit code만으로도 1차 분기 가능하게
- [ ] `--out-json <path>` 옵션도 같이 제공 (stdout 캡처가 불안정한 실행 환경 대비 — 결과를 파일로도
      떨어뜨려서 에이전트가 파일 존재 여부로 완료를 확인할 수 있게)
- [ ] 기존 `KNOWN_FFMPEG_DIRS` / `KNOWN_YTDLP_PATHS` / `DEFAULT_DRAFT_FOLDER` 탐색 실패 시에도
      `status: error` JSON으로 떨어지게 (현재는 초기화 단계 실패 시 바로 죽을 가능성 있음 — 확인 필요)

---

## 8. 향후 확장 지점 — 오디오 매칭 기능 연동 시

이전에 논의한 "편집본 기반 오디오 매칭"(`audio_match.py`) 기능을 붙일 때도 같은 인터페이스 규칙을
따르면 자연스럽게 확장됨:

- `request.json`의 `segments`를 사람이 직접 안 넣고, `audio_match.py`가 계산한 결과로 채워서
  `run_pipeline.py`에 넘기는 구조 (`segments`의 값 출처만 달라질 뿐 스키마는 동일)
- 매칭 신뢰도가 낮은 구간은 `cuts[].reason`을 `"audio_match_low_confidence"`로 표시해서, 에이전트가
  "이 구간은 확실하지 않으니 확인해달라"고 사용자에게 되물을 근거로 활용

---

## 요약

| 항목 | 방향 |
|---|---|
| 입력 | 자연어(에이전트) → 구조화 JSON(`request.json`) → 단일 CLI 진입점 |
| 출력 | stdout은 결과 JSON 한 줄 / stderr는 디버그 로그 — 분리 |
| 에러 | `error_code` + `retryable` 플래그로 에이전트 자동 판단 가능하게 |
| 재실행 | 캐시 + `on_duplicate` 정책으로 멱등성 확보 |
| 확장 | segments의 출처(수동/오디오매칭)만 바뀌고 인터페이스는 그대로 재사용 |
