FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py redact_patterns.json ./

# 公開APIをrootで動かさない(アプリはファイル書き込み不要なのでchownも不要)。
# compose 側は user: ${INGEST_UID} でさらに上書きする(file secretsが
# ホスト権限のままマウントされるため、所有uidと一致させる必要がある)
RUN useradd --system --no-create-home app
USER app

EXPOSE 8800
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8800"]
