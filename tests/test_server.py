from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.messages import MessageHandler

from ipython_mcp.config import ServerConfig
from ipython_mcp.runtime import ShellRuntime
from ipython_mcp.server import create_server

EXPECTED_TOOLS = {
    "list",
    "execute",
    "call_function",
    "search",
    "reload",
    "inspect",
    "remove",
    "reset",
    "register_tool",
    "unregister_tool",
    "runtime_status",
}


def call(coro):
    return asyncio.run(coro)


async def call_tool(client: Client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    return result.structured_content


def test_exact_catalog_and_persistent_cross_call_state():
    async def scenario():
        async with Client(create_server()) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == EXPECTED_TOOLS
            executed = await call_tool(client, "execute", {"code": "value = 7\n\ndef add(x: int) -> int:\n    \"\"\"Add to the persistent value.\"\"\"\n    return value + x\n\nclass Marker:\n    pass"})
            assert executed["ok"] is True
            listed = await call_tool(client, "list")
            assert all(item["name"] != "open" for item in listed["functions"])
            add = next(item for item in listed["functions"] if item["name"] == "add")
            assert add["signature"] == "(x: int) -> int"
            assert add["description"] == "Add to the persistent value."
            assert (await call_tool(client, "call_function", {"name": "add", "arguments": {"x": 5}}))["result"] == 12
            found = await call_tool(client, "search", {"query": "Mark"})
            assert any(item["name"] == "Marker" and item["kind"] == "type" for item in found["results"])

    call(scenario())


def test_execute_errors_capture_output_and_recover():
    async def scenario():
        async with Client(create_server()) as client:
            failed = await call_tool(client, "execute", {"code": "print('before')\nimport sys\nprint('err', file=sys.stderr)\nraise ValueError('broken')"})
            assert failed["ok"] is False
            assert failed["error"]["code"] == "execution_error"
            assert failed["error"]["error_type"] == "ValueError"
            assert "broken" in failed["error"]["message"]
            assert failed["stdout"] == "before\n"
            assert failed["stderr"] == "err\n"
            syntax = await call_tool(client, "execute", {"code": "def broken("})
            assert syntax["error"]["code"] == "syntax_error"
            assert syntax["stdout"] == ""
            assert syntax["stderr"] == ""
            recovered = await call_tool(client, "execute", {"code": "recovered = 42\nrecovered"})
            assert recovered["ok"] is True
            assert recovered["result"] == 42
            non_json = await call_tool(client, "execute", {"code": "object()"})
            assert non_json["ok"] is True
            assert non_json["result"]["json_compatible"] is False
            assert non_json["stdout"] == ""

    call(scenario())


def test_runtime_teardown_is_idempotent_and_closed():
    async def scenario():
        runtime = ShellRuntime(ServerConfig())
        await runtime.start()
        assert runtime.closed is False
        await runtime.execute("teardown_probe = 1")
        await runtime.close()
        assert runtime.closed is True
        await runtime.close()

    call(scenario())


def test_end_to_end_vertical_slice_and_clean_lifespan_teardown(tmp_path: Path):
    module = tmp_path / "vertical_lib.py"
    module.write_text("VALUE = 1\ndef value():\n    return VALUE\n", encoding="utf-8")
    config = ServerConfig(library_paths=(tmp_path,), preload_modules=("vertical_lib",))

    async def scenario():
        async with Client(create_server(config)) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == EXPECTED_TOOLS
            await call_tool(client, "execute", {"code": "seed = 10\ndef add(x):\n    return seed + x\nclass Marker: pass"})
            listed = await call_tool(client, "list")
            assert any(item["name"] == "add" for item in listed["functions"])
            searched = await call_tool(client, "search", {"query": "Mark"})
            assert any(item["name"] == "Marker" for item in searched["results"])
            assert (await call_tool(client, "call_function", {"name": "add", "arguments": {"x": 2}}))["result"] == 12
            assert (await call_tool(client, "call_function", {"name": "vertical_lib.value", "arguments": {}}))["result"] == 1
            module.write_text("VALUE = 2\ndef value():\n    return VALUE\n", encoding="utf-8")
            assert (await call_tool(client, "reload", {"modules": ["vertical_lib"]}))["results"][0]["ok"] is True
            assert (await call_tool(client, "call_function", {"name": "vertical_lib.value", "arguments": {}}))["result"] == 2

    call(scenario())
    assert not any(thread.name.startswith("ipython-shell_") for thread in threading.enumerate())
    assert not any(thread.name == "IPythonHistorySavingThread" for thread in threading.enumerate())


def test_call_function_structured_failures_and_json_boundary():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(client, "execute", {"code": "state = []\ndef remember(item):\n    state.append(item)\n    return {'count': len(state)}"})
            assert (await call_tool(client, "call_function", {"name": "missing", "arguments": {}}))["error"]["code"] == "name_not_found"
            assert (await call_tool(client, "call_function", {"name": "state", "arguments": {}}))["error"]["code"] == "not_callable"
            assert (await call_tool(client, "call_function", {"name": "remember", "arguments": "not-json"}))["error"]["code"] == "invalid_arguments"
            assert (await call_tool(client, "call_function", {"name": "remember", "arguments": {}}))["error"]["code"] == "argument_binding_error"
            assert (await call_tool(client, "call_function", {"name": "remember", "arguments": {"item": "x"}}))["result"] == {"count": 1}
            await call_tool(client, "execute", {"code": "def bad():\n    return object()"})
            assert (await call_tool(client, "call_function", {"name": "bad", "arguments": {}}))["error"]["code"] == "result_not_json"
            assert (await call_tool(client, "execute", {"code": "still_usable = 1"}))["ok"] is True

    call(scenario())


def test_preload_library_reload_and_alias(tmp_path: Path):
    module = tmp_path / "agent_lib.py"
    module.write_text("VALUE = 1\ndef value():\n    return VALUE\n", encoding="utf-8")
    config = ServerConfig(library_paths=(tmp_path,), preload_modules=("agent_lib",), module_aliases={"agent_lib": "lib"})
    library_path = str(tmp_path.resolve())

    async def scenario():
        async with Client(create_server(config)) as client:
            assert library_path not in sys.path
            assert (await call_tool(client, "call_function", {"name": "lib.value", "arguments": {}}))["result"] == 1
            module.write_text("VALUE = 2\ndef value():\n    return VALUE\n", encoding="utf-8")
            reloaded = await call_tool(client, "reload", {"modules": ["agent_lib"]})
            assert reloaded["results"][0]["ok"] is True
            assert (await call_tool(client, "call_function", {"name": "lib.value", "arguments": {}}))["result"] == 2

    call(scenario())
    assert library_path not in sys.path


def test_preload_failure_names_module_and_is_atomic(tmp_path: Path):
    config = ServerConfig(library_paths=(tmp_path,), preload_modules=("does_not_exist",))
    library_path = str(tmp_path.resolve())

    async def scenario():
        async with Client(create_server(config)) as client:
            status = await call_tool(client, "runtime_status")
            assert status["state"] == "unavailable"
            assert status["error"]["code"] == "worker_startup_failed"
            assert "does_not_exist" in status["error"]["message"]
            failed = await call_tool(client, "execute", {"code": "value = 1"})
            assert failed["error"]["code"] == "runtime_unavailable"
            assert library_path not in sys.path

    call(scenario())


def test_serialized_owner_prevents_overlap():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(client, "execute", {"code": "events = []"})
            first = call_tool(client, "execute", {"code": "import time\ntime.sleep(0.05)\nevents.append('first')"})
            second = call_tool(client, "execute", {"code": "events.append('second')"})
            await asyncio.gather(first, second)
            observed = await call_tool(client, "execute", {"code": "events"})
            assert observed["result"] in (["first", "second"], ["second", "first"])
            assert len(observed["result"]) == 2
            second_observation = await call_tool(client, "execute", {"code": "events"})
            assert second_observation["execution_count"] == observed["execution_count"] + 1

    call(scenario())


def test_default_logs_do_not_contain_sensitive_call_data(caplog):
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(client, "execute", {"code": "secret_result = 'result-value'"})
            await call_tool(client, "call_function", {"name": "str", "arguments": {"object": "json-argument"}})
            await call_tool(client, "execute", {"code": "def explode():\n    traceback_local = 'local-secret'\n    raise RuntimeError('failure')"})
            await call_tool(client, "call_function", {"name": "explode", "arguments": {}})

    with caplog.at_level(logging.INFO, logger="ipython_mcp.controller"):
        call(scenario())
    logs = caplog.text
    for secret in ("secret_result", "result-value", "json-argument", "local-secret", "traceback_local"):
        assert secret not in logs
    assert "tool=execute" in logs


def test_inspect_functions_objects_types_bounds_and_search_compatibility():
    config = ServerConfig(max_text_chars=40, max_repr_chars=24)

    async def scenario():
        async with Client(create_server(config)) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "class Item:\n    \"\"\"An item with deliberately long documentation for bounding.\"\"\"\n    def __repr__(self):\n        return '<Item ' + ('x' * 80) + '>'\n\nitem = Item()\n\ndef described(value: int = 1) -> str:\n    \"\"\"A deliberately long function description that must be bounded.\"\"\"\n    return str(value)"
                },
            )

            function = await call_tool(client, "inspect", {"name": "described"})
            assert function["ok"] is True
            assert function["kind"] == "function"
            assert function["qualified_type"] == "builtins.function"
            assert function["callable"] is True
            assert function["signature"] == "(value: int = 1) -> str"
            assert function["module"] == "__main__"
            assert function["truncated"]["documentation"] is True
            assert function["truncated"]["signature"] is False

            obj = await call_tool(client, "inspect", {"name": "item"})
            assert obj["ok"] is True
            assert obj["kind"] == "variable"
            assert obj["qualified_type"] == "__main__.Item"
            assert obj["callable"] is False
            assert obj["signature"] is None
            assert obj["module"] == "__main__"
            assert obj["truncated"]["representation"] is True
            assert len(obj["representation"]) == config.max_repr_chars

            inspected_type = await call_tool(client, "inspect", {"name": "Item"})
            assert inspected_type["ok"] is True
            assert inspected_type["kind"] == "type"
            assert inspected_type["callable"] is True

            searched = await call_tool(client, "search", {"query": "item", "exact": True})
            assert len(searched["results"]) == 1
            assert set(searched["results"][0]) == {
                "name",
                "kind",
                "qualified_type",
                "representation",
                "description",
                "truncated",
            }

    call(scenario())


