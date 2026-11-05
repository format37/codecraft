# Build templates

A strategy is built from a *base* Dockerfile plus the official client of its language
(`clients/<Lang>/`, with its `compile.sh` and `run.sh`), exactly like the 2020 testing system did.

For a `language:` value the builder looks for `templates/<language>/Dockerfile` first; if there is
none it uses the official `clients/<Lang>/Dockerfile` (matched case-insensitively, e.g. `cpp` ->
`clients/Cpp`). A template directory may contain a `client` file naming the official client to pair
it with (default: same name as the template).

- `python`       - official Python 3.8 client, deps fixed for today's PyPI (`scikit-learn` instead of the dead `sklearn` package).
- `python-cuda`  - Python client on the PyTorch CUDA 12.4 image; use with `gpu: true` in strategy.yaml.
- `cpp`          - official C++ client on `gcc:13` (the 2020 `gcc:10.2.0` image can no longer install cmake).
- `cpp-cuda`     - official C++ client on `nvidia/cuda:12.4-devel` (nvcc + cmake): real `.cu` kernels compiled at build time.
