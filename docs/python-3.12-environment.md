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

## Google Gen AI SDK compatibility floor

The engine pins `google-genai==1.55.0`. This is the smallest stable release
whose official changelog includes the public Interactions API required by the
Hero adapter. The existing Veo adapter also requires the public
`models.generate_videos`, `operations.get`, and `files.download` methods.

`tests/test_google_genai_sdk_contract.py` checks these public service and method
signatures using anonymous offline credentials. It reads no API key and makes
no network request. Provider behavior remains covered by injected fakes.
