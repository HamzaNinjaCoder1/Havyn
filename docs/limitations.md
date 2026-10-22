# Known limitations and refusals

Havyn refuses what Packet Tracer cannot deliver instead of producing a build
that only looks complete.

## IoT registration server

Packet Tracer has no IoT-server device type. IoT things are therefore only
cabled and addressed; there is no registration-server capability to configure
and none is invented. This is pinned by
`test_pt_has_no_iot_server_type_so_cabling_is_the_full_capability`.

## The rule behind every refusal

Anything PT cannot do must be refused at compile, **never half-configured**.
A refusal names the unsupported capability before any mutation; it is a
feature, not a gap.
