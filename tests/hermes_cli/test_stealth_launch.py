"""CLI contracts for the desktop stealth launch mode."""

from hermes_cli.main import _build_cli_parser, _wants_stealth_early


def test_top_level_stealth_flag_is_not_claimed_by_chat_fast_path():
    parser, _ = _build_cli_parser()

    args = parser.parse_args(["--stealth"])

    assert args.command is None
    assert args.stealth is True
    assert _wants_stealth_early(["--stealth"])
    assert not _wants_stealth_early(["chat"])