def test_inspect_structured_failures_do_not_destabilize_runtime():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "class Host:\n    @property\n    def broken(self):\n        raise RuntimeError('attribute exploded')\n\nclass BadRepr:\n    def __repr__(self):\n        raise ValueError('representation exploded')\n\nhost = Host()\nbad = BadRepr()\n\ndef ping():\n    return 'pong'"
                },
            )
            missing = await call_tool(client, "inspect", {"name": "missing"})
            assert missing["ok"] is False
            assert missing["error"]["code"] == "name_not_found"
            inaccessible = await call_tool(client, "inspect", {"name": "host.broken"})
            assert inaccessible["error"]["code"] == "attribute_access_error"
            failed_repr = await call_tool(client, "inspect", {"name": "bad"})
            assert failed_repr["error"]["code"] == "representation_error"

            assert (await call_tool(client, "execute", {"code": "recovered_after_inspect = 1"}))["ok"] is True
            assert any(item["name"] == "ping" for item in (await call_tool(client, "list"))["functions"])
            assert (await call_tool(client, "search", {"query": "host", "exact": True}))["results"][0]["name"] == "host"
            assert (await call_tool(client, "call_function", {"name": "ping", "arguments": {}}))["result"] == "pong"

    call(scenario())


def test_remove_partitions_names_and_protects_runtime_and_configured_bindings(tmp_path: Path):
    module = tmp_path / "cleanup_lib.py"
    module.write_text("VALUE = 7\n", encoding="utf-8")
    config = ServerConfig(
        library_paths=(tmp_path,),
        preload_modules=("cleanup_lib",),
        module_aliases={"cleanup_lib": "lib"},
    )

    async def scenario():
        async with Client(create_server(config)) as client:
            await call_tool(client, "execute", {"code": "alpha = 1\nbeta = 2\n_private = 3"})
            removed = await call_tool(
                client,
                "remove",
                {"names": ["alpha", "In", "_private", "lib", "missing", "alpha"]},
            )
            assert removed["ok"] is True
            assert removed["removed"] == ["alpha"]
            assert removed["refused"] == ["In", "_private", "lib"]
            assert removed["unknown"] == ["missing"]
            assert removed["error"] is None
            repeated = await call_tool(
                client,
                "remove",
                {"names": ["alpha", "In", "_private", "lib", "missing", "alpha"]},
            )
            assert repeated["removed"] == []
            assert repeated["refused"] == ["In", "_private", "lib"]
            assert repeated["unknown"] == ["alpha", "missing"]
            assert (await call_tool(client, "inspect", {"name": "lib"}))["kind"] == "module"
            assert (await call_tool(client, "search", {"query": "beta", "exact": True}))["results"][0]["name"] == "beta"

    call(scenario())


