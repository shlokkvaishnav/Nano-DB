#include <vector>
#include <map>
#include <iostream>
#include <cassert>
#include "../cluster/raft_node.hpp"

using namespace nanodb::raft;

// Constructs the actual Figure 8 trap (Raft paper section 5.4.2): an
// old-term entry that has reached a majority of logs must NOT be
// committed just because of that majority. A leader only directly
// commits entries from its own current term.
void test_old_term_majority_not_committed() {
    // 5-node cluster, current leader is in term 4. Its log:
    //   index 1: term 2  (left over from an earlier leader, since
    //            replicated to two followers as well -- now on a
    //            majority of 3/5, but still an old-term entry)
    //   index 2: term 4  (the current leader's own entry, not yet
    //            replicated anywhere but itself)
    std::vector<uint64_t> log_terms = {2, 4};
    auto term_at = [&](uint64_t idx) -> uint64_t {
        if (idx == 0 || idx > log_terms.size()) return 0;
        return log_terms[idx - 1];
    };
    std::map<int, uint64_t> match_index = {{2, 1}, {3, 1}, {4, 0}, {5, 0}};

    uint64_t result = compute_new_commit_index(2, match_index, 5, 0, 4, term_at);
    assert(result == 0); // must NOT commit despite the majority
    std::cout << "test_old_term_majority_not_committed passed.\n";
}

void test_current_term_majority_commits() {
    std::vector<uint64_t> log_terms = {2, 4};
    auto term_at = [&](uint64_t idx) -> uint64_t {
        if (idx == 0 || idx > log_terms.size()) return 0;
        return log_terms[idx - 1];
    };
    // index 2 (current term) now also reaches a majority
    std::map<int, uint64_t> match_index = {{2, 2}, {3, 2}, {4, 0}, {5, 0}};

    uint64_t result = compute_new_commit_index(2, match_index, 5, 0, 4, term_at);
    // index 2 commits, which transitively makes index 1 safe too --
    // commit index N means "everything up to and including N"
    assert(result == 2);
    std::cout << "test_current_term_majority_commits passed.\n";
}

// Proves the two tests above aren't vacuous passes: the same matchIndex
// data, run through the term-check-free version of the same algorithm,
// DOES incorrectly commit the old-term entry. If this assertion ever
// failed, it would mean the "old term" scenario above wasn't actually
// constructed correctly.
void test_scenario_actually_triggers_bug_without_safety_check() {
    auto without_term_check = [](uint64_t leader_last_index,
                                  const std::map<int, uint64_t>& mi,
                                  size_t cluster_size,
                                  uint64_t current_commit) -> uint64_t {
        std::vector<uint64_t> match_values;
        match_values.push_back(leader_last_index);
        for (auto& [id, m] : mi) match_values.push_back(m);
        std::sort(match_values.begin(), match_values.end(), std::greater<uint64_t>());
        int majority = (int)(cluster_size / 2) + 1;
        uint64_t candidate_n = match_values[majority - 1];
        return candidate_n > current_commit ? candidate_n : current_commit;
    };
    std::map<int, uint64_t> match_index = {{2, 1}, {3, 1}, {4, 0}, {5, 0}};
    uint64_t buggy_result = without_term_check(2, match_index, 5, 0);
    assert(buggy_result == 1); // confirms the unsafe version DOES commit the old-term entry
    std::cout << "test_scenario_actually_triggers_bug_without_safety_check passed.\n";
}

int main() {
    test_old_term_majority_not_committed();
    test_current_term_majority_commits();
    test_scenario_actually_triggers_bug_without_safety_check();
    std::cout << "All Raft commit-safety tests passed.\n";
    return 0;
}
