# Stage 4 live brownfield lab

- started: 2026-10-24T00:00:45+00:00
- finished: 2026-10-24T00:06:39+00:00
- devices: s4l-r1, s4l-s1, s4l-pc1, s4l-pc2
- import: {'devices': 4, 'links': 3, 'config_status': {'not_read': 2, 'known': 2}, 'incomplete': 2}
- clean flow: reachable (expected reachable)
- injected fault: wrong access VLAN on s4l-s1:FastEthernet1/1
- faulted flow: blocked (expected blocked, observed as blocked)
- diagnosed: wrong-access-vlan on s4l-s1:FastEthernet1/1 (high)
- repair: repaired
- backup available: True
- flow after repair: reachable

Evidence: `probes/raw/stage4-import.json`, `probes/raw/stage4-diagnosis.{json,md}`, `probes/raw/stage4-repair.json`, `probes/raw/stage4-live.json`.
