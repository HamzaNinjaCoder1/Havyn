# ASA probes (S3.C1–C6)

## The read path

The first probes could push to the ASA but could not read back: `enable` asks
for a password the console reader did not have. The IpcAPI exposes:

```js
var r = device.enterCommand("show running-config", "enable");
// r.first = CommandStatus (0 = OK, 2 = invalid, 3 = incomplete)
// r.second = the command's output
```

`probes/stage3_asa_readback.py` uses it to read `show running-config`,
`show interface ip brief`, `show route`, `show nat`, `show dhcpd`,
`show object-group` and `show access-list`. This is the post-push read-back
the earlier note said was impossible.

## Verdict

| Track | Feature | Status | Evidence |
| --- | --- | --- | --- |
| S3.C1 | Address-scoped ACL | **Verified** | `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`. |
| S3.C2 | DMZ third zone | **Verified** | `raw/S3.asa-lab.txt` — outside→DMZ reachable; outside→inside blocked. |
| S3.C3 | ASA object NAT/PAT | **Refused** | `raw/S3.asa-lab.txt` — object NAT, twice NAT and classic `nat/global` all return status 2; nothing is stored. |
| S3.C4 | ASA static/default routes | **Verified** | same file — inside→remote network reachable through the static route. |
| S3.C5 | ASA DHCP server | **Verified** | same file — inside PC leased 10.99.0.100 from the ASA. |
| S3.C6 | Object groups | **Refused** | same file — `object-group network` and a group-referencing ACL return status 2. |

The interface/nameif/security-level/IP, static routes and `dhcpd` lines are
stored and read back exactly as pushed. The ACL is enforced (DMZ permit +
outside-to-inside deny). NAT and object groups are the PT image's limits, not
the engine's.

## Reproduction

```bash
python probes/stage3_asa_readback.py   # config + show read-back
python probes/stage3_asa_lab.py        # DMZ/routes/DHCP flows and C3/C6 statuses
```
