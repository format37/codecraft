The local runner of https://codecraft.day: the game engine at version `@VERSION@` (the version every game page names), with the game live in the browser and the official GUI's debug interface for your strategy. The guide: https://codecraft.day/quickstart

**Docker** (Linux, macOS, Windows), in your strategy's folder:

    docker run --rm -it -p 31000:31000 -p 31001-31004:31001-31004 -v "$PWD":/game ghcr.io/@OWNER@/codecraft-runner:@VERSION@

**Binaries**, each with the runner and a README.txt:

- `codecraft-runner-linux-x86_64.tar.gz` and `codecraft-runner-linux-arm64.tar.gz` (glibc 2.35 or newer: Ubuntu 22.04, Debian 12 and later)
- `codecraft-runner-macos.tar.gz`: one universal binary for Apple silicon and Intel. It is not signed: unpacked from a curl download it runs as it is; after a browser download run `xattr -d com.apple.quarantine codecraft-runner` once.
- `codecraft-runner-windows-x86_64.zip`: `codecraft-runner.exe` for Windows 10 and 11, one file with nothing to install. It is not signed: after a browser download SmartScreen may stop it once (More info, then Run anyway).

      curl -L https://github.com/@OWNER@/codecraft/releases/download/runner-@VERSION@/codecraft-runner-linux-x86_64.tar.gz | tar xz && ./codecraft-runner

  Windows, in PowerShell or cmd:

      curl.exe -L -o runner.zip https://github.com/@OWNER@/codecraft/releases/download/runner-@VERSION@/codecraft-runner-windows-x86_64.zip
      tar xf runner.zip
      .\codecraft-runner.exe

Before this release every binary played the same game on its own system (the starter strategy over TCP against the built-in bot, in batch mode and in the viewer), and the replays agreed byte for byte. We run the Linux binaries ourselves; the macOS and Windows ones have been run by CI only so far: tell us how they go.
