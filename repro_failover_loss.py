#!/usr/bin/env python3
"""
repro_failover_loss.py

Deterministic, isolated proof for the failover data-loss hypothesis formed
during Phase 5 chaos-harness runs: a confirmed write (HTTP 201, quorum met)
can be permanently lost if the replica later promoted to primary wasn't
one of the replicas that acked it.

This does NOT rely on chaos-loop timing luck or the automatic
3-consecutive-failure health check. It constructs the exact scenario by
hand, step by step, using the manual /admin/shards/set_primary endpoint
(the doc's own "exists alongside the automatic path for deterministic
testing" mechanism) to promote a *specific, known-empty* replica:

  1. Start shard 0 with replica 0 (primary) and replica 1 up; replica 2
     is deliberately NOT started yet, so it cannot ack anything.
  2. Insert one vector. Quorum for 3 replicas is 2 (primary mandatory +
     1 more) -- primary + replica 1 ack, quorum met without replica 2,
     client gets 201.
  3. Confirm /stats shows the write landed (element_count == 1).
  4. Start replica 2 now (it comes up empty -- never got the insert).
  5. Manually promote replica 2 to primary via /admin/shards/set_primary
     (bypassing the automatic health-check path entirely, so this is not
     a timing race -- it's a deliberate, known-bad promotion target,
     constructed the same way a real automatic failover *could* pick it
     if replica 2 happened to be the first reachable candidate).
  6. Restart replica 0 (the old primary) so every replica is reachable
     and /stats is NOT degraded -- isolating "promoted replica lacks the
     data" from "a replica happens to be unreachable right now".
  7. Check /stats again. If total_element_count drops to 0 with a
     non-degraded response, the hypothesis is proven: a confirmed write
     vanished, with zero reliance on timing.
"""

import http.client
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(ROOT, "repro_run")
BUILD_DIR = os.path.join(ROOT, "build")
SHARD_NODE_BIN = os.path.join(BUILD_DIR, "nano_shard_node")
COORDINATOR_BIN = os.path.join(BUILD_DIR, "nano_coordinator")

SHARD_PORTS = {0: 29090, 1: 29091, 2: 29092}
COORD_HTTP_PORT = 28080
RAFT_PORT = 27100
CLUSTER_CONFIG_PATH = os.path.join(RUN_DIR, "cluster_config.json")
RAFT_PEERS_PATH = os.path.join(RUN_DIR, "raft_peers.json")