def test_reset_is_idempotent_rebinds_modules_and_preserves_execution_count(tmp_path: Path):
    module = tmp_path / "reset_lib.py"
    module.write_text("VALUE = 11\ndef value():\n    return VALUE\n", encoding="utf-8")
    config = ServerConfig(
        library_paths=(tmp_path,),
        preload_modules=("reset_lib",),
        module_aliases={"reset_lib": "lib"},
    )

    async def scenario():
        async with Client(create_server(config)) as client:
            created = await call_tool(
                client,
                "execute",
                {"code": "zeta = 1\nalpha = 2\n_private = 3\nlib = 'shadowed'\nIn = 'shadowed'"},
            )
            first = await call_tool(client, "reset")
            assert first["ok"] is True
            assert first["removed"] == ["alpha", "zeta"]
            assert first["execution_count"] == created["execution_count"]
            assert (await call_tool(client, "call_function", {"name": "lib.value", "arguments": {}}))["result"] == 11

            runtime_binding = await call_tool(client, "execute", {"code": "isinstance(In, list)"})
            assert runtime_binding["result"] is True
            assert runtime_binding["execution_count"] == created["execution_count"] + 1
            second = await call_tool(client, "reset")
            assert second["removed"] == []
            assert second["execution_count"] == runtime_binding["execution_count"]
            after = await call_tool(client, "execute", {"code": "42"})
            assert after["execution_count"] == runtime_binding["execution_count"] + 1

    call(scenario())


