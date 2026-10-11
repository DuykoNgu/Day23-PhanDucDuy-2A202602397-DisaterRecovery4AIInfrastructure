# Runbook — Region A không phục vụ được inference

Chạy tại thư mục gốc repo. Người trực ca (on-call) sở hữu incident; incident commander (IC) quyết định failover và rollback. Giữ một terminal cho load generator, health checker và replication như trong `GUIDE.md` trước khi diễn tập.

| # | Bước | Lệnh copy-paste | Tín hiệu hoàn tất | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `curl -sS -m 3 localhost:8001/readyz; curl -sS -m 3 localhost:8002/healthz; tail -n 5 reports/health-events.jsonl` | A có `UNHEALTHY` sau 3 lỗi liên tiếp; B còn trả lời `/healthz` | on-call |
| 2 | Mở incident, ghi mốc và xác nhận cutover | `python3 dr/runbook.py --primary a --target b --backend fs` | Sau khi nhập `y`, `reports/runbook-run.jsonl` có `thong_bao_incident` và `outage_ts`; lệnh này thực hiện các bước 3–7 đúng một lần | IC |
| 3 | Khôi phục snapshot | `tail -n 5 reports/failover-events.jsonl` | Có `2_restore_snapshot`, `rpo_seconds`, `docs_lost`, `embed_model_version` | on-call |
| 4 | Scale GPU pool và đợi sẵn sàng | `curl -sS -m 3 -i localhost:8002/readyz; tail -n 3 reports/failover-events.jsonl` | HTTP 200 và dòng `4_wait_ready` có `ok:true` | on-call |
| 5 | Cutover DNS/LB | `cat edge/active_region; curl -sS localhost:8080/edge/state; tail -n 1 reports/failover-events.jsonl` | Pointer là `b`; sau tối đa 5 giây cache, edge báo `active_region:b`; có `5_dns_cutover` | IC |
| 6 | Kiểm tra golden signals | `curl -sS localhost:8080/v1/infer; tail -n 2 reports/runbook-run.jsonl` | Request qua edge trả `region:b`; bước `verify_golden_signals` ghi 10 request, `error_rate:0`, p95 latency | on-call |
| 7 | Đo RTO/RPO và đóng incident | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `rto_verdict:PASS`, có RPO giây và `docs_lost`; ghi vào `reports/rto-evidence.md` và `reports/postmortem.md` | IC |

**Dừng trước cutover:** Nếu B không có snapshot, weights/vector không hợp lệ, hoặc `/readyz` không trả 200 trong 60 giây, giữ pointer ở A và điều tra; `failover.py` sẽ không tự đổi pointer. Nếu đã cutover mà B có lỗi inference hoặc mất dữ liệu ngoài giới hạn được chấp nhận, IC có quyền ra lệnh rollback **chỉ sau khi A phục hồi, đồng bộ dữ liệu và `/readyz` của A trả 200 ổn định**. Khi đó chạy `python3 chaos/kill_region.py restore --region a --backend bare`, kiểm tra `curl -sS -i localhost:8001/readyz`, rồi IC chạy `printf a > edge/active_region` và kiểm tra `curl -sS localhost:8080/edge/state` sau tối đa 5 giây; theo dõi thêm 10 request. Không tự động chuyển qua lại khi health chập chờn.
