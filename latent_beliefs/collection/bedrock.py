"""Collect responses from the seven open-weight models via Amazon Bedrock batch inference.

One batch per domain: every prompt of the 16 tasks, sent once to each model in
``models.BEDROCK_MODEL_IDS`` through the Converse API (a single user message,
temperature 1, at most 4096 output tokens).  Files live under
``<collection>/<domain>/bedrock/``:

    input.jsonl         batch input (one record per prompt; shared by all models)
    record_map.jsonl    record ID -> task_id, prompt_id
    jobs.json           submitted job ARNs
    outputs/<model>/    downloaded ``*.jsonl.out`` files

Subcommands: ``prepare`` (writes the input files; no AWS access), ``upload``,
``submit``, ``status``, ``download``.  AWS access needs an S3 bucket the
Bedrock service role can read and write, and that role's ARN::

    python -m latent_beliefs.collection.bedrock prepare
    python -m latent_beliefs.collection.bedrock upload --bucket B
    python -m latent_beliefs.collection.bedrock submit --bucket B --role-arn ARN
    python -m latent_beliefs.collection.bedrock status
    python -m latent_beliefs.collection.bedrock download --bucket B

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

from latent_beliefs.collection.records import (
    MAX_TOKENS,
    TEMPERATURE,
    WORKLOADS,
    load_task_rows,
    write_record_map,
)
from latent_beliefs.config import COLLECTION_DIR, DOMAINS, PROMPTS_DIR
from latent_beliefs.models import BEDROCK_MODEL_IDS


def batch_dir(collection_dir: Path, domain: str) -> Path:
    return collection_dir / domain / "bedrock"


def prepare(domain: str, prompts_dir: Path, collection_dir: Path) -> None:
    rows = load_task_rows(domain, prompts_dir)
    out_dir = batch_dir(collection_dir, domain)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = out_dir / "input.jsonl"
    with input_path.open("w", encoding="utf-8") as inputs:
        for row in rows:
            record = {
                "recordId": row["recordId"],
                "modelInput": {
                    "messages": [{"role": "user", "content": [{"text": row["prompt"]}]}],
                    "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
                },
            }
            inputs.write(json.dumps(record, ensure_ascii=False) + "\n")
    write_record_map(rows, out_dir / "record_map.jsonl")
    print(f"{domain}: prepared {len(rows)} records ({input_path.stat().st_size} bytes)")


# AWS requests
class Bedrock:
    def __init__(self, args: argparse.Namespace):
        import boto3

        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.bucket = args.bucket
        self.role_arn = args.role_arn
        self.s3_prefix = args.s3_prefix.strip("/")
        self.run_tag = args.run_tag
        self.collection_dir = args.collection_dir

    def require(self, **values: str | None) -> None:
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise SystemExit(f"Missing required setting(s): {', '.join(missing)}")

    def input_key(self, domain: str) -> str:
        return f"{self.s3_prefix}/{WORKLOADS[domain]}/input/input.jsonl"

    def output_prefix(self, domain: str, model: str) -> str:
        return f"{self.s3_prefix}/{WORKLOADS[domain]}/output/{model}/"

    def jobs_path(self, domain: str) -> Path:
        return batch_dir(self.collection_dir, domain) / "jobs.json"

    def load_jobs(self, domain: str) -> dict[str, Any]:
        path = self.jobs_path(domain)
        if not path.exists():
            return {"workload": WORKLOADS[domain], "jobs": {}}
        return json.loads(path.read_text())

    def save_jobs(self, domain: str, payload: dict[str, Any]) -> None:
        self.jobs_path(domain).write_text(json.dumps(payload, indent=2, default=str) + "\n")

    def upload(self, domain: str) -> None:
        self.require(bucket=self.bucket)
        path = batch_dir(self.collection_dir, domain) / "input.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Run prepare first: {path}")
        key = self.input_key(domain)
        self.session.client("s3").upload_file(str(path), self.bucket, key)
        print(f"{domain}: uploaded {path.stat().st_size} bytes to s3://{self.bucket}/{key}")

    def submit(self, domain: str, models: list[str]) -> None:
        self.require(bucket=self.bucket, role_arn=self.role_arn)
        client = self.session.client("bedrock")
        payload = self.load_jobs(domain)
        failures: dict[str, str] = {}
        for model in models:
            if payload["jobs"].get(model, {}).get("jobArn"):
                print(f"{domain}/{model}: already submitted")
                continue
            model_id = BEDROCK_MODEL_IDS[model]
            job_name = f"latent-beliefs-{WORKLOADS[domain]}-{model}-{self.run_tag}".replace("_", "-")
            try:
                response = client.create_model_invocation_job(
                    jobName=job_name,
                    clientRequestToken=hashlib.sha256(job_name.encode("utf-8")).hexdigest(),
                    modelId=model_id,
                    roleArn=self.role_arn,
                    modelInvocationType="Converse",
                    inputDataConfig={
                        "s3InputDataConfig": {"s3Uri": f"s3://{self.bucket}/{self.input_key(domain)}"}
                    },
                    outputDataConfig={
                        "s3OutputDataConfig": {
                            "s3Uri": f"s3://{self.bucket}/{self.output_prefix(domain, model)}"
                        }
                    },
                    timeoutDurationInHours=24,
                )
            except Exception as exc:
                failures[model] = f"{type(exc).__name__}: {exc}"
                print(f"{domain}/{model}: submission failed: {failures[model]}")
                continue
            payload["jobs"][model] = {
                "model_id": model_id,
                "jobArn": response["jobArn"],
                "jobName": job_name,
                "submitted_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.save_jobs(domain, payload)
            print(f"{domain}/{model}: submitted {response['jobArn']}")
        if failures:
            payload["submission_failures"] = failures
            self.save_jobs(domain, payload)
            raise RuntimeError(f"{domain}: {len(failures)} submissions failed")

    def status(self, domain: str) -> None:
        client = self.session.client("bedrock")
        payload = self.load_jobs(domain)
        statuses: dict[str, Any] = {}
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
            print(f"{domain}/{model}: {json.dumps(statuses[model], sort_keys=True)}")
        payload["latest_status"] = statuses
        payload["status_checked_utc"] = datetime.now(timezone.utc).isoformat()
        self.save_jobs(domain, payload)

    def download(self, domain: str) -> None:
        self.require(bucket=self.bucket)
        s3 = self.session.client("s3")
        payload = self.load_jobs(domain)
        for model in sorted(payload["jobs"]):
            destination = batch_dir(self.collection_dir, domain) / "outputs" / model
            destination.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            pages = s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.output_prefix(domain, model)
            )
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith(".jsonl.out"):
                        s3.download_file(self.bucket, key, str(destination / Path(key).name))
                        downloaded += 1
            print(f"{domain}/{model}: downloaded {downloaded} output files")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["prepare", "upload", "submit", "status", "download"])
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument(
        "--models", nargs="+", choices=sorted(BEDROCK_MODEL_IDS), default=list(BEDROCK_MODEL_IDS)
    )
    parser.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
    parser.add_argument("--collection-dir", type=Path, default=COLLECTION_DIR)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--bucket", default=os.environ.get("LB_BEDROCK_BUCKET"))
    parser.add_argument("--role-arn", default=os.environ.get("LB_BEDROCK_ROLE_ARN"))
    parser.add_argument("--s3-prefix", default="latent-beliefs")
    parser.add_argument(
        "--run-tag", default="replication", help="suffix that makes Bedrock job names unique"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "prepare":
        for domain in args.domains:
            prepare(domain, args.prompts_dir, args.collection_dir)
        return
    bedrock = Bedrock(args)
    for domain in args.domains:
        if args.command == "upload":
            bedrock.upload(domain)
        elif args.command == "submit":
            bedrock.submit(domain, args.models)
        elif args.command == "status":
            bedrock.status(domain)
        elif args.command == "download":
            bedrock.download(domain)


if __name__ == "__main__":
    main()
