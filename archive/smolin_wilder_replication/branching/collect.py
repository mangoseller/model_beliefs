"""Collect the branching experiments with Amazon Bedrock batch inference (Converse API).

Two stages per experiment, run as separate batches because the follow-ups
replay the first-stage answers:

``initial``    one first-stage prompt per case and prefix (temperature 1, at
               most 2048 output tokens); one input file shared by both models.
``followups``  [first-stage prompt, the model's own first-stage answer verbatim,
               follow-up prompt] (temperature 1, at most 4096 output tokens);
               one input file per model.  Built from the released (or newly
               materialized) first-stage responses.

Files live under ``COLLECTION_DIR/branching/<experiment>/<stage>/``:
``input.jsonl`` / ``record_map.jsonl`` (initial), ``input_<model>.jsonl`` /
``record_map_<model>.jsonl`` (follow-ups), ``jobs.json``, and downloaded
``outputs/<model>/*.jsonl.out``.

Usage (``prepare`` needs no AWS access)::

    python -m archive.smolin_wilder_replication.branching.collect prepare --stage initial
    python -m archive.smolin_wilder_replication.branching.collect upload --stage initial --bucket B
    python -m archive.smolin_wilder_replication.branching.collect submit --stage initial --bucket B --role-arn ARN
    python -m archive.smolin_wilder_replication.branching.collect status --stage initial
    python -m archive.smolin_wilder_replication.branching.collect download --stage initial --bucket B
    python -m archive.smolin_wilder_replication.branching.materialize --stage initial --out build/responses
    LB_RESPONSES_DIR=build/responses python -m archive.smolin_wilder_replication.branching.collect prepare --stage followups
    ...

``--bucket``, ``--role-arn``, ``--profile``, and ``--region`` default to the
environment variables ``LB_BEDROCK_BUCKET``, ``LB_BEDROCK_ROLE_ARN``,
``AWS_PROFILE``, and ``AWS_REGION``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archive.smolin_wilder_replication.branching.design import (
    BRANCHING_COLLECTION_DIR,
    BRANCHING_RESPONSES_DIR,
    CASES_PATH,
    EXPERIMENTS,
    FOLLOWUP_MAX_TOKENS,
    INITIAL_MAX_TOKENS,
    MODELS,
    PROBABILITY_VARIANT,
    STAGES,
    TEMPERATURE,
    Experiment,
    prefix_key,
    read_jsonl,
    response_path,
    stable_id,
    write_jsonl,
)
from archive.smolin_wilder_replication.branching.prompts import load_cases
from archive.smolin_wilder_replication.models import BEDROCK_MODEL_IDS


def stage_dir(collection_dir: Path, experiment: str, stage: str) -> Path:
    return collection_dir / experiment / stage


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def request(record_id: str, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    return {
        "recordId": record_id,
        "modelInput": {
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": TEMPERATURE},
        },
    }


def prefixes(experiment: Experiment, cases: list[dict[str, Any]]):
    """Yield (case, record-map row) for every first-stage prefix, in collection order."""
    for case in cases:
        prompt_sha = hashlib.sha256(case["initial_prompt"].encode("utf-8")).hexdigest()
        for index in range(1, experiment.prefixes + 1):
            key = prefix_key(experiment, case["domain"], case["instance_id"], index)
            yield case, {
                "record_id": stable_id(f"initial:{key}"),
                experiment.prefix_id_field: key,
                "domain": case["domain"],
                "instance_id": case["instance_id"],
                experiment.prefix_index_field: index,
                "initial_prompt_sha256": prompt_sha,
            }


def prepare_initial(experiment: Experiment, cases: list[dict[str, Any]], out_dir: Path) -> None:
    inputs, record_map = [], []
    for case, row in prefixes(experiment, cases):
        inputs.append(
            request(row["record_id"], [user_message(case["initial_prompt"])], INITIAL_MAX_TOKENS)
        )
        record_map.append(row)
    if len({row["recordId"] for row in inputs}) != len(inputs):
        raise ValueError("duplicate first-stage record IDs")
    write_jsonl(out_dir / "input.jsonl", inputs)
    write_jsonl(out_dir / "record_map.jsonl", record_map)
    print(f"{experiment.key}/initial: prepared {len(inputs)} records")


def followup_requests(experiment: Experiment, case: dict[str, Any], model: str, prefix: dict[str, Any]):
    """Yield (record_id, follow-up task, extra record-map fields) for one prefix."""
    prefix_id = prefix[experiment.prefix_id_field]
    if experiment.key == "behavior":
        for task in case["followups"]:
            yield stable_id(f"followup:{model}:{prefix_id}:{task['prompt_variant']}"), task, {}
        return
    variant = PROBABILITY_VARIANT[case["domain"]]
    (task,) = [task for task in case["followups"] if task["prompt_variant"] == variant]
    for index in range(1, experiment.answers_per_prefix + 1):
        answer_id = f"{prefix_id}:q{index:02d}"
        yield stable_id(f"followup:{model}:{answer_id}"), task, {
            "probability_branch_id": answer_id,
            "probability_replicate": index,
        }


def prepare_followups(
    experiment: Experiment, cases: list[dict[str, Any]], responses_dir: Path, out_dir: Path
) -> None:
    for model in MODELS:
        answers = {
            row["record_id"]: row
            for row in read_jsonl(response_path(responses_dir, experiment.key, "initial", model))
        }
        inputs, record_map = [], []
        for case, prefix in prefixes(experiment, cases):
            answer = answers.get(prefix["record_id"])
            if answer is None or not answer["ok"]:
                raise ValueError(f"{model}: no first-stage answer for {prefix['record_id']}")
            messages_head = [
                user_message(case["initial_prompt"]),
                {"role": "assistant", "content": [{"text": answer["output_text"]}]},
            ]
            metadata = {key: value for key, value in prefix.items() if key != "record_id"}
            for record_id, task, extra in followup_requests(experiment, case, model, prefix):
                inputs.append(
                    request(record_id, messages_head + [user_message(task["prompt"])], FOLLOWUP_MAX_TOKENS)
                )
                record_map.append(
                    {
                        **metadata,
                        "record_id": record_id,
                        **extra,
                        "model_alias": model,
                        "model_id": BEDROCK_MODEL_IDS[model],
                        "prompt_variant": task["prompt_variant"],
                        "task_id": task["task_id"],
                        "prompt_id": task["prompt_id"],
                        "initial_record_id": prefix["record_id"],
                    }
                )
        if len({row["recordId"] for row in inputs}) != len(inputs):
            raise ValueError(f"{model}: duplicate follow-up record IDs")
        write_jsonl(out_dir / f"input_{model}.jsonl", inputs)
        write_jsonl(out_dir / f"record_map_{model}.jsonl", record_map)
        print(f"{experiment.key}/followups/{model}: prepared {len(inputs)} records")


# AWS requests
class BedrockBatch:
    def __init__(self, args: argparse.Namespace, experiment: str, stage: str):
        import boto3

        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.bucket = args.bucket
        self.role_arn = args.role_arn
        self.run_tag = args.run_tag
        self.experiment = experiment
        self.stage = stage
        self.dir = stage_dir(args.collection_dir, experiment, stage)
        self.s3_root = f"{args.s3_prefix.strip('/')}/branching/{experiment}/{stage}"

    def require(self, **values: str | None) -> None:
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise SystemExit(f"Missing required setting(s): {', '.join(missing)}")

    def input_name(self, model: str) -> str:
        return "input.jsonl" if self.stage == "initial" else f"input_{model}.jsonl"

    def jobs(self) -> dict[str, Any]:
        path = self.dir / "jobs.json"
        return json.loads(path.read_text()) if path.exists() else {"stage": self.stage, "jobs": {}}

    def save_jobs(self, payload: dict[str, Any]) -> None:
        (self.dir / "jobs.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    def upload(self) -> None:
        self.require(bucket=self.bucket)
        s3 = self.session.client("s3")
        for name in sorted({self.input_name(model) for model in MODELS}):
            path = self.dir / name
            if not path.exists():
                raise FileNotFoundError(f"Run prepare first: {path}")
            s3.upload_file(str(path), self.bucket, f"{self.s3_root}/input/{name}")
            print(f"uploaded {path} to s3://{self.bucket}/{self.s3_root}/input/{name}")

    def submit(self) -> None:
        self.require(bucket=self.bucket, role_arn=self.role_arn)
        client = self.session.client("bedrock")
        payload = self.jobs()
        for model in MODELS:
            if payload["jobs"].get(model, {}).get("jobArn"):
                print(f"{self.stage}/{model}: already submitted")
                continue
            job_name = f"lb-{self.experiment}-{self.stage}-{model}-{self.run_tag}".replace("_", "-")
            response = client.create_model_invocation_job(
                jobName=job_name,
                clientRequestToken=hashlib.sha256(job_name.encode("utf-8")).hexdigest(),
                modelId=BEDROCK_MODEL_IDS[model],
                roleArn=self.role_arn,
                modelInvocationType="Converse",
                inputDataConfig={
                    "s3InputDataConfig": {
                        "s3Uri": f"s3://{self.bucket}/{self.s3_root}/input/{self.input_name(model)}"
                    }
                },
                outputDataConfig={
                    "s3OutputDataConfig": {"s3Uri": f"s3://{self.bucket}/{self.s3_root}/output/{model}/"}
                },
                timeoutDurationInHours=24,
            )
            payload["jobs"][model] = {
                "model_id": BEDROCK_MODEL_IDS[model],
                "jobArn": response["jobArn"],
                "jobName": job_name,
                "submitted_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.save_jobs(payload)
            print(f"{self.stage}/{model}: submitted {response['jobArn']}")

    def status(self) -> None:
        client = self.session.client("bedrock")
        payload = self.jobs()
        statuses = {}
        for model, job in sorted(payload["jobs"].items()):
            response = client.get_model_invocation_job(jobIdentifier=job["jobArn"])
            statuses[model] = {
                "status": response.get("status"),
                "message": response.get("message", ""),
                "total": response.get("totalRecordCount"),
                "processed": response.get("processedRecordCount"),
                "success": response.get("successRecordCount"),
                "errors": response.get("errorRecordCount"),
            }
            print(f"{self.stage}/{model}: {json.dumps(statuses[model], sort_keys=True)}")
        payload["latest_status"] = statuses
        payload["status_checked_utc"] = datetime.now(timezone.utc).isoformat()
        self.save_jobs(payload)

    def download(self) -> None:
        self.require(bucket=self.bucket)
        s3 = self.session.client("s3")
        for model in sorted(self.jobs()["jobs"]):
            destination = self.dir / "outputs" / model
            destination.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            pages = s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=f"{self.s3_root}/output/{model}/"
            )
            for page in pages:
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".jsonl.out"):
                        s3.download_file(self.bucket, obj["Key"], str(destination / Path(obj["Key"]).name))
                        downloaded += 1
            print(f"{self.stage}/{model}: downloaded {downloaded} output files")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["prepare", "upload", "submit", "status", "download"])
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--experiments", nargs="+", choices=list(EXPERIMENTS), default=list(EXPERIMENTS))
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument(
        "--responses-dir",
        type=Path,
        default=BRANCHING_RESPONSES_DIR,
        help="first-stage responses replayed in the follow-ups",
    )
    parser.add_argument("--collection-dir", type=Path, default=BRANCHING_COLLECTION_DIR)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--bucket", default=os.environ.get("LB_BEDROCK_BUCKET"))
    parser.add_argument("--role-arn", default=os.environ.get("LB_BEDROCK_ROLE_ARN"))
    parser.add_argument("--s3-prefix", default="latent-beliefs")
    parser.add_argument("--run-tag", default="replication", help="suffix that makes job names unique")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for key in args.experiments:
        experiment = EXPERIMENTS[key]
        if args.command == "prepare":
            out_dir = stage_dir(args.collection_dir, key, args.stage)
            cases = load_cases(args.cases)
            if args.stage == "initial":
                prepare_initial(experiment, cases, out_dir)
            else:
                prepare_followups(experiment, cases, args.responses_dir, out_dir)
            continue
        batch = BedrockBatch(args, key, args.stage)
        getattr(batch, args.command)()


if __name__ == "__main__":
    main()
