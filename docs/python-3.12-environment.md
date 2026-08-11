# Reproducible Python 3.12 environment

The local engine targets Python 3.12 exactly. Its direct dependencies are
declared in `pyproject.toml`, while `requirements.lock` records the complete
resolved environment, including transitive dependencies.

Create the isolated environment without changing the existing `.venv`:

```sh
python3.12 -m venv .venv312
.venv312/bin/python -m pip install --upgrade pip
.venv312/bin/python -m pip install -r requirements.lock
```

Confirm the interpreter and dependency set:

```sh
.venv312/bin/python --version
.venv312/bin/python -m pip check
.venv312/bin/python -m pip freeze
```

Run the local validation suite:

```sh
.venv312/bin/python -m unittest discover -s tests -v
.venv312/bin/python -m compileall -q -x '(^|/)(\.venv|\.venv312)(/|$)' .
git diff --check
```

The video pipeline also requires `ffmpeg` and `ffprobe` on `PATH`. Verify both
before running media actions:

```sh
ffmpeg -version
ffprobe -version
```

On macOS with Homebrew, install them only when either command is unavailable:

```sh
brew install ffmpeg
```
