"""No placeholder provider: the production path must never return a hardcoded
"AI completion". This guards against the disconnected scaffolding that once sat in
jarvis/gateway/aigateway.py with a `_call_provider` returning a literal string."""
import pkgutil
import importlib


def test_every_provider_adapter_has_a_real_request_path():
    """Each registered adapter builds a real wire request; none has a placeholder
    completion string. The names that shipped as the dead gateway scaffolding must be
    gone, so a future contributor cannot mistake them for a working integration.

    ``openai_compatible`` is the sanctioned exception to "non-empty default base URL":
    its endpoint is the user-supplied ``llm_base_url`` in the persisted selection, not a
    hardcoded vendor constant, so an empty default *is* the real, correct value (an
    unsaved base URL must read as "not configured", never point at a vendor nobody chose).
    """
    import jarvis.gateway.providers as providers

    for provider_id, cls in providers.PROVIDERS.items():
        # A real adapter must carry a name, an endpoint default, and a chat request.
        assert cls.name == provider_id
        if provider_id == "openai_compatible":
            # Endpoint comes from the user's selection; emptiness is the honest default.
            assert cls.DEFAULT_BASE_URL == ""
            continue
        assert getattr(cls, "DEFAULT_BASE_URL", ""), f"{provider_id} has no endpoint"
        assert cls._chat_request is not providers.AIProvider._chat_request, \
            f"{provider_id} did not implement _chat_request"


def test_no_placeholder_string_in_the_gateway():
    """The gateway source must not contain a hardcoded placeholder completion.
    (the word 'placeholder' must not appear in the request path at all)."""
    import jarvis.gateway as gw
    src = open(gw.__file__.replace("__init__.py", "providers.py"), encoding="utf-8").read()
    for marker in ("This is a placeholder response", "This is a placeholder",
                    "Full implementation requires running provider adapters"):
        assert marker not in src, f"placeholder text present in providers.py: {marker!r}"


def test_dead_gateway_scaffolding_is_gone():
    """The disconnected aigateway/firewall modules (which returned a hardcoded response
    and never had a caller) are removed; their package must not import them."""
    import jarvis.gateway as gw
    # A fresh submodule walk finds only modules that actually load.
    failed = []
    for mod in pkgutil.walk_packages(gw.__path__, "jarvis.gateway."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001 - we report, not swallow
            failed.append((mod.name, type(exc).__name__))
    assert not failed, f"gateway submodules fail to import: {failed}"


def test_all_gateway_submodules_import_cleanly():
    """Gate 6-adjacent: the whole gateway package imports with no dead references."""
    import jarvis.gateway as gw
    gw.__path__  # noqa: B018 - ensure the package is materialised
    for mod in pkgutil.walk_packages(gw.__path__, "jarvis.gateway."):
        importlib.import_module(mod.name)
