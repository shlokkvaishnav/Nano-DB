#pragma once
#include "raft.grpc.pb.h"
#include "raft_node.hpp"

namespace nanodb {
namespace raft {

class RaftServiceImpl final : public RaftService::Service {
public:
    explicit RaftServiceImpl(RaftNode& node) : node_(node) {}

    grpc::Status RequestVote(grpc::ServerContext*, const RequestVoteRequest* req,
                              RequestVoteResponse* resp) override {
        return node_.handle_request_vote(req, resp);
    }

    grpc::Status AppendEntries(grpc::ServerContext*, const AppendEntriesRequest* req,
                                AppendEntriesResponse* resp) override {
        return node_.handle_append_entries(req, resp);
    }

    grpc::Status InstallSnapshot(grpc::ServerContext*, const InstallSnapshotRequest* req,
                                  InstallSnapshotResponse* resp) override {
        return node_.handle_install_snapshot(req, resp);
    }

private:
    RaftNode& node_;
};

} // namespace raft
} // namespace nanodb
