# Build from the repo root: docker build -f board/Dockerfile .
# One image, two jobs: `python server/pxtrelay.py` (the relay) or `python board/board.py` (the board).
FROM python:3.12-slim
WORKDIR /app
COPY board/requirements.txt board/requirements.txt
RUN pip install --no-cache-dir -r board/requirements.txt
COPY server/pxtrelay.py server/
COPY tools/font46.py tools/
COPY board/agent.py board/board.py board/layouts.py board/preview.py board/
ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "board/board.py"]
