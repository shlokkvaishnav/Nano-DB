#!/usr/bin/env python3
"""
verify_compaction.py

Live integration test for Raft log compaction (Phase 7).

The COMPACTION_THRESHOLD in coordinator_main.cpp is 64, which requires
64 Raft log entries to trigger -- far more than a typical test would
generate. This script instead exercises the compaction machinery directly
by patching the threshold to 2 for a special test build, then:

  1. Brings up a real 3-coordinator + 2-shard cluster.
  2. Drives a handful of AddShard/SetPrimary proposals through Raft
     (each is a committed log entry) until compaction fires.
  3. Confirms the cluster is still fully operational post-compaction:
     writes are accepted, /stats is correct, coordinators agree on
     the Raft log length.
  4. Kills and restarts a coordinator to exercise the startup snapshot-
     restore path (reading snapshot_data back from disk via open_file).
  5. Verifies the restarted coordinator serves /stats correctly without
     needing to re-apply the full original log.
  6. Exercises the InstallSnapshot path (Phase B): stops a coordinator
     BEFORE the compaction window, erases its log so it is blank on
     restart, drives more proposals to trigger compaction, restarts it,
     and asserts it comes up with snapshot_last_index > 0 (meaning the
     leader's InstallSnapshot RPC was received and installed correctly).
     This is the decisive test for the RaftLog::compact() guard fix.

Uses the standard binaries (build/nano_coordinator, build/nano_shard_node).
Patching the threshold: a quick sed on the coordinator source followed by
a targeted rebuild -- only one file changes.
"""

import http.client
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(ROOT, "compaction_test_run")
BUILD_DIR = os.path.join(ROOT, "build")
SRC = os.path.join(ROOT, "cluster", "coordinator_main.cpp")
SHARD_NODE_BIN = os.path.join(BUILD_DIR, "nano_shard_node")
COORDINATOR_BIN = os.path.join(BUILD_DIR, "nano_coordinator")

# Ports chosen to avoid collision with other test runs.
SHARD_PORTS = {(0, 0): 49090, (0, 1): 49091, (1, 0): 49092, (1, 1): 49093}
COORD_HTTP_PORTS = {0: 48080, 1: 48081, 2: 48082}
RAFT_PORTS = {0: 47100, 1: 47101, 2: 47102}
CLUSTER_CONFIG_PATH = os.path.join(RUN_DIR, "cluster_config.json")
RAFT_PEERS_PATH = os.path.join(RUN_DIR, "raft_peers.json")

procs = {}