def test_cleanup_calls_share_serialized_owner_and_recover():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(client, "execute", {"code": "temporary = 1"})
            remove_result, reset_result = await asyncio.gather(
                call_tool(client, "remove", {"names": ["temporary"]}),
                call_tool(client, "reset"),
            )
            assert remove_result["ok"] is True
            assert reset_result["ok"] is True
            assert (await call_tool(client, "execute", {"code": "owner_recovered = 1"}))["ok"] is True

    call(scenario())


def test_dynamic_registration_schema_fingerprint_bounds_and_invocation():
    config = ServerConfig(max_tool_description_chars=24)

    async def scenario():
        async with Client(create_server(config)) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "from typing import Literal\n\ndef typed(name: str, tags: set[str], pair: tuple[str, int], mapping: dict[str, int], count: int = 2, values: list[float] = [], maybe: int | None = None, *, choice: Literal['a', 'b'] = 'a') -> str:\n    \"\"\"A deliberately overlong dynamic tool description.\"\"\"\n    return f'{name}:{sorted(tags)}:{pair}:{mapping}:{count}:{values}:{maybe}:{choice}'"
                },
            )
            registered = await call_tool(client, "register_tool", {"name": "typed"})
            assert registered["ok"] is True
            registration = registered["registration"]
            assert registration["tool_name"] == "typed"
            assert registration["description_truncated"] is True
            assert len(registration["description"]) == config.max_tool_description_chars
            schema = registration["input_schema"]
            assert schema["required"] == ["name", "tags", "pair", "mapping"]
            assert schema["additionalProperties"] is False
            assert schema["properties"]["count"]["default"] == 2
            assert schema["properties"]["values"]["items"] == {"type": "number"}
            assert schema["properties"]["tags"]["uniqueItems"] is True
            assert schema["properties"]["pair"]["prefixItems"] == [
                {"type": "string"},
                {"type": "integer"},
            ]
            assert schema["properties"]["mapping"] == {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            }
            assert schema["properties"]["maybe"]["anyOf"] == [
                {"type": "integer"},
                {"type": "null"},
            ]
            canonical = json.dumps(
                schema,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            assert registration["schema_fingerprint"] == hashlib.sha256(
                canonical.encode()
            ).hexdigest()

            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["typed"].inputSchema == schema
            invoked = await call_tool(
                client,
                "typed",
                {
                    "name": "item",
                    "tags": ["b", "a"],
                    "pair": ["p", 3],
                    "mapping": {"k": 4},
                    "values": [1, 2.5],
                    "choice": "b",
                },
            )
            assert invoked["result"] == "item:['a', 'b']:('p', 3):{'k': 4}:2:[1.0, 2.5]:None:b"
            invalid = await call_tool(
                client,
                "typed",
                {
                    "name": "item",
                    "tags": [],
                    "pair": ["p", 3],
                    "mapping": {},
                    "extra": 1,
                },
            )
            assert invalid["error"]["code"] == "invalid_arguments"

    call(scenario())


def test_dynamic_registration_name_protection_collisions_and_catalog_limit():
    config = ServerConfig(max_dynamic_tools=2, max_tool_name_chars=8)

    async def scenario():
        async with Client(create_server(config)) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "def first(x: int) -> int:\n    return x\n\ndef second(x: int) -> int:\n    return x\n\ndef third(x: int) -> int:\n    return x"
                },
            )
            invalid_symbol = await call_tool(client, "register_tool", {"name": "first.attr"})
            assert invalid_symbol["error"]["code"] == "invalid_symbol_name"
            protected = await call_tool(client, "register_tool", {"name": "open"})
            assert protected["error"]["code"] == "invalid_symbol_name"
            stable = await call_tool(
                client, "register_tool", {"name": "first", "tool_name": "execute"}
            )
            assert stable["error"]["code"] == "stable_tool_collision"
            invalid_tool = await call_tool(
                client, "register_tool", {"name": "first", "tool_name": "1bad"}
            )
            assert invalid_tool["error"]["code"] == "invalid_tool_name"
            too_long = await call_tool(
                client, "register_tool", {"name": "first", "tool_name": "tool_name"}
            )
            assert too_long["error"]["code"] == "invalid_tool_name"

            assert (await call_tool(client, "register_tool", {"name": "first"}))["ok"]
            duplicate_name = await call_tool(
                client, "register_tool", {"name": "second", "tool_name": "first"}
            )
            assert duplicate_name["error"]["code"] == "duplicate_tool_name"
            duplicate_symbol = await call_tool(
                client, "register_tool", {"name": "first", "tool_name": "alias"}
            )
            assert duplicate_symbol["error"]["code"] == "duplicate_symbol"
            assert (await call_tool(client, "register_tool", {"name": "second"}))["ok"]
            full = await call_tool(client, "register_tool", {"name": "third"})
            assert full["error"]["code"] == "registry_full"
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS | {
                "first",
                "second",
            }

    call(scenario())


