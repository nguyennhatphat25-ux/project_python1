FROM python:3.11-slim

WORKDIR /app

# Cài đặt các gói system cần thiết cho MySQL
RUN apt-get update && apt-get install -y \
    default-libmysqlclient-dev \
    build-essential \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements trước để tận dụng cache layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ code
COPY . .

# Tạo thư mục cần thiết
RUN mkdir -p static uploads/projects uploads/submissions

# Biến môi trường cho Flask
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Port mặc định của ứng dụng
EXPOSE 5000

# Chạy Flask với gunicorn trong production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:create_app()"]
