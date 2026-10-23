# S3.A2 OSPF multi-area — live refusal

The configuration is accepted, the ABR forms full adjacencies in both areas,
but Packet Tracer does **not** inject area-0 prefixes into area 1. The required
cross-area flow therefore never becomes reachable.

## Lab

* r1 (area 0) — r2 (ABR) — r3 (area 1)
* r2's 10.0.1.0/24 LAN is connected in area 0; r3's 10.0.3.0/24 LAN is in
  area 1; both routers report `FULL` OSPF neighbors.
* Flow `pc1 (10.0.1.x) -> pc3 (10.0.3.x)` observed **blocked**.

## Verbatim route tables after convergence

`r1` (area 0) learned the area-1 LAN:

```
O IA    10.0.3.0/24 [110/3] via 10.0.12.2, 00:03:32, GigabitEthernet0/0
```

`r2` (ABR) has the area-0 LAN directly connected and the area-1 LAN via OSPF:

```
C       10.0.1.0/24 is directly connected, GigabitEthernet0/2
O       10.0.3.0/24 [110/2] via 10.0.23.2, 00:03:40, GigabitEthernet0/1
```

`r3` (area 1) has **no** route to 10.0.1.0/24:

```
     10.0.0.0/8 is variably subnetted, 4 subnets, 3 masks
C       10.0.3.0/24 is directly connected, GigabitEthernet0/1
L       10.0.3.1/32 is directly connected, GigabitEthernet0/1
C       10.0.23.0/30 is directly connected, GigabitEthernet0/0
L       10.0.23.2/32 is directly connected, GigabitEthernet0/0
```

`show ip ospf neighbor` on r2 proves it is a real ABR with both adjacencies:

```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.0.12.1         1   FULL/BDR        00:00:36    10.0.12.1       GigabitEthernet0/0
10.0.23.2         1   FULL/DR         00:00:31    10.0.23.2       GigabitEthernet0/1
```

## Verdict

**Refused.** The limitation is Packet Tracer's OSPF ABR, not Havyn: the push
and read-back are correct, the adjacencies are full, and PT still does not
advertise area-0 prefixes into area 1 (the reverse direction works). A second
topology (r2 carrying the area-0 LAN) reproduced it.

## Practical alternative

* Use single-area OSPF (S3.A1, Verified) for the same reachability objective.
* Or use static routes between the areas, which the compiler already supports.
* Do not mark multi-area OSPF Verified until PT's ABR is shown to advertise
  both directions.

## Evidence files

* `probes/live/S3-A2-ospf-multi-area.json` — full lab record.
* `probes/raw/` — router probe captures (multi-area config accepted).
* This document — route tables and neighbor states verbatim.