def test_dynamic_registration_rejects_unsupported_signatures_and_callable_kinds():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "def unannotated(x):\n    return x\n\ndef positional(x: int, /) -> int:\n    return x\n\ndef variadic(*args: int) -> int:\n    return len(args)\n\ndef bad_default(x: int = object()) -> int:\n    return x\n\ndef unresolved(x: 'MissingType') -> int:\n    return 1\n\nasync def async_fn(x: int) -> int:\n    return x\n\ndef generator_fn(x: int):\n    yield x\n\nasync def async_generator_fn(x: int):\n    yield x\n\nclass AsyncCallable:\n    async def __call__(self, x: int) -> int:\n        return x\nasync_callable = AsyncCallable()\n\nclass BadSignature:\n    __signature__ = 'malformed'\n    def __call__(self, x: int) -> int:\n        return x\nbad_signature = BadSignature()"
                },
            )
            for name in (
                "unannotated",
                "positional",
                "variadic",
                "bad_default",
                "unresolved",
                "bad_signature",
            ):
                response = await call_tool(client, "register_tool", {"name": name})
                assert response["error"]["code"] == "unsupported_signature", name
            for name in (
                "async_fn",
                "generator_fn",
                "async_generator_fn",
                "async_callable",
            ):
                response = await call_tool(client, "register_tool", {"name": name})
                assert response["error"]["code"] == "unsupported_callable_kind", name
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS
            await call_tool(
                client,
                "execute",
                {"code": "def recovered(x: int) -> int:\n    return x + 1"},
            )
            assert (await call_tool(client, "register_tool", {"name": "recovered"}))["ok"]

    call(scenario())


