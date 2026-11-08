<!-- The server renders this with the numbers of config.yaml and the engine's version ({{ ... }}): https://codecraft.day/quickstart -->
# Quick start

A strategy of yours fighting on your own screen in ten minutes, on the ladder a few minutes later. You need git,
the toolchain of your language, and Docker or nothing more than a terminal.

## 1. Get the starter

```bash
git clone https://github.com/format37/codecraft-starter
cd codecraft-starter
```

Or **Code → Download ZIP** on [GitHub]({{ starter }}); fork it later, when you submit. One folder per language: the
official 2020 client of that language, with the same small strategy dropped in. The strategy is deliberately minimal
(about seventy lines in Python): its builders mine to the right and put up one house, nobody is trained, nothing
fights. The rest of the game is yours.

| Language | Folder | Your file |
|---|---|---|
| Python, Python (PyPy) | `python/` | `my_strategy.py` |
| C++ | `cpp/` | `MyStrategy.cpp` |
| Rust | `rust/` | `src/my_strategy.rs` |
| Go | `go/` | `my_strategy.go` |
| Java, Java (GraalVM native) | `java/`, `java-graalvm-native/` | `src/main/java/MyStrategy.java` |
| Kotlin, Kotlin (GraalVM native) | `kotlin/`, `kotlin-graalvm-native/` | `src/main/kotlin/MyStrategy.kt` |
| Scala | `scala/` | `src/main/scala/MyStrategy.scala` |
| C# | `csharp/` | `MyStrategy.cs` |
| F# | `fsharp/` | `MyStrategy.fs` |
| JavaScript | `javascript/` | `my-strategy.js` |
| Ruby | `ruby/` | `my_strategy.rb` |
| D | `dlang/` | `source/my_strategy.d` |

## 2. Run the runner

The runner is this server's engine on your machine: the same rules, maps and replays, with the game live in the
browser and the debug interface your strategy draws and logs through. Three ways to get it; each writes its files
(replays, saved states) into the folder it is started from, so start it in the starter's folder.

**Docker** (Linux, macOS, Windows with Docker Desktop):

```bash
docker run --rm -it -p 31000:31000 -p 31001-31004:31001-31004 -v "$PWD":/game {{ image }}
```

**A binary**, no Docker:

```bash
# Linux x86_64
curl -L {{ release }}/codecraft-runner-linux-x86_64.tar.gz | tar xz && ./codecraft-runner
# Linux arm64 (a Raspberry Pi, an Ampere box)
curl -L {{ release }}/codecraft-runner-linux-arm64.tar.gz | tar xz && ./codecraft-runner
# macOS, Apple silicon or Intel: one universal binary
curl -L {{ release }}/codecraft-runner-macos.tar.gz | tar xz && ./codecraft-runner
```

**Windows** 10 or 11, in PowerShell or cmd (both have `curl.exe` and `tar`): one exe, nothing to install.

```powershell
curl.exe -L -o runner.zip {{ release }}/codecraft-runner-windows-x86_64.zip
tar xf runner.zip
.\codecraft-runner.exe
```

The macOS and Windows binaries are not signed. On macOS, unpacked from a curl download as above, the binary runs as
it is; downloaded with the browser it needs `xattr -d com.apple.quarantine codecraft-runner` once. On Windows, after
a browser download, SmartScreen may say "Windows protected your PC" once: **More info**, then **Run anyway**. A fresh
unsigned exe can also trip Microsoft Defender; if it does, tell us. Before every release our CI plays the same game
with each binary on its own system and checks that the replays agree, but we have not run the macOS and Windows ones
ourselves yet: tell us how it goes. **From source**: `make -C sim-cpp` in the [server repository]({{ repo }}) with
g++ or clang and C++17 (on Windows, MSYS2's UCRT64 g++).

Either way the viewer opens at <http://127.0.0.1:31000/> (the binary opens the browser for you; with Docker, open it
yourself). Its config screen offers a Round 1 game: seat 1 is a TCP seat on port 31001 for your strategy, the other
three are the engine's built-in bot, which plays the starter's strategy. Press **Start**: the runner now waits for
your strategy to connect.

{% if runner_version %}This server plays engine version `{{ runner_version }}` today (every game page names the
version of its game); the image `{{ image }}:{{ runner_version }}` and the release
[`runner-{{ runner_version }}`]({{ releases }}/tag/runner-{{ runner_version }}) are that very build. {% endif %}The
lines above take the newest: `{{ image }}` is `:latest` and `{{ release }}/...` is the latest release.

## 3. Run your strategy

