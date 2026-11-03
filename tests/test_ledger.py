from audit_loop import ledger


def rec(project, round_no, status, cost=1.0, scope="s"):
    return {"ts": "t", "scope": scope, "project": project, "name": project, "slug": f"me/{project}",
            "round": round_no, "status": status, "cost_usd": cost}


def test_append_read_and_total(tmp_path):
    path = tmp_path / "runs" / "ledger.jsonl"
    ledger.append(path, rec("a", 1, "ok", 2.5))
    ledger.append(path, rec("b", 1, "error", 0.5))
    records = ledger.read(path)
    assert [r["project"] for r in records] == ["a", "b"]
    assert ledger.total_cost(records) == 3.0
    assert ledger.read(tmp_path / "nothing.jsonl") == []


def test_round_resumes_and_rolls_over():
    projects = ["a", "b"]
    assert ledger.current_round([], "s", projects) == (1, set())
    assert ledger.current_round([rec("a", 1, "ok")], "s", projects) == (1, {"a"})
    # credit/transient failures are retried in the same round
    assert ledger.current_round([rec("a", 1, "ok"), rec("b", 1, "transient")], "s", projects) == (1, {"a"})
    full = [rec("a", 1, "ok"), rec("b", 1, "error")]
    assert ledger.current_round(full, "s", projects) == (2, set())
    # another environment's records do not count for this scope
    assert ledger.current_round([rec("a", 7, "ok", scope="other")], "s", projects) == (1, set())


def test_attempt_cap_and_last_attempt():
    crashes = [rec("a", 1, "transient") for _ in range(ledger.MAX_ATTEMPTS)]
    assert ledger.current_round(crashes, "s", ["a", "b"]) == (1, {"a"})
    two = [rec("a", 1, "transient"), {**rec("a", 1, "transient"), "session_id": "x"}]
    assert ledger.last_attempt(two, "s", "a", 1)["session_id"] == "x"
    assert ledger.last_attempt(two, "s", "a", 2) is None


def test_report_has_no_local_paths():
    text = ledger.report([{**rec("a", 1, "ok"), "project": "/somewhere/private/a"}])
    assert "/somewhere/private" not in text
    assert "[a](https://github.com/me/a)" in text
