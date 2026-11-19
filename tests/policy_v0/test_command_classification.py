from a_share_research.policy.v0 import cache
from a_share_research.policy.v0.cache import COMMANDS, COMMAND_CLASSIFICATION


def test_every_cache_command_has_one_classification() -> None:
    assert set(COMMANDS) == set(COMMAND_CLASSIFICATION)
    assert set(COMMAND_CLASSIFICATION.values()) == {"R0", "R1"}


def test_help_lists_every_command_without_a_write_gate(capsys) -> None:
    assert cache.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in COMMANDS)
    assert "--confirm-write" not in output
