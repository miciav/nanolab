from __future__ import annotations

from pathlib import Path

from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.k6 import K6Task as SharedK6Task, k6_argv as shared_k6_argv
from sonata_tasks.k6_models import K6Config as SharedK6Config

from nanolab.tasks.loadtest.models import K6Config, K6RunResult


def _shared_config(config: K6Config, target_url: str) -> SharedK6Config:
    env = {**config.env, "NANOFAAS_URL": target_url}
    if config.payload_path is not None:
        env["NANOFAAS_PAYLOAD"] = str(config.payload_path)
    return SharedK6Config(config.script_path, config.summary_output_path, config.stages,
                          env, config.vus, config.duration)


def k6_argv(config: K6Config, target_url: str) -> tuple[str, ...]:
    return shared_k6_argv(_shared_config(config, target_url))


class K6Task(SharedK6Task):
    def __init__(self, config: K6Config, *, executor: CommandTaskExecutor, role: str,
                 remote_dir: str | None = None, title: str = "Run k6",
                 cwd: Path | None = None, require_pass: bool = False) -> None:
        def resolve(inputs: TaskInputs) -> SharedK6Config:
            target = config.target_url if isinstance(config.target_url, str) \
                else inputs.resource(config.target_url)
            return _shared_config(config, target)

        key = fingerprint_digest(
            {
                "script_path": config.script_path,
                "summary_output_path": config.summary_output_path,
                "stages": tuple((stage.duration, stage.target) for stage in config.stages),
                "env": config.env,
                "vus": config.vus,
                "duration": config.duration,
                "payload_path": config.payload_path,
                "target": (
                    config.target_url
                    if isinstance(config.target_url, str)
                    else {"resource": config.target_url.title}
                ),
            }
        )
        super().__init__(resolve, executor=executor, role=role,
                         options=CommandOptions(cwd=cwd, remote_dir=remote_dir),
                         title=title, semantic_key=f"nanolab-k6:v2:{key}",
                         require_pass=require_pass)


__all__ = ["K6RunResult", "K6Task", "k6_argv"]
