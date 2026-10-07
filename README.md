# snakemake-executor-plugin-workbench

A Snakemake executor plugin for Posit Workbench, submitting jobs via the Workbench launcher API.
Works whether Snakemake is driven from inside a Workbench session (ambient RPC cookie auth, no
token needed) or from anywhere else (Bearer token via `WORKBENCH_SERVER`/`WORKBENCH_API_KEY`).

The Workbench API client itself (`posit.workbench` / `posit.workbench.admin`) lives in a fork of
`posit-sdk-py` (branch `workbench`), pulled in automatically as a dependency -- no separate clone
needed.

## Setup

Requires Python >=3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo>
cd posit-sdk-snakemake
uv venv
uv pip install ".[snakemake]"
```

**Important**: install this plugin itself non-editable (`uv pip install .`, not `-e .`).
Snakemake discovers executor plugins by scanning *installed* top-level packages named
`snakemake_executor_plugin_<name>` (`pkgutil.iter_modules()`), and that scan isn't guaranteed to
find editable installs -- confirmed live during development.

If you're running this against a Kubernetes-backed Workbench cluster and driving Snakemake from
a shared-storage host (e.g. exec'd into the Workbench server pod, or from a session), make sure
your virtualenv's Python itself lives on that shared storage too -- a `uv`-managed Python
(`uv python install 3.13`, then `uv venv --python 3.13 --python-preference only-managed`) installs
under `~/.local/share/uv/python/`, which is typically on the shared home directory; the *system*
Python (e.g. `/opt/python/...`) usually isn't, and job containers won't be able to find it.

## Credentials

- **Inside a Workbench session**: nothing to set -- the plugin uses the ambient session cookie
  automatically (`posit.workbench.Client`).
- **Anywhere else**: set `WORKBENCH_SERVER` and `WORKBENCH_API_KEY` (Bearer token via
  `posit.workbench.admin.Client`).

## Example

A three-step pipeline (`generate` -> `square` -> `sum`), each step a separate Python script
declared as an explicit input, so Snakemake's dependency tracking is visible end to end:

```bash
mkdir example && cd example
cp -r ../examples/* .
snakemake --executor workbench --workbench-cluster <cluster-name> -j1
```

This submits three Workbench jobs (plus the local `all` aggregator) and produces `sum.txt`
(`55`, the sum of 1..5 squared).

Edit `scripts/step2_square.py` and rerun the same command: because that script is a declared
input of the `square` rule (not just referenced in its shell command), Snakemake reruns `square`
and the downstream `sum`, but *not* `generate` -- `numbers.txt` is untouched and its job is
skipped:

```
localrule square: ...
localrule sum: ...
localrule all: ...
```

Find valid cluster names for `--workbench-cluster`:
```python
from posit.workbench.admin import Client   # or: from posit.workbench import Client, if in-session
print([c["name"] for c in Client().compute_envs.list()["clusters"]])
```

Note: this assumes a shared filesystem between wherever Snakemake is driven from and wherever
Workbench runs jobs (the standard Snakemake `RemoteExecutor` assumption) -- see `--container-image`
if your cluster needs a specific job container image (defaults to the official Snakemake image).

### RNA-seq example (parallelism)

[`examples/rnaseq/`](examples/rnaseq/README.md) is a more realistic pipeline: Salmon index,
then FastQC + Salmon quant for each of 4 samples, then MultiQC. It's a port of the same example
in `posit-sdk-nextflow`. It runs 10 Workbench Jobs, with up to 8 at a time:

```bash
cd examples/rnaseq
snakemake          # executor, cluster, -j and --latency-wait come from profiles/default/config.yaml
```

## Container images per step

Add a `container:` directive to any rule to give that step its own image (`generate` and
`square` in `examples/Snakefile` do this, with two different images); rules without one fall
back to `--container-image` (or its own default) instead. This only takes effect on clusters
whose compute env reports `supportsContainers`. The image becomes the Workbench job's own
container and the rule runs natively inside it, so no `--software-deployment-method apptainer`
is needed (apptainer couldn't run inside a Kubernetes job pod anyway). `container: "docker://..."` is Snakemake's usual
apptainer/singularity URI form -- the `docker://` prefix is stripped automatically since
Workbench expects a plain image reference.
