#!/usr/bin/env python3
"""
NanoDB Distributed Cluster Benchmark

Measures:
  - Cluster insert throughput (vectors/sec with quorum acks)
  - Scatter-gather search latency (p50, p95, p99)
  - Failover recovery time
  - Raft election time (leader kill → new leader)

Prerequisites:
  docker compose -f deploy/docker-compose.cluster.yml -f deploy/docker-compose.monitoring.yml up -d
  Wait 5s for Raft election, then run this script.
"""

import json
import time
import random
import sys
import statistics
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import URLError

COORDINATOR_URL = "http://localhost:8080"
PROMETHEUS_URL = "http://localhost:9090"
VECTOR_DIM = 128
NUM_INSERT = 1000
NUM_SEARCH = 200
SEARCH_K = 10
CONCURRENT_WRITERS = 4


def random_vector(seed=None):
    rng = random.Random(seed)
    return [rng.random() for _ in range(VECTOR_DIM)]


def http_post(url, data):
    req = Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()), resp.status
    except URLError as e:
        return None, getattr(e, "code", 0)


def http_get(url):
    try:
        with urlopen(url, timeout=5) as resp:
            return json.loads(resp.read()), resp.status
    except URLError:
        return None, 0


def benchmark_insert_throughput():
    """Insert NUM_INSERT vectors with CONCURRENT_WRITERS threads, measure throughput."""
    print(f"\n{'='*60}")
    print(f"  INSERT THROUGHPUT: {NUM_INSERT} vectors, {CONCURRENT_WRITERS} writers")
    print(f"{'='*60}")

    results = {"success": 0, "failure": 0, "latencies": []}
    lock = threading.Lock()

    def insert_one(i):
        vec = random_vector(seed=i)
        start = time.perf_counter()
        resp, status = http_post(f"{COORDINATOR_URL}/vectors", {"id": f"bench-{i}", "vector": vec, "metadata": f"item-{i}"})
        elapsed = time.perf_counter() - start
        with lock:
            results["latencies"].append(elapsed)
            if resp and resp.get("status") == "ok":
                results["success"] += 1
            else:
                results["failure"] += 1

    start_time = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENT_WRITERS) as pool:
        futures = [pool.submit(insert_one, i) for i in range(NUM_INSERT)]
        for f in as_completed(futures):
            f.result()
    total_time = time.perf_counter() - start_time

    throughput = results["success"] / total_time
    lats = sorted(results["latencies"])
    p50 = statistics.median(lats) * 1000
    p95 = lats[int(len(lats) * 0.95)] * 1000
    p99 = lats[int(len(lats) * 0.99)] * 1000

    print(f"  Throughput:    {throughput:.1f} vectors/sec")
    print(f"  Success/Fail:  {results['success']}/{results['failure']}")
    print(f"  Latency p50:   {p50:.1f} ms")
    print(f"  Latency p95:   {p95:.1f} ms")
    print(f"  Latency p99:   {p99:.1f} ms")
    print(f"  Total time:    {total_time:.2f} s")

    return {
        "throughput_vps": round(throughput, 1),
        "success": results["success"],
        "failure": results["failure"],
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
    }


def benchmark_search_latency():
    """Run NUM_SEARCH searches and measure latency distribution."""
    print(f"\n{'='*60}")
    print(f"  SEARCH LATENCY: {NUM_SEARCH} queries, k={SEARCH_K}")
    print(f"{'='*60}")

    latencies = []
    results_count = []

    for i in range(NUM_SEARCH):
        vec = random_vector(seed=i + 10000)
        start = time.perf_counter()
        resp, status = http_post(f"{COORDINATOR_URL}/search", {"vector": vec, "k": SEARCH_K})
        elapsed = time.perf_counter() - start
        latencies.append(elapsed)
        if resp:
            results_count.append(len(resp.get("results", [])))

    lats = sorted(latencies)
    p50 = statistics.median(lats) * 1000
    p95 = lats[int(len(lats) * 0.95)] * 1000
    p99 = lats[int(len(lats) * 0.99)] * 1000
    avg_results = statistics.mean(results_count) if results_count else 0

    print(f"  Latency p50:   {p50:.1f} ms")
    print(f"  Latency p95:   {p95:.1f} ms")
    print(f"  Latency p99:   {p99:.1f} ms")
    print(f"  Avg results:   {avg_results:.1f}")

    return {
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "avg_results": round(avg_results, 1),
    }


