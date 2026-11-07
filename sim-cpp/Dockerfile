# CodeCraft 2020 game engine: our own, in C++ (sim-cpp; the standard library only), with the command line of the
# official local runner aicup2020 1.3.0 in batch mode; it plays exactly the games of the Python engine in sim/.
# The server builds it (config.yaml: engine.image codecraft/sim-cpp, engine.context sim-cpp) from this Dockerfile
# and the files it copies, and tags it with their hash, the version the engine reports (the Makefile: `make
# version`). By hand: docker build -t codecraft/sim-cpp:dev sim-cpp
FROM gcc:13.3.0 AS build
WORKDIR /src
COPY Dockerfile Makefile ./
COPY src ./src
COPY viewer ./viewer
RUN make -j"$(nproc)" LDFLAGS="-pthread -static-libstdc++ -static-libgcc"

FROM debian:bookworm-slim
COPY --from=build /src/build/codecraft-sim-cpp /engine/codecraft-sim-cpp
RUN mkdir -p /game
WORKDIR /game
ENTRYPOINT ["/engine/codecraft-sim-cpp"]
