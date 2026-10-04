FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home botuser
COPY bot ./bot
RUN mkdir /app/data && chown botuser:botuser /app/data
USER botuser
CMD ["python", "-m", "bot.main"]
