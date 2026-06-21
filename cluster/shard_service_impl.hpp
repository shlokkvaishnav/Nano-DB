#pragma once
#include "nanodb_cluster.grpc.pb.h"
#include "../include/index/hnsw.hpp"
#include "id_map_store.hpp"
#include <grpcpp/grpcpp.h>

namespace nanodb {
namespace cluster {

class ShardServiceImpl final : public ShardService::Service {
public:
    ShardServiceImpl(HNSW& index, IdMapStore& id_map, const std::string& shard_id)
        : index_(index), id_map_(id_map), shard_id_(shard_id) {}

    grpc::Status Insert(grpc::ServerContext*, const InsertRequest* request,
                         InsertResponse* response) override {
        if (request->vector_size() != (int)config::VECTOR_DIM) {
            response->set_ok(false);
            response->set_error("vector dimension mismatch, expected " +
                                 std::to_string(config::VECTOR_DIM));
            return grpc::Status::OK;
        }
        if (!check_and_advance_epoch(request->epoch(), response->mutable_error())) {
            response->set_ok(false);
            return grpc::Status::OK;
        }
        std::vector<float> vec(request->vector().begin(), request->vector().end());
        auto [local_id, is_new] = id_map_.assign(request->external_id());
        (void)is_new;
        index_.insert(vec, local_id, request->metadata());
        response->set_ok(true);
        return grpc::Status::OK;
    }

    grpc::Status Search(grpc::ServerContext*, const SearchRequest* request,
                         SearchResponse* response) override {
        if (request->vector_size() != (int)config::VECTOR_DIM) {
            response->set_ok(false);
            response->set_error("vector dimension mismatch, expected " +
                                 std::to_string(config::VECTOR_DIM));
            return grpc::Status::OK;
        }
        std::vector<float> vec(request->vector().begin(), request->vector().end());
        auto results = index_.search(vec, request->k());
        for (const auto& r : results) {
            auto* out = response->add_results();
            out->set_external_id(id_map_.reverse_lookup(r.id));
            out->set_distance(r.distance);
            out->set_metadata(r.metadata);
        }
        response->set_ok(true);
        return grpc::Status::OK;
    }

    grpc::Status Delete(grpc::ServerContext*, const DeleteRequest* request,
                         DeleteResponse* response) override {
        if (!check_and_advance_epoch(request->epoch(), response->mutable_error())) {
            response->set_ok(false);
            return grpc::Status::OK;
        }
        uint32_t local_id;
        if (!id_map_.lookup(request->external_id(), local_id)) {
            response->set_ok(false);
            response->set_error("external_id not found on this shard");
            return grpc::Status::OK;
        }
        index_.delete_vector(local_id);
        response->set_ok(true);
        return grpc::Status::OK;
    }

    grpc::Status Stats(grpc::ServerContext*, const StatsRequest*,
                        StatsResponse* response) override {
        response->set_element_count(index_.size());
        response->set_vector_dim((uint32_t)config::VECTOR_DIM);
        response->set_shard_id(shard_id_);
        return grpc::Status::OK;
    }

    grpc::Status Ping(grpc::ServerContext*, const PingRequest*,
                       PingResponse* response) override {
        response->set_ok(true);
        response->set_shard_id(shard_id_);
        return grpc::Status::OK;
    }

    grpc::Status ListLocalIds(grpc::ServerContext*, const ListLocalIdsRequest*,
                               ListLocalIdsResponse* response) override {
        for (const auto& id : id_map_.list_all_external_ids()) {
            uint32_t local_id;
            // IdMapStore's mapping never gets removed on delete (only the
            // underlying HNSW node gets tombstoned), so a key that's
            // already been migrated away from this shard -- delete_vector
            // was called on it as the last step of a prior migration --
            // would otherwise still show up here every time ListLocalIds
            // is called again. That makes a second rebalance operation
            // re-attempt migrating already-gone keys, which then fail at
            // GetVector (the tombstoned node returns no data) and get
            // miscounted as real failures instead of being silently
            // skipped, exactly what they should be.
            if (id_map_.lookup(id, local_id) && !index_.is_deleted(local_id)) {
                response->add_external_ids(id);
            }
        }
        response->set_ok(true);
        return grpc::Status::OK;
    }

    grpc::Status GetVector(grpc::ServerContext*, const GetVectorRequest* request,
                            GetVectorResponse* response) override {
        uint32_t local_id;
        if (!id_map_.lookup(request->external_id(), local_id)) {
            response->set_ok(false);
            response->set_error("external_id not found on this shard");
            return grpc::Status::OK;
        }
        auto vec = index_.get_vector_data(local_id);
        if (vec.empty()) {
            response->set_ok(false);
            response->set_error("vector data unavailable (deleted or out of range)");
            return grpc::Status::OK;
        }
        for (float f : vec) response->add_vector(f);
        response->set_metadata(index_.get_metadata(local_id));
        response->set_ok(true);
        return grpc::Status::OK;
    }

private:
    HNSW& index_;
    IdMapStore& id_map_;
    std::string shard_id_;
    // The fencing token: the highest epoch this shard has seen in any
    // accepted write. An epoch is just the Raft log index of the
    // SetPrimary (or initial AddShard) command that most recently
    // declared cluster topology for this shard -- already monotonically
    // increasing, no new machinery needed to generate it. Persistence
    // across a shard node restart is a known, accepted gap for this
    // phase (see Phase 4b plan, Section 1): the scenario it would matter
    // for -- this exact process crashing and restarting in the narrow
    // window between being demoted and a stale coordinator's next write
    // arriving -- is far narrower than the actual problem this fixes
    // (an old primary that's still running, not one that crashed).
    std::atomic<uint64_t> current_epoch_{0};

    // Returns false (and sets error_out) if request_epoch is behind what
    // this shard has already seen -- the request came from a coordinator
    // with a stale view of who's allowed to write here. Returns true and
    // advances the high-water mark otherwise, including the normal case
    // where request_epoch equals the current value (most writes, between
    // primary changes, are all at the same epoch).
    bool check_and_advance_epoch(uint64_t request_epoch, std::string* error_out) {
        uint64_t current = current_epoch_.load();
        if (request_epoch < current) {
            *error_out = "stale epoch " + std::to_string(request_epoch) +
                         ", this shard has already seen epoch " + std::to_string(current);
            return false;
        }
        if (request_epoch > current) current_epoch_.store(request_epoch);
        return true;
    }
};

} // namespace cluster
} // namespace nanodb
