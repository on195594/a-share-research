from a_share_research.policy.v0 import cache
from a_share_research.policy.v0.cache import COMMANDS, COMMAND_CLASSIFICATION


def test_every_cache_command_has_one_classification() -> None:
    assert set(COMMANDS) == set(COMMAND_CLASSIFICATION)
    assert set(COMMAND_CLASSIFICATION.values()) <= {"R0", "R1", "W1"}
    assert all(
        list(COMMAND_CLASSIFICATION.values()).count(name) >= 1
        for name in ("R0", "R1", "W1")
    )


def test_help_states_the_write_gate_and_lists_every_w1_command(capsys) -> None:
    assert cache.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in COMMANDS)
    _, marker, gate_section = output.partition("W1（需 --confirm-write）：")
    assert marker
    assert {"".join(name.split()) for name in gate_section.split(",")} == {
        name for name, level in COMMAND_CLASSIFICATION.items() if level == "W1"
    }
