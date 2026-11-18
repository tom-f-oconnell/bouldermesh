"""EC2 machines that connect here for work, started and ended by hand from the admin page.

With machines that connect here (compute.provider "pull"), the admin page can also start an EC2 GPU instance. It runs
the worker script with a token of its own, so it works like any other machine there, alongside the rest. Nothing
starts or ends one by itself. It runs until it's ended from the admin page, either at once (its climb goes back to
wait for another machine) or once its climb is done (it's given no new one meanwhile).

AWS can still end one itself, as when it takes back a spot instance. That's noticed within a minute: its token is
revoked, and you're told.

Instances are tagged TAG with this server's id, a tag of their own: the "aws" provider (compute.py) ends any instance
with compute.TAG left from before a restart, and these are kept running.
"""
from __future__ import annotations

import re
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Callable

import compute

TAG = "bouldermesh-pull"
MACHINE_TAG = "bouldermesh-machine"
HOME = "/opt/bouldermesh"
USER = "ubuntu"             # the Deep Learning AMIs' (Ubuntu) user
NEW_GRACE = 300             # seconds an instance may be missing from AWS's answers just after it starts
ROOM_GB = 60                # at least this much disk beyond the AMI's own: the image (pulled, about 4x its download, as the
                            # script checks), two climbs and the models
ENDED_KEPT = 7 * 86400      # ended ones shown this long
ENDING = ("shutting-down", "terminated", "stopping", "stopped")


class Fleet:
    """The EC2 machines this server started for its machines that connect here.

    Args:
        db_path: where it keeps them.
        board: the machines that connect here (pullwork.Board), which makes each one's token.
        settings: the admin page's compute settings (compute.aws.*).
        creds: the AWS credentials saved on the admin page, if any.
        server_id: this server's id, for the instances' tag.
        url: the address machines reach this server at (WORKER_URL), or None.
        notify: called with (title, text) when AWS ends one itself.
        client: makes a boto3 client for a service; compute.aws_client() by default.
    """

    def __init__(self, db_path: Path, board, settings: Callable[[], dict], creds: Callable[[], dict],
                 server_id: Callable[[], str], url: Callable[[], str | None],
                 notify: Callable[[str, str], None] = lambda title, text: None, log: Callable[[str], None] = print,
                 client: Callable[[str], object] | None = None):
        self.db_path, self.board, self.settings, self.creds = db_path, board, settings, creds
        self.server_id, self.url, self.notify, self.log = server_id, url, notify, log
        self.client = client or (lambda service: compute.aws_client(self.settings(), self.creds(), service))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS ec2(instance_id TEXT PRIMARY KEY, machine_id TEXT, name TEXT,
                             instance_type TEXT, spot INTEGER, region TEXT, zone TEXT, price REAL, started REAL, state TEXT,
                             address TEXT, stop_after_job REAL, ended REAL, ended_why TEXT)""")
        for r in self.q("SELECT machine_id FROM ec2 WHERE ended IS NULL AND stop_after_job IS NOT NULL"):
            self.board.parked[r["machine_id"]] = PARKED

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args) -> list[dict]:
        with self.db() as con:
            return [dict(r) for r in con.execute(sql, args)]

    # -- starting one -------------------------------------------------------------------------------------------------
    def _url(self) -> str:
        url = (self.url() or "").rstrip("/")
        if not re.match(r"https?://[^\s'\"]+$", url):
            raise ValueError("this server's address for machines isn't known: set WORKER_URL (such as https://boulderme.sh)")
        return url

    def user_data(self, url: str, token: str) -> str:
        """What a new instance runs first: the worker script, as a service that starts again if it stops."""
        args = " ".join(shlex.quote(a) for a in ("--server", url, "--token", token, "--data", f"{HOME}/data"))
        return f"""#!/bin/bash
# a BoulderMesh machine on EC2: the worker script asks {url} for climbs to mesh, until the admin page ends it
exec >> /var/log/bouldermesh-setup.log 2>&1
install -d -o {USER} -g {USER} -m 700 {HOME} {HOME}/home {HOME}/data
usermod -aG docker {USER}
for i in $(seq 30); do
  curl -fsS -o {HOME}/bouldermesh_worker.py {shlex.quote(url + "/worker/bouldermesh_worker.py")} && break
  sleep 20
done
chown {USER}:{USER} {HOME}/bouldermesh_worker.py
cat > /etc/systemd/system/bouldermesh-worker.service <<'EOS'
[Unit]
Description=BoulderMesh worker
After=docker.service network-online.target
Wants=network-online.target docker.service