def test_dynamic_class_schema_uses_constructor_and_tracks_in_place_replacement():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "class Product:\n    class_level: str\n    def __init__(self, count: int = 1):\n        self.count = count"
                },
            )
            registered = await call_tool(client, "register_tool", {"name": "Product"})
            assert registered["ok"] is True
            schema = registered["registration"]["input_schema"]
            assert schema["properties"] == {"count": {"type": "integer", "default": 1}}
            assert "class_level" not in schema["properties"]
            cached = next(tool for tool in await client.list_tools() if tool.name == "Product")

            await call_tool(
                client,
                "execute",
                {
                    "code": "def replacement_init(self, count: str = 'one'):\n    self.count = count\nProduct.__init__ = replacement_init"
                },
            )
            assert "Product" not in {tool.name for tool in await client.list_tools()}
            assert cached.inputSchema == schema
            stale = await call_tool(client, "Product", {"count": 1})
            assert stale["error"]["code"] == "stale_registration"

            refreshed = await call_tool(client, "register_tool", {"name": "Product"})
            assert refreshed["ok"] is True
            assert refreshed["registration"]["input_schema"]["properties"] == {
                "count": {"type": "string", "default": "one"}
            }

    call(scenario())


def test_dynamic_compatible_body_and_description_changes_use_current_callable():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {"code": "def changing(x: int = 1) -> int:\n    \"\"\"original description\"\"\"\n    return x + 1"},
            )
            first = await call_tool(client, "register_tool", {"name": "changing"})
            fingerprint = first["registration"]["schema_fingerprint"]
            revision = first["registration"]["revision"]
            await call_tool(
                client,
                "execute",
                {"code": "def changing(x: int = 1) -> int:\n    \"\"\"new description\"\"\"\n    return x + 10"},
            )
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["changing"].description == "original description"
            assert (await call_tool(client, "changing", {"x": 2}))["result"] == 12
            repeated = await call_tool(client, "register_tool", {"name": "changing"})
            assert repeated["registration"]["schema_fingerprint"] == fingerprint
            assert repeated["registration"]["description"] == "new description"
            assert repeated["registration"]["revision"] == revision + 1

    call(scenario())


def test_return_annotations_do_not_affect_input_compatibility_or_resolution():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {"code": "def returns_unknown(x: int) -> 'MissingReturnType':\n    return x"},
            )
            registered = await call_tool(
                client, "register_tool", {"name": "returns_unknown"}
            )
            assert registered["ok"] is True
            fingerprint = registered["registration"]["schema_fingerprint"]
            await call_tool(
                client,
                "execute",
                {"code": "def returns_unknown(x: int) -> str:\n    return str(x)"},
            )
            assert "returns_unknown" in {tool.name for tool in await client.list_tools()}
            assert (await call_tool(client, "returns_unknown", {"x": 7}))["result"] == "7"
            repeated = await call_tool(
                client, "register_tool", {"name": "returns_unknown"}
            )
            assert repeated["registration"]["schema_fingerprint"] == fingerprint

    call(scenario())


def test_every_incompatible_signature_change_becomes_stale():
    cases = {
        "add optional": "def target(x: int, y: int = 1) -> int:\n    return x + y",
        "remove": "def target() -> int:\n    return 1",
        "rename": "def target(y: int) -> int:\n    return y",
        "default": "def target(x: int = 2) -> int:\n    return x",
        "narrow": "from typing import Literal\ndef target(x: Literal[1]) -> int:\n    return x",
        "widen": "def target(x: int | str) -> int:\n    return 1",
        "change": "def target(x: str) -> int:\n    return len(x)",
    }

    async def one(label: str, replacement: str):
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {"code": "def target(x: int = 1) -> int:\n    return x"},
            )
            assert (await call_tool(client, "register_tool", {"name": "target"}))["ok"]
            cached = next(tool for tool in await client.list_tools() if tool.name == "target")
            await call_tool(client, "execute", {"code": replacement})
            assert "target" not in {tool.name for tool in await client.list_tools()}, label
            assert cached.inputSchema["properties"]["x"]["type"] == "integer"
            stale = await call_tool(client, "target", {"x": 1})
            assert stale["error"]["code"] == "stale_registration", label
            refreshed = await call_tool(client, "register_tool", {"name": "target"})
            assert refreshed["ok"] is True

    async def scenario():
        for label, replacement in cases.items():
            await one(label, replacement)

    call(scenario())


