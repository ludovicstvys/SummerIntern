# Local security tools

`strix/` contains a shallow checkout of https://github.com/usestrix/strix at commit `52b19233477a783004467c1522651eec96015e73`. It is deliberately ignored by the parent Git repository and excluded from pytest discovery. The available standalone CLI is Strix 1.5.2 (`~/.strix/bin/strix`); the downloaded source is kept separately.

Recreate the checkout if needed:

```sh
git clone --depth 1 https://github.com/usestrix/strix.git tools/strix
```

Docker is provided locally by Colima (`brew install colima docker`). Start it with `colima start --cpu 2 --memory 4 --disk 20` and verify `docker info`.

Configure your model through Strix's `strix auth login chatgpt` subscription flow and set `STRIX_LLM` to an available `chatgpt/<model>`, or supply your provider's model and API key via local `STRIX_LLM`/`LLM_API_KEY` environment variables. Never put keys in this repository.

Run the project wrapper from the repository root:

```sh
python3 scripts/run_strix.py --prepare-only
python3 scripts/run_strix.py --mode quick --max-budget 5
```

The wrapper copies allowlisted source files, including uncommitted work, to `audit/strix-work/<timestamp>/source`. It omits private data, dependencies, Git history and downloaded tooling; it never mounts the actual working tree. Instructions are in `audit/STRIX_SCOPE.md`. Telemetry is disabled. The default model-cost limit is USD 5; the scope is the complete copied project, not only the last commit's diff.

Per-attempt metadata is in `audit/strix-work/<timestamp>/preflight.json`; Strix's own reports, when a scan starts, are under that attempt's `strix_runs/`. Exit code 1 means a launch/runtime error; 2 means findings; 0 still requires checking coverage and budget exhaustion in Strix's `run.json` and report. A failed preflight is not a security assessment.

The wrapper resolves the active Docker CLI context into `DOCKER_HOST` for Strix's Python SDK. It uses an isolated, credential-free Docker configuration for the public sandbox image, avoiding stale Docker Desktop credential helpers without modifying personal Docker credentials.
