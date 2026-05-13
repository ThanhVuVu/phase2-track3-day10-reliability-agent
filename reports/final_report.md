# Báo cáo Cuối cùng về Độ tin cậy (Day 10)

## 1. Tóm tắt Kiến trúc

Hệ thống là một gateway độ tin cậy chuẩn sản xuất cho các LLM agent, bao gồm kiến trúc phòng thủ nhiều lớp:

- **Lớp Cache (Bộ nhớ đệm)**: Chặn các yêu cầu để cung cấp phản hồi gần như tức thì cho các truy vấn tương tự, giúp giảm cả độ trễ và chi phí. Hỗ trợ cả backend in-memory (bộ nhớ trong) và Redis.
- **Lớp Circuit Breaker (Ngắt mạch)**: Bảo vệ các nhà cung cấp (providers) hạ nguồn bằng cách ngắt kết nối nhanh khi đạt tới ngưỡng lỗi, cho phép hệ thống có thời gian phục hồi.
- **Chuỗi Fallback (Dự phòng)**: Điều phối nhiều nhà cung cấp LLM (Chính -> Dự phòng) để đảm bảo tính sẵn sàng cao ngay cả khi một nhà cung cấp gặp sự cố.
- **Fallback Tĩnh**: Cung cấp thông báo lỗi thân thiện khi tất cả các nhà cung cấp và bộ nhớ đệm đều thất bại.

```
Yêu cầu từ Người dùng
    |
    v
[Gateway] ---> [Kiểm tra Cache (Memory/Redis)] ---> HIT? Trả về kết quả từ cache
    |                                               |
    v                                               v MISS
[Ngắt mạch: Chính] ---------------------------> Nhà cung cấp A
    |  (OPEN? bỏ qua)
    v
[Ngắt mạch: Dự phòng] ------------------------> Nhà cung cấp B
    |  (OPEN? bỏ qua)
    v
[Thông báo fallback tĩnh]
```

## 2. Cấu hình (Configuration)

| Tham số | Giá trị | Lý do |
|---|---:|---|
| failure_threshold | 3 | Cân bằng giữa việc phát hiện lỗi nhanh và khả năng chống nhiễu. |
| reset_timeout_seconds | 2 | Cung cấp một khoảng thời gian ngắn để nhà cung cấp phục hồi sau sự cố. |
| success_threshold | 1 | Một lần thăm dò thành công là đủ để đóng mạch lại trong bài lab này. |
| cache TTL | 300 | Giữ dữ liệu tươi mới trong 5 phút cho các truy vấn phổ biến. |
| similarity_threshold | 0.92 | Được tinh chỉnh bằng n-grams để phân biệt các khác biệt nhỏ (ví dụ: năm). |
| load_test requests | 200 | Tải cao hơn để kiểm tra độ ổn định và các chỉ số p99. |

## 3. Định nghĩa SLO (SLO definitions)

| Chỉ số (SLI) | Mục tiêu SLO | Giá trị Thực tế | Đạt? |
|---|---|---:|---|
| Tính sẵn sàng (Availability) | >= 99% | 99.4% | ✅ CÓ |
| Độ trễ P95 (Latency) | < 1000 ms | 509.8 ms | ✅ CÓ |
| Tỷ lệ Fallback thành công | >= 90% | 93.7% | ✅ CÓ |
| Tỷ lệ Cache hit | >= 20% | 79.0% | ✅ CÓ |
| Thời gian phục hồi | < 10000 ms | 2285.0 ms | ✅ CÓ |

## 4. Chỉ số đo lường (Metrics - Redis Backend)

Kết quả từ mô phỏng 800 yêu cầu cuối cùng sử dụng **Redis Shared Cache**:

| Chỉ số | Giá trị |
|---|---:|
| Tổng số yêu cầu (total_requests) | 800 |
| Tính sẵn sàng (availability) | 0.9938 |
| Tỷ lệ lỗi (error_rate) | 0.0063 |
| Độ trễ p50_ms | 234.0 |
| Độ trễ p95_ms | 509.8 |
| Độ trễ p99_ms | 531.1 |
| Tỷ lệ fallback thành công | 0.9367 |
| Tỷ lệ cache hit | 0.7900 |
| Chi phí tiết kiệm được (ước tính) | 0.632 |
| Số lần ngắt mạch (circuit_open_count) | 9 |
| Thời gian phục hồi (recovery_time_ms) | 2285.0 |

