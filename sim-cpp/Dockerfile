# CodeCraft 2020 game engine: our own, in C++ (sim-cpp; the standard library only), with the command line of the
# official local runner aicup2020 1.3.0 in batch mode; it plays exactly the games of the Python engine in sim/.
# The server builds it (config.yaml: engine.image codecraft/sim-cpp, engine.context sim-cpp) from this Dockerfile
# and the files it copies, and tags it with their hash, the version the engine reports (the Makefile: `make
# version`). By hand: docker build -t codecraft/sim-cpp:dev sim-cpp
# The same image is everyone's local runner: .github/workflows/runner.yml publishes it as
# ghcr.io/format37/codecraft-runner:<version> and :latest (docs/QUICKSTART.md has the docker run line).
FROM gcc:13.3.0 AS build
WORKDIR /src
COPY Dockerfile Makefile ./
COPY src ./src
COPY viewer ./viewer
RUN make -j"$(nproc)" LDFLAGS="-pthread -static-libstdc++ -static-libgcc"

FROM debian:bookworm-slim
LABEL org.opencontainers.image.source="https://github.com/format37/codecraft" \
      org.opencontainers.image.description="CodeCraft 2020: the game engine and the local runner (the viewer on port 31000, strategies on 31001-31004)" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /src/build/codecraft-sim-cpp /engine/codecraft-sim-cpp
RUN mkdir -p /game
WORKDIR /game
# In a container the runner listens on every address (`docker -p` brings the host's 127.0.0.1 here) and opens no
# browser (main.cpp reads this); the server's batch mode ignores it.
ENV CODECRAFT_RUNNER_CONTAINER=1
EXPOSE 31000 31001 31002 31003 31004
ENTRYPOINT ["/engine/codecraft-sim-cpp"]
