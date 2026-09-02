"""Snakemake executor plugin for Posit Workbench, backed by posit.workbench(.admin).

Registration is by import-name convention (see
snakemake_interface_common.plugin_registry.PluginRegistryBase.collect_plugins): Snakemake scans
installed top-level packages named ``snakemake_executor_plugin_<name>`` and expects this module
to define ``common_settings``, ``Executor``, and optionally ``ExecutorSettings`` -- there is no
separate entry-points registration step.

IMPORTANT: per that same discovery code's own comment, plugins installed in *editable* mode are
not guaranteed to be picked up by ``pkgutil.iter_modules()``. Install this distribution normally
(``pip install .`` / ``uv pip install .``, not ``-e``) before relying on
``snakemake --executor workbench`` to find it. (In practice this worked fine under `uv pip
install -e .` in testing, but the plugin registry's own comment says not to rely on that.)

Jobs are submitted via posit.workbench's shared ``Jobs.launch()`` as Workbench jobs whose
command is the jobscript Snakemake itself generates (a re-invocation of
``python -m snakemake ...`` for that one job) -- the standard RemoteExecutor pattern, which
assumes a shared filesystem between wherever Snakemake is driven from and wherever Workbench
runs the job (per team decision, an acceptable assumption for now).

The client is chosen automatically: ``posit.workbench.Client`` (ambient session RPC cookie, no
token needed) if running inside a Workbench session, else ``posit.workbench.admin.Client``
(Bearer token via WORKBENCH_SERVER/WORKBENCH_API_KEY). Both expose the identical
``.sessions``/``.jobs``/``.compute_envs``/``.users``/``.server`` resource-manager interface, so
everything below works unmodified regardless of which one wins.

IMPORTANT: this module deliberately does NOT use `from __future__ import annotations`.
`ExecutorSettings`' CLI-arg registration (snakemake_interface_common's
`dataclass_field_to_argument_args`) inspects `dataclasses.fields(...)[i].type` directly rather
than through `typing.get_type_hints()` -- with postponed evaluation enabled, that attribute is
the raw annotation *string* (e.g. "Optional[str]") instead of the actual `Optional` type, which
breaks its `Union`/`Optional` detection and crashes argparse with
`ValueError: 'Optional[str]' is not callable`. None of the upstream example plugins
(cluster-generic, kubernetes) use postponed evaluation either, for the same reason.
"""

import getpass
import os
import shlex
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Optional  # see note on `cluster` below -- must stay typing.Optional

import posit.workbench
import posit.workbench.admin
from posit.workbench import WorkbenchError
from snakemake_interface_common.exceptions import WorkflowError
from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins.jobs import JobExecutorInterface
from snakemake_interface_executor_plugins.settings import CommonSettings, ExecutorSettingsBase

_TERMINAL_FAILURE_STATUSES = frozenset({"Failed", "Killed", "Canceled"})


@dataclass
class ExecutorSettings(ExecutorSettingsBase):
    """CLI-exposed as --workbench-cluster."""

    # Must stay `typing.Optional[str]`, not `str | None`: the CLI-arg-registration code
    # (snakemake_interface_common's dataclass_field_to_argument_args) only recognizes
    # `typing.Union` origin, not the `types.UnionType` produced by `X | None` syntax --
    # using the modern syntax here reproduces the same crash the future-annotations
    # removal above fixed (a non-callable type object/string passed to argparse).
    cluster: Optional[str] = field(  # noqa: UP045
        default=None,
        metadata={
            "help": "Workbench compute env/cluster name to launch jobs on "
            "(see get_compute_envs()['clusters'][*]['name']).",
            "required": True,
        },
    )


# Required: settings shared across all remote executors that Snakemake needs to know about
# this plugin.
common_settings = CommonSettings(
    non_local_exec=True,
    implies_no_shared_fs=False,
    job_deploy_sources=False,
    pass_default_storage_provider_args=True,
    pass_default_resources_args=True,
)


def _make_client():
    """Prefer the ambient in-session client; fall back to the Bearer-token admin client."""
    try:
        return posit.workbench.Client()
    except OSError:
        return posit.workbench.admin.Client()


