"""Every _SLASH_DISPATCH entry's pass_arg flag must match its handler's signature.

A mismatch raises TypeError at dispatch, which the classic REPL swallows — the
command echoes and prints nothing (regression: /id).
"""
import inspect

import cli


def _accepts_positional(params):
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in params)


def _required_positional(params):
    return [p for p in params
            if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def test_slash_dispatch_arity_matches_handlers():
    bad = []
    for name, (method, pass_arg) in cli.HermesCLI._SLASH_DISPATCH.items():
        fn = getattr(cli.HermesCLI, method, None)
        assert fn is not None, f"/{name}: missing handler {method}"
        params = [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]
        if pass_arg and not _accepts_positional(params):
            bad.append(f"/{name}: {method} takes no arg but pass_arg=True")
        if not pass_arg and _required_positional(params):
            bad.append(f"/{name}: {method}{inspect.signature(fn)} needs an arg but pass_arg=False")
    assert not bad, "\n".join(bad)


def test_id_command_prints_session_id(capsys, monkeypatch):
    printed = []
    monkeypatch.setattr(cli, "_cprint", lambda s, *a, **k: printed.append(s))
    c = cli.HermesCLI.__new__(cli.HermesCLI)
    c.session_id = "sess-abc123"
    c._pending_resume_sessions = None
    assert c.process_command("/id") is True
    assert any("sess-abc123" in line for line in printed)
