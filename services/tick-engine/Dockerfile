FROM python:3.12-slim

WORKDIR /app
COPY extract.py cluster.py markers.py brain_keeper.py \
     brain_tick_prompt.py brain_apply.py brain_tick.py amygdala.py ./

RUN pip install --no-cache-dir redis

ENTRYPOINT ["python3"]