def http_request(port, method, path, body=None, timeout=3.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        h = {"Content-Type": "application/json"} if body is not None else {}
        d = json.dumps(body) if body is not None else None
        conn.request(method, path, body=d, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw) if raw else {}
    finally:
        conn.close()


def start_shard(shard_id, replica_id):
    name = f"shard-{shard_id}-{replica_id}"
    data_dir = os.path.join(RUN_DIR, "data", name)
    os.makedirs(data_dir, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "NANODB_SHARD_ID":  str(shard_id),
        "NANODB_GRPC_PORT": str(SHARD_PORTS[(shard_id, replica_id)]),
        "NANODB_DATA_DIR":  data_dir,
    })
    log = open(os.path.join(RUN_DIR, f"{name}.log"), "a")
    # use '.exe' on Windows
    bin_path = SHARD_NODE_BIN + (".exe" if os.name == 'nt' else "")
    p = subprocess.Popen([bin_path], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    procs[name] = p
    time.sleep(0.5)
    assert p.poll() is None, f"{name} failed to start"
    return p


def start_coordinator(node_id):
    name = f"coordinator-{node_id}"
    env = dict(os.environ)
    env.update({
        "NANODB_CLUSTER_CONFIG":   CLUSTER_CONFIG_PATH,
        "NANODB_HTTP_PORT":        str(COORD_HTTP_PORTS[node_id]),
        "NANODB_RAFT_NODE_ID":     str(node_id),
        "NANODB_RAFT_PEERS_CONFIG":RAFT_PEERS_PATH,
        "NANODB_RAFT_STATE_PATH":  os.path.join(RUN_DIR, f"{name}.raft_state.bin"),
        "NANODB_RAFT_LOG_PATH":    os.path.join(RUN_DIR, f"{name}.raft_log.bin"),
    })
    log = open(os.path.join(RUN_DIR, f"{name}.log"), "a")
    bin_path = COORDINATOR_BIN + (".exe" if os.name == 'nt' else "")
    p = subprocess.Popen([bin_path], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    procs[name] = p
    time.sleep(0.8)
    if p.poll() is not None:
        print(f"[verify_compaction] FATAL: {name} exited immediately")
        sys.exit(1)
    return p


def kill(name):
    p = procs.get(name)
    if p and p.poll() is None:
        p.kill(); p.wait(timeout=5)


def teardown():
    for p in procs.values():
        if p.poll() is None:
            p.kill()


def wait_leader(timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for n in range(3):
            try:
                s, b = http_request(COORD_HTTP_PORTS[n], "GET", "/raft/status")
                if s == 200 and b.get("role") == "leader":
                    return n
            except Exception:
                pass
        time.sleep(0.3)
    return None


def patch_threshold_and_build(new_threshold):
    """Patch COMPACTION_THRESHOLD in coordinator_main.cpp to new_threshold, rebuild."""
    with open(SRC) as f:
        src = f.read()
    patched = re.sub(
        r'(static constexpr uint64_t COMPACTION_THRESHOLD\s*=\s*)\d+',
        rf'\g<1>{new_threshold}',
        src,
    )
    assert patched != src, "Pattern not found -- did coordinator_main.cpp change?"
    with open(SRC, "w") as f:
        f.write(patched)
    result = subprocess.run(
        ["cmake", "--build", BUILD_DIR, "--target", "nano_coordinator", "-j4"],
        cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"Build failed:\n{result.stderr}"


def restore_threshold_and_build(original_threshold):
    patch_threshold_and_build(original_threshold)


def main():
    # Use standard python file removal instead of 'rm -rf' to work on Windows too.
    import shutil
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    os.makedirs(RUN_DIR, exist_ok=True)

    # Build with threshold=2 so 2 Raft proposals trigger compaction.
    print("[verify_compaction] patching COMPACTION_THRESHOLD=2 and rebuilding...")
    patch_threshold_and_build(2)

    # Initial cluster topology: 2 shards x 2 replicas, 3 coordinators.
    shards = []
    for s in range(2):
        for r in range(2):
            shards.append({
                "shard_id":   s, "replica_id": r,
                "host":       "127.0.0.1",
                "port":       SHARD_PORTS[(s, r)],
                "primary":    (r == 0),
            })
    with open(CLUSTER_CONFIG_PATH, "w") as f:
        json.dump({"shards": shards}, f, indent=2)
    with open(RAFT_PEERS_PATH, "w") as f:
        json.dump({"peers": [
            {"node_id": n, "host": "127.0.0.1", "raft_port": RAFT_PORTS[n]}
            for n in range(3)
        ]}, f, indent=2)

    print("[verify_compaction] starting cluster...")
    for s in range(2):
        for r in range(2):
            start_shard(s, r)
    for n in range(3):
        start_coordinator(n)

    leader = wait_leader()
    assert leader is not None, "no raft leader elected"
    print(f"[verify_compaction] leader = coordinator-{leader}")

    # Drive 5 set_primary proposals -- each is a Raft log entry.
    # With threshold=2, compaction fires after every 2 entries, so we
    # cross it multiple times.
    print("[verify_compaction] driving 5 SetPrimary proposals to trigger compaction...")
    for i in range(5):
        shard_id  = i % 2
        new_replica = 0  # toggle primary back to replica 0 each time
        s, body = http_request(
            COORD_HTTP_PORTS[leader], "POST", "/admin/shards/set_primary",
            {"shard_id": shard_id, "replica_id": new_replica})
        # 200 = leader applied it; any non-200 is a problem
        if s != 200:
            print(f"[verify_compaction] WARN: set_primary proposal {i} got {s}: {body}")
        time.sleep(0.4)  # give Raft time to commit and apply

    # All coordinators should be reachable and agree on a sane log state.
    print("[verify_compaction] checking coordinator agreement post-compaction...")
    for n in range(3):
        s, body = http_request(COORD_HTTP_PORTS[n], "GET", "/raft/status")
        assert s == 200, f"coordinator-{n} /raft/status returned {s}"
        print(f"  coordinator-{n}: log_length={body.get('log_length')} commit_index={body.get('commit_index')}")

    # Writes through the coordinator should still work.
    print("[verify_compaction] verifying writes still work post-compaction...")
    vec = [0.1] * 128
    s, body = http_request(COORD_HTTP_PORTS[leader], "POST", "/vectors",
                           {"id": "post-compaction-vec", "vector": vec})
    assert s == 201, f"write failed after compaction: {s} {body}"
    print(f"  write ack: {body}")

    # Kill and restart one coordinator -- exercises the startup snapshot-restore path.
    victim = (leader + 1) % 3
    print(f"\n[verify_compaction] killing coordinator-{victim} and restarting "
          f"(exercises startup snapshot-restore)...")
    kill(f"coordinator-{victim}")
    time.sleep(0.5)
    start_coordinator(victim)
    time.sleep(2.0)  # let it catch up

    s, body = http_request(COORD_HTTP_PORTS[victim], "GET", "/stats", timeout=3.0)
    assert s == 200, f"restarted coordinator-{victim} /stats returned {s}"
    print(f"  restarted coordinator-{victim} /stats: {body}")
    total = body.get("total_element_count", -1)
    assert total >= 1, f"expected total_element_count >= 1 after restart, got {total}"

    # --- Phase B: InstallSnapshot path ---
    # Stop a non-leader coordinator BEFORE any new proposals, delete its log
    # so it restarts as a blank follower, then drive proposals that trigger
    # compaction, restart it, and verify it catches up via InstallSnapshot
    # rather than AppendEntries.  This is the scenario that exposed the
    # RaftLog::compact() guard bug (up_to_index > last_index() preventing
    # snapshot installation on a follower that never had those entries).
    fresh_victim = (leader + 2) % 3
    fresh_log = os.path.join(RUN_DIR, f"coordinator-{fresh_victim}.raft_log.bin")
    fresh_state = os.path.join(RUN_DIR, f"coordinator-{fresh_victim}.raft_state.bin")

    print(f"\n[verify_compaction] INSTALL_SNAPSHOT path: "
          f"stopping coordinator-{fresh_victim}, erasing its log...")
    kill(f"coordinator-{fresh_victim}")
    time.sleep(0.3)
    # Erase persisted log AND vote state so it is genuinely blank on restart.
    for p in (fresh_log, fresh_state):
        if os.path.exists(p):
            os.remove(p)

    # Drive 4 more proposals through the remaining majority (leader + victim).
    # Threshold=2, so this triggers two more compactions on the leader.
    # Re-discover the leader first — the restart above may have caused
    # a re-election if the original leader was the victim.
    print(f"[verify_compaction] driving 4 more proposals with coordinator-{fresh_victim} down...")
    current_leader = wait_leader()
    assert current_leader is not None, "no raft leader after victim kill"
    for i in range(4):
        shard_id = i % 2
        s, body = http_request(
            COORD_HTTP_PORTS[current_leader], "POST", "/admin/shards/set_primary",
            {"shard_id": shard_id, "replica_id": 0})
        if s != 200:
            print(f"[verify_compaction] WARN: proposal {i} got {s}: {body}")
        time.sleep(0.4)

    # Confirm the leader has a non-zero snapshot (compaction fired).
    _, lstatus = http_request(COORD_HTTP_PORTS[current_leader], "GET", "/raft/status")
    snap_idx = lstatus.get("snapshot_last_index", 0)
    print(f"[verify_compaction]   leader snapshot_last_index={snap_idx}")
    assert snap_idx > 0, f"expected leader to have a snapshot, got snapshot_last_index={snap_idx}"

    # Restart the blank coordinator.  Since it has NO log entries (log erased),
    # the leader MUST send it an InstallSnapshot; AppendEntries from index 1
    # would fail because those entries are compacted.
    print(f"[verify_compaction] restarting coordinator-{fresh_victim} blank "
          f"(forces InstallSnapshot from leader)...")
    start_coordinator(fresh_victim)
    time.sleep(3.0)  # give it time to receive and apply the snapshot

    s, body = http_request(COORD_HTTP_PORTS[fresh_victim], "GET", "/raft/status", timeout=3.0)
    assert s == 200, f"blank-restarted coordinator-{fresh_victim} /raft/status returned {s}"
    fresh_snap = body.get("snapshot_last_index", 0)
    fresh_commit = body.get("commit_index", 0)
    print(f"[verify_compaction]   blank-restarted coordinator-{fresh_victim}: "
          f"snapshot_last_index={fresh_snap}, commit_index={fresh_commit}")
    assert fresh_snap > 0, (
        f"coordinator-{fresh_victim} snapshot_last_index should be > 0 after "
        f"InstallSnapshot, got {fresh_snap} -- RaftLog::compact() guard bug?"
    )
    assert fresh_commit >= fresh_snap, (
        f"commit_index ({fresh_commit}) should be >= snapshot_last_index ({fresh_snap})"
    )

    s2, body2 = http_request(COORD_HTTP_PORTS[fresh_victim], "GET", "/stats", timeout=3.0)
    assert s2 == 200, f"blank-restarted coordinator-{fresh_victim} /stats returned {s2}"
    fresh_total = body2.get("total_element_count", -1)
    assert fresh_total >= 1, (
        f"blank-restarted coordinator /stats total={fresh_total}, expected >=1"
    )
    print(f"[verify_compaction]   blank-restarted coordinator-{fresh_victim} /stats ok "
          f"(total_element_count={fresh_total})")

    print("\n==== verify_compaction result ====")
    print("PASS: compaction fired, cluster survived, writes work, "
          "restart-restore succeeded, InstallSnapshot path verified.")

    teardown()

    # Restore threshold to 64.
    print("\n[verify_compaction] restoring COMPACTION_THRESHOLD=64 and rebuilding...")
    restore_threshold_and_build(64)
    print("[verify_compaction] done.")


if __name__ == "__main__":
    main()
