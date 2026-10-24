"""Live Stage 4 import/read-back proof (skips when Packet Tracer is closed).

This is the live half of S4.1/S4.2: it creates a real router and PC, pushes the
smallest real configuration, and reads both back through the adapter the
engine uses — the running configuration from the IOS console and the host's
own `ipconfig /all`. It then imports the pair and asserts the inventory kept
the observed values.

Run with ``python -m pytest tests/autopilot/test_s4_live_brownfield.py -q
--require-live`` when Packet Tracer is open.
"""

from __future__ import annotations

import sys
import time
import uuid

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from network_autopilot.brownfield.importer import BrownfieldImporter

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable on either channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)

LAN = "10.222.0.0/24"
ROUTER_IP = "10.222.0.1"
HOST_IP = "10.222.0.10"


def test_live_running_config_and_host_ipconfig_are_imported() -> None:
    adapter = live.make_adapter()
    tag = f"q{uuid.uuid4().hex[:6]}"
    router = f"{tag}-s4r1"
    host = f"{tag}-s4pc1"
    names = [router, host]
    try:
        created_router = adapter.create_device(router, "router", position=(300, 200))
        assert created_router.result.value == "success", created_router.error
        created_host = adapter.create_device(host, "pc", position=(500, 200))
        assert created_host.result.value == "success", created_host.error
        linked = adapter.create_link(
            router, "FastEthernet0/0", host, "FastEthernet0"
        )
        assert linked.result.value == "success", linked.error
        pushed = adapter.configure_ios(
            router,
            [
                "interface FastEthernet0/0",
                f"ip address {ROUTER_IP} 255.255.255.0",
                "no shutdown",
                "exit",
            ],
        )
        assert pushed.result.value == "success", pushed.error
        configured = adapter.configure_host(
            host,
            dhcp=False,
            ip=HOST_IP,
            mask="255.255.255.0",
            gateway=ROUTER_IP,
            dns="8.8.8.8",
        )
        assert configured.result.value == "success", configured.error
        time.sleep(6)

        config = adapter.read_running_config(router)
        assert config.is_known, config.detail
        assert "ip address 10.222.0.1 255.255.255.0" in str(config.value)
        assert "interface FastEthernet0/0" in str(config.value)

        host_config = adapter.read_host_ip_config(host)
        assert host_config.is_known, host_config.detail
        assert host_config.value["ip"] == HOST_IP
        assert host_config.value["gateway"] == ROUTER_IP
        assert "8.8.8.8" in (host_config.value.get("dns") or [])

        imported = BrownfieldImporter(
            adapter, options=__import__(
                "network_autopilot.brownfield.importer", fromlist=["ImportOptions"]
            ).ImportOptions(read_positions=False)
        ).import_network(device_names=names)
        router_inv = imported.inventory.device(router)
        host_inv = imported.inventory.device(host)
        assert router_inv is not None and router_inv.config is not None
        assert router_inv.config.status.value == "known"
        assert host_inv is not None and host_inv.host is not None
        assert host_inv.host.gateway.value == ROUTER_IP
        assert imported.inventory.links
    finally:
        live.delete_devices(names)
