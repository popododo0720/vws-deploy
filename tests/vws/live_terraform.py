#!/usr/bin/env python3
"""Terraform boundary double backed by the loopback live fixture."""
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

assert "CREDENTIALS_DIRECTORY" not in os.environ
assert "VWS_API_TOKEN_FILE" not in os.environ
args = sys.argv[1:]
operation = args[0]
payload = [operation, dict(run_id=os.environ["TF_VAR_live_run_id"],
                          instance_type=os.environ["TF_VAR_instance_type"],
                          image=os.environ.get("TF_VAR_image_id", ""), argv=args)]
auth = "EC2 " + os.environ["AWS_ACCESS_KEY_ID"] + ":" + os.environ["AWS_SECRET_ACCESS_KEY"]
endpoint = os.environ["TF_VAR_ec2_endpoint"].removesuffix("/ec2") + "/terraform"
with urlopen(Request(endpoint, json.dumps(payload).encode(),
                     headers={"Authorization": auth}), timeout=30) as response:
    data = json.load(response)
state = Path(os.environ["HOME"]) / "tf-state"
addresses = ["aws_instance.this", "aws_ebs_volume.this", "aws_volume_attachment.this"]
if operation == "apply":
    state.write_text("\n".join(addresses))
    for address in addresses:
        print(json.dumps(dict(type="apply_complete",
                             hook=dict(resource=dict(addr=address), action="create"))))
elif operation == "destroy":
    state.unlink(missing_ok=True)
elif operation == "state" and state.exists():
    print(state.read_text())
elif operation == "version":
    print('{"terraform_version":"1.17.1"}')
if data.get("fail"):
    print(json.dumps({"type": "diagnostic", "@level": "error",
                     "diagnostic": {"summary": "apply failed", "detail": os.environ["AWS_SECRET_ACCESS_KEY"],
                                    "address": "aws_instance.this"}}))
    sys.exit(1)
