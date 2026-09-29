"""Compile a NetworkSpec ``PolicySpec`` into Packet Tracer ASA CLI.

What PT's ASA can actually enforce was measured live, not assumed. The
findings that shape this module:

* With **no** access-group every flow is blocked, in both directions. PT's ASA
  does not implement the real ASA's implicit "inside (security-level 100) may
  reach outside (security-level 0)" rule.
* ``access-list X extended permit <proto> any any`` + ``access-group X in
  interface outside`` passes that protocol **both ways**.
* Enforcement is **protocol-selective**: with only ``permit tcp any any``
  applied, ICMP is blocked and TCP passes. This is what makes a deliberately
  blocked flow expressible.
* An access-group on the **inside** nameif passes nothing.
* **Address matching is not implemented.** ``permit icmp host A host B`` and
  ``permit icmp 192.168.10.0 255.255.255.0 192.168.20.0 255.255.255.0`` are
  both accepted by PT and both leave every flow blocked. So a host- or
  subnet-scoped policy cannot be enforced on an ASA, and this module refuses
  to compile one for an ASA rather than emit config that silently does
  nothing.

**IOS routers are different** (fresh live probe, issue #16): ``access-list 100
permit icmp any any`` + ``ip access-group 100 in`` on an interface passes that
protocol, and unlike the ASA, address matching works — a ``deny icmp host A
host B`` line blocks exactly that pair while other flows keep passing. Router
policies therefore render as numbered IOS ACLs bound ``in`` on every
addressed interface of the target (see :func:`build_router_policy_commands`),
and address-scoped rules are allowed for routers.

The last point about ASAs is the whole reason this module raises instead of
degrading: a green build that does not enforce the policy the spec asked for
is worse than a refusal, because the failure only shows up as unexplained
traffic loss later.
"""

from __future__ import annotations

from typing import Callable, Iterable

from ..core.network_spec import AclRuleSpec, NetworkSpec, PolicySpec

# The only nameif PT honours for an inbound access-group.
ENFORCING_NAMEIF = "outside"

#: Addressing PT's ASA will not match on. Only these pass traffic.
WILDCARD_ADDRESSES = frozenset({"any", "0.0.0.0 0.0.0.0"})

#: Protocols PT's ASA will match on, mapped to the CLI keyword.
PROTOCOL_KEYWORDS = {
    "ip": "ip",
    "icmp": "icmp",
    "tcp": "tcp",
    "udp": "udp",
}


class PolicyCompileError(ValueError):
    """The requested policy cannot be enforced by Packet Tracer's ASA."""


def _is_wildcard(address: str) -> bool:
    return address.strip() in WILDCARD_ADDRESSES


def _check_rule_is_enforceable(policy_id: str, index: int, rule: AclRuleSpec) -> None:
    """Refuse a rule PT would accept but not enforce.

    PT takes the line, reports success and then matches nothing, so the policy
    would look applied while every flow stayed blocked. Saying so at compile
    time is the only honest outcome.
    """
    where = f"policy {policy_id!r} rule {index + 1}"
    if not _is_wildcard(rule.source):
        raise PolicyCompileError(
            f"{where}: source {rule.source!r} is not 'any'. Packet Tracer's ASA "
            "accepts address-scoped permit/deny lines but does not match on "
            "them - the flow would be blocked no matter what the rule says. "
            "Use source='any' and separate the zones with subnets, or enforce "
            "the restriction at the host/router."
        )
    if not _is_wildcard(rule.destination):
        raise PolicyCompileError(
            f"{where}: destination {rule.destination!r} is not 'any'. Packet "
            "Tracer's ASA accepts address-scoped permit/deny lines but does not "
            "match on them - the flow would be blocked no matter what the rule "
            "says. Use destination='any' and separate the zones with subnets."
        )
    if rule.dest_port is not None and rule.protocol == "icmp":
        raise PolicyCompileError(
            f"{where}: dest_port does not apply to icmp. PT rejects the line and "
            "configureIosDevice aborts the rest of the block, which would cost "
            "the interface addresses above it."
        )
    if rule.protocol not in PROTOCOL_KEYWORDS:
        raise PolicyCompileError(
            f"{where}: protocol {rule.protocol!r} is not one of "
            f"{sorted(PROTOCOL_KEYWORDS)}."
        )


def rule_to_cli(policy_id: str, index: int, rule: AclRuleSpec) -> str:
    """One ``access-list`` line, in the exact shape proven against live PT."""
    _check_rule_is_enforceable(policy_id, index, rule)
    protocol = PROTOCOL_KEYWORDS[rule.protocol]
    port = f" eq {rule.dest_port}" if rule.dest_port is not None else ""
    return (
        f"access-list {policy_id} extended {rule.action} "
        f"{protocol} any any{port}"
    )


