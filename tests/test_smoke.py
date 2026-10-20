from tally.cli import main


def test_cli_runs_with_no_arguments() -> None:
    assert main([]) == 0
