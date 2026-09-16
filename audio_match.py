#!/usr/bin/env python
"""
편집본 오디오 → 원본에서 어느 구간을 썼는지 자동 탐색 (오디오 지문 매칭)

  python audio_match.py 편집본.mp4 원본.mp4
  python audio_match.py 편집본.mp4 "https://youtube.com/watch?v=XXXX" --emit-request req.json

원리:
  1. 양쪽 오디오를 8kHz 모노로 뽑아 log-mel 스펙트로그램(10ms 프레임) → 시간축 고역통과(BGM/음량 차이 제거)
  2. 편집본을 3초 창으로 훑으며 각 창이 원본 어디에 있는지 FFT 정규화 상호상관(NCC)으로 탐색
  3. 창별 (원본위치 - 편집위치) 오프셋이 일정하게 이어지는 구간을 하나의 세그먼트로 묶고 경계를 프레임 단위로 다듬음
  4. 편집본이 배속(1.2x 등)이면 후보 배속별로 매칭 점수를 비교해 자동 판별

출력(stdout JSON): segments [{start, end, edit_start, edit_end, confidence, low_confidence}], speed, coverage
numpy만 사용 (scipy 불필요). 유료 API 없음.
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np

from silence_core import PipelineError, find_binary, is_url

log = logging.getLogger("audio_match")

SR = 8000
N_FFT = 512
HOP = 80                 # 10ms
N_BANDS = 32
HPF_FRAMES = 100         # 1초 이동평균 제거
WINDOW_SEC = 3.0
STRIDE_SEC = 1.0
OFFSET_TOL_SEC = 0.15    # 같은 세그먼트로 볼 오프셋 편차
MIN_SEGMENT_SEC = 0.5
LOW_CONF = 0.35          # 이 미만이면 low_confidence 표시
MIN_MARGIN = 0.05        # 1위-2위 피크 차이가 이보다 작으면 "매칭 안 됨"으로 간주
LOW_MARGIN = 0.10        # 이 미만이면 low_confidence
DEFAULT_SPEEDS = [1.0, 1.1, 1.15, 1.2, 1.25, 1.3, 1.5]
ENERGY_FLOOR_RATIO = 0.05  # 창 에너지가 중앙값의 5% 미만이면 무음으로 간주


# ---------------------------------------------------------------------------
# 오디오 → 특징
# ---------------------------------------------------------------------------
def extract_audio(ffmpeg, path, sr=SR):
    cmd = [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 4 * sr:
        raise PipelineError("AUDIO_EXTRACT_FAILED", f"오디오 추출 실패: {path}\n{r.stderr.decode('utf-8', 'replace')[-500:]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _mel_filterbank(sr, n_fft, n_bands, fmin=100.0, fmax=3800.0):
    def hz2mel(f): return 2595 * np.log10(1 + f / 700)
    def mel2hz(m): return 700 * (10 ** (m / 2595) - 1)
    mels = np.linspace(hz2mel(fmin), hz2mel(fmax), n_bands + 2)
    hz = mel2hz(mels)
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    fb = np.zeros((n_bands, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_bands):
        lo, c, hi = bins[i], bins[i + 1], bins[i + 2]
        if c <= lo: c = lo + 1
        if hi <= c: hi = c + 1
        fb[i, lo:c] = np.linspace(0, 1, c - lo, endpoint=False)
        fb[i, c:hi] = np.linspace(1, 0, hi - c, endpoint=False)
    return fb


_FB = _mel_filterbank(SR, N_FFT, N_BANDS)


def features(audio):
    """[N_BANDS, n_frames] 정규화 특징. 시간축 이동평균을 빼서 BGM/게인 차이에 둔감하게."""
    n_frames = max(1, (len(audio) - N_FFT) // HOP + 1)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = audio[idx] * np.hanning(N_FFT).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    mel = np.log(mag @ _FB.T + 1e-6).T.astype(np.float32)        # [B, T]
    # 이동평균 제거 (고역통과)
    k = HPF_FRAMES
    pad = np.pad(mel, ((0, 0), (k // 2, k - k // 2 - 1)), mode="edge")
    cs = np.cumsum(pad, axis=1)
    ma = (cs[:, k:] - cs[:, :-k]) / k
    ma = np.concatenate([mel[:, :1] * 0 + ma[:, :1], ma], axis=1)[:, :mel.shape[1]]
    return mel - ma


def time_stretch(feat, factor):
    """편집본이 factor 배속이면 편집 프레임 k ↔ 원본 프레임 k*factor. 특징을 시간축으로 늘려 원본 템포로 되돌린다."""
    if abs(factor - 1.0) < 1e-6:
        return feat
    T = feat.shape[1]
    new_T = int(round(T * factor))
    src = np.linspace(0, T - 1, new_T)
    lo = np.floor(src).astype(int)
    hi = np.minimum(lo + 1, T - 1)
    w = (src - lo).astype(np.float32)
    return feat[:, lo] * (1 - w) + feat[:, hi] * w


# ---------------------------------------------------------------------------
# 매칭
# ---------------------------------------------------------------------------
class Matcher:
    """원본 특징의 FFT를 한 번만 계산해두고 여러 창을 빠르게 상관시킨다."""

    def __init__(self, orig_feat, max_win_frames):
        self.O = orig_feat
        self.N = orig_feat.shape[1]
        self.L = max_win_frames
        self.n_fft = 1 << int(np.ceil(np.log2(self.N + self.L)))
        self.O_fft = np.fft.rfft(orig_feat, n=self.n_fft, axis=1)
        # 슬라이딩 에너지 (정규화용)
        e = np.sum(orig_feat ** 2, axis=0)
        self.e_cum = np.concatenate([[0.0], np.cumsum(e)])

    def match(self, win):
        """win [B, L'] → (best_orig_frame, ncc, second_ncc)"""
        Lw = win.shape[1]
        W_fft = np.fft.rfft(win[:, ::-1], n=self.n_fft, axis=1)
        corr = np.fft.irfft(self.O_fft * W_fft, n=self.n_fft, axis=1).sum(axis=0)
        # corr[t + Lw - 1] = sum_k O[t+k] * win[k]
        valid = self.N - Lw + 1
        if valid <= 0:
            return 0, 0.0, 0.0
        num = corr[Lw - 1: Lw - 1 + valid]
        e_o = self.e_cum[Lw:Lw + valid] - self.e_cum[:valid]
        # 디지털 무음(에너지≈0) 구간은 분모가 부동소수점 노이즈 수준이 되어 NCC가 폭발한다.
        # 에너지 하한을 전체 중앙값의 일부로 두고, 그 이하인 위치는 후보에서 제외한다.
        floor = ENERGY_FLOOR_RATIO * float(np.median(e_o))
        e_w = float(np.sum(win ** 2))
        if e_w < floor:                     # 편집본 창 자체가 무음이면 매칭 불가
            return 0, 0.0, 0.0
        denom = np.sqrt(np.maximum(e_o, floor)) * np.sqrt(e_w)
        ncc = num / denom
        ncc[e_o < floor] = 0.0
        best = int(np.argmax(ncc))
        # 2위 피크 (1위 주변 ±1초 제외)
        mask = np.ones(valid, dtype=bool)
        mask[max(0, best - 100): best + 100] = False
        second = float(ncc[mask].max()) if mask.any() else 0.0
        return best, float(ncc[best]), second


def match_windows(matcher, edit_feat, window_frames, stride_frames, limit=None):
    T = edit_feat.shape[1]
    starts = list(range(0, max(1, T - window_frames + 1), stride_frames))
    if starts[-1] + window_frames < T:
        starts.append(max(0, T - window_frames))
    if limit and len(starts) > limit:
        starts = [starts[i] for i in np.linspace(0, len(starts) - 1, limit).astype(int)]
    out = []
    for s in starts:
        win = edit_feat[:, s:s + window_frames]
        pos, ncc, second = matcher.match(win)
        out.append({"edit_start": s, "edit_end": s + win.shape[1], "orig_pos": pos,
                    "offset": pos - s, "ncc": ncc, "second": second})
    return out


def refine_speed_from_drift(wins, speed, tol_frames, min_wins=4):
    """같은 세그먼트 안에서 orig_pos가 edit_start보다 빨리/느리게 진행하면 배속 추정이 어긋난 것.
    그룹별 기울기(orig/edit)의 중앙값으로 배속을 보정한다."""
    slopes, weights = [], []
    for g in group_windows(list(wins), tol_frames * 4):
        ws = g["wins"]
        if len(ws) < min_wins:
            continue
        x = np.array([w["edit_start"] for w in ws], dtype=float)
        y = np.array([w["orig_pos"] for w in ws], dtype=float)
        slope = np.polyfit(x, y, 1)[0]
        slopes.append(slope); weights.append(len(ws))
    if not slopes:
        return speed
    slope = float(np.average(slopes, weights=weights))
    return speed * slope


def detect_speed(matcher, edit_feat_raw, speeds, window_frames, stride_frames):
    """후보 배속별로 몇 개 창만 매칭해 중앙값 NCC가 가장 높은 배속을 고른다."""
    best = (1.0, -1.0)
    for sp in speeds:
        ef = time_stretch(edit_feat_raw, sp)
        wins = match_windows(matcher, ef, window_frames, stride_frames, limit=6)
        score = float(np.median([w["ncc"] for w in wins]))
        log.info("      배속 후보 %.2fx: NCC 중앙값 %.3f", sp, score)
        if score > best[1]:
            best = (sp, score)
    return best[0]


# ---------------------------------------------------------------------------
# 세그먼트 구성
# ---------------------------------------------------------------------------
def group_windows(wins, tol_frames, keep_unmatched=False):
    """창들을 오프셋이 일정한 그룹으로 묶는다. keep_unmatched=True면 매칭 실패 창들도
    offset=None 인 의사 그룹으로 남긴다 (세그먼트 사이 구멍 표시용)."""
    groups = []
    for w in wins:
        min_margin = MIN_MARGIN * (0.6 if w.get("fine") else 1.0)
        if w["ncc"] - w["second"] < min_margin:
            w["unmatched"] = True
            if keep_unmatched:
                if groups and groups[-1]["offset"] is None:
                    groups[-1]["wins"].append(w)
                else:
                    groups.append({"wins": [w], "offset": None})
            continue
        if (groups and groups[-1]["offset"] is not None
                and abs(w["offset"] - groups[-1]["offset"]) <= tol_frames and w["ncc"] >= LOW_CONF * 0.6):
            g = groups[-1]
            g["wins"].append(w)
            g["offset"] = int(np.median([x["offset"] for x in g["wins"]]))
        else:
            groups.append({"wins": [w], "offset": w["offset"]})
    return groups


def fine_pass(matcher, edit_feat, wins, fps):
    """연속 2창 이상 매칭 실패한 구간을 짧은 창(1.5s/0.5s)으로 다시 훑는다.
    편집본에 짧은 컷이 촘촘하거나 효과음이 덮인 곳에서 3초 창은 놓치기 쉽다."""
    fine_w, fine_s = int(1.5 * fps), int(0.5 * fps)
    runs, cur = [], []
    for w in wins:
        if w.get("unmatched"):
            cur.append(w)
        elif cur:
            runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    extra = []
    for run in runs:
        if len(run) < 2:
            continue
        lo, hi = run[0]["edit_start"], run[-1]["edit_end"]
        sub = edit_feat[:, lo:hi]
        if sub.shape[1] < fine_w:
            continue
        for w in match_windows(matcher, sub, fine_w, fine_s):
            w["edit_start"] += lo; w["edit_end"] += lo; w["offset"] = w["orig_pos"] - w["edit_start"]
            w["fine"] = True
            extra.append(w)
        log.info("      정밀 재탐색: 편집 %.1f~%.1fs 구간 %d창", lo / fps, hi / fps, len(extra))
    if not extra:
        return wins
    # 정밀 창으로 덮인 구간의 굵은 창은 제거하고 합친다
    covered = [(r[0]["edit_start"], r[-1]["edit_end"]) for r in runs if len(r) >= 2]
    keep = [w for w in wins if not any(a <= w["edit_start"] < b for a, b in covered)]
    for w in keep:
        w.pop("unmatched", None)
    return sorted(keep + extra, key=lambda w: w["edit_start"])


def refine_boundary(edit_feat, orig_feat, lo, hi, off_a, off_b, smooth=15):
    """edit 프레임 lo~hi 사이에서 A(off_a)→B(off_b) 전환점을 프레임 단위로 찾는다."""
    N = orig_feat.shape[1]
    best_t, best_score = lo, -1e9
    fr = np.arange(lo, hi)
    def sim(off):
        idx = fr + off
        ok = (idx >= 0) & (idx < N)
        s = np.full(len(fr), -1.0, dtype=np.float32)
        a, b = edit_feat[:, fr[ok]], orig_feat[:, idx[ok]]
        s[ok] = np.sum(a * b, axis=0) / (np.linalg.norm(a, axis=0) * np.linalg.norm(b, axis=0) + 1e-6)
        return s
    sa, sb = sim(off_a), sim(off_b)
    # 전환점 t: 앞은 A, 뒤는 B 일 때 총합 최대
    gain = np.concatenate([[0.0], np.cumsum(sa - sb)])
    best_t = lo + int(np.argmax(gain))
    return best_t


def build_segments(wins, edit_feat, orig_feat, tol_frames, min_frames):
    """반환: (segments, holes). holes는 원본에서 못 찾은 편집본 프레임 구간 목록."""
    T = edit_feat.shape[1]
    groups = group_windows(wins, tol_frames, keep_unmatched=True)
    # 고립된(1창) 매칭 실패는 경계 창일 뿐이므로 구멍으로 취급하지 않고 버린다
    groups = [g for g in groups if g["offset"] is not None or len(g["wins"]) >= 2]
    if not groups:
        return [], []

    # 경계 결정
    bounds = [0]
    for a, b in zip(groups, groups[1:]):
        if a["offset"] is None or b["offset"] is None:
            # 구멍 경계: 매칭된 쪽의 창 끝/시작을 그대로 사용
            t = a["wins"][-1]["edit_end"] if a["offset"] is not None else b["wins"][0]["edit_start"]
            bounds.append(min(max(bounds[-1], t), T))
            continue
        lo = b["wins"][0]["edit_start"]
        hi = a["wins"][-1]["edit_end"]
        if hi <= lo:
            lo, hi = hi, lo
        lo, hi = max(bounds[-1], lo), max(lo + 1, hi)
        bounds.append(refine_boundary(edit_feat, orig_feat, lo, hi, a["offset"], b["offset"]))
    bounds.append(T)

    segs, holes = [], []
    for g, es, ee in zip(groups, bounds, bounds[1:]):
        if ee - es < min_frames:
            continue
        if g["offset"] is None:
            holes.append((es, ee))
            continue
        conf = float(np.mean([w["ncc"] for w in g["wins"]]))
        margin = float(np.mean([w["ncc"] - w["second"] for w in g["wins"]]))
        fine = any(w.get("fine") for w in g["wins"])
        segs.append({
            "edit_start_f": es, "edit_end_f": ee,
            "orig_start_f": es + g["offset"], "orig_end_f": ee + g["offset"],
            "confidence": round(conf, 3), "margin": round(margin, 3),
            "low_confidence": conf < LOW_CONF or margin < LOW_MARGIN or fine,
        })
    # 인접 세그먼트가 원본에서도 연속이면 합침 (경계 오차로 갈라진 경우)
    merged = []
    for s in segs:
        if (merged and merged[-1]["edit_end_f"] == s["edit_start_f"]
                and abs(merged[-1]["orig_end_f"] - s["orig_start_f"]) <= tol_frames):
            m = merged[-1]
            m["edit_end_f"], m["orig_end_f"] = s["edit_end_f"], s["orig_end_f"]
            m["confidence"] = round(min(m["confidence"], s["confidence"]), 3)
            m["low_confidence"] = m["low_confidence"] or s["low_confidence"]
        else:
            merged.append(s)
    return merged, holes


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def match(edited_path, original_path, speeds=None, ffmpeg=None):
    """반환: {"speed", "segments": [...], "coverage", "edit_duration", "warnings"}"""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    speeds = speeds if speeds is not None else DEFAULT_SPEEDS
    fps = SR / HOP

    log.info("[match 1/4] 오디오 추출 중...")
    ea = extract_audio(ffmpeg, edited_path)
    oa = extract_audio(ffmpeg, original_path)
    edit_dur, orig_dur = len(ea) / SR, len(oa) / SR
    log.info("      편집본 %.1fs, 원본 %.1fs", edit_dur, orig_dur)

    log.info("[match 2/4] 특징 계산 중...")
    ef_raw, of = features(ea), features(oa)
    win_f, stride_f = int(WINDOW_SEC * fps), int(STRIDE_SEC * fps)
    max_stretch = max(speeds) if speeds else 1.0
    matcher = Matcher(of, int(win_f * max_stretch) + 8)

    speed = speeds[0] if speeds else 1.0
    if len(speeds) > 1:
        log.info("[match 3/4] 배속 판별 중...")
        speed = detect_speed(matcher, ef_raw, speeds, win_f, stride_f)
        log.info("      판별된 배속: %.2fx", speed)
    ef = time_stretch(ef_raw, speed)

    log.info("[match 4/4] 구간 매칭 중 (창 %.0fs / 보폭 %.0fs)...", WINDOW_SEC, STRIDE_SEC)
    wins = match_windows(matcher, ef, win_f, stride_f)
    tol_f = int(OFFSET_TOL_SEC * fps)
    if len(speeds) > 1:
        refined = refine_speed_from_drift(wins, speed, tol_f)
        if abs(refined - speed) / speed > 0.004:
            log.info("      오프셋 드리프트로 배속 보정: %.3fx -> %.3fx, 재매칭", speed, refined)
            speed = round(refined, 3)
            ef = time_stretch(ef_raw, speed)
            wins = match_windows(matcher, ef, win_f, stride_f)
    for w in wins:
        log.debug("      edit %6.2fs -> orig %8.2fs  ncc %.3f (2위 %.3f)",
                  w["edit_start"] / fps, w["orig_pos"] / fps, w["ncc"], w["second"])
    group_windows(wins, tol_f)          # unmatched 표시
    wins = fine_pass(matcher, ef, wins, fps)
    segs, holes = build_segments(wins, ef, of, tol_f, int(MIN_SEGMENT_SEC * fps))

    warnings = []
    out = []
    for s in segs:
        o_s, o_e = max(0.0, s["orig_start_f"] / fps), min(orig_dur, s["orig_end_f"] / fps)
        if o_e - o_s < MIN_SEGMENT_SEC:
            continue
        # 편집본 시간은 늘린(원본 템포) 축이므로 speed로 나눠 실제 편집본 시간으로
        out.append({
            "start": round(o_s, 3), "end": round(o_e, 3),
            "edit_start": round(s["edit_start_f"] / fps / speed, 3),
            "edit_end": round(s["edit_end_f"] / fps / speed, 3),
            "confidence": s["confidence"], "low_confidence": s["low_confidence"],
        })
    covered = sum(x["edit_end"] - x["edit_start"] for x in out)
    coverage = round(covered / edit_dur, 3) if edit_dur else 0.0
    low = [x for x in out if x["low_confidence"]]
    if low:
        warnings.append(f"신뢰도 낮은 구간 {len(low)}개 — 확인 필요: " +
                        ", ".join(f"{x['start']:.1f}~{x['end']:.1f}s" for x in low))
    holes_out = [{"edit_start": round(a / fps / speed, 3), "edit_end": round(b / fps / speed, 3)} for a, b in holes]
    if holes_out:
        warnings.append("편집본에서 원본을 못 찾은 구간(드래프트에서 빠짐): " +
                        ", ".join(f"{h['edit_start']:.1f}~{h['edit_end']:.1f}s" for h in holes_out))
    if coverage < 0.9:
        warnings.append(f"편집본의 {coverage * 100:.0f}%만 원본에서 찾았습니다.")
    if not out:
        raise PipelineError("AUDIO_MATCH_FAILED", "편집본과 일치하는 구간을 원본에서 찾지 못했습니다.",
                            suggested_action="원본 링크가 맞는지, 편집본에 원본 오디오가 남아있는지 확인")
    return {"speed": round(speed, 3), "segments": out, "unmatched": holes_out, "coverage": coverage,
            "edit_duration": round(edit_dur, 2), "original_duration": round(orig_dur, 2), "warnings": warnings}


def main():
    ap = argparse.ArgumentParser(description="편집본 → 원본 구간 자동 매칭")
    ap.add_argument("edited", help="편집본 영상/오디오 파일")
    ap.add_argument("original", help="원본 파일 경로 또는 유튜브 URL")
    ap.add_argument("--speeds", default=",".join(map(str, DEFAULT_SPEEDS)),
                    help="배속 후보 (쉼표 구분). '1.0'만 주면 배속 판별 생략")
    ap.add_argument("--emit-request", help="run_pipeline.py용 request.json을 이 경로에 저장")
    ap.add_argument("--project-name")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    try:
        original = args.original
        if is_url(original):
            import link_to_capcut as ltc
            local, _, _ = ltc.resolve_source(original, ltc.WORK_DIR)
            original_local = local
        else:
            original_local = Path(original)
        res = match(args.edited, original_local, speeds=[float(x) for x in args.speeds.split(",")])
    except PipelineError as e:
        sys.exit(f"[error:{e.code}] {e.message}")

    for w in res["warnings"]:
        print(f"[주의] {w}", file=sys.stderr)
    if args.emit_request:
        req = {
            "source": args.original,
            "segments": [{"start": s["start"], "end": s["end"], "low_confidence": s["low_confidence"]}
                         for s in res["segments"]],
            "options": {"speed": res["speed"], "silence_cut": False},
            "project_name": args.project_name,
        }
        Path(args.emit_request).write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[저장] {args.emit_request}", file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
