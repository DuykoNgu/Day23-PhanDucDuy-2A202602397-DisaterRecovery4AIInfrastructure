"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw: object) -> dict[str, object]:
    now = time.time()
    event = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
             "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event), flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    return auto or input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be distinct known regions")
    started = time.time()
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    events = [json.loads(line) for line in chaos.read_text().splitlines() if line.strip()] if chaos.exists() else []
    kills = [event for event in events if event.get("action") == "kill"]
    outage = next((event for event in reversed(kills) if event.get("region") == primary), None)
    if outage is None:
        return {"ok": False, "reason": "no_outage_event"}
    health_log = pathlib.Path("reports/health-events.jsonl")
    detected = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if health_log.exists():
            detected = next((event for line in reversed(health_log.read_text().splitlines())
                             if line.strip() for event in [json.loads(line)]
                             if event.get("event") == "state_change"
                             and event.get("region") == primary and event.get("to") == "UNHEALTHY"
                             and event["ts"] >= outage["ts"]), None)
        if detected:
            break
        time.sleep(0.5)
    if not detected:
        return {"ok": False, "reason": "health_checker_did_not_detect_outage"}
    primary_ready, reason = probe(primary, 2)
    target_ready, target_reason = probe(target, 2)
    step(1, "xac_nhan_outage", primary_ready=primary_ready, reason=reason,
         target_ready=target_ready, target_reason=target_reason, detected_at=detected["ts"])
    if primary_ready:
        return {"ok": False, "reason": "primary_is_ready"}
    if not confirm(auto, f"Region {primary} down. Fail over to {target}?"):
        return {"ok": False, "reason": "operator_declined"}
    step(2, "thong_bao_incident", outage_ts=outage["ts"],
         notification_delay_s=round(time.time() - outage["ts"], 2))
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", result=result)
    if not result.get("ok"):
        return result
    replica = result["state"]
    step(4, "verify_state_replica", count=replica["count"], weights=replica["weights"],
         embed_model_version=result["embed_model_version"])
    active = pathlib.Path("edge/active_region").read_text().strip()
    step(5, "dns_cutover", active_region=active, ok=active == target)
    if active != target or not replica["weights"] or replica["count"] < 1:
        return {"ok": False, "reason": "post_cutover_verification_failed"}
    latencies = []
    errors = 0
    for _ in range(10):
        start = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3)
            errors += response.status_code != 200
        except httpx.HTTPError:
            errors += 1
        latencies.append((time.monotonic() - start) * 1000)
    p95 = sorted(latencies)[9]
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 1),
         error_rate=errors / 10)
    summary = {"ok": errors == 0, "target": target, "elapsed_s": round(time.time() - started, 2),
               "measure_command": "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"}
    step(7, "post_incident", **summary)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
