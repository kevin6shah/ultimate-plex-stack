#!/usr/bin/env bash
set -euo pipefail

AWS_PROFILE_NAME="${AWS_PROFILE:-iris}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
EC2_SSH_KEY_PATH="${EC2_SSH_KEY:-$HOME/.aws/keys/iris-migration-20260510.pem}"
SHARED_STACK_NAME="${SHARED_STACK_NAME:-friday-shared-host}"
WORKER_STACK_NAME="${WORKER_STACK_NAME:-friday-hands-worker}"

export AWS_PROFILE="$AWS_PROFILE_NAME"
export AWS_REGION="$AWS_REGION_NAME"

shared_host_ip="$(
  aws cloudformation describe-stacks \
    --stack-name "$SHARED_STACK_NAME" \
    --query 'Stacks[0].Outputs[?OutputKey==`PublicIp`].OutputValue' \
    --output text
)"

worker_instance_id="$(
  aws cloudformation describe-stacks \
    --stack-name "$WORKER_STACK_NAME" \
    --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' \
    --output text
)"

python3 - <<'PY' "$EC2_SSH_KEY_PATH" "$shared_host_ip" "$worker_instance_id"
import subprocess
import sys

key_path, shared_host_ip, worker_instance_id = sys.argv[1:4]

def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)

running_ids: list[str] = []
if shared_host_ip and shared_host_ip != "None":
    result = run(
        [
            "ssh",
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            f"ubuntu@{shared_host_ip}",
            "temporal workflow list --address 127.0.0.1:7233",
        ],
        check=False,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Running":
                running_ids.append(parts[1])
    for workflow_id in running_ids:
        run(
            [
                "ssh",
                "-i",
                key_path,
                "-o",
                "StrictHostKeyChecking=no",
                f"ubuntu@{shared_host_ip}",
                f"temporal workflow terminate --address 127.0.0.1:7233 --workflow-id {workflow_id} --reason 'operator stop all'",
            ],
            check=False,
        )

if worker_instance_id and worker_instance_id != "None":
    run(["aws", "ec2", "stop-instances", "--instance-ids", worker_instance_id], check=False)

print("TERMINATED_WORKFLOWS=" + (" ".join(running_ids) if running_ids else ""))
print("WORKER_INSTANCE=" + worker_instance_id)
PY
