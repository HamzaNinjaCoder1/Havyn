"""S1.8 / F5: reconcile-on-reuse.

A device that already exists in the workspace is reused instead of recreated.
If it carries config the spec no longer asks for, that stale config used to
survive the build: the normal configure push only sets what the spec wants and
never removes the extra, so the device never equals the spec.
"""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence import EvidenceStore
from src.network_autopilot.ipam import plan_addressing
from tests.autopilot.test_compiler import GOLDEN_SPEC


def _dag():
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    plan = plan_addressing(spec)
    return compile_spec(spec, plan).dag


def _executor(adapter, evidence=None, mode=SafetyMode.AUTONOMOUS):
    return DagExecutor(
        adapter,
        SafetyGate(mode, MutationBudget(max_mutations=500)),
        evidence,
    )


def _observed_ip(adapter, device: str, iface: str) -> str | None:
    obs = adapter.inspect_device(device)
    port = obs.ports.get(iface)
    if port is None or not port.ip.is_known:
        return None
    return str(port.ip.value)


def _all_observed_ips(adapter, device: str) -> set[str]:
    obs = adapter.inspect_device(device)
    return {
        str(p.ip.value)
        for p in obs.ports.values()
        if p.ip.is_known and p.ip.value
    }


def test_reconcile_on_reuse_removes_stale_interface_config(tmp_path):
    """RED before the fix: a stale extra address on a reused device survives
    the build, so the device does not equal the spec."""
    adapter = NullAdapter()
    _executor(adapter).execute(_dag())

    # Out-of-band drift: an address the spec never assigns lands on r1's
    # third interface (an operator's leftover, or a crashed earlier build).
    adapter._devices["r1"]["ports"]["GigabitEthernet0/2"]["config"]["ip"] = "192.168.99.1"
    adapter._devices["r1"]["ports"]["GigabitEthernet0/2"]["config"]["mask"] = "255.255.255.0"

    evidence = EvidenceStore(tmp_path)
    result = _executor(adapter, evidence).execute(_dag())

    assert not result.failed, result.failed
    assert "op-create-r1" in result.reused
    stale = _all_observed_ips(adapter, "r1") & {"192.168.99.1"}
    assert not stale, f"stale address survived reuse: {stale}"
    # the spec's own addresses are still exactly right
    assert _observed_ip(adapter, "r1", "GigabitEthernet0/0") == "10.0.0.1"
    assert _observed_ip(adapter, "r1", "GigabitEthernet0/1") == "10.0.1.1"
    kinds = {r.kind for r in evidence.read_all()}
    assert "device_reconciliation" in kinds


def test_reconcile_leaves_a_matching_device_untouched(tmp_path):
    """Pin: reuse with NO drift must not send a removal command."""
    adapter = NullAdapter()
    _executor(adapter).execute(_dag())

    counting = _Counting()
    counting._devices = adapter._devices

    evidence = EvidenceStore(tmp_path)
    result = _executor(counting, evidence).execute(_dag())
    assert not result.failed, result.failed
    removals = [
        (dev, cmd)
        for dev, cmds in counting.configured
        for cmd in cmds
        if cmd.strip().lower().startswith("no ip address")
    ]
    assert removals == [], removals


class _Counting(NullAdapter):
    def __init__(self):
        super().__init__()
        self.configured: list[tuple[str, tuple[str, ...]]] = []

    def configure_ios(self, device, commands, operation_id=None):
        self.configured.append((device, tuple(commands)))
        return super().configure_ios(device, commands, operation_id=operation_id)


def test_reconcile_does_not_flap_once_clean(tmp_path):
    """Pin: after the drift is removed once, a further reuse sends no removal
    command again - reconcile is convergent, not a command treadmill."""
    adapter = NullAdapter()
    _executor(adapter).execute(_dag())
    adapter._devices["r1"]["ports"]["GigabitEthernet0/2"]["config"]["ip"] = "192.168.99.1"

    counting = _Counting()
    counting._devices = adapter._devices
    evidence = EvidenceStore(tmp_path)
    first = _executor(counting, evidence).execute(_dag())
    assert not first.failed, first.failed
    first_removals = [
        cmd
        for _dev, cmds in counting.configured
        for cmd in cmds
        if cmd.strip().lower().startswith("no ip address")
    ]
    assert first_removals, "the drift should have been removed on the first reuse"

    counting.configured.clear()
    second = _executor(counting).execute(_dag())
    assert not second.failed, second.failed
    second_removals = [
        cmd
        for _dev, cmds in counting.configured
        for cmd in cmds
        if cmd.strip().lower().startswith("no ip address")
    ]
    assert second_removals == [], second_removals