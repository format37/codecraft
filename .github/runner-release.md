The local runner of https://codecraft.day: the game engine at version `@VERSION@` (the version every game page names), with the game live in the browser and the official GUI's debug interface for your strategy. The guide: https://codecraft.day/quickstart

**Docker** (Linux, macOS, Windows), in your strategy's folder:

    docker run --rm -it -p 31000:31000 -p 31001-31004:31001-31004 -v "$PWD":/game ghcr.io/@OWNER@/codecraft-runner:@VERSION@

**Binaries**, each a tar.gz with `codecraft-runner` and a README.txt:

- `codecraft-runner-linux-x86_64.tar.gz` and `codecraft-runner-linux-arm64.tar.gz` (glibc 2.35 or newer: Ubuntu 22.04, Debian 12 and later)
- `codecraft-runner-macos.tar.gz`: one universal binary for Apple silicon and Intel. It is not signed: unpacked from a curl download it runs as it is; after a browser download run `xattr -d com.apple.quarantine codecraft-runner` once.

      curl -L https://github.com/@OWNER@/codecraft/releases/download/runner-@VERSION@/codecraft-runner-linux-x86_64.tar.gz | tar xz && ./codecraft-runner

Tested on Linux. The macOS binary is built here and has not been run by us: tell us how it goes. Windows: Docker Desktop (above) or the Linux binary in WSL2.
