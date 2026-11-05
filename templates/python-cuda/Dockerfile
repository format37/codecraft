# Python client on top of PyTorch + CUDA 12.4, plus CuPy: `cupy.RawKernel` compiles CUDA C kernel
# source at run time (NVRTC), see strategies/cuda-demo-py. Pair with `gpu: true` in strategy.yaml
# so the container gets the host GPUs (nvidia container runtime).
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy cython scikit-learn lightgbm catboost numba pandas cupy-cuda12x
COPY client /project
WORKDIR /project
