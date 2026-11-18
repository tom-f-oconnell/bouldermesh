#!/usr/bin/env python3
"""EC2 machines that connect here for work, started and ended from the admin page (ec2fleet.py), against stand-ins for
AWS and a real board of machines: starting one with a token of its own, its user data, ending it at once or once its
climb is done (given no new one meanwhile), and AWS ending one itself. Offline.

    python3 tests/test_ec2fleet.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import compute  # noqa: E402
import ec2fleet  # noqa: E402
import pullwork  # noqa: E402
import settings as settings_mod  # noqa: E402
from climbs.common import JobControl  # noqa: E402


class FakeEC2:
    def __init__(self):
        self.calls, self.instances, self.n = [], {}, 0

    def describe_images(self, ImageIds):
        return {"Images": [{"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [
            {"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 150}}, {"DeviceName": "/dev/sdb", "VirtualName": "ephemeral0"}]}]}

    def run_instances(self, **kw):
        self.calls.append(("run", kw))
        if kw.get("DryRun"):
            raise Exception("An error occurred (DryRunOperation) when calling the RunInstances operation: Request would have succeeded")
        if kw["InstanceType"] == "p5.48xlarge":
            raise Exception("VcpuLimitExceeded: You have requested more vCPU capacity than your current vCPU limit of 0")
        self.n += 1
        iid = f"i-{self.n}"
        tags = {t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}
        self.instances[iid] = {"InstanceId": iid, "State": {"Name": "running"}, "PublicIpAddress": f"203.0.113.{self.n}",
                               "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": tags}
        return {"Instances": [{"InstanceId": iid}]}

    def describe_instances(self, Filters):
        self.calls.append(("describe", Filters))
        (f,) = Filters
        key, value = f["Name"].removeprefix("tag:"), f["Values"][0]
        return {"Reservations": [{"Instances": [i for i in self.instances.values() if i["Tags"].get(key) == value]}]}

    def terminate_instances(self, InstanceIds):
        self.calls.append(("terminate", InstanceIds))
        for iid in InstanceIds:
            if iid not in self.instances:
                raise Exception("InvalidInstanceID.NotFound")
            self.instances[iid]["State"] = {"Name": "terminated"}

    def describe_spot_price_history(self, **kw):
        return {"SpotPriceHistory": [{"SpotPrice": "0.2950"}]}


class FakeSSM:
    def get_parameter(self, Name):
        assert Name == compute.DLAMI
        return {"Parameter": {"Value": "ami-dl"}}


class FakeSTS:
    def get_caller_identity(self):
        return {"Account": "1234"}


TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-ec2-"))
board = pullwork.Board(TMP / "machines.sqlite", TMP / "jobs", lambda: "ghcr.io/me/bouldermesh:abc-gpu", JobControl())
ec2 = FakeEC2()
clients = {"ec2": ec2, "ssm": FakeSSM(), "sts": FakeSTS()}
s = {**settings_mod.DEFAULTS, "compute.provider": "pull"}
url = ["https://boulders.example"]
told = []
fleet = ec2fleet.Fleet(TMP / "ec2.sqlite", board, lambda: s, lambda: {}, lambda: "srv1", lambda: url[0],
                       notify=lambda title, text: told.append((title, text)), log=lambda line: None, client=lambda name: clients[name])

# --- checking, without starting one or making a token -----------------------------------------------------------------
out = fleet.test()
assert "account 1234" in out and "g4dn.2xlarge" in out and "ami-dl" in out, out
assert ec2.calls[-1][1]["DryRun"] and not board.q("SELECT * FROM machines"), "a dry run, and no machine made"
try:
    fleet.test("not a type; rm -rf /")
    raise AssertionError("should have refused")
except ValueError as e:
    assert "isn't an EC2 instance type" in str(e)
print("checking OK")

# --- starting one: a machine of its own, and what the instance runs ---------------------------------------------------
r = fleet.start()
assert r["instance_id"] == "i-1" and r["instance_type"] == "g4dn.2xlarge" and r["spot"] == 1 and not r["ended"], r
m = board.get(r["machine_id"])
assert m and m["name"] == "EC2 g4dn.2xlarge" and not m["revoked"] and m["status"] == "approved", m
args = ec2.calls[-1][1]
assert args["ImageId"] == "ami-dl" and args["InstanceMarketOptions"]["MarketType"] == "spot"
assert args["InstanceInitiatedShutdownBehavior"] == "terminate" and args["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
assert args["BlockDeviceMappings"][0]["DeviceName"] == "/dev/sda1" and args["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == 150 + ec2fleet.ROOM_GB, \
    "room beyond the AMI's own disk"
assert "SecurityGroupIds" not in args, "the VPC's default: nothing reaches in"
tags = {t["Key"]: t["Value"] for t in args["TagSpecifications"][0]["Tags"]}
assert tags[ec2fleet.TAG] == "srv1" and tags[ec2fleet.MACHINE_TAG] == m["id"] and compute.TAG not in tags, \
    "not the aws provider's tag, whose instances it ends at startup"
ud = args["UserData"]
token = board.token(m["id"])["token"]
assert f"--token {token}" in ud and "--server https://boulders.example" in ud and "https://boulders.example/worker/bouldermesh_worker.py" in ud
assert "systemctl enable --now bouldermesh-worker" in ud and "Restart=always" in ud
script = TMP / "user-data.sh"
script.write_text(ud)
assert subprocess.run(["bash", "-n", str(script)]).returncode == 0, "its user data parses"

r2 = fleet.start("g6.4xlarge")
assert r2["instance_type"] == "g6.4xlarge" and board.get(r2["machine_id"])["name"] == "EC2 g6.4xlarge"
before = len(board.q("SELECT * FROM machines"))
try:
    fleet.start("p5.48xlarge")
    raise AssertionError("should have failed")
except ValueError as e:
    assert "VcpuLimitExceeded" in str(e)
gone = board.q("SELECT * FROM machines ORDER BY created DESC")[0]
assert len(board.q("SELECT * FROM machines")) == before + 1 and gone["revoked"], "its token revoked when AWS refused"
url[0] = None
try:
    fleet.start()
    raise AssertionError("should have refused")
except ValueError as e:
    assert "WORKER_URL" in str(e)
url[0] = "https://boulders.example"
print("starting OK")

# --- every minute: its address, zone and price -------------------------------------------------------------------------
fleet.sweep()
r = fleet.row("i-1")
assert r["state"] == "running" and r["address"] == "203.0.113.1" and r["zone"] == "us-east-1a" and r["price"] == 0.295, r
st = fleet.state()
assert [x["instance_id"] for x in st["machines"]] == ["i-2", "i-1"] and st["machines"][1]["cost"] == 0.0, st
print("sweep OK")

# --- ending one once its climb is done: given no new climb meanwhile -------------------------------------------------
mid = r["machine_id"]
machine = board.get(mid)
client = {"version": 99, "gpu": True, "gpu_free_mb": 12000, "gpu_total_mb": 16000}
job = pullwork.Job(id="job1", label="climb", input=TMP / "jobs" / "job1.tar", size=0, internet=False, image="x", owner=0)
job.state, job.machine, job.machine_name = "running", mid, machine["name"]
with board.cond:
    board.jobs[job.id] = job
assert board.busy(mid)
said = fleet.stop("i-1", after_job=True)
assert "once its climb is done" in said and fleet.row("i-1")["stop_after_job"] and not fleet.row("i-1")["ended"]
assert ec2.instances["i-1"]["State"]["Name"] == "running"
fleet.sweep()
assert not fleet.row("i-1")["ended"], "still meshing"
board.parked.clear()
again = ec2fleet.Fleet(TMP / "ec2.sqlite", board, lambda: s, lambda: {}, lambda: "srv1", lambda: url[0], log=lambda line: None,
                       client=lambda name: clients[name])
assert mid in board.parked, "kept from being given work over a restart"
job.state = "done"
assert board.work(board.get(mid), "203.0.113.1", client).get("held"), "given no new climb"
fleet.sweep()
r = fleet.row("i-1")
assert r["ended"] and "once its climb was done" in r["ended_why"] and ec2.instances["i-1"]["State"]["Name"] == "terminated"
assert board.get(mid)["revoked"] and mid not in board.parked
print("ending after its climb OK")

# --- ending one at once; and AWS ending one itself ---------------------------------------------------------------------
r3 = fleet.start()
said = fleet.stop(r3["instance_id"], after_job=True)
assert "ended" in said and fleet.row(r3["instance_id"])["ended"], "with no climb, at once"
ec2.instances["i-2"]["State"] = {"Name": "terminated"}
ec2.instances["i-2"]["StateReason"] = {"Message": "Server.SpotInstanceTermination: Spot instance termination"}
fleet.sweep()
r2 = fleet.row("i-2")
assert r2["ended"] and "SpotInstanceTermination" in r2["ended_why"] and board.get(r2["machine_id"])["revoked"]
assert told and "ended" in told[-1][0] and "SpotInstanceTermination" in told[-1][1], told
try:
    fleet.stop("i-2")
    raise AssertionError("should have refused")
except ValueError:
    pass
r4 = fleet.start()
del ec2.instances[r4["instance_id"]]
fleet.sweep()
assert not fleet.row(r4["instance_id"])["ended"], "just started: AWS may not list it yet"
fleet.q("UPDATE ec2 SET started=? WHERE instance_id=?", time.time() - ec2fleet.NEW_GRACE - 1, r4["instance_id"])
fleet.sweep()
assert fleet.row(r4["instance_id"])["ended"], "gone"
calls = len(ec2.calls)
fleet.sweep()
assert len(ec2.calls) == calls, "nothing running: no calls to AWS"
print("ending OK")
print("ALL EC2 FLEET TESTS PASSED")