def _inverse_mask(netmask: str) -> str:
    """Dotted netmask -> dotted wildcard (the IOS ACL operand form).

    Callers pass masks produced by :mod:`ipaddress`, so contiguity is already
    guaranteed; this only guards against a malformed dotted quad.
    """
    octets = [int(o) for o in netmask.split(".")]
    if len(octets) != 4 or any(not 0 <= o <= 255 for o in octets):
        raise PolicyCompileError(f"netmask {netmask!r} is not a valid mask")
    return ".".join(str(255 - o) for o in octets)


def _ios_address(value: str) -> str:
    """One rule operand as the IOS numbered-ACL form.

    Shapes accepted (the first two are live-probed on an IOS router, issue
    #16; CIDR and netmask forms reuse the conversion already proven for
    OSPF/static routes): ``any``, a bare IP -> ``host <ip>``, ``host <ip>``
    verbatim, ``<net>/<prefix>``, ``<net> <dotted-netmask>`` (converted to
    the wildcard pair IOS wants) and an already-wildcard pair. Anything else
    is refused instead of guessed at - PT would accept a malformed operand
    and match nothing.
    """
    import ipaddress

    text = value.strip()
    if text in WILDCARD_ADDRESSES:
        return "any"
    if text.startswith("host "):
        return text
    parts = text.split()
    if len(parts) == 2:
        try:
            network = ipaddress.ip_network(f"{parts[0]}/{parts[1]}", strict=False)
        except ValueError:
            # Not a netmask pair: either an already-wildcard pair such as
            # `10.0.0.0 0.0.0.255` or junk. A real netmask always starts with
            # 255 while a wildcard virtually always starts with 0, so a
            # second half with a leading 255 that does not parse is a bogus
            # netmask (S1.16 probe S4) - refuse it by name instead of
            # emitting a wildcard PT accepts and never matches. Otherwise
            # verify both halves are addresses before passing a wildcard
            # pair through.
            if parts[1].split(".")[0] == "255":
                raise PolicyCompileError(
                    f"address {value!r} is not a valid netmask pair: "
                    f"{parts[1]!r} is no valid mask"
                ) from None
            try:
                ipaddress.ip_address(parts[0])
                ipaddress.ip_address(parts[1])
            except ValueError:
                raise PolicyCompileError(
                    f"address {value!r} is neither 'any', a host, a CIDR "
                    "network, a netmask pair nor a wildcard pair"
                ) from None
            return text
        if network.prefixlen == 32:
            # `host X` written as `X 255.255.255.255` (S1.16 probe): the
            # canonical host form, not a redundant `X 0.0.0.0` pair.
            return f"host {network.network_address}"
        return f"{network.network_address} {_inverse_mask(str(network.netmask))}"
    if "/" in text:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise PolicyCompileError(f"address {value!r} is not a valid CIDR network") from None
        return f"{network.network_address} {_inverse_mask(str(network.netmask))}"
    try:
        ipaddress.ip_address(text)
    except ValueError:
        raise PolicyCompileError(
            f"address {value!r} is neither 'any', a host nor a network"
        ) from None
    return f"host {text}"


def rule_to_ios_cli(acl_number: str, index: int, rule: AclRuleSpec) -> str:
    """One numbered IOS ACL line (``access-list 100 permit ...``).

    Unlike :func:`rule_to_cli` (ASA), addresses are rendered rather than
    refused: PT's IOS router matches them live (issue #16).
    """
    where = f"policy rule {index + 1}"
    if rule.dest_port is not None and rule.protocol == "icmp":
        raise PolicyCompileError(f"{where}: dest_port does not apply to icmp.")
    if rule.protocol not in PROTOCOL_KEYWORDS:
        raise PolicyCompileError(
            f"{where}: protocol {rule.protocol!r} is not one of "
            f"{sorted(PROTOCOL_KEYWORDS)}."
        )
    protocol = PROTOCOL_KEYWORDS[rule.protocol]
    port = f" eq {rule.dest_port}" if rule.dest_port is not None else ""
    return (
        f"access-list {acl_number} {rule.action} {protocol} "
        f"{_ios_address(rule.source)} {_ios_address(rule.destination)}{port}"
    )


def build_router_policy_commands(
    spec: NetworkSpec, device_id: str, interfaces: list[str]
) -> tuple[list[str], list[str]]:
    """Numbered IOS ACLs plus ``ip access-group N in`` on each interface.

    ``interfaces`` are the router's addressed interfaces (the compiler's own
    assignment loop): ``ip access-group`` is an interface-level command, so a
    policy that names a router with no addressed interface cannot bind
    anywhere - that is refused here instead of emitted as a no-op.

    The binding direction is ``in`` on every such interface: live probes
    blocked ``host A -> host B`` while other flows passed, on the interface
    where the source traffic enters the router (issue #16).
    """
    policies = policies_for_device(spec, device_id)
    if not policies:
        return [], []
    if not interfaces:
        raise PolicyCompileError(
            f"policy on router {device_id!r}: the router has no addressed "
            "interface to bind the access-group to; give the device an "
            "ip_assignment."
        )
    commands: list[str] = []
    acl_names: list[str] = []
    for offset, policy in enumerate(policies):
        # Numbered ACLs are per-device in PT, so each policy on this device
        # gets the next extended number (100-199). The policy id stays the
        # spec's name; the number is only the CLI handle.
        number = str(100 + offset)
        for index, rule in enumerate(policy.rules):
            commands.append(rule_to_ios_cli(number, index, rule))
        for iface in interfaces:
            commands += [f"interface {iface}", f"ip access-group {number} in", "exit"]
        acl_names.append(number)
    return commands, acl_names


