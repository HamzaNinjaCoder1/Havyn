"""Independence proof: the autopilot must not need the upstream package.

The cleanest way to test an absence is to make the thing absent. This conftest
installs a meta-path finder that raises ImportError for anything under
``packet_tracer_mcp`` and ``src.packet_tracer_mcp``, so any import of it
fails loudly instead of silently working because it happens to be installed.

Selected with ``-p no:cacheprovider`` style invocation::

    python -m pytest tests/autopilot -p tests.independence_blocker
"""

import sys


class _BlockUpstream:
    """Refuse to import the upstream package for the whole test session."""

    BLOCKED = ("packet_tracer_mcp", "src.packet_tracer_mcp")

    def find_module(self, fullname, path=None):  # legacy API, still honoured
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if fullname in self.BLOCKED or root in self.BLOCKED:
            raise ImportError(
                f"independence check: {fullname!r} must not be imported — "
                f"network_autopilot owns this functionality now"
            )
        return None


def pytest_configure(config):
    for mod in list(sys.modules):
        if mod.split(".")[0] in _BlockUpstream.BLOCKED:
            del sys.modules[mod]
    sys.meta_path.insert(0, _BlockUpstream())
