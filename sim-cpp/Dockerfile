# CodeCraft 2020 game engine: our own, in C++ (sim-cpp; the standard library only), with the command line of the
# official local runner aicup2020 1.3.0 in batch mode; it plays exactly the games of the Python engine in sim/.
# Built by `./cw build` when config.yaml names it (engine.image codecraft/sim-cpp:<version>, engine.context sim-cpp),
# or by hand: docker build -t codecraft/sim-cpp:0.1.0 sim-cpp
FROM gcc:13.3.0 AS build
WORKDIR /src
COPY Makefile ./
COPY src ./src
COPY viewer ./viewer
RUN make -j"$(nproc)" LDFLAGS="-pthread -static-libstdc++ -static-libgcc"

FROM debian:bookworm-slim
COPY --from=build /src/build/codecraft-sim-cpp /engine/codecraft-sim-cpp
RUN mkdir -p /game
WORKDIR /game
ENTRYPOINT ["/engine/codecraft-sim-cpp"]
