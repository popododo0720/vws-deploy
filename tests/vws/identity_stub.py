#!/usr/bin/env python3
"""Loopback console/PVE identity fixture shared by shell scenario tests."""
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

from live_stub import PVE, SECRET, TOKEN, Stub


class IdentityStub(Stub):
    def do_GET(self):
        s = self.server
        state = json.loads((s.work / "identity.json").read_text())
        s.cap = {"kind": "proxmox", "cluster": {"name": state.get("cluster", "pve-test")},
                 "features": {"liveTests": True}}
        s.replies = {PVE + "/nodes/node1/subscription":
                     (state.get("status", 200), {"data": {"serverid": state.get("serverid")}})}
        with (s.work / "identity.calls").open("a") as log:
            log.write(self.command + " " + self.path + "\n")
        self.handle_request()


def main():
    work = Path(sys.argv[1])
    credentials = work / "credentials"
    credentials.mkdir()
    (credentials / "console").write_text(TOKEN)
    (credentials / "pve-token").write_text("vws@pve!live=" + SECRET)
    (work / "identity.json").write_text(json.dumps({"serverid": "TestServer826"}))
    with ThreadingHTTPServer(("127.0.0.1", 0), IdentityStub) as server:
        server.work = work
        server.calls, server.errors = [], []
        server.health = {"nodes": [{"name": "node1"}]}
        server.pve_url = f"http://127.0.0.1:{server.server_port}"
        server.pool_status = 200
        print(server.pve_url, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
