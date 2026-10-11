# RTO/RPO evidence — Lab 23

Các mốc dưới đây được đo từ chính lần chạy ngày 11/10/2026 (UTC). Đồng hồ RTO là timestamp của request từ load generator, không phải thời gian in ở terminal.

## 1. Drill 1 — không có DR

| Chỉ số | Đo được | Evidence |
|---|---:|---|
| A bị chặn (`netblock`) | 2026-10-11T16:06:35Z | `chaos/chaos-events.jsonl:3` |
| Request lỗi đầu tiên | +0.0s (timestamp request +0.035s), `ReadTimeout` | `reports/drill-1-nodr.jsonl:17` |
| Request phục hồi | Không có trong thời gian tải; 16 request lỗi | `reports/drill-1-nodr.jsonl:32`, `reports/measure-drill-1.json` |
| RTO | **NO_RECOVERY** | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | Giây từ outage | Evidence |
|---|---:|---|
| A bị chặn, mốc 0 (2026-10-11T16:11:00Z) | 0.0s | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi đầu tiên | +0.1s | `reports/drill-2-withdr.jsonl:25` |
| Health checker báo A `UNHEALTHY` | +19.1s | `reports/health-events.jsonl:2` |
| Snapshot đã restore vào B | +21.6s | `reports/failover-events.jsonl:2` |
| B hoàn tất warm-up và ready | +27.8s | `reports/failover-events.jsonl:4` |
| DNS/LB pointer cutover sang B | +27.8s | `reports/failover-events.jsonl:5` |
| Request đầu tiên thành công từ B | **+28.3s** | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu | Kết luận | Evidence |
|---|---:|---:|---|---|
| RTO inference API | **28.3s** | 300s | PASS | `reports/measure-drill-2.json`, `reports/drill-2-withdr.jsonl:39` |
| RPO vector DB tại lúc restore | **10.03s / 5 documents** | 300s | PASS | `reports/failover-events.jsonl:2`, `reports/replication.jsonl:2` |

`tools/measure_rto.py` xác nhận `valid:true`, `warnings:[]`, region bị kill là A và request phục hồi do B phục vụ (`reports/measure-drill-2.json`).

## 3. Phân rã RTO thực đo

| Thành phần | Giây | Cách tính và evidence | Cách giảm |
|---|---:|---|---|
| Health-check detection | 19.1s | Outage → `UNHEALTHY`; cấu hình `interval_s=5`, `threshold=3`, floor 15.0s. `chaos/chaos-events.jsonl:7`, `reports/health-events.jsonl:2` | Giảm interval hoặc timeout, cân nhắc false alarm |
| Xác nhận của operator và restore snapshot | 2.5s | `UNHEALTHY` → `3_scale_pool`; bản thân `get` mất khoảng 0.005s; phần lớn là probe A bị timeout. `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:1`, `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:3` | Dùng trạng thái health checker đã xác nhận để bỏ probe lặp |
| GPU pool warm-up | 6.2s | `3_scale_pool` → `4_wait_ready` (`waited_s=6.2`). `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4` | Giữ capacity warm hoặc dự trữ pool |
| DNS/LB TTL và lần request kế tiếp | 0.5s | `5_dns_cutover` → request B đầu tiên; TTL cấu hình 5s nhưng cache đã gần hết hạn. `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:39` | TTL thấp hơn nếu cần RTO thấp hơn |
| **Tổng** | **28.3s** | Tổng bốn thành phần làm tròn 1 chữ số, khớp RTO do công cụ đo. `reports/measure-drill-2.json` | |

Snapshot được tạo trước outage bởi replication 30 giây/lần (`reports/replication.jsonl:2`); tại lúc restore, primary có 5 document mới hơn snapshot (`reports/failover-events.jsonl:2`).