[Service]
User={USER}
Environment=BOULDERMESH_WORKER_HOME={HOME}/home
ExecStart=/usr/bin/python3 {HOME}/bouldermesh_worker.py {args}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOS
chmod 600 /etc/systemd/system/bouldermesh-worker.service
systemctl daemon-reload
systemctl enable --now bouldermesh-worker
"""

    def launch_args(self, ec2, instance_type: str, spot: bool, user_data: str, machine: dict) -> dict:
        s = self.settings()
        ami = s["compute.aws.ami"] or self.client("ssm").get_parameter(Name=compute.DLAMI)["Parameter"]["Value"]
        image = ec2.describe_images(ImageIds=[ami])["Images"][0]
        root = image["RootDeviceName"]
        least = max([m["Ebs"].get("VolumeSize", 0) for m in image.get("BlockDeviceMappings", [])
                     if m.get("DeviceName") == root and "Ebs" in m] or [0])
        args = {"ImageId": ami, "InstanceType": instance_type, "MinCount": 1, "MaxCount": 1, "UserData": user_data,
                "InstanceInitiatedShutdownBehavior": "terminate",
                # its jobs' containers can't reach the metadata service, and so its user data with the token
                "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
                "BlockDeviceMappings": [{"DeviceName": root, "Ebs": {"VolumeSize": max(int(s["compute.aws.volume_gb"]), least + ROOM_GB),
                                                                      "VolumeType": "gp3", "DeleteOnTermination": True}}],
                "TagSpecifications": [{"ResourceType": "instance", "Tags": [
                    {"Key": TAG, "Value": self.server_id()}, {"Key": MACHINE_TAG, "Value": machine["id"]},
                    {"Key": "Name", "Value": f"bouldermesh {machine['name']}"}]}]}
        if spot:
            args["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time",
                                                                                     "InstanceInterruptionBehavior": "terminate"}}
        if s["compute.aws.security_group"]:
            args["SecurityGroupIds"] = [s["compute.aws.security_group"]]
        if s["compute.aws.subnet"]:
            args["SubnetId"] = s["compute.aws.subnet"]
        if s["compute.aws.instance_profile"]:
            args["IamInstanceProfile"] = {"Name": s["compute.aws.instance_profile"]}
        return args

    def _choice(self, instance_type: str | None, spot: bool | None) -> tuple[str, bool]:
        s = self.settings()
        itype = (instance_type or "").strip() or s["compute.aws.instance_type"]
        if not re.match(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$", itype):
            raise ValueError(f"{itype!r} isn't an EC2 instance type, such as g4dn.2xlarge")
        return itype, bool(s["compute.aws.spot"] if spot is None else spot)

    def start(self, instance_type: str | None = None, spot: bool | None = None) -> dict:
        """Start an instance, with a machine (and token) of its own: its row."""
        itype, spot = self._choice(instance_type, spot)
        url = self._url()
        ec2 = self.client("ec2")
        machine = self.board.add(f"EC2 {itype}")
        try:
            args = self.launch_args(ec2, itype, spot, self.user_data(url, machine["token"]), machine)
            iid = ec2.run_instances(**args)["Instances"][0]["InstanceId"]
        except Exception as e:
            self.board.revoke(machine["id"])
            raise ValueError(f"EC2 wouldn't start a {itype}: {e}") from None
        self.q("INSERT INTO ec2(instance_id, machine_id, name, instance_type, spot, region, started, state) "
               "VALUES(?,?,?,?,?,?,?,?)", iid, machine["id"], machine["name"], itype, int(spot),
               self.settings()["compute.aws.region"], time.time(), "pending")
        self.log(f"ec2: started {iid}, {itype}{' (spot)' if spot else ''}, as machine {machine['name']}")
        return self.row(iid)

    def test(self, instance_type: str | None = None, spot: bool | None = None) -> str:
        """Check the credentials and that an instance could be started, without starting one or making a token."""
        itype, spot = self._choice(instance_type, spot)
        url = self._url()
        account = self.client("sts").get_caller_identity()["Account"]
        ec2 = self.client("ec2")
        args = self.launch_args(ec2, itype, spot, self.user_data(url, "bmw_test"), {"id": "test", "name": "test"})
        try:
            ec2.run_instances(DryRun=True, **args)
        except Exception as e:
            if "DryRunOperation" not in str(e):
                raise ValueError(f"EC2 wouldn't start a {itype}{' spot' if spot else ''} instance: {e}") from None
        return (f"account {account}: allowed to start a {itype}{' spot' if spot else ''} instance from {args['ImageId']}, "
                f"its worker reaching {url} (AWS's quota for its kind can still refuse it when it's started)")

    # -- ending one ---------------------------------------------------------------------------------------------------
    def row(self, iid: str) -> dict | None:
        rows = self.q("SELECT * FROM ec2 WHERE instance_id=?", iid)
        return rows[0] if rows else None

    def stop(self, iid: str, after_job: bool = False) -> str:
        """End one: at once, or with after_job once its climb is done (it's given no new one meanwhile)."""
        r = self.row(iid)
        if not r or r["ended"]:
            raise ValueError("no such EC2 machine running")
        if after_job and self.board.busy(r["machine_id"]):
            self.q("UPDATE ec2 SET stop_after_job=? WHERE instance_id=?", time.time(), iid)
            self.board.parked[r["machine_id"]] = PARKED
            return f"{r['name']} ({iid}) is given no new climb, and ends once its climb is done"
        self._end(r, "ended from the admin page")
        return f"ended {r['name']} ({iid})" + ("; its climb waits for another machine" if self.board.busy(r["machine_id"]) else "")

    def _end(self, r: dict, why: str, terminate: bool = True) -> None:
        if terminate:
            try:
                self.client("ec2").terminate_instances(InstanceIds=[r["instance_id"]])
            except Exception as e:
                if "NotFound" not in str(e):
                    raise
        self.board.parked.pop(r["machine_id"], None)
        self.board.revoke(r["machine_id"])
        self.q("UPDATE ec2 SET ended=?, ended_why=? WHERE instance_id=?", time.time(), why, r["instance_id"])
        self.log(f"ec2: {r['name']} ({r['instance_id']}) {why}")

    # -- every minute -------------------------------------------------------------------------------------------------
    def sweep(self) -> None:
        """End those waiting for their climb to be done once it is, and notice any AWS ended itself, and their addresses."""
        live = self.q("SELECT * FROM ec2 WHERE ended IS NULL")
        if not live:
            return
        for r in live:
            if r["stop_after_job"]:
                self.board.parked[r["machine_id"]] = PARKED
                if not self.board.busy(r["machine_id"]):
                    self._end(r, "ended once its climb was done, as asked")
        live = [r for r in live if not self.row(r["instance_id"])["ended"]]
        if not live:
            return
        ec2 = self.client("ec2")
        found = {i["InstanceId"]: i for res in ec2.describe_instances(Filters=[{"Name": f"tag:{TAG}", "Values": [self.server_id()]}])
                 ["Reservations"] for i in res["Instances"]}
        now = time.time()
        for r in live:
            i = found.get(r["instance_id"])
            if i is None and now - r["started"] < NEW_GRACE:
                continue
            state = i["State"]["Name"] if i else "gone"
            if state in ENDING or i is None:
                reason = ((i or {}).get("StateReason") or {}).get("Message") or "it's no longer there"
                self._end(r, f"ended by AWS ({reason})", terminate=state in ("stopping", "stopped"))
                self.notify(f"EC2 machine {r['name']} ended", f"AWS ended {r['instance_id']} ({r['instance_type']}"
                            f"{', spot' if r['spot'] else ''}) after {(now - r['started']) / 3600:.1f} h: {reason}. "
                            "Its climb, if any, waits for another machine.")
                continue
            zone = (i.get("Placement") or {}).get("AvailabilityZone")
            price = r["price"]
            if r["spot"] and price is None and zone:
                price = self._spot_price(ec2, r["instance_type"], zone)
            self.q("UPDATE ec2 SET state=?, address=?, zone=?, price=? WHERE instance_id=?", state,
                   i.get("PublicIpAddress"), zone, price, r["instance_id"])

    def _spot_price(self, ec2, itype: str, zone: str) -> float | None:
        try:
            h = ec2.describe_spot_price_history(InstanceTypes=[itype], AvailabilityZone=zone,
                                                ProductDescriptions=["Linux/UNIX"], StartTime=time.time(), MaxResults=1)
            return float(h["SpotPriceHistory"][0]["SpotPrice"])
        except Exception:
            return None

    def state(self) -> dict:
        """For the admin page: those running, and those ended in the last week, newest first, each with hours up."""
        now = time.time()
        rows = self.q("SELECT * FROM ec2 WHERE ended IS NULL OR ended>? ORDER BY started DESC", now - ENDED_KEPT)
        for r in rows:
            r["hours"] = round(((r["ended"] or now) - r["started"]) / 3600, 2)
            r["cost"] = round(r["hours"] * r["price"], 2) if r["price"] is not None else None
        return {"machines": rows, "url": self.url() or None}


PARKED = "to be ended once its climb is done (EC2)"
