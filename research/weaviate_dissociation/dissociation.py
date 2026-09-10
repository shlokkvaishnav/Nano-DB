#!/usr/bin/env python3
"""
Does `completeness` heal on Weaviate while `index_recall` does not? (issue #54)

The experiment the four Weaviate method studies (#41, #43, #46, #48) were built
to make runnable, and the only one that tests this project's field-level claim:
object-level anti-entropy repairs the DATA and cannot repair the GRAPH, because
two correct HNSW graphs over identical data differ bit-for-bit.

THE INSTRUMENT IS ASYMMETRIC AND THAT IS PERMANENT. `_search` is unavailable on
the cluster-internal port (#43, 415 across 12 content types), so:

    completeness   sampled every 1 s, peers up, non-perturbing      (#43/#46)
    index_recall   two snapshots, via isolation probing, ~10 min
                   of node health each                              (#41)

So this compares a time series against a pair of endpoints. Any writeup must
carry that in the claim rather than implying both axes were watched.

GROUND TRUTH NEEDS NO SERVER SUPPORT. The corpus comes from a seeded RNG, so
the exact top-k over *the subset a replica actually holds* -- which
objects_present_ids reports -- is computable locally with numpy. index_recall is
the replica's ANN answer against exact search over its own data, holding data
constant. Same definition as nano-db and Qdrant.

Usage:
    python research/weaviate_dissociation/dissociation.py --seeds 5 --dry-run
    python research/weaviate_dissociation/dissociation.py --seed 20260900
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for _p in ("weaviate_probe", "weaviate_nonperturbing_probe", "weaviate_probe_per_id",
           "weaviate_repair_window"):
    sys.path.insert(0, os.path.join(ROOT, "research", _p))

import weaviate_topology as t                      # noqa: E402
import internal_api as ia                          # noqa: E402
from per_id import objects_present_ids             # noqa: E402
from characterize import write_objects, wait_ready  # noqa: E402

VICTIM = 2
PEERS = (0, 1)

# From #48, all measured rather than chosen. See SPEC.md.
DIVERGENCE = 5_000      # size-independent repair, but 5k gives a resolvable ramp
CADENCE_S = 1.0         # #43: 5 s proven; 1 s resolves the ~6 s ramp at this size
OBSERVE_S = 60.0        # #54's value, kept as the DEFAULT so that study stays
                        # reproducible from its own committed results. #69 runs
                        # 180 s via --observe-s: at 60 s the margin was 6.7 s
                        # against the slowest repair seen anywhere (53.3 s), and
                        # #54 duly right-censored 2 of 5 seeds, with observed
                        # recovery at 40.08-60.55 s -- one past nominal.
                        # Amendment 1 applies at any window: a series that never
                        # completes is RIGHT-censored, not a failure to heal.
ID_CAP = 15_000         # base64 ids in the URL fail SILENTLY above this
K = 10

# index_recall quantises at 1 / (N_QUERIES * K). At 20 queries that is
# 1/200 = 0.005, and #54's whole result lives inside that granularity: the
# paired chaos-vs-control differences were 0.000, 0.000 and +0.015 -- zero,
# zero, and three steps. A bound of "under ~0.02" is a statement about the
# RULER, not about Weaviate.
#
# Raising the count lowers the floor directly and costs no cluster time: the
# isolation probe is already paused and the corpus already loaded, so each extra
# query is one more GraphQL call in a window that is otherwise idle. #61's
# review named this the cheapest available improvement.
#
# 20 is kept as the DEFAULT so #54 stays reproducible. #69 runs 100 via
# --queries, giving 1/1000 = 0.001, a 5x finer floor.
N_QUERIES = 20


def log(m):
    print(m, flush=True)


def search_full_ids(node, vec, k):
    """k-NN against one node, returning FULL object UUIDs.

    Deliberately not `feasibility_check.search()`, which returns `(ok, [vid])`
    where `vid` is the last 8 characters of the uuid, set as a property at write
    time. Comparing truncated ids against a ground-truth set of full uuids would
    silently score ~0 -- and would look like a real finding rather than a key
    mismatch. This asks for `_additional { id }` instead.
    """
    q = {"query": ("{ Get { %s(nearVector: {vector: %s}, limit: %d) "
                   "{ _additional { id } } } }")
                  % (t.CLASS_NAME, json.dumps([float(x) for x in vec]), k)}
    st, body = t.http_request(t.http_port(node), "POST", "/v1/graphql", q, timeout=30)
    if st != 200 or not isinstance(body, dict) or body.get("errors"):
        return False, []
    got = (body.get("data") or {}).get("Get", {}).get(t.CLASS_NAME) or []
    return True, [(o.get("_additional") or {}).get("id") for o in got]


def corpus(seed, n, offset=0):
    """Deterministic corpus: ids and vectors regenerate from the seed, which is
    what makes local exact ground truth possible.

    `offset` separates the BASE set from the DIVERGENCE set. They must be
    disjoint id ranges: re-writing the same ids while the victim is down would
    leave it missing only the UPDATES, and objects_present_ids checks presence
    rather than version -- so the victim would read as complete and the
    experiment would report a false negative on its primary metric.
    """
    rng = np.random.default_rng(seed + offset)
    ids = [str(uuid.UUID(int=seed * 10_000_000 + offset + i)) for i in range(n)]
    vecs = rng.standard_normal((n, t.VECTOR_DIM)).astype(np.float32)
    return ids, vecs


def schema_distance(node=0):
    """The distance metric the class's HNSW index actually uses.

    Read from the live schema rather than hardcoded. Amendment 2: the first
    version computed ground truth with L2 while the class indexes with COSINE,
    so `index_recall` scored Weaviate's cosine-nearest-10 against exact
    L2-nearest-10 -- a comparison between two different neighbour sets, which
    has nothing to do with graph quality. Reading it from the artifact is the
    same fix #17's review applied to the kill scheduler's mirrored constant:
    a value that can silently disagree with reality should be derived, not
    duplicated.
    """
    st, sch = t.http_request(t.http_port(node), "GET", f"/v1/schema/{t.CLASS_NAME}",
                             None, timeout=15)
    if st != 200 or not isinstance(sch, dict):
        return None
    return ((sch.get("vectorIndexConfig") or {}).get("distance") or "").lower()


def exact_topk(query, vecs, held_idx, k, distance="cosine"):
    """Exact top-k over ONLY the vectors this replica holds -- the 'data held
    constant' half of index_recall's definition.

    `distance` MUST match the index's own metric or this is not a recall
    measurement at all (Amendment 2).
    """
    if len(held_idx) == 0:
        return []
    sub = vecs[held_idx]
    if distance == "cosine":
        sn = sub / np.linalg.norm(sub, axis=1, keepdims=True)
        qn = query / np.linalg.norm(query)
        d = 1.0 - sn @ qn
    elif distance in ("l2-squared", "l2", "euclidean"):
        d = np.linalg.norm(sub - query, axis=1)
    elif distance == "dot":
        d = -(sub @ query)
    else:
        raise ValueError(f"unsupported index distance {distance!r}; refusing to "
                         "score index_recall against a metric the index does "
                         "not use")
    order = np.argsort(d)[:k]
    return [held_idx[i] for i in order]


def index_recall_snapshot(node, shard, ids, vecs, queries, dry, distance="cosine"):
    """Isolate `node`, ask it k-NN for each query at consistency ONE, compare
    against exact search over the ids it actually holds, restore peers.

    COSTS ~10 MINUTES OF NODE HEALTH (#41). That is why this is a snapshot and
    not a series, and why the experiment is asymmetric.
    """
    if dry:
        log(f"    [dry] isolate node{node}, {len(queries)} queries, restore")
        return None
    # Amendment 2c: PAUSE the peers, do not STOP them.
    #
    # #41 isolated by stopping peers, ran the probe once, and recorded that it
    # cost ~10 minutes of node health. This experiment needs 20 isolations, and
    # at that rate stopping is not merely slow, it is destructive: stopping two
    # of three nodes drops Raft below quorum, and on restart the peers come back
    # with new addresses. Twice in this study that left the cluster unable to
    # elect a leader at all ("attempted to join and failed", "invalid port
    # 99999999") and forced a rebuild from docker-compose with fresh volumes.
    #
    # `docker pause` SIGSTOPs the processes instead. The victim is just as
    # isolated -- its peers answer nothing -- but nothing terminates, no address
    # changes, and unpausing restores the identical membership. Measured:
    # isolated index_recall 0.98 with all 5,000 ids held, and after unpause
    # 3 of 3 replicas still hold the shard with the schema readable.
    for p in PEERS:
        if p != node:
            subprocess.run(["docker", "pause", t.container_name(p)], capture_output=True)
    time.sleep(3)
    try:
        ok, held = objects_present_ids(node, shard, ids[:ID_CAP])
        if not ok:
            return None
        held_idx = np.array([i for i, x in enumerate(ids[:ID_CAP]) if x in held])
        hits = tot = 0
        for q in queries:
            okq, got = search_full_ids(node, q, K)
            truth = set(ids[i] for i in exact_topk(q, vecs, held_idx, K, distance))
            if not okq or not truth:
                continue
            got_ids = {g for g in got if g}
            hits += len(got_ids & truth)
            tot += len(truth)
        return {"index_recall": (hits / tot) if tot else None,
                "held": int(len(held_idx)), "queries": len(queries),
                # Recorded so the analyser derives the resolution floor rather
                # than mirroring K. A constant duplicated in two files is the
                # defect #17's review found in the kill scheduler.
                "k": K,
                "distance": distance}
    finally:
        for p in PEERS:
            if p != node:
                subprocess.run(["docker", "unpause", t.container_name(p)],
                               capture_output=True)
        for p in PEERS:
            if p != node:
                wait_ready(p)


def recovery_with_censoring(series):
    """Time-to-completeness-recovery, with censoring recorded, per Amendment 1.

    A 1 s cadence cannot tell "repair was instantaneous" from "the first sample
    landed after repair finished" -- #56's probe-perturbation check was voided by
    exactly that, reporting repair_s = 0.000 with the victim already holding
    50/50 ids on its first sample. So a t=0 completion is CENSORED, not instant.

      left   -- the first sample was already complete. Recovery happened at or
                before that sample's offset. The bound is real; the time is not.
      right  -- the last sample was still incomplete. Recovery did not occur
                within the window. NOT evidence that it never would.
      none   -- an incomplete first sample and a complete later one. This is the
                only case that yields a recovery TIME.

    Censored runs are kept. For the primary metric a left-censored run still
    answers "did completeness return to 1.0 within the window" with yes -- the
    censoring bounds *when*, not *whether*.
    """
    out = {"first_sample_offset_s": None, "recovery_s": None,
           "recovery_bound_s": None, "censored": None}
    ok = [s for s in series if s["ok"] and s["n"] is not None]
    if not ok:
        out["censored"] = "no-data"
        return out

    out["first_sample_offset_s"] = ok[0]["t"]
    complete = [s for s in ok if s["n"] >= DIVERGENCE]

    if not complete:
        out["censored"] = "right"
        out["recovery_bound_s"] = ok[-1]["t"]
        return out

    if ok[0]["n"] >= DIVERGENCE:
        # Already complete on the first look: repair fell inside the blind spot
        # between the restart and the first probe.
        out["censored"] = "left"
        out["recovery_bound_s"] = ok[0]["t"]
        return out

    out["censored"] = "none"
    out["recovery_s"] = complete[0]["t"]
    return out


def wait_full_membership(expect=3, timeout_s=120, node=0):
    """Block until all `expect` nodes are back in schema membership.

    `wait_ready()` answers "is the HTTP port serving", which a node does before
    it has rejoined the schema's replica set. This asks the question that
    actually governs replica placement.
    """
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st, d = t.http_request(t.http_port(node), "GET", "/v1/nodes?output=verbose",
                               None, timeout=20)
        nodes = (d or {}).get("nodes") or []
        healthy = [n for n in nodes if n.get("status") == "HEALTHY"]
        if len(healthy) >= expect:
            waited = time.time() - t0
            if waited > 2:
                log(f"  waited {waited:.0f}s for {expect}/{expect} membership")
            return True
        time.sleep(3)
    log(f"  TIMEOUT after {timeout_s}s waiting for {expect}/{expect} membership")
    return False


def shard_for_class(cls, node=0):
    """The shard belonging to THIS class.

    `internal_api.shard_name()` returns the first shard of the first node
    regardless of class -- correct while exactly one class existed, and wrong the
    moment Amendment 4 started making one per run. It silently handed back
    another class's shard, so every per-id probe queried the wrong index and
    reported held=0, which aborted all ten runs.

    Latent shared-code defect (#43/#46/#48/#56 all call it); scoped locally here
    rather than changed underneath closed studies.
    """
    st, nodes = t.nodes_status(node, verbose=True)
    for n in (nodes.get("nodes") or []):
        for sh in (n.get("shards") or []):
            if sh.get("class") == cls:
                return sh.get("name")
    return None


def clear_objects(dry, node=0):
    """Empty the class WITHOUT touching the schema.

    Amendment 2b. Amendment 2 cleared the corpus by deleting and recreating the
    class, which is wrong twice over on a replicated cluster:

      * recreating the class MINTS A NEW SHARD, so any shard name already read
        goes stale (Amendment 2a); and
      * class creation is a Raft operation. It needs a leader and it re-places
        the shard's replicas -- observed placing the new shard on only 2 of 3
        nodes, after which every write at consistency ALL failed with "cannot
        reach enough replicas". The isolation probe stops two of three nodes, so
        a reset between runs can also land while there is no quorum at all
        ("leader not found"), which is how the previous attempt bricked the
        cluster's Raft state.

    Deleting the OBJECTS is a data operation: the shard keeps its name, the
    replica placement is untouched, and no leader election is involved.
    """
    if dry:
        log("  [dry] batch-delete all objects (corpus isolation)")
        return True
    body = {"match": {"class": t.CLASS_NAME,
                      "where": {"path": ["vid"], "operator": "Like",
                                "valueText": "*"}},
            "output": "minimal"}
    st, resp = t.http_request(t.http_port(node), "DELETE", "/v1/batch/objects",
                              body, timeout=300)
    if st != 200:
        log(f"  FAILED to clear objects: {st} {resp}")
        return False
    time.sleep(3)
    n = class_count(node)
    if n:
        log(f"  FAILED to clear objects: {n} still present")
        return False
    log("  class emptied; shard and replica placement untouched")
    return True


def reset_class(dry):
    """Delete and recreate the class, so it holds ONLY this run's corpus.

    Amendment 2, and this is the defect that made the graph axis meaningless.
    `RrdVector` is shared scratch: #41, #43, #46, #48 and #56 all wrote into it
    and nothing ever cleared it. At the first attempted run it held **14,200
    objects** while a run's own corpus is 5,000.

    `index_recall` is the replica's ANN answer scored against exact search over
    *the ids this run wrote*. If the index also contains 9,200 objects from
    older studies, Weaviate's nearest ten are drawn from the superset while the
    ground truth is drawn from the subset, and the two sets disagree for reasons
    that have nothing to do with graph quality. Measured on the polluted class,
    a healthy untouched replica scored **0.23**.

    Recreating is safe: the class is scratch infrastructure. Every study that
    wrote into it has its own committed `results/`, so nothing whose evidence
    matters lives here.
    """
    if dry:
        log("  [dry] delete + recreate class (corpus isolation)")
        return True
    t.http_request(t.http_port(0), "DELETE", f"/v1/schema/{t.CLASS_NAME}",
                   None, timeout=60)
    time.sleep(3)
    st, resp = t.create_class(0)
    if st != 200:
        log(f"  FAILED to recreate class: {st} {resp}")
        return False
    time.sleep(3)
    okc, info = t.verify_class(0)
    log(f"  class reset; topology {okc} {info}")
    return okc


def class_count(node=0):
    """How many objects the class holds, cluster-wide."""
    q = {"query": "{ Aggregate { %s { meta { count } } } }" % t.CLASS_NAME}
    st, body = t.http_request(t.http_port(node), "POST", "/v1/graphql", q, timeout=30)
    try:
        return (body["data"]["Aggregate"][t.CLASS_NAME][0]["meta"]["count"])
    except Exception:
        return None


# The graph axis has to be shown to read ~1 when nothing is wrong, or a low
# number under chaos means nothing. Amendment 2: the spec required a
# no-divergence control for COMPLETENESS and nothing equivalent for
# index_recall, which is why two fatal instrument defects survived into a run.
BASELINE_INDEX_RECALL_FLOOR = 0.90


def one_run(seed, chaos, shard, dry, distance="cosine",
            observe_s=OBSERVE_S, n_queries=N_QUERIES):
    """One seed, one condition.

    Two disjoint id sets, and the distinction is load-bearing:

      BASE       written at consistency ALL, present on every replica.
                 index_recall is measured over this -- the graph axis.
      DIVERGENCE written at ONE while the victim is down, so they are ids the
                 victim has NEVER SEEN. completeness is measured over this --
                 the data axis.

    Re-writing the base ids instead would leave the victim missing only updates,
    which a presence probe cannot see.
    """
    base_ids, base_vecs = corpus(seed, DIVERGENCE, offset=0)
    div_ids, div_vecs = corpus(seed, DIVERGENCE, offset=DIVERGENCE)
    # Amendment 3 (review round 1). GROUND TRUTH MUST COVER EVERYTHING THE
    # REPLICA MIGHT HOLD, not just the base set.
    #
    # index_recall is defined -- in this project and in this file's own
    # docstring -- as the replica's ANN answer against exact search over ITS OWN
    # DATA. Scored against base_ids alone it was exact search over PART of its
    # own data: after repair the victim also holds the 5,000-object divergence
    # set, in the same class and the same HNSW index, so Weaviate drew its
    # nearest ten from up to 10,000 objects while ground truth drew from 5,000.
    # Every returned divergence object counted as a miss although it was simply
    # something ground truth was never told about.
    #
    # The measured drop was fully explained by that: predicted recall
    # 5000/(5000 + 5000*completeness) matched all five seeds to a mean absolute
    # error of 0.027, with no free parameters -- including the partially
    # repaired seed the writeup had read as a mechanism hint.
    #
    # objects_present_ids() reports which of these the replica actually holds,
    # so passing the union makes held_idx resolve to the right subset in BOTH
    # arms and BOTH snapshots: the before-snapshot is unaffected (the divergence
    # set does not exist yet) and the control is unaffected (it never writes
    # one).
    all_ids = list(base_ids) + list(div_ids)
    all_vecs = np.concatenate([base_vecs, div_vecs], axis=0)
    rng = np.random.default_rng(seed + 1)
    queries = rng.standard_normal((n_queries, t.VECTOR_DIM)).astype(np.float32)
    # Instrument parameters recorded per run, so the analyser derives the
    # resolution and the horizon from the artifact instead of a constant that
    # may since have changed. #63 is the standing lesson: a published number
    # that came from a sampler setting nobody recorded.
    rec = {"seed": seed, "chaos": chaos, "divergence": DIVERGENCE,
           "distance": distance, "observe_s": observe_s, "n_queries": n_queries}

    log("")
    # Amendment 2: the class is shared scratch and was never cleared, so the
    # ANN searched a superset of the ground-truth corpus. Reset per run.
    # Amendment 4 (review round 1 follow-on): A FRESH CLASS PER RUN.
    #
    # Amendment 2b cleared the corpus by batch-deleting the objects, to avoid
    # the Raft/shard problems of recreating the class. It broke the very thing
    # this experiment measures. The class carries
    # `deletionStrategy: NoAutomatedResolution`, so deletion conflicts are never
    # resolved, and after repeated batch deletes async repair STALLED
    # PERMANENTLY: the victim sat at 5,000 objects against its peers' 10,000
    # while hashbeat logged "iteration successfully completed", indefinitely.
    #
    # Measured, decisively: on a class that has never seen a delete, the same
    # outage repairs 2,000/2,000 in 43.9s. On the batch-deleted class, repair
    # had not converged an hour later.
    #
    # A fresh class per run has neither problem: no deletes, so no deletion
    # conflicts and no tombstones; and the create happens between runs with
    # every node up, not while the isolation probe has peers paused.
    cls = f"RrdV{seed}{'Chaos' if chaos else 'Ctl'}"
    if not dry:
        # WAIT FOR FULL MEMBERSHIP BEFORE CREATING THE CLASS.
        #
        # The previous run's chaos arm restarts the victim at the end. Creating
        # the next run's class immediately places its shard on whatever nodes
        # are in schema membership *at that instant* -- and a node that is
        # HTTP-ready is not necessarily back in the membership yet. #69 lost 5
        # of 10 runs to this: node2 was 6 minutes into a restart while its peers
        # had been up 58, and the class landed on 2 of 3 replicas.
        #
        # The placement guard below caught every one of them, which is the
        # difference from the silent "cannot achieve consistency level ALL"
        # partial writes that preceded it. But an abort is the wrong response to
        # something that resolves itself in seconds, so wait first and keep the
        # abort as the backstop.
        if not wait_full_membership():
            rec["aborted"] = "cluster did not reach 3/3 membership"
            return rec
        t.CLASS_NAME = cls
        # DELETE BEFORE CREATE. Amendment 4 promised "a fresh class per run" and
        # only ever created one -- create_class() tolerates an existing class
        # (it returns 200 with "class already existed with the expected
        # config"), so a re-run silently INHERITS the previous attempt's
        # populated class.
        #
        # #69 attempt 2 lost 5 of 10 runs to exactly this: every run whose
        # attempt-1 counterpart had COMPLETED read `before == after`, because
        # the before-snapshot was taken on a corpus that already held 10,000
        # objects. Every run whose counterpart had ABORTED was correct. The
        # correlation was perfect, which is what identified it.
        #
        # Third recurrence of stale state silently reused in this study, after
        # the 14,200-object corpus leak (Amendment 2) and the batch-delete
        # tombstones (Amendment 4). The common shape: a cleanup that is
        # tolerant rather than assertive.
        t.http_request(t.http_port(0), "DELETE", f"/v1/schema/{cls}",
                       None, timeout=60)
        time.sleep(2)
        st, resp = t.create_class(0)
        if st != 200:
            log(f"  FAILED to create {cls}: {st} {resp}")
            rec["aborted"] = "class creation failed"
            return rec
        time.sleep(5)
        okc, info = t.verify_class(0)
        stn, nd = t.http_request(t.http_port(0), "GET", "/v1/nodes?output=verbose",
                                 None, timeout=20)
        holders = sum(1 for n in (nd.get("nodes") or []) if n.get("shards"))
        if not okc or holders != 3:
            # The replica-placement race that produced "cannot achieve
            # consistency level ALL" -- caught here rather than as a silent
            # partial write.
            log(f"  FAILED placement for {cls}: verify={okc} {info} "
                f"holders={holders}/3")
            rec["aborted"] = f"class placed on {holders}/3 replicas"
            return rec
        n0 = class_count()
        if n0:
            log(f"  FAILED: {cls} holds {n0} objects at creation, expected 0")
            rec["aborted"] = f"class not empty at creation ({n0} objects)"
            return rec
        shard = shard_for_class(cls)
        if not shard:
            log(f"  FAILED: no shard found for {cls}")
            rec["aborted"] = "no shard for class"
            return rec
        rec["class"] = cls
        rec["shard"] = shard
        # Read the metric from the class THIS RUN created, not from whatever
        # existed at startup. Amendment 4 gives each run its own class, so a
        # startup read describes a different object -- and on a clean cluster
        # there is no object at all. Same principle as Amendment 2: derive the
        # value from the artifact being measured, never duplicate it.
        live = schema_distance(0)
        if not live:
            log(f"  FAILED: cannot read distance metric for {cls}")
            rec["aborted"] = "distance metric unreadable"
            return rec
        if live != distance:
            log(f"  note: distance for {cls} is {live!r} (startup saw "
                f"{distance!r}); using the class's own value")
        distance = live
        rec["distance"] = distance
        log(f"  fresh class {cls}, shard {shard}, 3/3 replicas, distance {distance}")
    log(f"--- seed {seed} chaos={chaos} ---")
    log(f"  writing BASE corpus ({DIVERGENCE} ids) at consistency ALL")
    if not dry:
        write_objects(base_ids, seed, consistency="ALL")

    log("  index_recall snapshot BEFORE (isolation probe, ~10 min node health)")
    rec["index_recall_before"] = index_recall_snapshot(
        VICTIM, shard, all_ids, all_vecs, queries, dry, distance)
    if not dry:
        rec["class_count_after_base_write"] = class_count()
    # THE POSITIVE CONTROL the spec never had. If a healthy, untouched replica
    # does not score near 1.0, the graph axis is not measuring graph quality and
    # nothing downstream of it means anything (Amendment 2).
    b = (rec["index_recall_before"] or {}).get("index_recall")
    if not dry and b is None:
        # Amendment 2a. The first version of this control only fired on a LOW
        # score, so a snapshot that failed outright -- returning None -- sailed
        # straight through it. "The instrument did not answer" is exactly as
        # fatal as "the instrument answered 0.23", and it is what a stale shard
        # name produced.
        log("  ABORT: the no-chaos index_recall snapshot returned nothing at "
            f"all (shard={rec.get('shard')}). A control that cannot answer is "
            "not a passing control.")
        rec["aborted"] = "baseline index_recall snapshot returned None"
        return rec
    if not dry and b < BASELINE_INDEX_RECALL_FLOOR:
        log(f"  ABORT: baseline index_recall {b:.3f} < "
            f"{BASELINE_INDEX_RECALL_FLOOR} on an undisturbed replica. The "
            f"instrument is broken, not the cluster. Class holds "
            f"{rec.get('class_count_after_base_write')} objects against a "
            f"{DIVERGENCE}-object corpus; distance={distance}.")
        rec["aborted"] = "baseline index_recall below floor"
        return rec

    if chaos:
        log(f"  divergence: stop victim, write {DIVERGENCE} NEW ids it cannot see")
        if not dry:
            subprocess.run(["docker", "stop", t.container_name(VICTIM)],
                           capture_output=True)
            time.sleep(3)
            t_write0 = time.time()
            write_objects(div_ids, seed + DIVERGENCE, consistency="ONE")
            t_write = time.time()
            subprocess.run(["docker", "start", t.container_name(VICTIM)],
                           capture_output=True)
            t_restart = time.time()
            # SPEC Amendment 1 (2026-09-06). #56 reported: within a regime the
            # repair clock runs from the RESTART, and divergence age selects the
            # regime. So the origin is the restart, not max(write, restart) --
            # under the old rule a write that takes minutes opens the window
            # AFTER repair has already fired.
            rec["write_s"] = round(t_write - t_write0, 2)
            # Realized divergence age. ~0 here by construction, which puts this
            # run in #56's young regime (expect a ~32 s wait). Recorded, not
            # assumed: a run above 15 s is out of that regime and is reported
            # separately rather than pooled -- pooling across the step is what
            # gave #56 its wrong aggregate answer.
            rec["age_s"] = round(t_restart - t_write, 3)
            rec["regime"] = "young" if rec["age_s"] < 15.0 else "out-of-regime"
            origin = t_restart
        else:
            origin = time.time()

        log(f"  sampling completeness over the DIVERGENCE set every "
            f"{CADENCE_S}s for {observe_s}s from the restart")
        series = []
        if not dry:
            end = origin + observe_s
            while time.time() < end:
                ok, got = objects_present_ids(VICTIM, shard, div_ids[:ID_CAP])
                series.append({"t": round(time.time() - origin, 2),
                               "n": len(got) if ok else None, "ok": ok})
                time.sleep(CADENCE_S)
        rec["completeness_series"] = series
        last = series[-1]["n"] if series and series[-1]["n"] is not None else None
        rec["completeness_end"] = (last / DIVERGENCE) if last is not None else None
        rec.update(recovery_with_censoring(series))

    else:
        # Amendment 3: THE CONTROL MUST BE CORPUS-MATCHED.
        #
        # Review round 1 found the arms differed in corpus size as well as in
        # chaos: the control searched 5,000 objects while the chaos arm searched
        # up to 10,000 after repair. Fixing ground truth (above) removes the
        # bookkeeping half of that, but ANN recall depends on how many objects
        # are in the index, so a 5,000-object control still is not a baseline for
        # a 10,000-object treatment.
        #
        # The control therefore writes the SAME divergence set, with every
        # replica up. Both arms end at 10,000 objects and differ in exactly one
        # thing: whether the victim was down while that set was written.
        log(f"  control: write the same {DIVERGENCE} ids with NO outage "
            "(corpus-matched baseline)")
        if not dry:
            write_objects(div_ids, seed + DIVERGENCE, consistency="ALL")
            time.sleep(5)

    log("  index_recall snapshot AFTER")
    rec["index_recall_after"] = index_recall_snapshot(
        VICTIM, shard, all_ids, all_vecs, queries, dry, distance)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, action="append",
                    help="repeatable; default is 5 pre-registered seeds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    # Instrument parameters, overridable so a re-run at a different horizon or
    # resolution does not have to edit the file and silently change what an
    # earlier study reproduces. Both are recorded per run.
    ap.add_argument("--observe-s", type=float, default=OBSERVE_S,
                    help=f"observation window seconds (default {OBSERVE_S})")
    ap.add_argument("--queries", type=int, default=N_QUERIES,
                    help=f"queries per index_recall snapshot (default {N_QUERIES})")
    a = ap.parse_args()
    seeds = a.seed or [20260900, 20260901, 20260902, 20260903, 20260904]
    os.makedirs(a.out, exist_ok=True)

    if not a.dry_run:
        okc, info = t.verify_class(0)
        log(f"topology check (#46's hazard): {okc}  {info}")
        # A MISSING class is fine; a WRONGLY-SHAPED one is not.
        #
        # This check predates Amendment 4, when the study used one long-lived
        # class and a stale auto-schema version of it was the hazard (#46:
        # factor 1 with 3 shards, i.e. sharded rather than replicated, so
        # "what does replica N hold" was not even the right question).
        #
        # Amendment 4 gives every run its own fresh class and verifies factor 3
        # / 1 shard AND 3-of-3 replica placement before writing a single object.
        # So on a clean cluster there is nothing here to check yet, and failing
        # on 404 refuses to run for the one reason that cannot be a stale class:
        # no class at all. Found by rebuilding the cluster from scratch for #69.
        absent = isinstance(info, dict) and info.get("status") == 404
        if not okc and not absent:
            log("REFUSING TO RUN: the class exists and is not factor 3 / 1 "
                "shard. A stale auto-schema class would give per-replica "
                "numbers from a topology with no replicas.")
            return 2
        if absent:
            log("  no pre-existing class -- each run creates and verifies its "
                "own (Amendment 4), so there is nothing stale to inherit.")

    distance = "cosine"
    if not a.dry_run:
        distance = schema_distance(0) or ""
        if distance:
            log(f"index distance metric (read from the live schema): {distance!r}")
        else:
            # No class yet, so nothing to read. Not fatal since Amendment 4:
            # each run creates its own class and reads the metric back from it
            # before measuring anything. The per-run read is the one that
            # governs; this is only an early warning.
            distance = "cosine"
            log("index distance metric: no class yet -- each run reads it from "
                "the class it creates (Amendment 4)")
        log(f"class holds {class_count()} objects before reset")

    shard = ia.shard_name(0) if not a.dry_run else "DRYSHARD"
    log(f"shard: {shard}   divergence: {DIVERGENCE}   cadence: {CADENCE_S}s   "
        f"observe: {a.observe_s}s   queries/snapshot: {a.queries}")

    rows = []
    for seed in seeds:
        for chaos in (False, True):        # the no-chaos control is REQUIRED
            rows.append(one_run(seed, chaos, shard, a.dry_run, distance,
                                a.observe_s, a.queries))
            with open(os.path.join(a.out, "dissociation.json"), "w") as f:
                json.dump(rows, f, indent=1)
    log(f"\nwrote {os.path.join(a.out, 'dissociation.json')}")
    log("\nReminder for the writeup: completeness is a 1 s series, index_recall "
        "is two endpoints.\nThe dissociation, if found, is between a series and "
        "a pair of snapshots -- say so.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
