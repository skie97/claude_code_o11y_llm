# Scorer service image.
#
# Build context MUST be the repo root (not deploy/) so the image can COPY the
# scoring scripts that live under scripts/. The compose override sets
# `context: ../claude_code_o11y_llm` for exactly this reason.
FROM python:3.12-slim

# Unbuffered stdout/stderr so `docker logs -f scorer` streams each stage's progress
# live. Without this, Python block-buffers stdout when piped (not a TTY), so the
# score/push prints stay invisible until the job exits — making a long real-judge
# run look hung when it is actually working.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The scoring brain. analyze.py / model_scoring.py / score_model_fit.py /
# project_attribution.py / push_scores_to_loki.py and their imports.
COPY scripts/ /app/scripts/
COPY deploy/run_scorer.sh /app/run_scorer.sh
RUN chmod +x /app/run_scorer.sh

# data/ is provided at runtime (mounted volume now; in-cluster HTTP fetch later).
ENTRYPOINT ["/app/run_scorer.sh"]
