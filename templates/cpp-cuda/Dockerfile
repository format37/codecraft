# Official C++ client + the CUDA toolkit (nvcc). A strategy brings its own CMakeLists.txt that
# enables the CUDA language and lists its .cu files; see strategies/cuda-demo-cpp. Pair with
# `gpu: true` so the container gets the host GPUs at game time.
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends cmake make g++ \
    && rm -rf /var/lib/apt/lists/*
COPY client /project
WORKDIR /project
