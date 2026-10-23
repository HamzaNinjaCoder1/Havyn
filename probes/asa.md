# Stage 3 ASA probes (S3.C1–C6)

The ASA is addressed over the same `configureIosDevice` path as IOS, but its
console is a different object: `getCommandLine()` is null and
`getConsoleLine()` is the only door. The ASA also boots for ~40–60 s before it
accepts configuration, and `enable` in the PT ASA image asks for a password
that the engine does not have.

| Track | Feature | Live observation | Status |
| --- | --- | --- | --- |
| S3.C1 | Address-scoped ACL | Already proven by the S1.16 live policy tests (`test_asa_address_scoped_acl_is_enforced`); PT matches the netmask form. | **Verified** |
| S3.C2 | DMZ third zone | `interface GigabitEthernet1/3`, `nameif dmz`, `security-level 50` push returned success. No post-push read-back was possible: `show running-config` never returned to a prompt and the generic console reader cannot anchor on the ASA prompt. | **Unverified** |
| S3.C3 | ASA object NAT | `object network`, `subnet`, `nat (inside,outside) dynamic interface` push returned success; `show nat` could not be read at user EXEC (`% Invalid input`). | **Unverified** |
| S3.C4 | ASA static/default route | `route outside 0.0.0.0 0.0.0.0 ... 1` push returned success; `show route` was not observable in the same session. | **Unverified** |
| S3.C5 | ASA DHCP server | `dhcpd address ...` and `dhcpd enable inside` push returned success; `show dhcpd` rejected at user EXEC. | **Unverified** |
| S3.C6 | Object groups | `object-group network`, `network-object host`, ACL referencing the group push returned success; `show object-group`/`show access-list` not observable at user EXEC. | **Unverified** |

## Why these are not Verified

Packet Tracer does not expose an ASA enable credential to the Script Engine,
and the ASA image rejects `enable` with the password prompt
(`probes/raw/asa-shows.txt`). A push receipt only proves the script was
accepted (AGENTS rule 8), not that the ASA applied it. Marking C2–C6 Verified
would be exactly the "configured means verified" lie the project forbids.

## Practical alternative

* Keep the ASA policy work on the existing, live-proven address-scoped ACL path
  (C1).
* For DMZ/NAT/DHCP/object-group intent, the compiler emits the commands; a
  human can confirm them in the ASA GUI. The engine reports UNKNOWN until a
  credentialed read path exists.
* A 5506-X with two zones remains the safe, verified ASA shape.

## Reproduction

```bash
python probes/stage3_asa_read.py     # boot wait + user-mode reads
python probes/stage3_asa_shows.py    # enable attempt + show commands
```