def http_request(port, method, path, body=None, timeout=3.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        parsed = json.loads(raw) if raw else {}
        return resp.status, parsed
    finally:
        conn.close()


procs = {}


def start_shard(replica_id):
    name = f"shard-0-{replica_id}"
    data_dir = os.path.join(RUN_DIR, "data", name)
    os.makedirs(data_dir, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "NANODB_SHARD_ID": "0",
        "NANODB_GRPC_PORT": str(SHARD_PORTS[replica_id]),
        "NANODB_DATA_DIR": data_dir,
    })
    log = open(os.path.join(RUN_DIR, f"{name}.log"), "a")
    p = subprocess.Popen([SHARD_NODE_BIN], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    procs[name] = p
    time.sleep(0.5)
    assert p.poll() is None, f"{name} failed to start"
    print(f"[repro] started {name} on port {SHARD_PORTS[replica_id]}")


def kill(name):
    p = procs.get(name)
    if p and p.poll() is None:
        p.kill()
        p.wait(timeout=5)
    print(f"[repro] killed {name}")


def start_coordinator():
    name = "coordinator-0"
    env = dict(os.environ)
    env.update({
        "NANODB_CLUSTER_CONFIG": CLUSTER_CONFIG_PATH,
        "NANODB_HTTP_PORT": str(COORD_HTTP_PORT),
        "NANODB_RAFT_NODE_ID": "0",
        "NANODB_RAFT_PEERS_CONFIG": RAFT_PEERS_PATH,
        "NANODB_RAFT_STATE_PATH": os.path.join(RUN_DIR, "raft_state.bin"),
        "NANODB_RAFT_LOG_PATH": os.path.join(RUN_DIR, "raft_log.bin"),
    })
    log = open(os.path.join(RUN_DIR, f"{name}.log"), "a")
    p = subprocess.Popen([COORDINATOR_BIN], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    procs[name] = p
    time.sleep(0.8)
    assert p.poll() is None, "coordinator failed to start"
    print("[repro] started coordinator-0")


def fail(msg):
    print(f"\n[repro] FAILED: {msg}")
    teardown()
    sys.exit(1)


def teardown():
    for name, p in procs.items():
        if p.poll() is None:
            p.kill()


def main():
    subprocess.run(["rm", "-rf", RUN_DIR])
    os.makedirs(RUN_DIR, exist_ok=True)

    # All 3 replicas listed in the static topology from the start (this
    # matters: set_primary's validity check only requires the replica to
    # be "active" in the topology, not currently reachable -- exactly
    # like the real system, replica 2 can be registered but down).
    shards = [
        {"shard_id": 0, "replica_id": 0, "host": "127.0.0.1", "port": SHARD_PORTS[0], "primary": True},
        {"shard_id": 0, "replica_id": 1, "host": "127.0.0.1", "port": SHARD_PORTS[1], "primary": False},
        {"shard_id": 0, "replica_id": 2, "host": "127.0.0.1", "port": SHARD_PORTS[2], "primary": False},
    ]
    with open(CLUSTER_CONFIG_PATH, "w") as f:
        json.dump({"shards": shards}, f, indent=2)
    with open(RAFT_PEERS_PATH, "w") as f:
        json.dump({"peers": [{"node_id": 0, "host": "127.0.0.1", "raft_port": RAFT_PORT}]}, f, indent=2)

    print("[repro] step 1: starting replica 0 (primary) and replica 1 only; "
          "replica 2 stays down")
    start_shard(0)
    start_shard(1)
    start_coordinator()

    for _ in range(20):
        try:
            status, body = http_request(COORD_HTTP_PORT, "GET", "/raft/status")
            if status == 200 and body.get("role") == "leader":
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        fail("coordinator never became raft leader")
    print("[repro] coordinator is raft leader (single-node cluster)")

    print("\n[repro] step 2: inserting one vector with replica 2 down "
          "(quorum must come from primary + replica 1 only)")
    vec = [0.1] * 128
    status, body = http_request(COORD_HTTP_PORT, "POST", "/vectors",
                                 {"id": "probe-1", "vector": vec})
    if status != 201:
        fail(f"insert did not get quorum with replica 2 down: status={status} body={body}")
    print(f"[repro] insert confirmed: {body}")
    if body.get("acks") != 2 or body.get("needed") != 2:
        fail(f"expected acks=2/needed=2 (primary + replica 1 only), got {body}")
    print("[repro] confirmed: quorum was met via primary + replica 1 ONLY -- "
          "replica 2 did not ack and does not have this vector")

    status, body = http_request(COORD_HTTP_PORT, "GET", "/stats")
    print(f"[repro] step 3: /stats after insert: {body}")
    if body.get("total_element_count") != 1:
        fail(f"expected total_element_count=1 after the confirmed insert, got {body}")

    print("\n[repro] step 4: starting replica 2 now (comes up with ZERO data)")
    start_shard(2)

    print("\n[repro] step 5: manually promoting replica 2 to primary via "
          "/admin/shards/set_primary (the exact replica that never got probe-1)")
    status, body = http_request(COORD_HTTP_PORT, "POST", "/admin/shards/set_primary",
                                 {"shard_id": 0, "replica_id": 2})
    if status != 200:
        fail(f"set_primary failed: status={status} body={body}")
    print(f"[repro] set_primary response: {body}")

    print("\n[repro] step 6: restarting replica 0 (old primary) so every "
          "replica is reachable -- isolating 'wrong replica promoted' from "
          "'a replica happens to be unreachable'")
    kill("shard-0-0")
    start_shard(0)
    time.sleep(1.0)

    print("\n[repro] step 7: checking /stats post-promotion")
    status, body = http_request(COORD_HTTP_PORT, "GET", "/stats")
    print(f"[repro] /stats after promoting the empty replica: {body}")

    degraded = body.get("degraded", False)
    total = body.get("total_element_count")

    print("\n==== repro_failover_loss result ====")
    print(f"degraded_response      : {degraded}")
    print(f"total_element_count    : {total}")
    print(f"expected (correct)     : 1")

    if degraded:
        print("\n[repro] INCONCLUSIVE: response was degraded -- a replica was "
              "unreachable when /stats was queried, so a dropped total here "
              "could be the (expected, self-reported, transient) degraded "
              "case rather than the hypothesis. Re-run.")
        teardown()
        sys.exit(2)

    if total == 0:
        print("\n[repro] HYPOTHESIS CONFIRMED: a confirmed (HTTP 201) write is "
              "permanently invisible after promoting a replica that never "
              "acked it, with a NON-degraded /stats response (the system "
              "claims full visibility and is still wrong). This reproduces "
              "deterministically -- zero dependence on chaos-loop timing.")

        print("\n[repro] step 8: scoping the blast radius -- can a search "
              "still find probe-1 via eventual consistency (which can route "
              "to a non-primary replica), versus strong consistency (which "
              "only ever asks the current primary)?")
        for consistency in ("eventual", "strong"):
            sstatus, sbody = http_request(
                COORD_HTTP_PORT, "POST", "/search",
                {"vector": vec, "k": 1, "consistency": consistency})
            found = any(r.get("id") == "probe-1" for r in sbody.get("results", []))
            print(f"[repro]   consistency={consistency:9s} found_probe_1={found}  raw={sbody}")

        teardown()
        sys.exit(0)
    else:
        print(f"\n[repro] HYPOTHESIS NOT CONFIRMED: total_element_count={total}, "
              f"expected 0 under the hypothesis. Some other mechanism (e.g. "
              f"the new primary replaying/syncing from a peer) must be "
              f"protecting the data here -- worth reading shard_service_impl "
              f"and raft application code again before concluding anything.")
        teardown()
        sys.exit(3)


if __name__ == "__main__":
    main()
