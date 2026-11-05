# CodeCraft 2020 game engine: the official local runner (aicup2020 1.3.0) in headless batch mode.
# The binary only needs glibc >= 2.18; assets are only used by the GUI, so they are not copied.
FROM debian:bookworm-slim
RUN mkdir -p /engine /game
COPY aicup2020 /engine/aicup2020
RUN chmod +x /engine/aicup2020
WORKDIR /game
ENTRYPOINT ["/engine/aicup2020"]