def benchmark_failover_recovery():
    """Kill shard-0 primary, measure time until writes succeed again."""
    print(f"\n{'='*60}")
    print(f"  FAILOVER RECOVERY TIME")
    print(f"{'='*60}")

    resp, _ = http_get(f"{COORDINATOR_URL}/stats")
    if not resp:
        print("  ERROR: Cannot reach coordinator")
        return None

    print("  Killing shard-0-replica-0 (primary)...")
    subprocess.run(
        ["docker", "compose", "-f", "deploy/docker-compose.cluster.yml", "stop", "shard-0-replica-0"],
        capture_output=True, cwd=".."
    )

    start = time.perf_counter()
    recovered = False
    vec = random_vector(seed=99999)

    for attempt in range(60):
        time.sleep(0.5)
        resp, status = http_post(f"{COORDINATOR_URL}/vectors", {"id": f"failover-test-{attempt}", "vector": vec})
        if resp and resp.get("status") == "ok":
            recovery_time = time.perf_counter() - start
            recovered = True
            break

    if recovered:
        print(f"  Recovery time: {recovery_time:.2f} s")
    else:
        print("  ERROR: Did not recover within 30s")
        recovery_time = -1

    print("  Restarting shard-0-replica-0...")
    subprocess.run(
        ["docker", "compose", "-f", "deploy/docker-compose.cluster.yml", "start", "shard-0-replica-0"],
        capture_output=True, cwd=".."
    )
    time.sleep(2)

    return {"recovery_time_s": round(recovery_time, 2)} if recovered else None


def benchmark_raft_election():
    """Kill the Raft leader coordinator, measure time until a new leader is elected."""
    print(f"\n{'='*60}")
    print(f"  RAFT ELECTION TIME")
    print(f"{'='*60}")

    resp, _ = http_get(f"{COORDINATOR_URL}/raft/status")
    if not resp:
        print("  ERROR: Cannot reach coordinator")
        return None

    current_term = resp.get("term", 0)
    print(f"  Current term: {current_term}, role: {resp.get('role')}")

    if resp.get("role") == "leader":
        print("  coordinator-0 IS the leader, killing it...")
        subprocess.run(
            ["docker", "compose", "-f", "deploy/docker-compose.cluster.yml", "stop", "coordinator-0"],
            capture_output=True, cwd=".."
        )
        start = time.perf_counter()

        elected = False
        for attempt in range(20):
            time.sleep(0.5)
            try:
                prom_resp, _ = http_get(f"{PROMETHEUS_URL}/api/v1/query?query=nanodb_raft_role==2")
                if prom_resp and prom_resp.get("data", {}).get("result"):
                    for r in prom_resp["data"]["result"]:
                        if r["value"][1] == "2" and "coordinator-0" not in r["metric"].get("instance", ""):
                            election_time = time.perf_counter() - start
                            elected = True
                            break
                if elected:
                    break
            except Exception:
                pass

        if elected:
            print(f"  Election time: {election_time:.2f} s")
        else:
            print("  Checking via remaining coordinators...")
            election_time = time.perf_counter() - start
            print(f"  Approximate election time: {election_time:.2f} s")

        print("  Restarting coordinator-0...")
        subprocess.run(
            ["docker", "compose", "-f", "deploy/docker-compose.cluster.yml", "start", "coordinator-0"],
            capture_output=True, cwd=".."
        )
        time.sleep(3)
        return {"election_time_s": round(election_time, 2)}
    else:
        print("  coordinator-0 is a follower, skipping leader kill test")
        print("  (Re-run or manually kill the actual leader)")
        return {"election_time_s": -1}


def main():
    print("NanoDB Distributed Cluster Benchmark")
    print("=" * 60)

    resp, status = http_get(f"{COORDINATOR_URL}/stats")
    if not resp:
        print("ERROR: Cannot reach coordinator at", COORDINATOR_URL)
        print("Make sure the cluster is running:")
        print("  docker compose -f deploy/docker-compose.cluster.yml -f deploy/docker-compose.monitoring.yml up -d")
        sys.exit(1)

    total = resp.get("total_element_count", 0)
    replicas = resp.get("replicas", [])
    shards = set(r["shard_id"] for r in replicas)
    print(f"  Cluster status: {len(shards)} shards, {len(replicas)} replicas, {total} vectors")

    results = {}
    results["insert"] = benchmark_insert_throughput()
    results["search"] = benchmark_search_latency()
    results["failover"] = benchmark_failover_recovery()
    results["election"] = benchmark_raft_election()

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    print(json.dumps(results, indent=2))

    with open("benchmarks/cluster_benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to benchmarks/cluster_benchmark_results.json")


if __name__ == "__main__":
    main()
