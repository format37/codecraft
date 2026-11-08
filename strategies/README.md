# Strategies

One directory per strategy with a `strategy.yaml`:

```yaml
name: format37-2020            # unique id (also the docker image name suffix)
title: format37 (Python)       # shown in the UI
language: python               # a template in templates/ or an official client dir (cpp, java, kotlin, rust, ...)
gpu: false                     # true -> the container gets all host GPUs (use with language: python-cuda)
enabled: true
source:
  git: https://github.com/format37/codecraft-2020
  ref: 428252a45e0923a341b41192aa4d1f44fe072418   # commit/tag/branch to pin
  path: .                      # sub-directory of the repo that is the strategy (optional)
  include: [my_strategy.py]    # only these files/globs become /src (optional). One file = drop-in
                               # replacement of the client's strategy file, as on the 2020 server.
  merge: false                 # true: lay the files over the official client and compile in place
                               # (for strategies that add files, e.g. CUDA kernels + CMakeLists.txt)
  # or instead of git:
  # dir: src                   # a directory next to strategy.yaml
limits:                        # optional overrides of config.yaml `limits` for this strategy
  memory_mb: 2048
overlay: overlay               # files copied over the source before the build (e.g. a missing pom.xml)
prebuild: prebuild.sh          # script run inside /src before compile.sh (e.g. strip debug flags)
```

A built-in engine bot needs no build:

```yaml
name: quickstart
title: QuickStart (engine bot)
builtin: QuickStart
```

`./cw build` builds every enabled strategy into `codecraft/strategy-<name>:<content hash>`.

The bundled 2020 entries (`*-2020`) are disabled: the bare names stay free for their authors, who
submit through the web like everyone else. `./cw rename OLD NEW` renames a strategy in the database
(row, versions, past games) when a manifest's name changes.