In a second terminal, in the starter's folder. Every client takes `host port [token]` and connects to `127.0.0.1
31001` when given nothing; the game starts the moment it connects. On Windows, Python is `python` or `py` where these
lines say `python3`.

<div class="chips"></div>

```bash lang=python
cd python
python3 main.py                        # Python 3.8 or newer; nothing to install
```

```bash lang=python-pypy
cd python
pypy3 main.py                          # the same code as Python; the server runs it on PyPy
```

```bash lang=cpp
cd cpp
cmake -DCMAKE_BUILD_TYPE=Release . && cmake --build .     # a C++17 compiler and CMake
./aicup2020
```

```bash lang=rust
cd rust
cargo run --release                    # Rust 2018 edition or newer
```

```bash lang=go
cd go
go run .                               # Go 1.13 or newer
```

```bash lang=java
cd java
mvn -q package                         # JDK 11 or newer, Maven
java -jar target/aicup2020-jar-with-dependencies.jar
```

```bash lang=java-graalvm-native
cd java-graalvm-native                 # the same code as java/: develop there on the JVM, submit this folder
mvn -q package                         # GraalVM with native-image, Maven
./target/aicup2020-native-with-dependencies
```

```bash lang=kotlin
cd kotlin
mvn -q package                         # JDK 11 or newer, Maven
java -jar target/aicup2020-jar-with-dependencies.jar
```

```bash lang=kotlin-graalvm-native
cd kotlin-graalvm-native               # the same code as kotlin/: develop there on the JVM, submit this folder
mvn -q package                         # GraalVM with native-image, Maven
./target/aicup2020-native-with-dependencies
```

```bash lang=scala
cd scala
mvn -q package                         # JDK 11 or newer, Maven
java -jar target/aicup2020-jar-with-dependencies.jar
```

```bash lang=csharp
cd csharp
dotnet run -c Release                  # the .NET SDK
```

```bash lang=fsharp
cd fsharp
dotnet run -c Release                  # the .NET SDK
```

```bash lang=javascript
cd javascript
node index.js                          # Node.js; nothing to install
```

```bash lang=ruby
cd ruby
ruby main.rb                           # plain Ruby; the server runs it on JRuby
```

```bash lang=dlang
cd dlang
dub run -b release                     # a D compiler (dmd or ldc) and dub
```

## 4. Watch, change, play again

The game plays at ten ticks a second to begin with; the panel sets the speed, up to as fast as the strategies
answer. **P** pauses, **←/→** step a tick, the wheel zooms, **Home** fits the map, a click on an entity shows what
it is and what it was told to do. What your strategy draws and logs while it thinks (the starter draws its planned
house and logs the count of its entities) appears on the tick it belongs to, history included: that is the debug
interface, the official GUI's, yours to use. Lines your strategy prints to stderr show in its own terminal here, and
in the game's log on the server, visible to you alone.

Stop your strategy, change it, press **↻ Restart** in the viewer (with **same seed** for the same map and dice) and
start it again: the runner waits for it. No need to restart the runner, ever; **New game…** after a game opens the
config screen again.

The runner holds your strategy to the server's limits, {{ runner_limits }}, and cuts it off past them as the server
would; the panel shows how long every answer took. To stop at a breakpoint in a debugger, start the runner with
`--limits warn` (or switch in the panel): it then waits as long as it takes and only marks what the server would
not have allowed.

**Ctrl-S** saves the replay so far, **Ctrl-E** the state of the tick shown, which a config can start a game from;
`--replay replay.dat` watches a replay (every game page here offers its own); `--config game.json` plays a game of
your making, two of your versions against each other say (the config screen shows the format); `--help` lists
every flag.

## 5. Submit

[Sign in]({{ base }}/login) with your e-mail (a code is mailed, no password) and open [Submit]({{ base }}/submit):
a name, the language, and the source in one of three ways: the URL of your fork (public; a branch, tag or commit
next to it; the folder named after the language is built), a zip of the language's folder, or just your strategy
file. The server builds it with the official client's own build script, in seconds for Python, up to ten minutes
for Java, Rust or GraalVM; the build log is yours to read.

A built version plays {{ per_build }} rated games against strategies of similar rating, then {{ daily_text }} a day
for as long as the strategy is enabled; the [leaderboard]({{ base }}/leaderboard) orders everyone by Elo rating and
links every game with its replay. You may submit {{ versions_per_day }} versions a day; a new version keeps its
strategy's rating. How the ladder, the ratings and the leagues work: [the arena]({{ base }}/rules#the-arena).

## 6. Or hand it to an agent

A coding agent (Claude Code, Codex, Cursor, a claude.ai connector, anything that speaks MCP or runs curl) can do
all of the above with a token of your account: read the rules, write the strategy, submit it, wait for the build,
play games against opponents of its choice, read the timelines of its games and iterate.
[Agents]({{ base }}/agents) sets it up in one command.

## Questions

{% if community_url %}[{{ community_name }}]({{ community_url }}) is where the players are: ask there, show your
games, be the first Mac tester. {% endif %}Bugs and ideas: [issues]({{ repo }}/issues) on GitHub.
