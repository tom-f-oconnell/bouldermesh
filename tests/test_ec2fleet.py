#!/usr/bin/env python3
"""EC2 machines that connect here for work, started and ended from the admin page (ec2fleet.py), against stand-ins for
AWS and a real board of machines: starting one with a token of its own, its user data, ending it at once or once its
climb is done (given no new one meanwhile), and AWS ending one itself. Offline.

    python3 tests/test_ec2fleet.py
"""
from __future__ import annotations

import json
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

    spot = {"us-east-1a": 0.295, "us-east-1b": 0.31}

    def describe_spot_price_history(self, InstanceTypes, ProductDescriptions, StartTime, AvailabilityZone=None):
        return {"SpotPriceHistory": [{"SpotPrice": f"{p:.4f}", "AvailabilityZone": z} for z, p in self.spot.items()
                                     if AvailabilityZone in (None, z)]}


class FakePricing:
    def get_products(self, ServiceCode, Filters, MaxResults):
        f = {x["Field"]: x["Value"] for x in Filters}
        assert ServiceCode == "AmazonEC2" and f["regionCode"] == "us-east-1" and f["operatingSystem"] == "Linux"
        usd = {"g4dn.2xlarge": "0.7520000000", "g6.4xlarge": "1.3232000000"}.get(f["instanceType"])
        return {"PriceList": [json.dumps({"terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": usd}}}}}}})]
                if usd else []}


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
clients = {"ec2": ec2, "ssm": FakeSSM(), "sts": FakeSTS(), "pricing": FakePricing()}
s = {**settings_mod.DEFAULTS, "compute.provider": "pull"}
url = ["https://boulders.example"]
told = []
fleet = ec2fleet.Fleet(TMP / "ec2.sqlite", board, lambda: s, lambda: {}, lambda: "srv1", lambda: url[0],
                       notify=lambda title, text: told.append((title, text)), log=lambda line: None, client=lambda name: clients[name])

# --- checking, without starting one or making a token -----------------------------------------------------------------
out = fleet.test()
assert "account 1234" in out and "g4dn.2xlarge" in out and "ami-dl" in out and "about $0.33" in out, out
assert ec2.calls[-1][1]["DryRun"] and not board.q("SELECT * FROM machines"), "a dry run, and no machine made"
try:
    fleet.test("not a type; rm -rf /")
    raise AssertionError("should have refused")
except ValueError as e:
    assert "isn't an EC2 instance type" in str(e)
print("checking OK")

# --- starting one: a machine of its own, and what the instance runs ---------------------------------------------------
try:
    fleet.start()
    raise AssertionError("should have refused")
except ValueError as e:
    assert "top it up first" in str(e) and "$0.00 left" in str(e), e
assert not board.q("SELECT * FROM machines"), "nothing made"
for bad in (0, "x", 1e6):
    try:
        fleet.top_up(bad)
        raise AssertionError("should have refused")
    except ValueError:
        pass
assert fleet.top_up(100) == "$100.00 left"
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

assert r["price"] == 0.31 and r["volume_gb"] == 210, "a spot price before its zone is known: the dearest zone's"
r2 = fleet.start("g6.4xlarge", spot=False)
assert r2["instance_type"] == "g6.4xlarge" and board.get(r2["machine_id"])["name"] == "EC2 g6.4xlarge" and r2["price"] == 1.3232
try:
    fleet.start("g9.unknown", spot=False)
    raise AssertionError("should have refused")
except ValueError as e:
    assert "couldn't find what" in str(e) and "pricing:GetProducts" in str(e), e
before = len(board.q("SELECT * FROM machines"))
try:
    fleet.start("p5.48xlarge", spot=True)
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
ec2.spot["us-east-1a"] = 0.4
fleet.sweep()
assert fleet.row("i-1")["price"] == 0.295, "looked up again only hourly"
fleet.q("UPDATE ec2 SET price_at=? WHERE instance_id='i-1'", time.time() - ec2fleet.SPOT_EVERY - 1)
fleet.sweep()
assert fleet.row("i-1")["price"] == 0.4
ec2.spot["us-east-1a"] = 0.295
fleet.q("UPDATE ec2 SET price=0.295 WHERE instance_id='i-1'")
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

# --- the balance: charged by the minute, told as it runs low, stopped as it runs out --------------------------------
for x in fleet.q("SELECT * FROM ec2 WHERE ended IS NULL"):
    fleet.stop(x["instance_id"])
b = fleet.balance()
spent0 = b["spent"]
assert b["topped"] == 100 and b["per_hour"] == 0 and b["hours_left"] is None and abs(b["left"] - (100 - spent0)) < 1e-6, b
fleet.top_up(-(b["left"] - 10))     # $10 left
ec2.spot["us-east-1b"] = 0.295      # the same in every zone, so the same once its zone is known
r5 = fleet.start()
rate = ec2fleet.rate_of(r5["price"], r5["volume_gb"])
assert abs(rate - (0.295 + 210 * ec2fleet.DISK_RATE + ec2fleet.ADDRESS_RATE)) < 1e-9
assert abs(fleet.balance()["per_hour"] - rate) < 1e-3


def ago(hours):     # as if it had run this many hours unbilled
    fleet.q("UPDATE ec2 SET costed_at=? WHERE instance_id=?", time.time() - hours * 3600, r5["instance_id"])


ago(2)
fleet.sweep()
b = fleet.balance()
assert abs(b["left"] - (10 - 2 * rate)) < 0.01 and abs(fleet.row(r5["instance_id"])["cost"] - 2 * rate) < 0.01, b
assert not told or "low" not in told[-1][0]
ago((b["left"] - 1) / rate)     # $1 left: under a fifth of the $10 after the last top-up
fleet.sweep()
assert "running low" in told[-1][0] and "$1.00 left" in told[-1][1], told[-1]
n = len(told)
fleet.sweep()
assert len(told) == n, "told once"
try:
    fleet.start("g6.4xlarge", spot=False)
    raise AssertionError("should have refused")
except ValueError as e:
    assert "top it up first" in str(e) and "less than 1 h" in str(e), e
mid5 = r5["machine_id"]
job.state, job.machine = "running", mid5
ago(1.5 / rate)     # $0.50 past the end
fleet.sweep()
r5 = fleet.row(r5["instance_id"])
assert r5["stop_after_job"] and r5["stop_why"] == "balance" and not r5["ended"] and mid5 in board.parked, r5
assert "balance spent" in told[-1][0] and "end once theirs is done" in told[-1][1], told[-1]
assert "carry on" in fleet.top_up(5) and mid5 not in board.parked and not fleet.row(r5["instance_id"])["stop_after_job"], \
    "a top-up lets it carry on"
ago((fleet.balance()["left"] + 0.1) / rate)
fleet.sweep()
assert fleet.row(r5["instance_id"])["stop_why"] == "balance" and not fleet.row(r5["instance_id"])["ended"]
ago((fleet.balance()["left"] + ec2fleet.OVERRUN + 0.1) / rate)
fleet.sweep()
r5 = fleet.row(r5["instance_id"])
assert r5["ended"] and "past the end of the EC2 balance" in r5["ended_why"] and board.get(mid5)["revoked"], r5
assert "machines ended" in told[-1][0]
spent = fleet.balance()["spent"]
fleet.sweep()
assert fleet.balance()["spent"] == spent, "nothing charged once ended"
st = fleet.state()
assert st["balance"]["left"] < 0 and st["machines"][0]["cost"] > 0 and st["machines"][0]["rate"] == round(rate, 4), st
print("balance OK")
print("ALL EC2 FLEET TESTS PASSED")
