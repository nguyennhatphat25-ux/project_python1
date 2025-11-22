FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_APP=app.py
EXPOSE 5000
CMD ["gunicorn", "-b", ":5000", "app:app"]