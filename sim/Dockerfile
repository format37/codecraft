# CodeCraft 2020 game engine: our own (sim/codecraft_sim; Python, standard library only), with the command line of
# the official local runner aicup2020 1.3.0 in batch mode. Built by `./cw build` when config.yaml names it
# (engine.image codecraft/sim:<version>, engine.context sim), or by hand: docker build -t codecraft/sim:0.1.0 sim
FROM python:3.12-slim
ENV PYTHONPATH=/engine PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY codecraft_sim /engine/codecraft_sim
RUN python -m compileall -q /engine/codecraft_sim && mkdir -p /game
WORKDIR /game
ENTRYPOINT ["python", "-m", "codecraft_sim"]
