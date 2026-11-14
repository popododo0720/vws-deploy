#!/usr/bin/env python3
"""aws subprocess boundary double; all state lives in the loopback fixture."""
import json
import os
import sys
from urllib.request import Request, urlopen

assert "CREDENTIALS_DIRECTORY" not in os.environ
assert "VWS_API_TOKEN_FILE" not in os.environ
args = sys.argv
if args[1:] == ["--version"]:
    print("aws-cli/2.39.4")
    sys.exit(0)
payload = [args[args.index("ec2") + 1], json.loads(args[args.index("--cli-input-json") + 1])]
auth = "EC2 " + os.environ["AWS_ACCESS_KEY_ID"] + ":" + os.environ["AWS_SECRET_ACCESS_KEY"]
request = Request(os.environ["AWS_ENDPOINT_URL_EC2"], json.dumps(payload).encode(),
                  headers={"Authorization": auth})
with urlopen(request, timeout=30) as response:
    sys.stdout.write(response.read().decode())
