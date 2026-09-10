"""
Weaviate cluster topology for the feasibility study (issue #41).

Deliberately mirrors `../cross_system_replication/qdrant_topology.py`'s shape
-- module-level constants, a generated compose file under a run directory,
fixed host ports, and small HTTP helpers -- so a reader who knows the Qdrant
harness can follow this one by its differences.

The differences that matter:

  * Weaviate clusters via a Raft-backed metadata store (RAFT_JOIN /
    RAFT_BOOTSTRAP_EXPECT) plus gossip on 7946, rather than Qdrant's single
    --bootstrap URI.
  * Replication is a per-class property (`replicationConfig.factor`), set at
    class creation, not a collection-creation parameter alongside shards.
  * ASYNC_REPLICATION is opt-in per class in v1.29+; the whole point of
    testing Weaviate is that this exists, so the class turns it on and the
    image is digest-pinned, because a feasibility verdict on one version is
    not one on another (#41 Confounds).

One shard, replication factor 3, three nodes: the smallest topology in which
"what does replica N hold" is a question. Matching Qdrant's 2x3 is not
required for a feasibility verdict and is not attempted here.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "weaviate_run")
COMPOSE_PATH = os.path.join(RUN_DIR, "docker-compose.yml")

PROJECT = "rrd-weaviate"
# Pinned BY DIGEST (issue #46), not by tag. A tag can be repointed, and this
# harness depends on an UNDOCUMENTED internal API (see
# ../weaviate_nonperturbing_probe/), so "1.29.0" is not a specification of
# anything a result can rest on. Re-derive with:
#     docker image inspect cr.weaviate.io/semitechnologies/weaviate:1.29.0 #         --format '{{index .RepoDigests 0}}'
# Pinning makes runs reproducible against one build; it does NOT make the
# internal API a stability contract.
WEAVIATE_IMAGE = ("cr.weaviate.io/semitechnologies/weaviate@sha256:"
                  "4d2eceef34882b5e573ee77ef0e92423838583676f1cf0f054c186b36444b132")

N_NODES = 3
REPLICATION_FACTOR = 3
CLASS_NAME = "RrdVector"
VECTOR_DIM = 128

HTTP_BASE = 8080          # host port for node 0; node n -> HTTP_BASE + n
GRPC_BASE = 50551      # NOT 50151: Windows reserves 50060-50259 for Hyper-V
                       # /WinNAT, and binding there fails with "an attempt was
                       # made to access a socket in a way forbidden by its
                       # access permissions" -- which reads like a permissions
                       # problem and is a reserved-range problem. Nothing in
                       # this project connects to the gRPC port; it is mapped
                       # for a client that might. Check with:
                       #   netsh interface ipv4 show excludedportrange protocol=tcp
CLUSTER_BASE = 7100
# Issue #43: the inter-node replication API listens on CLUSTER_DATA_BIND_PORT
# and is NOT served on the main HTTP port (checked: /replicas/... and
# /indices/... 404 there). Publishing it is what lets a probe ask one replica
# for its own state without stopping its peers.
INTERNAL_BASE = 7947


def node_service_name(n: int) -> str:
    return f"weaviate-node{n}"


def container_name(n: int) -> str:
    return f"{PROJECT}-node{n}"


def http_port(n: int) -> int:
    return HTTP_BASE + n


def grpc_port(n: int) -> int:
    return GRPC_BASE + n


def internal_port(n: int) -> int:
    """Host port for node n's CLUSTER_DATA_BIND_PORT (issue #43)."""
    return INTERNAL_BASE + n


def _write_yaml(path: str, obj) -> None:
    """Minimal YAML writer -- same reason as the Qdrant harness: no PyYAML
    dependency for a file this shape."""
    def emit(o, indent=0):
        pad = "  " * indent
        out = []
        if isinstance(o, dict):
            for k, v in o.items():
                if v is None:
                    out.append(f"{pad}{k}:")
                elif isinstance(o[k], (dict, list)):
                    out.append(f"{pad}{k}:")
                    out.extend(emit(v, indent + 1))
                else:
                    out.append(f"{pad}{k}: {v}")
        elif isinstance(o, list):
            for item in o:
                if isinstance(item, (dict, list)):
                    sub = emit(item, indent + 1)
                    sub[0] = pad + "- " + sub[0].strip()
                    out.extend(sub)
                else:
                    out.append(f"{pad}- {item}")
        return out

    with open(path, "w", newline="\n") as f:
        f.write("\n".join(emit(obj)) + "\n")