## 5. So sánh Cache

So sánh hiệu suất giữa baseline (không có cache) và gateway sản xuất (có Redis cache):

| Chỉ số | Không có cache | Có Redis cache | Thay đổi (Delta) |
|---|---:|---:|---|
| latency_p50_ms | 226.5 | 234.0 | +3.3%* |
| latency_p95_ms | 531.0 | 509.8 | -4% |
| Chi phí ước tính | 0.0494 | 0.0806 | +63.1%* |
| Tỷ lệ cache hit | 0 | 0.7900 | +79% |

*\*Ghi chú: Chi phí và p50 cao hơn trong trường hợp "Có cache" là do khối lượng yêu cầu cao hơn đáng kể (800 so với 100) và các kịch bản chaos hoạt động mạnh hơn trong đợt chạy cuối cùng.*

## 6. Redis Shared Cache

### Tại sao Shared Cache lại quan trọng?
Bộ nhớ đệm in-memory chỉ có tác dụng cục bộ trong một tiến trình. Trong các triển khai đa instance (như Kubernetes), nếu một instance lưu cache, các instance khác vẫn bị "lạnh" (cold). `SharedRedisCache` đảm bảo tất cả các instance chia sẻ cùng một trạng thái, giúp tăng tỷ lệ hit rate toàn cục và giảm chi phí dư thừa cho nhà cung cấp.

### Bằng chứng triển khai
`SharedRedisCache` đã được triển khai với:
- **Hash mapping**: Lưu trữ cả truy vấn và phản hồi trong một Redis Hash.
- **TTL**: Tự động dọn dẹp bằng lệnh `EXPIRE` của Redis.
- **Similarity Scanning**: Duyệt qua các key bằng `SCAN` để tìm kiếm sự tương đồng về ngữ nghĩa.
- **Lớp bảo vệ quyền riêng tư**: Bỏ qua cache cho các từ khóa nhạy cảm (ví dụ: "password", "ssn").

```python
# Đoạn mã triển khai từ cache.py
key = f"{self.prefix}{self._query_hash(query)}"
mapping = {"query": query, "response": value}
self._redis.hset(key, mapping=mapping)
self._redis.expire(key, self.ttl_seconds)
```

## 7. Các kịch bản Chaos (Chaos scenarios)

| Kịch bản | Hành vi kỳ vọng | Hành vi quan sát được | Đạt/Thất bại |
|---|---|---|---|
| primary_timeout_100 | Toàn bộ traffic fallback, mạch ngắt | Traffic được chuyển hướng thành công sang backup | ✅ ĐẠT |
| primary_flaky_50 | Mạch dao động | Ghi nhận nhiều lần chuyển đổi trạng thái liên tục | ✅ ĐẠT |
| all_healthy | Toàn bộ yêu cầu qua primary | Tỷ lệ thành công 100% trên primary | ✅ ĐẠT |
| cache_stale_candidate | Tỷ lệ cache hit cao | Logic tương đồng đã bắt được các biến thể | ✅ ĐẠT |

## 8. Phân tích lỗi (Failure analysis)

**Điểm yếu còn tồn tại**: `SharedRedisCache` hiện tại sử dụng `SCAN` để tìm kiếm các truy vấn tương tự.
- **Rủi ro**: Khi bộ nhớ đệm tăng lên hàng triệu bản ghi, việc `SCAN` cộng với tính toán tương đồng cục bộ sẽ trở thành nút thắt cổ chai, làm tăng độ trễ và tải cho Redis.
- **Giải pháp đề xuất**: Triển khai **Vector Similarity Search** sử dụng RedisVL hoặc một cơ sở dữ liệu vector chuyên dụng (như Pinecone/Milvus). Lưu trữ các query embedding và thực hiện tìm kiếm hàng xóm gần nhất (top-K) thay vì quét toàn bộ.

## 9. Các bước tiếp theo (Next steps)

1. **Tích hợp Async**: Chuyển đổi gateway sang `asyncio` để xử lý các yêu cầu đồng thời mà không gây nghẽn.
2. **Ngắt mạch phân tán (Distributed Circuit Breaker)**: Chuyển trạng thái mạch (số lần lỗi) lên Redis để các instance có thể "học" lỗi từ nhau.
3. **Xử lý lỗi Redis mềm dẻo (Graceful Degradation)**: Triển khai cơ chế fallback ngược về in-memory cache nếu Redis không thể kết nối để tránh làm sập gateway.