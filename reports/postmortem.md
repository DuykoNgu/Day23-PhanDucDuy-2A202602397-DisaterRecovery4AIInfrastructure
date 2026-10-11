# Postmortem — diễn tập DR ngày 11/10/2026

Đây là diễn tập local, không phải sự cố khách hàng thật. Mục tiêu là kiểm tra khả năng phục hồi bằng timestamp của request và ước lượng dữ liệu thực sự thiếu ở replica. Giờ dưới đây là UTC (giờ Việt Nam +7).

**Baseline (bước 1):** Trước DR, không có thành phần nào tự phát hiện A ngừng phục vụ để chuyển tuyến; edge chỉ đọc pointer `active_region` (`edge/proxy.py`). B có process sống nhưng 0 vector, không có weights và pool `warm` (kiểm tra `/v1/state` trước drill). Nếu đổi pointer sang B ngay, `/readyz` trả 503 và inference trả `region_not_ready` (`serving/app.py`). Với `interval=5s`, `threshold=3`, detection floor là 15s; RTO 300s chỉ khả thi khi phần này cộng restore, warm-up và TTL vẫn dưới 300s.

## 1. Timeline

| UTC | Sự kiện | Evidence |
|---|---|---|
| 16:11:00.450 | Region A bị `netblock` | `chaos/chaos-events.jsonl:7` |
| 16:11:00.522 | User request đầu tiên lỗi | `reports/drill-2-withdr.jsonl:25` |
| 16:11:19.561 | Health checker phát hiện A `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 16:11:22.052 | Runbook xác nhận và mở incident; notification delay 21.6s | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| 16:11:22.065 | Snapshot restore xong; RPO 10.03s / 5 documents | `reports/failover-events.jsonl:2` |
| 16:11:28.282 | B ready, IC cutover sang B | `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5` |
| 16:11:28.735 | Request đầu tiên thành công từ B; resolved | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO và gap so với mục tiêu

- **RTO:** mục tiêu 300s, đo được **28.3s**, còn dư **271.7s** so với giới hạn (`reports/measure-drill-2.json`). 14 request lỗi sau outage trước khi B phục vụ (`reports/measure-drill-2.json`).
- **RPO:** mục tiêu 300s, đo được **10.03s** và **5 documents** có ở A nhưng không có trong snapshot khôi phục ở B; còn dư **289.97s** so với giới hạn giây (`reports/failover-events.jsonl:2`).
- **Gap lớn nhất trong RTO:** health detection mất **19.1s**, chiếm **67.5%** RTO. Floor cấu hình là 5s × 3 = **15s**; phần còn lại do pha polling và timeout (`reports/health-events.jsonl:2`). Snapshot/xác nhận tốn 2.5s; warm-up 6.2s; TTL/request kế tiếp 0.5s (`reports/rto-evidence.md`).

## 3. Root cause — 5 whys

1. Vì sao request lỗi? Region A dừng phản hồi, trong khi edge vẫn định tuyến tới A (`chaos/chaos-events.jsonl:7`, `reports/drill-2-withdr.jsonl:25`).
2. Vì sao edge không tự chuyển ngay? Pointer của edge chỉ đổi khi runbook viết `active_region`; đây là thiết kế bán tự động để tránh flapping (`edge/proxy.py`, `reports/failover-events.jsonl:5`).
3. Vì sao cần đợi health checker? Một probe timeout có thể là lỗi thoáng qua; ngưỡng 3 lần liên tiếp mới cho phép xử lý (`reports/health-events.jsonl:2`).
4. Vì sao B chưa nhận traffic ngay sau alert? B ban đầu không có weights/vector và pool mới ở `warm`; phải restore snapshot và đợi `/readyz` trả 200 (`reports/failover-events.jsonl:1`, `reports/failover-events.jsonl:4`).
5. Vì sao có 5 documents thiếu? Replication chạy mỗi 30 giây, snapshot cuối trước restore không bao gồm các document ingest sau đó (`reports/replication.jsonl:2`, `reports/failover-events.jsonl:2`).

Runbook có thể thất bại nếu snapshot không tồn tại, bị hỏng, hoặc B không ready trong 60s. Trong các trường hợp đó, `failover.py` phải dừng trước cutover; IC quyết định phục hồi A hoặc chọn nguồn snapshot khác, không chuyển traffic sang B chưa sẵn sàng.

## 4. Action items

| # | Việc cần làm | Owner | Hạn | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thử giữ B ở trạng thái `full` với weights/vector được replicate liên tục và diễn tập lại | platform on-call | 12/10/2026 | Có thể bỏ khoảng 6.2s warm-up, đổi lại chi phí standby cao hơn |
| 2 | Thêm kiểm tra integrity/version vào quy trình snapshot trước khi báo sẵn sàng | data/ML platform | 13/10/2026 | Giảm rủi ro restore sai; chưa có cơ sở định lượng số giây RPO/RTO giảm |
| 3 | Thử replication mỗi 10s và đo lại docs lost dưới cùng mức ingest | data platform | 13/10/2026 | Kỳ vọng giảm cửa sổ mất dữ liệu tối đa từ ~30s xuống ~10s; cần đo lại |

## 5. Câu hỏi phản tư

1. `interval × threshold = 5s × 3 = 15s`, chiếm **53.0%** RTO 28.3s. Detection thực tế 19.1s, chiếm 67.5%. Để đạt RTO 300s, interval phải chừa thời gian cho restore, warm-up, TTL và thao tác; dựa trên lần đo này, floor lý thuyết cho phép khoảng `(300−9.2)/3 ≈ 96.9s`, nhưng không nên cấu hình sát trần vì timeout và độ lệch polling.
2. Hạ interval xuống 1s làm floor từ 15s còn 3s, có thể giảm khoảng **12s** nếu các phần khác giữ nguyên; phải đo lại mới kết luận. Đổi lại tần suất probe tăng 5 lần, tăng tải và khả năng ba lỗi thoáng qua bị hiểu nhầm là outage; vẫn cần ngưỡng liên tiếp và quyền xác nhận của IC.
3. Nếu A mất vĩnh viễn, 5 documents đó là 5 cập nhật gần nhất khách hàng không thấy ở B. Chúng không thể phục hồi từ snapshot đang có; cần replay từ nguồn ingest độc lập hoặc chấp nhận mất dữ liệu và thông báo phạm vi ảnh hưởng. Sau 6 giờ, con số có thể lớn hơn nếu replication đã dừng; **5 chỉ là số đo tại thời điểm restore của drill này**.
