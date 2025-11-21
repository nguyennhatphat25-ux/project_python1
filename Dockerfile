# 1. Chọn hệ điều hành nền (Base Image)
# Dùng python 3.9 bản slim cho nhẹ giống ý bạn
FROM python:3.9-slim

# 2. Thiết lập thư mục làm việc
# Mọi lệnh sau này sẽ chạy trong thư mục /app của container
WORKDIR /app

# 3. Cài đặt thư viện (Tận dụng Cache)
# Copy file requirements trước để Docker cache lại các thư viện,
# giúp lần build sau nhanh hơn nếu không đổi thư viện
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# Cài thêm gunicorn và pymysql như bạn đã phân tích
RUN pip install gunicorn pymysql cryptography

# 4. Nạp Code nguồn
# Vì dự án của bạn file app.py nằm ngay bên ngoài, nên ta COPY tất cả (.) vào (.)
COPY . .

# 5. Cấu hình biến môi trường (Optional)
# Để Flask biết file chạy chính là app.py
ENV FLASK_APP=app.py

# 6. Mở cổng
# Container sẽ lắng nghe ở cổng 5000
EXPOSE 5000

# 7. Lệnh chạy ứng dụng (ENTRYPOINT/CMD)
# Vì bạn chưa có file boot.sh, ta chạy trực tiếp bằng python
# Hoặc dùng gunicorn cho "xịn" (production grade)
CMD ["gunicorn", "-b", ":5000", "app:app"]