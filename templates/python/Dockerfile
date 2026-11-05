# Official Python client image, with the 2020 dependency list fixed for today's PyPI.
FROM python:3.8
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy cython scikit-learn lightgbm catboost numba pandas
COPY client /project
WORKDIR /project
