# Stage 4 enhancement benchmark

_mode: simulated_

Networks enhanced **6** · previously passing criteria preserved: **True**

| library | added | executed | preserved | operations | result |
| --- | --- | --- | --- | --- | --- |
| T01 | pc-enh1 on s1:FastEthernet0/7 | True | True | 6 | enhanced |
| T02 | pc-enh2 on s1:FastEthernet0/5 | True | True | 6 | enhanced |
| T05 | pc-enh3 on s1:FastEthernet0/3 | True | True | 6 | enhanced |
| T10 | pc-enh4 on s1:FastEthernet0/3 | True | True | 6 | enhanced |
| T11 | pc-enh5 on s1:FastEthernet0/4 | False | False | 6 | plan blocked by conflicts |
| T13 | pc-enh6 on s1:FastEthernet0/5 | True | True | 6 | enhanced |
| T16 | pc-enh7 on swcore:FastEthernet0/4 | True | True | 7 | enhanced |

The added host, cable and assignment are real new resources on an existing subnet; unchanged devices are not re-pushed (the operation count is the changed DAG subset).