def test_wrapped_and_partial_in_place_signature_state_cannot_bypass_reconciliation():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "import functools\n\ndef inner(x: int = 1) -> int:\n    return x\n\n@functools.wraps(inner)\ndef wrapped(*args, **kwargs):\n    return inner(*args, **kwargs)\n\ndef base(x: int, *, y: int = 2) -> int:\n    return x + y\npartial_tool = functools.partial(base, y=2)"
                },
            )
            assert (await call_tool(client, "register_tool", {"name": "wrapped"}))["ok"]
            assert (
                await call_tool(
                    client,
                    "register_tool",
                    {"name": "partial_tool", "tool_name": "partial-live"},
                )
            )["ok"]
            await call_tool(
                client,
                "execute",
                {"code": "inner.__defaults__ = (3,)\npartial_tool.keywords['y'] = 4"},
            )
            tools = {tool.name for tool in await client.list_tools()}
            assert "wrapped" not in tools
            assert "partial-live" not in tools
            assert (await call_tool(client, "wrapped", {}))["error"]["code"] == "stale_registration"
            assert (
                await call_tool(client, "partial-live", {"x": 1})
            )["error"]["code"] == "stale_registration"

            await call_tool(
                client,
                "execute",
                {"code": "def cyclic(x: int) -> int:\n    return x\ncyclic.__wrapped__ = cyclic"},
            )
            cyclic = await call_tool(client, "register_tool", {"name": "cyclic"})
            assert cyclic["error"]["code"] == "unsupported_signature"
            assert (await call_tool(client, "execute", {"code": "after_cycle = 1"}))["ok"]

    call(scenario())


def test_dynamic_delete_remove_reset_unregister_and_restart_lifecycle():
    async def scenario():
        server = create_server()
        async with Client(server) as client:
            await call_tool(
                client,
                "execute",
                {"code": "def deleted(x: int) -> int:\n    return x\ndef removed(x: int) -> int:\n    return x\ndef reset_me(x: int) -> int:\n    return x"},
            )
            for name in ("deleted", "removed", "reset_me"):
                assert (await call_tool(client, "register_tool", {"name": name}))["ok"]
            await call_tool(client, "execute", {"code": "del deleted"})
            assert "deleted" not in {tool.name for tool in await client.list_tools()}
            await call_tool(client, "remove", {"names": ["removed"]})
            assert "removed" not in {tool.name for tool in await client.list_tools()}
            reset = await call_tool(client, "reset")
            assert reset["ok"] is True
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS

            await call_tool(
                client,
                "execute",
                {"code": "def explicit(x: int) -> int:\n    return x"},
            )
            assert (await call_tool(client, "register_tool", {"name": "explicit"}))["ok"]
            unregistered = await call_tool(
                client,
                "unregister_tool",
                {"names": ["explicit", "execute", "missing", "explicit"]},
            )
            assert unregistered["unregistered"] == ["explicit"]
            assert unregistered["unknown"] == ["execute", "missing"]
            repeated = await call_tool(
                client, "unregister_tool", {"names": ["explicit"]}
            )
            assert repeated["unregistered"] == []
            assert repeated["unknown"] == ["explicit"]
            assert "execute" in {tool.name for tool in await client.list_tools()}

        async with Client(server) as restarted:
            assert {tool.name for tool in await restarted.list_tools()} == EXPECTED_TOOLS

    call(scenario())


class ToolChangeRecorder(MessageHandler):
    def __init__(self):
        self.count = 0

    async def on_tool_list_changed(self, message):
        del message
        self.count += 1