class Executor(RemoteExecutor):
    def __post_init__(self):
        if not self.workflow.executor_settings.cluster:
            raise WorkflowError(
                "You have to specify a Workbench cluster via --workbench-cluster."
            )
        try:
            self.client = _make_client()
        except ValueError as e:
            raise WorkflowError(f"Failed to configure the Workbench client: {e}") from e
        self._container = self._resolve_container()

    def _resolve_container(self) -> dict[str, str] | None:
        # `remote_execution_settings.container_image` always has a value (it defaults to the
        # official snakemake image), so gate on the target cluster actually supporting
        # containers -- otherwise every job on a non-container cluster (e.g. "Local") would
        # get an unwanted `container` field.
        image = self.workflow.remote_execution_settings.container_image
        if not image:
            return None

        envs = self.client.compute_envs.list()
        cluster = next(
            (
                c
                for c in envs.get("clusters") or []
                if c.get("name") == self.workflow.executor_settings.cluster
            ),
            None,
        )
        if cluster is None or not cluster.get("supportsContainers"):
            return None
        return {"image": image}

    def get_job_exec_prefix(self, job: JobExecutorInterface) -> str:
        if not self.workflow.storage_settings.assume_common_workdir:
            return ""
        # Without the cd, the jobscript runs wherever the job container's own default
        # working directory happens to be (e.g. "/tmp/repo" in the official snakemake
        # image) instead of the shared working directory -- confirmed live: outputs got
        # written there and were never seen by the driving process. Matches snakemake-
        # executor-plugin-cluster-generic's get_job_exec_prefix() for the same reason.
        #
        # Without the HOME export, job containers running as a non-root uid (see the
        # Kubernetes cluster's securityContext) may have no $HOME at all -- confirmed
        # live: this makes Python's cache-dir resolution fall back to filesystem root and
        # crash with `PermissionError: [Errno 13] Permission denied: '/.cache'`. Exporting
        # the driving process's own $HOME is safe because it's the same shared mount.
        #
        # Without USER/LOGNAME, `getpass.getuser()` (which Snakemake itself calls for its
        # run-info header) has nothing to fall back to either, since the job container's
        # generic image has no /etc/passwd entry for this cluster's LDAP-resolved uid --
        # confirmed live: `OSError: No username set in the environment`. A root-run job
        # never hit this, since every image has a passwd entry for uid 0.
        home = os.path.expanduser("~")
        username = getpass.getuser()
        workdir = self.workflow.workdir_init
        return (
            f"export HOME={shlex.quote(home)} "
            f"USER={shlex.quote(username)} "
            f"LOGNAME={shlex.quote(username)} "
            f"&& cd {shlex.quote(workdir)}"
        )

    def run_job(self, job: JobExecutorInterface):
        jobscript = self.get_jobscript(job)
        self.write_jobscript(job, jobscript)

        resources = self.get_resource_declarations_dict(job)
        resource_limits: list[dict[str, str]] = []
        if job.threads:
            resource_limits.append({"type": "cpuCount", "value": str(job.threads)})
        if "mem_mb" in resources:
            resource_limits.append({"type": "memory", "value": str(resources["mem_mb"])})

        job_info = SubmittedJobInfo(job)
        try:
            wb_job = self.client.jobs.launch(
                cluster=self.workflow.executor_settings.cluster,
                name=self.get_jobname(job),
                exe="/bin/bash",
                args=[jobscript],
                resource_limits=resource_limits or None,
                container=self._container,
            )
        except WorkbenchError as e:
            self.report_job_error(job_info, msg=str(e))
            return

        job_info.external_jobid = wb_job["id"]
        self.report_job_submission(job_info)

    async def check_active_jobs(
        self, active_jobs: list[SubmittedJobInfo]
    ) -> AsyncGenerator[SubmittedJobInfo, None]:
        if not active_jobs:
            return

        # One batched lookup for all active jobs this tick, instead of one API call per job.
        statuses = self.client.jobs.get_status_map()

        for job_info in active_jobs:
            async with self.status_rate_limiter:
                status = statuses.get(job_info.external_jobid)
                if status is None:
                    # not found yet (e.g. rotated out before this poll) -- keep waiting
                    yield job_info
                    continue

                if status.get("status") == "Finished" and status.get("exitCode") in (0, None):
                    self.report_job_success(job_info)
                elif status.get("status") in _TERMINAL_FAILURE_STATUSES:
                    self.report_job_error(job_info, msg=status.get("statusMessage"))
                else:
                    yield job_info

    def cancel_jobs(self, active_jobs: list[SubmittedJobInfo]):
        for job_info in active_jobs:
            try:
                self.client.jobs.stop(job_info.external_jobid, force_quit=True)
            except WorkbenchError:
                pass  # best-effort cancellation, e.g. job may have already finished
        self.shutdown()
