CodeCraft 2020 local runner, engine @VERSION@                           https://codecraft.day/quickstart

  ./codecraft-runner                       the viewer opens at http://127.0.0.1:31000/ with a game to start: your
                                           strategy against three of the engine's bots; it connects to 127.0.0.1 31001
  ./codecraft-runner --config game.json    a game from a config file (the viewer's config screen shows the format)
  ./codecraft-runner --replay replay.dat   watch a saved game (Ctrl-S in the viewer saves the one shown)
  ./codecraft-runner --limits warn         wait for a strategy stopped at a breakpoint instead of cutting it off
  ./codecraft-runner --help                every flag

The same engine plays the games at codecraft.day: every game page names the version that played it, and
ghcr.io/@OWNER@/codecraft-runner:@VERSION@ is this very one as a Docker image.

macOS: the binary is not signed. Unpacked from a curl download it runs as it is; downloaded with the browser it
needs `xattr -d com.apple.quarantine codecraft-runner` once.

Windows 10 or 11: .\codecraft-runner.exe in PowerShell or cmd, with the same flags (a double click starts it too).
It is not signed: after a browser download Windows may say "Windows protected your PC" once (More info, then Run
anyway). Should Defender take it for a threat, tell us: https://github.com/@OWNER@/codecraft/issues

Source and issues: https://github.com/@OWNER@/codecraft (sim-cpp/)
