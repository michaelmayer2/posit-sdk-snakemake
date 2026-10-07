# RNA-seq example: parallel Workbench Jobs

A small RNA-seq quantification pipeline, adapted from the canonical
[nextflow-io/rnaseq-nf](https://github.com/nextflow-io/rnaseq-nf) tutorial. It's a port of the
matching `examples/rnaseq` in `posit-sdk-nextflow`: same tools, same images, same test data,
same results. It shows how the `workbench` executor runs independent jobs in parallel. Every
rule instance is its own Posit Workbench Job, and Snakemake submits a job as soon as its inputs
exist, up to `-j N` at a time.

```text
                     +--> fastqc (x N samples) --+
  reads (N samples) -+                           +--> multiqc
                     +--> quant  (x N samples) --+
                             ^
  transcriptome --> index ---+  (one index, reused by every quant job)
```

| Rule | Tool (container) | Runs | Parallelism |
|---|---|---|---|
| `download_*` | `curl`, on the driver (`localrules`) | per file | Local, not Workbench Jobs |
| `index` | Salmon `quay.io/biocontainers/salmon:1.10.3--h6dccd9a_2` | once | Starts immediately, alongside `fastqc` |
| `fastqc` | FastQC `quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0` | per sample | Fan-out: one job per sample, all at once |
| `quant` | Salmon | per sample | Fan-out: all start as soon as `index` is done |
| `multiqc` | MultiQC `quay.io/biocontainers/multiqc:1.32--pyhdfd78af_0` | once | Fan-in: waits for all 2×N per-sample results |

Each rule's `container:` image becomes the Workbench job's own container, and the rule runs
natively inside it. There's no apptainer/singularity, so **don't** pass
`--software-deployment-method apptainer`.

## Run it

Same requirements as the root example (plugin installed non-editable, a venv whose Python lives
on the shared filesystem, a shared working directory). The driver downloads the test data
(a chicken transcriptome slice and 4 paired-end samples, about 6 MB) from GitHub with `curl`.
The Workbench Jobs only need to be able to pull the `quay.io` images.

```bash
cd examples/rnaseq
snakemake                                 # uses profiles/default/config.yaml
snakemake --workbench-cluster <cluster>   # different cluster
snakemake -j 2                            # throttle to 2 concurrent Workbench Jobs
```

`profiles/default/config.yaml` is a Snakemake workflow profile that is loaded automatically. It
sets `executor: workbench`, `workbench-cluster: Kubernetes`, `jobs: 10` and `latency-wait: 60`.
Anything on the command line overrides it.

Measured on a Kubernetes cluster, with images already pulled:

| Setting | Max concurrent Workbench Jobs | Wall time |
|---|---|---|
| default (`jobs: 10`) | 8 | ~53 s |
| `-j 2` | 2 | ~82 s |

## Config

Override with `--config key=value`:

| Key | Default | Purpose |
|---|---|---|
| `samples` | `gut,liver,lung,spleen` | Comma-separated sample names from the test data. Use fewer to scale down, e.g. `--config samples=gut,liver`. |
| `data_url` | rnaseq-nf `data/ggal` on GitHub | Base URL for the FASTQs and transcriptome. |
| `transcriptome` | `ggal_1_48850000_49020000.Ggal71.500bpflank.fa` | Transcriptome file name under `data_url`. |

Per-job resources come from each rule's `threads` (Workbench `cpuCount`) and `resources.mem_mb`
(Workbench `memory`): 2 CPU / 2 GB for `index` and `quant`, 1 CPU / 1 GB otherwise. Each job's
request has to fit within the cluster's `resourceLimits` `maxValue`, or Workbench rejects it.

## Outputs

```text
data/                          # downloaded test data
results/
├── index/                     # Salmon index
├── fastqc/<sample>/           # FastQC reports, per sample
├── quant/<sample>/quant.sf    # Salmon transcript quantification, per sample
└── multiqc_report.html        # QC + mapping summary across all samples
```

## Troubleshooting

- **`MissingOutputException ... completed successfully, but some output files are missing`**:
  the shared filesystem hasn't shown the job's outputs to the driver yet. Raise `latency-wait`.
  The profile's 60 s was enough in testing, where Snakemake's 5 s default was not.
- **`<tool>: command not found`**: the rule ran in the fallback `--container-image` instead of
  its own image. Make sure the rule has a `container:` directive and that the cluster reports
  `supportsContainers`.
