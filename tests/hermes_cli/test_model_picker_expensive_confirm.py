import threading
from types import SimpleNamespace

from hermes_cli.model_switch import ModelSwitchResult


def _bound(fn, instance):
    return fn.__get__(instance, type(instance))


def _picker(monkeypatch, *, reasoning=True, resolver=None):
    import cli as cli_mod

    result = ModelSwitchResult(
        success=True, new_model="openai/gpt-5.5-pro", target_provider="nous",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        resolver or (lambda **_kwargs: result),
    )
    captured = {}
    applied = threading.Event()
    self_ = SimpleNamespace(
        _app=object(),
        _model_picker_state={
            "stage": "model",
            "provider_data": {
                "slug": "nous", "capabilities": {
                    "openai/gpt-5.5-pro": {"reasoning": reasoning},
                },
            },
            "model_list": ["openai/gpt-5.5-pro"],
            "selected": 0,
            "user_provs": None,
            "custom_provs": None,
        },
        provider="nous", model="openai/gpt-5.5", base_url="", api_key="",
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._commit_picker_result = _bound(cli_mod.HermesCLI._commit_picker_result, self_)

    def apply(*args):
        captured["args"] = args
        applied.set()

    self_._confirm_and_apply_model_switch_result = apply
    return self_, captured, applied, result


def test_picker_catalogs_use_cache_unless_explicitly_refreshed(monkeypatch):
    from hermes_cli.cli_model_switch_mixin import _show_model_picker

    calls = []

    def inventory(_ctx, **kwargs):
        calls.append(kwargs)
        return {"providers": [{"slug": "connected", "models": ["model-a"]}]}

    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", inventory)
    opened = []
    cli = SimpleNamespace(model="old", provider="connected",
                          _open_model_picker=lambda *args, **kw: opened.append(args))
    ctx = SimpleNamespace(user_providers={}, custom_providers=[])
    _show_model_picker(cli, ctx, False)
    _show_model_picker(cli, ctx, True)
    assert len(opened) == 2
    assert calls[0]["non_blocking_catalogs"] is True
    assert calls[0]["probe_custom_providers"] is False
    assert calls[0]["probe_current_custom_provider"] is False
    assert calls[1]["non_blocking_catalogs"] is False
    assert calls[1]["probe_custom_providers"] is True


def test_picker_moves_to_reasoning_without_waiting_for_remote_validation(monkeypatch):
    import cli as cli_mod
    from hermes_cli.cli_model_switch_mixin import _picker_reasoning_rows

    started = threading.Event()
    release = threading.Event()

    def slow_resolver(**_kwargs):
        started.set()
        assert release.wait(5)
        return ModelSwitchResult(
            success=True, new_model="openai/gpt-5.5-pro", target_provider="nous",
        )

    picker, captured, applied, result = _picker(monkeypatch, resolver=slow_resolver)
    try:
        _bound(cli_mod.HermesCLI._handle_model_picker_selection, picker)(persist_global=True)
        assert picker._model_picker_state["stage"] == "reasoning"
        assert picker._model_picker_state["selected_model"] == "openai/gpt-5.5-pro"
        assert started.wait(2)
        assert not applied.is_set()

        picker._model_picker_state["selected"] = len(_picker_reasoning_rows()) - 1
        _bound(cli_mod.HermesCLI._handle_model_picker_selection, picker)(persist_global=True)
        assert picker._model_picker_state is None
        assert not applied.is_set()  # waiting off the input thread
    finally:
        release.set()
    assert applied.wait(5)
    assert captured["args"] == (result, True, None, "")


def test_picker_without_reasoning_also_resolves_off_the_input_thread(monkeypatch):
    import cli as cli_mod
    started = threading.Event()
    release = threading.Event()

    def slow_resolver(**_kwargs):
        started.set()
        assert release.wait(5)
        return ModelSwitchResult(success=True, new_model="x", target_provider="nous")

    picker, captured, applied, _ = _picker(monkeypatch, reasoning=False, resolver=slow_resolver)
    try:
        _bound(cli_mod.HermesCLI._handle_model_picker_selection, picker)(persist_global=False)
        assert picker._model_picker_state is None
        assert started.wait(2)
        assert not applied.is_set()
    finally:
        release.set()
    assert applied.wait(5)
    assert captured["args"][1:] == (False, None, "")
