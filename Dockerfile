# Occuris Echo — runs the agent + page. Mount your own .env / token.json; nothing is baked in.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY echo/ echo/
COPY static/ static/
COPY tests/ tests/
COPY server.py .
EXPOSE 8000
# Sandbox by default (no credentials needed). For real providers: -e ECHO_TWINS=0 and mount .env, token.json.
ENV ECHO_TWINS=1 ECHO_HOST=0.0.0.0
CMD ["python", "server.py"]
