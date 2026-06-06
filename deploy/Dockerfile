# Scorer service image.
#
# Build context MUST be the repo root (not deploy/) so the image can COPY the
# scoring scripts that live under scripts/. The compose override sets
# `context: ../claude_code_o11y_llm` for exactly this reason.
FROM python:3.12-slim

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
