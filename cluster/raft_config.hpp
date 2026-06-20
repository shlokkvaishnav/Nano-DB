#pragma once
#include <string>
#include <vector>
#include <fstream>
#include <stdexcept>
#include "json.hpp"
#include "raft_node.hpp"

namespace nanodb {
namespace raft {

inline std::vector<RaftPeer> load_raft_peers(const std::string& path) {
    std::ifstream f(path);
    if (!f.is_open()) {
        throw std::runtime_error("cannot open raft peers config: " + path);
    }
    nlohmann::json j;
    f >> j;
    std::vector<RaftPeer> peers;
    for (const auto& p : j.at("peers")) {
        RaftPeer peer;
        peer.node_id = p.at("node_id").get<int>();
        peer.host = p.at("host").get<std::string>();
        peer.port = p.at("raft_port").get<int>();
        peers.push_back(peer);
    }
    if (peers.empty()) throw std::runtime_error("raft peers config has zero peers");
    return peers;
}

} // namespace raft
} // namespace nanodb
