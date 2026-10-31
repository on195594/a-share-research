"""Tool-loop rounds store only appended messages; captured production-shape prompts rebuild exactly."""

import gzip
import hashlib
import json
from pathlib import Path

from a_share_research import db, integrity
from a_share_research.prompts import RULES, SCHEMA

CHAIN = json.loads(gzip.decompress((Path(__file__).parent / "data" / "explore-call-chain.json.gz").read_bytes()))
PROMPTS = [call["prompt"] for call in CHAIN["calls"]]


def test_captured_rounds_compact_and_expand_to_the_hashed_prompt():
    for call in CHAIN["calls"]:
        assert hashlib.sha256(call["prompt"].encode()).hexdigest() == call["prompt_sha256"]
    for base, prompt in zip(PROMPTS, PROMPTS[1:]):
        delta = db.compact_prompt(base, prompt)
        assert delta is not None and len(delta) < len(prompt) / 2
        assert db.expand_prompt(base, delta) == prompt


def test_changed_history_or_plain_prompt_is_stored_in_full():
    edited = json.loads(PROMPTS[1])
    edited["messages"][0]["content"] = edited["messages"][0]["content"].replace("投资研究员", "研究员", 1)
    assert db.compact_prompt(PROMPTS[0], json.dumps(edited, ensure_ascii=False)) is None
    escaped = json.dumps(json.loads(PROMPTS[1]))  # same messages, not the bytes that were sent
    assert db.compact_prompt(PROMPTS[0], escaped) is None
    assert db.compact_prompt(PROMPTS[0], "你是独立复核员。") is None
    assert db.compact_prompt("你是独立复核员。", PROMPTS[0]) is None