def policies_for_device(spec: NetworkSpec, device_id: str) -> list[PolicySpec]:
    """The policies that target this device, in spec order.

    ``apply_to`` names device ids (the spec validator rejects anything else), so
    a policy is bound to the box the spec author drew. Note what this does NOT
    do: it does not check that the target is a firewall. That check lives in
    :func:`assert_policies_target_firewalls`, because a policy aimed at a
    router would otherwise be accepted and then silently never applied.
    """
    return [
        policy
        for policy in spec.policies
        if policy.enforcement == "available" and device_id in
        {t.strip() for t in policy.apply_to}
    ]


#: Categories PT's pushed access-lists were measured to enforce on. ASA
#: firewalls bind a named ACL to a nameif; IOS routers bind a numbered ACL
#: ``in`` on an interface (live probes, issue #16).
ENFORCING_CATEGORIES = ("firewall", "router")


def assert_policies_target_firewalls(
    spec: NetworkSpec, model_for: "Callable[[str], object | None]"
) -> None:
    """Every enforced policy must land on a device that can enforce it.

    Live probes cover two categories: ASA firewalls (named ACL + nameif) and
    IOS routers (numbered ACL + ``ip access-group ... in``, address matching
    included - issue #16). A policy aimed at anything else would compile
    cleanly and then have no effect, which is the same silent-no-op failure
    as an address-scoped rule on an ASA. ``model_for`` resolves a device id
    to its catalog DeviceModel (or None).
    """
    for policy in spec.policies:
        if policy.enforcement != "available":
            continue
        for target in policy.apply_to:
            model = model_for(target.strip())
            if model is None or getattr(model, "category", None) not in ENFORCING_CATEGORIES:
                raise PolicyCompileError(
                    f"policy {policy.id!r} targets {target!r}, which is not an "
                    "ASA firewall or an IOS router. This build verifies "
                    "access-list enforcement only on those two (ASA models "
                    "5505, 5506-X and IOS routers); on any other device the "
                    "policy would compile and then do nothing."
                )


def build_policy_commands(
    spec: NetworkSpec, device_id: str
) -> tuple[list[str], list[str]]:
    """Return ``(commands, acl_names)`` for the policies targeting ``device_id``.

    Commands are ordered so the access-list exists before it is referenced. An
    empty list means the spec asked for nothing enforceable on this device and
    the caller should fall back to its own default.
    """
    policies = policies_for_device(spec, device_id)
    if not policies:
        return [], []

    commands: list[str] = []
    acl_names: list[str] = []
    for policy in policies:
        lines = [
            rule_to_cli(policy.id, index, rule)
            for index, rule in enumerate(policy.rules)
        ]
        # configIosDevice aborts at the first invalid line, so the definition
        # and the binding are emitted as one uninterrupted run and the binding
        # comes last: a partially applied ACL would silently change the
        # firewall's behaviour.
        commands.extend(lines)
        commands.append(
            f"access-group {policy.id} in interface {ENFORCING_NAMEIF}"
        )
        acl_names.append(policy.id)
    return commands, acl_names


def describe_enforcement(
    spec: NetworkSpec,
    model_for: "Callable[[str], object | None] | None" = None,
) -> Iterable[str]:
    """Human-readable notes about what a spec's policies can and cannot do.

    ``model_for`` resolves ``apply_to`` ids to catalog models. When it is
    given and every target is an IOS router, the ASA address-scoping check
    is skipped for that policy: router ACLs match addresses live (issue #16),
    so an address-scoped rule on a router is not a limitation to report.
    """
    for policy in spec.policies:
        if policy.enforcement != "available":
            yield (
                f"policy {policy.id!r} is marked planned: it is carried in the "
                "spec but not enforced, so verification reports it UNKNOWN"
            )
            continue
        if model_for is not None:
            targets = [model_for(t.strip()) for t in policy.apply_to]
            if targets and all(
                m is not None and getattr(m, "category", None) == "router"
                for m in targets
            ):
                continue
        try:
            for index, rule in enumerate(policy.rules):
                _check_rule_is_enforceable(policy.id, index, rule)
        except PolicyCompileError as exc:
            yield f"policy {policy.id!r} cannot be enforced: {exc}"