def test_dynamic_notifications_revision_and_stable_schema_compatibility():
    async def scenario():
        recorder = ToolChangeRecorder()
        async with Client(create_server(), message_handler=recorder) as client:
            before = {
                tool.name: tool.inputSchema
                for tool in await client.list_tools()
                if tool.name in EXPECTED_TOOLS
            }
            await call_tool(
                client,
                "execute",
                {"code": "def notified(x: int) -> int:\n    return x"},
            )
            registered = await call_tool(client, "register_tool", {"name": "notified"})
            first_revision = registered["registration"]["revision"]
            await asyncio.sleep(0)
            assert recorder.count >= 1
            unchanged = await call_tool(client, "register_tool", {"name": "notified"})
            assert unchanged["registration"]["revision"] == first_revision
            after = {
                tool.name: tool.inputSchema
                for tool in await client.list_tools()
                if tool.name in EXPECTED_TOOLS
            }
            assert after == before
            removed = await call_tool(
                client, "unregister_tool", {"names": ["notified"]}
            )
            assert removed["revision"] == first_revision + 1
            await asyncio.sleep(0)
            assert recorder.count >= 2

    call(scenario())


def test_dynamic_concurrency_exactly_once_immutable_snapshots_and_recovery():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "calls = []\ndef once(value: int) -> int:\n    calls.append(value)\n    return len(calls)\ndef explodes(value: int) -> int:\n    raise RuntimeError('boom')"
                },
            )
            assert (await call_tool(client, "register_tool", {"name": "once"}))["ok"]
            assert (await call_tool(client, "register_tool", {"name": "explodes"}))["ok"]
            snapshot = next(tool for tool in await client.list_tools() if tool.name == "once")
            results = await asyncio.gather(
                *(call_tool(client, "once", {"value": value}) for value in range(8))
            )
            assert sorted(item["result"] for item in results) == list(range(1, 9))
            observed = await call_tool(client, "execute", {"code": "calls"})
            assert sorted(observed["result"]) == list(range(8))
            failed = await call_tool(client, "explodes", {"value": 1})
            assert failed["error"]["code"] == "callable_error"
            invalid = await call_tool(client, "once", {"value": "not-an-int"})
            assert invalid["error"]["code"] == "invalid_arguments"
            assert (await call_tool(client, "once", {"value": 9}))["result"] == 9

            started = time.monotonic()
            long_execute = call_tool(
                client,
                "execute",
                {
                    "code": "import time\ntime.sleep(0.06)\ndef once(value: str) -> int:\n    return len(value)"
                },
            )
            await asyncio.sleep(0.01)
            discovery = client.list_tools()
            await asyncio.gather(long_execute, discovery)
            assert time.monotonic() - started >= 0.05
            assert "once" not in {tool.name for tool in await client.list_tools()}
            assert snapshot.inputSchema["properties"]["value"] == {"type": "integer"}
            assert (
                await call_tool(client, "once", {"value": 1})
            )["error"]["code"] == "stale_registration"
            assert (await call_tool(client, "execute", {"code": "recovered = True"}))["ok"]

    call(scenario())


def test_dynamic_failing_representation_and_mixed_concurrency_recover_atomically():
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(
                client,
                "execute",
                {
                    "code": "class BadReprCallable:\n    def __repr__(self):\n        raise RuntimeError('no repr')\n    def __call__(self, value: int) -> int:\n        return value + 1\nbad_repr_callable = BadReprCallable()\n\ndef alpha(value: int) -> int:\n    return value\ndef beta(value: int) -> int:\n    return value\ndef gamma(value: int) -> int:\n    return value"
                },
            )
            assert (
                await call_tool(client, "register_tool", {"name": "bad_repr_callable"})
            )["ok"]
            assert (
                await call_tool(client, "bad_repr_callable", {"value": 2})
            )["result"] == 3
            assert (await call_tool(client, "register_tool", {"name": "alpha"}))["ok"]
            assert (await call_tool(client, "register_tool", {"name": "beta"}))["ok"]

            results = await asyncio.gather(
                call_tool(client, "alpha", {"value": 1}),
                call_tool(client, "unregister_tool", {"names": ["beta"]}),
                call_tool(client, "remove", {"names": ["alpha"]}),
                call_tool(client, "register_tool", {"name": "gamma"}),
                client.list_tools(),
                return_exceptions=True,
            )
            assert not any(isinstance(result, BaseException) for result in results)
            reset = await call_tool(client, "reset")
            assert reset["ok"] is True
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS
            await call_tool(
                client,
                "execute",
                {"code": "def recovered_tool(value: int) -> int:\n    return value"},
            )
            assert (
                await call_tool(client, "register_tool", {"name": "recovered_tool"})
            )["ok"]

    call(scenario())