def chained(path, *, base=True):
    conn = db.connect(path)
    run = db.create_run(conn, "600309.SH", status="running")
    previous = None
    for prompt in PROMPTS:
        call = db.log_call(conn, run, "explore", prompt, base=previous if base else None,
                           identity={"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
        previous = (call, prompt)
    return conn


def test_logged_chain_rebuilds_every_round(tmp_path):
    conn = chained(tmp_path / "app.db")
    rows = conn.execute("SELECT id, prompt, prompt_base FROM calls ORDER BY id").fetchall()
    assert [r["prompt_base"] for r in rows] == [None, rows[0]["id"], rows[1]["id"]]
    assert rows[0]["prompt"] == PROMPTS[0]
    assert [db.call_prompt(conn, r["id"]) for r in rows] == PROMPTS
    assert sum(len(r["prompt"]) for r in rows) < sum(map(len, PROMPTS)) / 2
    conn.close()


def test_backfill_compacts_full_rows_once_and_keeps_prompts(tmp_path):
    conn = chained(tmp_path / "app.db", base=False)
    counts = db.compact_calls(conn)
    # Round 1 gives up its material block; rounds 2 and 3 keep only what they appended.
    assert counts["compacted"] == 3 and counts["bytes_after"] < counts["bytes_before"] / 2
    ids = [r[0] for r in conn.execute("SELECT id FROM calls ORDER BY id")]
    assert [db.call_prompt(conn, i) for i in ids] == PROMPTS
    assert db.compact_calls(conn)["compacted"] == 0
    conn.close()


def test_integrity_reports_a_round_that_no_longer_rebuilds(tmp_path):
    path = tmp_path / "app.db"
    chained(path).close()
    problems, counts = integrity.check(path, tmp_path / "pdf")
    assert problems == [] and counts["calls"] == 3
    conn = db.connect(path)
    middle = conn.execute("SELECT id, prompt FROM calls ORDER BY id LIMIT 1 OFFSET 1").fetchone()
    delta = json.loads(middle["prompt"])
    delta["append"][-1]["content"][0]["text"] += " "
    conn.execute("UPDATE calls SET prompt = ? WHERE id = ?", (json.dumps(delta, ensure_ascii=False), middle["id"]))
    conn.close()
    problems, _ = integrity.check(path, tmp_path / "pdf")
    # The edited round and the one built on it no longer match what was sent.
    assert problems == [f"模型调用 {middle['id']}：提示与调用时记录的哈希不符",
                        f"模型调用 {middle['id'] + 1}：提示与调用时记录的哈希不符"]


def test_integrity_reports_a_broken_chain(tmp_path):
    path = tmp_path / "app.db"
    chained(path).close()
    conn = db.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    first = conn.execute("SELECT min(id) FROM calls").fetchone()[0]
    conn.execute("DELETE FROM calls WHERE id = ?", (first,))
    conn.close()
    problems, _ = integrity.check(path, tmp_path / "pdf")
    assert problems == [f"模型调用 {first + 1}：提示无法还原（前一轮或共享块缺失）", f"模型调用 {first + 2}：提示无法还原（前一轮或共享块缺失）"]


BLOCK = db.material_block(PROMPTS[0])
# What compose sends for the same material: the plain-text stage shares the block with exploration.
COMPOSE = f"你是 A 股财报研究员，为普通投资者写一份研究报告。\n\n{RULES}\n\n{SCHEMA}\n\n# 资料\n{BLOCK}"


def test_material_block_is_cut_once_from_plain_and_tool_loop_prompts(tmp_path):
    assert len(BLOCK) > 30000 and db._escaped(BLOCK) in PROMPTS[0]
    conn = db.connect(tmp_path / "app.db")
    run = db.create_run(conn, "600309.SH", status="running")
    ids = [db.log_call(conn, run, "explore", PROMPTS[0], block=BLOCK),
           db.log_call(conn, run, "compose", COMPOSE, block=BLOCK),
           db.log_call(conn, run, "review", "你是独立复核员。", block=BLOCK)]
    rows = conn.execute("SELECT prompt, prompt_block FROM calls ORDER BY id").fetchall()
    assert [json.loads(r["prompt_block"])["escaped"] if r["prompt_block"] else None for r in rows] == [True, False, None]
    assert conn.execute("SELECT count(*) FROM prompt_blocks").fetchone()[0] == 1
    assert len(rows[1]["prompt"]) < len(COMPOSE) - len(BLOCK) + 1
    assert [db.call_prompt(conn, i) for i in ids] == [PROMPTS[0], COMPOSE, "你是独立复核员。"]
    conn.close()


def test_backfill_cuts_material_from_old_full_rows(tmp_path):
    conn = chained(tmp_path / "app.db", base=False)
    run = conn.execute("SELECT run_id FROM calls").fetchone()[0]
    db.log_call(conn, run, "compose", COMPOSE)
    db.log_call(conn, run, "review", COMPOSE + "\n\n# 上一次复核输出被拒\n缺少 issues 数组")
    assert db.compact_calls(conn)["compacted"] == 5
    ids = [r[0] for r in conn.execute("SELECT id FROM calls ORDER BY id")]
    assert [db.call_prompt(conn, i) for i in ids] == [*PROMPTS, COMPOSE, COMPOSE + "\n\n# 上一次复核输出被拒\n缺少 issues 数组"]
    assert conn.execute("SELECT count(*) FROM prompt_blocks").fetchone()[0] == 1
    assert db.compact_calls(conn)["compacted"] == 0
    conn.close()


def test_integrity_reports_calls_built_on_a_damaged_block(tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    run = db.create_run(conn, "600309.SH", status="running")
    sha = hashlib.sha256(COMPOSE.encode()).hexdigest()
    first = db.log_call(conn, run, "compose", COMPOSE, block=BLOCK, identity={"prompt_sha256": sha})
    db.log_call(conn, run, "compose", COMPOSE, block=BLOCK, identity={"prompt_sha256": sha})
    conn.execute("UPDATE prompt_blocks SET text = text || ' '")
    conn.close()
    problems, _ = integrity.check(path, tmp_path / "pdf")
    assert problems == [f"模型调用 {first}：提示与调用时记录的哈希不符",
                        f"模型调用 {first + 1}：提示与调用时记录的哈希不符"]
