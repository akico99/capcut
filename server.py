"""로컬 웹 UI: 영상 업로드 -> 화면 전환 기준 구간 분석 -> 사용자가 웹에서 구간 선택
-> 선택된 구간 안에서만 무음 제거 -> CapCut 드래프트 생성. SSE로 진행 상황 스트리밍.

두 단계로 나뉜다 (EventSource가 GET만 지원해서 업로드(POST)와 SSE를 하나로 합칠 수 없고,
이번엔 중간에 사용자 선택까지 끼어들어서 더더욱 한 요청으로 못 묶는다):
  1) POST /api/jobs                    파일 업로드 -> 화면 전환 분석 -> 후보 구간 목록 반환
     GET  /api/jobs/{id}/events        (SSE) 1단계 진행 상황
     GET  /api/jobs/{id}/thumb/{i}     구간 i의 대표 썸네일
  2) POST /api/jobs/{id}/segments      사용자가 고른 구간 인덱스 -> 무음 제거 + 드래프트 생성 시작
     GET  /api/jobs/{id}/events        (SSE, 재연결) 2단계 진행 상황
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from build_draft import build_jumpcut_draft
from scene_detect import extract_thumbnail, get_candidate_segments
from silence_detect import detect_silence, get_duration, get_keep_ranges

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

STEP_MIN_DURATION = 0.5  # 단계당 최소 지연(초) - 너무 빨리 끝나도 애니메이션이 보이게

app = FastAPI()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """개발 중인 로컬 도구라 index.html/static이 캐싱되면 서버를 재시작해도
    브라우저가 옛날 버전을 계속 보여줄 수 있다 (FileResponse는 Cache-Control을
    안 붙여서 브라우저가 자체 판단으로 캐싱함). 그래서 아예 못 하게 막는다."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class Job:
    def __init__(self, job_id: str, video_path: Path, job_dir: Path, draft_name: str):
        self.job_id = job_id
        self.video_path = video_path
        self.job_dir = job_dir
        self.draft_name = draft_name
        self.queue: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
        self.segments: List[Tuple[float, float]] = []


JOBS: Dict[str, Job] = {}


async def _emit(job: Job, step: str, status: str, **detail: Any) -> None:
    await job.queue.put({"step": step, "status": status, **detail})


async def _run_step(job: Job, step: str, fn, *args) -> Any:
    """블로킹 함수 fn(*args)를 스레드에서 돌리면서 start/done(or error) 이벤트를 낸다.

    최소 STEP_MIN_DURATION초는 걸리도록 보정해서, 빨리 끝나도 스텝퍼 애니메이션이 보이게 한다.
    """
    await _emit(job, step, "start")
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, *args)
    except Exception as exc:
        await _emit(job, step, "error", message=str(exc))
        raise

    elapsed = time.monotonic() - started
    if elapsed < STEP_MIN_DURATION:
        await asyncio.sleep(STEP_MIN_DURATION - elapsed)
    return result


def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem
    safe = "".join(c for c in stem if c.isalnum() or c in "._- 가-힣").strip()
    return safe or "video"


# ---------- 1단계: 업로드 -> 화면 전환 분석 -> 후보 구간 ----------

async def process_phase1(job: Job) -> None:
    try:
        duration = await _run_step(job, "upload", get_duration, str(job.video_path))
        await _emit(job, "upload", "done", duration=duration)

        segments = await _run_step(job, "scene", get_candidate_segments, str(job.video_path))
        job.segments = segments

        # 각 구간 중앙 지점의 썸네일을 뽑아둔다 (사용자가 웹에서 보고 고를 수 있게).
        for i, (s, e) in enumerate(segments):
            mid = (s + e) / 2
            await asyncio.to_thread(
                extract_thumbnail, str(job.video_path), mid, str(job.job_dir / f"thumb_{i}.jpg")
            )

        await _emit(
            job, "scene", "done",
            segments=[{"index": i, "start": s, "end": e} for i, (s, e) in enumerate(segments)],
        )
        await _emit(job, "job", "await_selection")
    except Exception as exc:
        await _emit(job, "job", "error", message=str(exc))
    finally:
        await job.queue.put(None)  # 스트림 종료 신호


@app.post("/api/jobs")
async def create_job(file: UploadFile):
    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "input.mp4").suffix or ".mp4"
    video_path = job_dir / f"original{suffix}"
    with open(video_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    draft_name = f"{_sanitize_stem(file.filename or 'video')}_{job_id[:6]}"
    job = Job(job_id, video_path, job_dir, draft_name)
    JOBS[job_id] = job

    asyncio.create_task(process_phase1(job))

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/thumb/{index}")
async def get_thumbnail(job_id: str, index: int) -> FileResponse:
    job = JOBS[job_id]
    return FileResponse(job.job_dir / f"thumb_{index}.jpg")


# ---------- 2단계: 선택된 구간 -> 무음 제거 -> 드래프트 생성 ----------

async def process_phase2(job: Job, selected_ranges: List[Tuple[float, float]]) -> None:
    try:
        silences = await _run_step(job, "silence", detect_silence, str(job.video_path))

        keep_ranges: List[Tuple[float, float]] = []
        for bounds in selected_ranges:
            keep_ranges.extend(
                get_keep_ranges(str(job.video_path), silences, bounds=bounds)
            )

        selected_total = sum(e - s for s, e in selected_ranges)
        kept_total = sum(e - s for s, e in keep_ranges)
        await _emit(
            job, "silence", "done",
            silence_count=len(silences), keep_count=len(keep_ranges),
            removed_seconds=selected_total - kept_total,
        )

        draft_path = await _run_step(
            job, "draft", build_jumpcut_draft, job.draft_name, str(job.video_path), keep_ranges,
        )
        await _emit(job, "draft", "done", draft_path=draft_path)

        duration = get_duration(str(job.video_path))
        await _emit(
            job, "job", "done",
            draft_name=job.draft_name,
            original_seconds=duration,
            kept_seconds=kept_total,
            removed_seconds=duration - kept_total,
            cut_count=len(keep_ranges),
        )
    except Exception as exc:
        await _emit(job, "job", "error", message=str(exc))
    finally:
        await job.queue.put(None)


@app.post("/api/jobs/{job_id}/segments")
async def confirm_segments(job_id: str, body: dict):
    job = JOBS[job_id]
    # 화면 전환 자동감지로 고른 구간이든, 사용자가 초 단위로 직접 입력한 구간이든
    # 여기서는 그냥 (start, end) 목록으로 받는다 - 출처를 구분할 필요가 없다.
    ranges: List[List[float]] = body["ranges"]
    if not ranges:
        return {"error": "선택된 구간이 없습니다."}

    duration = get_duration(str(job.video_path))
    for start, end in ranges:
        if not (0 <= start < end <= duration + 0.5):  # ffprobe/실제 길이 오차 약간 허용
            return {"error": f"구간 [{start}, {end}]이 영상 길이({duration:.1f}s)를 벗어났습니다."}

    selected_ranges = sorted((float(s), float(e)) for s, e in ranges)

    job.queue = asyncio.Queue()  # 2단계용 새 큐 (1단계 큐는 이미 다 소비되고 닫혔음)
    asyncio.create_task(process_phase2(job, selected_ranges))

    return {"status": "processing"}


# ---------- 공통: SSE 스트림, 정적 파일 ----------

@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if job is None:
        async def not_found() -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'step': 'job', 'status': 'error', 'message': '알 수 없는 job_id'})}\n\n"
        return StreamingResponse(not_found(), media_type="text/event-stream")

    async def stream() -> AsyncGenerator[str, None]:
        while True:
            event = await job.queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
