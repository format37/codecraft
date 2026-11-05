# Official C++ client on a current gcc image (the 2020 gcc:10.2.0 image is Debian buster, whose
# apt repositories are gone, so cmake can no longer be installed there).
FROM gcc:13.3.0
RUN apt-get update && apt-get install -y --no-install-recommends cmake make \
    && rm -rf /var/lib/apt/lists/*
COPY client /project
WORKDIR /project
