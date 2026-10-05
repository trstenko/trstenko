"""Tools that do not ship with the device (plan 10).

A third-party tool is a `.trsttool` file: a description the firmware reads
(`package.py`) and code the firmware never imports. Each call runs in a process
of its own (`runner.py`), which can reach the internet and its own storage only
by asking `host.py`, where the tool's declared permissions are checked.

`functions()` turns an unpacked package into functions the registry takes like
any other tool. They are always `open_world` (the result is data from outside,
never instructions) and never `final` (the model reads the result and answers
itself, so a tool cannot put words in the device's mouth).
"""

import os

from ..tools.registry import Spec, ToolError
from . import host, package

__all__ = ["functions", "host", "package", "MAX_ENABLED"]

# Tool functions switched on at once, across every installed file. The list is
# sent with every request, and a small model picks worse from a long one.
MAX_ENABLED = 4


def functions(pkg, folder, storage_dir=None, fetcher=host.fetch,
              seconds=host.CALL_SECONDS):
    """The registry functions for one unpacked package."""
    storage = None
    if pkg.storage and storage_dir:
        storage = host.Storage(os.path.join(storage_dir, pkg.id + ".json"))
    return [_function(pkg, folder, tool, storage, fetcher, seconds)
            for tool in pkg.tools]


def _function(pkg, folder, tool, storage, fetcher, seconds):
    def call(ctx, **args):
        given = {key: value for key, value in args.items()
                 if value is not None}
        payload, problem = host.run(
            folder, tool["function"], given,
            host.Call(pkg, storage, fetcher), lang=ctx.lang,
            now=ctx.now_local(), tz=ctx.tz(), seconds=seconds)
        if problem:
            raise ToolError(problem)
        return payload

    call.spec = Spec(tool["name"], tool["description"], dict(tool["params"]),
                     final=False, destructive=False, open_world=True,
                     preview=None, fn=call)
    call.package_id = pkg.id
    return call