def write_compose_file() -> None:
    os.makedirs(RUN_DIR, exist_ok=True)
    services = {}
    for n in range(N_NODES):
        svc = node_service_name(n)
        env = [
            "QUERY_DEFAULTS_LIMIT=25",
            "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true",
            "PERSISTENCE_DATA_PATH=/var/lib/weaviate",
            "DEFAULT_VECTORIZER_MODULE=none",
            "ENABLE_MODULES=",
            "CLUSTER_GOSSIP_BIND_PORT=7946",
            "CLUSTER_DATA_BIND_PORT=7947",
            f"CLUSTER_HOSTNAME=node{n}",
            f"RAFT_BOOTSTRAP_EXPECT={N_NODES}",
            # RAFT_PORT / RAFT_INTERNAL_RPC_PORT must be set explicitly. Left
            # unset, a node that RESTARTS tries to rejoin with a garbage RPC
            # port ("dial tcp: address 99999999: invalid port") and never
            # regains membership -- fatal for this project, whose entire
            # protocol is kill-and-restart. Found by restarting a node
            # (SPEC.md, "Cluster setup findings").
            "RAFT_PORT=8300",
            "RAFT_INTERNAL_RPC_PORT=8301",
            "RAFT_JOIN=" + ",".join(f"node{i}:8300" for i in range(N_NODES)),
            # #41: the property that makes Weaviate worth testing at all.
            "ASYNC_INDEXING=false",
        ]
        if n > 0:
            env.append(f"CLUSTER_JOIN={node_service_name(0)}:7946")
        services[svc] = {
            "image": WEAVIATE_IMAGE,
            "container_name": container_name(n),
            "environment": env,
            "ports": [
                f"{http_port(n)}:8080",
                f"{grpc_port(n)}:50051",
                f"{internal_port(n)}:7947",   # issue #43
            ],
            "volumes": [f"{PROJECT}-data{n}:/var/lib/weaviate"],
        }
        if n > 0:
            services[svc]["depends_on"] = [node_service_name(0)]

    _write_yaml(COMPOSE_PATH, {
        "services": services,
        "volumes": {f"{PROJECT}-data{n}": None for n in range(N_NODES)},
    })


def http_request(port: int, method: str, path: str, body=None, timeout=10.0):
    """(status, parsed_json_or_text). Same contract as the Qdrant harness's."""
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        # A stopped node is the normal case in this study (isolation probing
        # stops peers on purpose), so an unreachable endpoint is a RESULT,
        # not an exception: status 0. Raising here would abort a check whose
        # whole design involves nodes being down -- which it did, once.
        return 0, repr(e)


def create_class(node: int = 0, async_replication: bool = True) -> tuple[int, object]:
    """One class, replication factor 3, no vectorizer (vectors supplied by the
    client, as every other harness in this project does)."""
    body = {
        "class": CLASS_NAME,
        "vectorizer": "none",
        "vectorIndexType": "hnsw",
        "replicationConfig": {
            "factor": REPLICATION_FACTOR,
            # v1.29+: the hash-tree anti-entropy this whole leg exists to test.
            "asyncEnabled": bool(async_replication),
        },
        "shardingConfig": {"desiredCount": 1},
        "properties": [{"name": "vid", "dataType": ["text"]}],
    }
    st, resp = http_request(http_port(node), "POST", "/v1/schema", body)
    if st == 200:
        return st, resp
    # Issue #46: a 422 means the class already exists -- and NOT necessarily
    # with this config. A stale class left by auto-schema had
    # replicationConfig.factor 1 with shardingConfig.actualCount 3, i.e.
    # SHARDED across the nodes rather than replicated onto all of them, so
    # each node held a different subset and "what does replica N hold" was
    # not even the right question. Tolerating 422 silently is how a run gets
    # a topology its spec does not describe, so verify and say so.
    got, _ = verify_class(node)
    if got:
        return 200, {"note": "class already existed with the expected config"}
    return st, {"error": "class exists with a DIFFERENT config; delete it first",
                "response": resp}


def verify_class(node: int = 0) -> tuple[bool, dict]:
    """(matches_expected, actual). Checks the two properties this project's
    per-replica question depends on: replication factor over every node, and
    exactly one shard, so all replicas name the same shard."""
    st, sch = http_request(http_port(node), "GET", f"/v1/schema/{CLASS_NAME}")
    if st != 200 or not isinstance(sch, dict):
        return False, {"status": st}
    rep = (sch.get("replicationConfig") or {})
    shard = (sch.get("shardingConfig") or {})
    actual = {"factor": rep.get("factor"), "asyncEnabled": rep.get("asyncEnabled"),
              "shards": shard.get("actualCount")}
    ok = (actual["factor"] == REPLICATION_FACTOR and actual["shards"] == 1)
    return ok, actual


def nodes_status(node: int = 0, verbose: bool = True):
    """GET /v1/nodes -- path (b) in #41. `verbose` is what the issue says must
    be checked against a running cluster rather than the docs."""
    path = "/v1/nodes" + ("?output=verbose" if verbose else "")
    return http_request(http_port(node), "GET", path)
