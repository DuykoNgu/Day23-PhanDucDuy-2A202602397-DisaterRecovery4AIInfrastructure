"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw: object) -> dict[str, object]:
    now = time.time()
    event = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event), flush=True)
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    if target not in URL or wait <= 0:
        raise ValueError("invalid target or wait")
    source = "b" if target == "a" else "a"
    try:
        before = state_of(target)
        emit(step="1_verify_target", target=target, state=before)
        meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        emit(step="2_restore_snapshot", target=target, **rpo,
             embed_model_version=meta["embed_model_version"], snapshot_at=meta["snapshot_at"])
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("full\n")
        emit(step="3_scale_pool", target=target, pool_state="full")
        started = time.monotonic()
        ready = False
        while time.monotonic() - started < wait:
            try:
                ready = httpx.get(f"{URL[target]}/readyz", timeout=2).status_code == 200
            except httpx.HTTPError:
                ready = False
            if ready:
                break
            time.sleep(0.5)
        emit(step="4_wait_ready", target=target, ok=ready,
             waited_s=round(time.monotonic() - started, 2))
        if not ready:
            return {"ok": False, "reason": "target_not_ready", "target": target}
        restored_state = state_of(target)
        if not restored_state.get("weights") or restored_state.get("count", 0) < 1:
            return {"ok": False, "reason": "target_state_invalid", "target": target}
        pathlib.Path("edge/active_region").write_text(target)
        emit(step="5_dns_cutover", target=target, ok=True)
        return {"ok": True, "target": target, "state": restored_state, **rpo,
                "embed_model_version": meta["embed_model_version"]}
    except (httpx.HTTPError, OSError, KeyError, ValueError, sqlite3.DatabaseError, SystemExit) as error:
        emit(step="abort", target=target, reason=str(error))
        return {"ok": False, "target": target, "reason": str(error)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
