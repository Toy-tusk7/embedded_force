# EmbedForge runs

Every agent run EmbedForge made during the hackathon, generated from each project's `trace.jsonl` with
`python -m embedforge.runlog --runs` (the traces themselves stay local: they hold the full conversations). Each line:
start time, provider, model, how the run ended, steps, whether the last build succeeded and verify passed, prompt +
completion tokens, and the project.

110 runs: 69 on **Nebius Token Factory** (`nebius`), from 8 October until our Token Factory credits ran out on
9 October, and 41 afterwards with the same Nemotron models through NVIDIA's API (`nvidia`). With a
`NEBIUS_API_KEY` in `.env`, Token Factory is used again: it comes first in the provider order.

## Summary

| Provider | Model | Runs | Ended built + verified | Steps (median per run) | Prompt + completion tokens | Model time per step | Dates |
|---|---|---|---|---|---|---|---|
| nebius | nvidia/Nemotron-3-Ultra-550b-a55b | 47 | 3 | 1611 (36) | 59.7M + 735k | 5 s | 2026-10-08 to 2026-10-08 |
| nvidia | nvidia/nemotron-3-ultra-550b-a55b | 23 | 13 | 544 (12) | 22.3M + 236k | 19 s | 2026-10-16 to 2026-10-20 |
| nvidia | nvidia/nemotron-3-super-120b-a12b | 18 | 3 | 758 (38.5) | 42.8M + 923k | 93 s | 2026-10-17 to 2026-10-19 |
| nebius | nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B | 8 | 1 | 150 (7.5) | 1.8M + 230k | 12 s | 2026-10-08 to 2026-10-09 |
| nebius | nvidia/nemotron-3-super-120b-a12b | 8 | 1 | 412 (60) | 8.8M + 294k | 6 s | 2026-10-08 to 2026-10-08 |
| nebius | nvidia/Nemotron-3_5-Lightning | 6 | 2 | 313 (58) | 8.5M + 124k | 4 s | 2026-10-08 to 2026-10-08 |

## Every run

```
2026-10-08 01:57  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      interrupted  steps=48 built=None verified=None tokens=542009+80274  20261008-015746-ev0
2026-10-08 01:57  nebius     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=True verified=False tokens=950188+73370  20261008-015746-ev1
2026-10-08 01:57  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1152526+47681  20261008-015746-ev2
2026-10-08 01:57  nebius     nvidia/Nemotron-3_5-Lightning              completed    steps=55 built=True verified=True tokens=1055640+20467  20261008-015746-ev3
2026-10-08 02:13  nebius     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=False verified=False tokens=800532+52945  20261008-021315-ev1
2026-10-08 02:13  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=True tokens=1222825+26434  20261008-021315-ev2
2026-10-08 02:13  nebius     nvidia/Nemotron-3_5-Lightning              needs_input  steps=60 built=True verified=False tokens=1540695+21690  20261008-021315-ev3
2026-10-08 02:13  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      needs_input  steps=60 built=False verified=False tokens=729088+91763  20261008-021315-ev0
2026-10-08 02:18  nebius     nvidia/Nemotron-3_5-Lightning              needs_input  steps=60 built=True verified=False tokens=2115974+12979  20261008-021315-ev3
2026-10-08 02:18  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          completed    steps=1 built=True verified=True tokens=22368+520  20261008-021315-ev2
2026-10-08 02:21  nebius     nvidia/nemotron-3-super-120b-a12b          failed       steps=40 built=False verified=False tokens=807726+23893  20261008-021315-ev1
2026-10-08 02:24  nebius     nvidia/Nemotron-3_5-Lightning              completed    steps=22 built=True verified=True tokens=929194+7979  20261008-021315-ev3
2026-10-08 02:25  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      interrupted  steps=21 built=None verified=None tokens=347900+33072  20261008-021315-ev0
2026-10-08 02:28  nebius     nvidia/Nemotron-3_5-Lightning              needs_input  steps=60 built=True verified=False tokens=999308+34282  20261008-022807-ev2
2026-10-08 02:28  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=False tokens=1688533+35582  20261007-221720-5e6b
2026-10-08 02:28  nebius     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=True verified=False tokens=975968+49499  20261008-022807-ev1
2026-10-08 02:28  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      needs_input  steps=1 built=False verified=False tokens=6584+1353  20261008-022807-ev0
2026-10-08 02:33  nebius     nvidia/Nemotron-3_5-Lightning              failed       steps=56 built=False verified=True tokens=1903408+26828  20261008-022807-ev2
2026-10-08 02:33  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          completed    steps=17 built=True verified=True tokens=533037+4824  20261007-221720-5e6b
2026-10-08 02:37  nebius     nvidia/nemotron-3-super-120b-a12b          completed    steps=36 built=True verified=True tokens=842928+14520  20261008-022807-ev1
2026-10-08 17:10  nebius     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=True verified=False tokens=1407971+37766  20261008-171025-bmp2
2026-10-08 17:10  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=False tokens=1851803+20537  20261008-171025-bmp0
2026-10-08 17:10  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1079780+7655  20261008-171025-bmp1
2026-10-08 17:16  nebius     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=True verified=False tokens=1682302+29453  20261008-171025-bmp2
2026-10-08 17:18  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1519459+10700  20261008-171025-bmp1
2026-10-08 17:18  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=False tokens=2252604+19518  20261008-171025-bmp0
2026-10-08 17:23  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=8 built=False verified=False tokens=248194+2712  20261008-171025-bmp1
2026-10-08 17:24  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=False tokens=2948968+20856  20261008-171025-bmp0
2026-10-08 17:24  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=58 built=False verified=False tokens=1708053+19546  20261008-171025-bmp1
2026-10-08 17:28  nebius     nvidia/nemotron-3-super-120b-a12b          failed       steps=36 built=False verified=False tokens=1282884+12984  20261008-171025-bmp2
2026-10-08 18:32  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=2142220+36196  20261008-183148-bmp0
2026-10-08 18:32  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1634660+36875  20261008-183148-bmp1
2026-10-08 18:37  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=3005858+44302  20261008-183148-bmp1
2026-10-08 18:37  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=47 built=False verified=False tokens=2760514+11504  20261008-183148-bmp0
2026-10-08 18:42  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=32 built=False verified=False tokens=1715044+3825  20261008-183148-bmp0
2026-10-08 18:46  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=20 built=None verified=None tokens=1187614+6612  20261008-183148-bmp1
2026-10-08 18:49  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=7 built=None verified=None tokens=389214+2142  20261008-183148-bmp0
2026-10-08 19:18  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1604053+11307  20261008-191800-bmp0
2026-10-08 19:18  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=2244673+44417  20261008-191800-bmp1
2026-10-08 19:30  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=2593740+28237  20261008-191800-bmp0
2026-10-08 19:32  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=True verified=False tokens=2986771+30645  20261008-191800-bmp1
2026-10-08 19:44  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=9 built=None verified=None tokens=393834+2270  20261008-191800-bmp0
2026-10-08 19:46  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=3 built=None verified=None tokens=173413+1661  20261008-191800-bmp1
2026-10-08 19:47  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=22717+510  20261008-194715-bmp0
2026-10-08 19:47  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1805216+24755  20261008-194715-bmp1
2026-10-08 19:47  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=20 built=False verified=False tokens=467963+3506  20261008-194715-bmp0
2026-10-08 19:52  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=27 built=False verified=False tokens=755728+5376  20261008-194715-bmp0
2026-10-08 19:58  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=49253+2133  20261008-194715-bmp0
2026-10-08 19:58  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=51319+2643  20261008-194715-bmp0
2026-10-08 19:58  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=54107+245  20261008-194715-bmp0
2026-10-08 19:59  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=52629+2040  20261008-194715-bmp0
2026-10-08 20:00  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=25 built=None verified=None tokens=1244817+8965  20261008-194715-bmp1
2026-10-08 20:06  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=22717+8922  20261008-200637-bmp0
2026-10-08 20:06  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=22717+514  20261008-200637-bmp1
2026-10-08 20:07  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=2279559+38771  20261008-200637-bmp1
2026-10-08 20:07  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=36 built=None verified=None tokens=1675884+13740  20261008-200637-bmp0
2026-10-08 20:20  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=12 built=None verified=None tokens=641312+4092  20261008-200637-bmp1
2026-10-08 20:35  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      needs_input  steps=4 built=False verified=False tokens=38163+3901  20261008-202933-bmp0
2026-10-08 20:35  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      cancelled    steps=9 built=False verified=False tokens=113922+12613  20261008-202933-bmp1
2026-10-08 20:37  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      cancelled    steps=6 built=False verified=False tokens=54821+6996  20261008-202933-bmp0
2026-10-08 20:39  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=1965887+39186  20261008-203922-bmp0
2026-10-08 20:39  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=2 built=False verified=False tokens=22717+14449  20261008-203922-bmp1
2026-10-08 20:40  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=2624767+25583  20261008-203922-bmp1
2026-10-08 20:53  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=38 built=False verified=False tokens=1896616+24962  20261008-203922-bmp0
2026-10-08 20:54  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          needs_input  steps=60 built=False verified=False tokens=3075048+26944  20261008-203922-bmp1
2026-10-08 21:03  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=32 built=None verified=None tokens=1776224+8276  20261008-203922-bmp0
2026-10-08 21:10  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=3 built=None verified=None tokens=156734+2924  20261008-203922-bmp1
2026-10-08 21:20  nebius     nvidia/Nemotron-3-Ultra-550b-a55b          interrupted  steps=0 built=None verified=None tokens=0+0  20261008-203922-bmp1
2026-10-09 20:40  nebius     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B      failed       steps=1 built=True verified=True tokens=0+0  20261007-221720-5e6b
2026-10-16 00:24  nvidia     nvidia/nemotron-3-ultra-550b-a55b          failed       steps=21 built=False verified=False tokens=403788+2977  20261016-002347-cc5f
2026-10-16 00:33  nvidia     nvidia/nemotron-3-ultra-550b-a55b          failed       steps=1 built=False verified=False tokens=0+0  20261016-002347-cc5f
2026-10-16 00:59  nvidia     nvidia/nemotron-3-ultra-550b-a55b          cancelled    steps=53 built=False verified=False tokens=1512313+15844  20261016-002347-cc5f
2026-10-16 01:20  nvidia     nvidia/nemotron-3-ultra-550b-a55b          cancelled    steps=15 built=False verified=False tokens=566398+11671  20261016-002347-cc5f
2026-10-16 01:30  nvidia     nvidia/nemotron-3-ultra-550b-a55b          cancelled    steps=39 built=False verified=False tokens=1921706+16557  20261016-002347-cc5f
2026-10-16 01:53  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=65 built=True verified=True tokens=3460077+35907  20261016-002347-cc5f
2026-10-16 23:03  nvidia     nvidia/nemotron-3-ultra-550b-a55b          failed       steps=1 built=True verified=True tokens=0+0  20261016-002347-cc5f
2026-10-16 23:10  nvidia     nvidia/nemotron-3-ultra-550b-a55b          needs_input  steps=1 built=True verified=True tokens=0+0  20261016-002347-cc5f
2026-10-17 16:17  nvidia     nvidia/nemotron-3-ultra-550b-a55b          needs_input  steps=1 built=True verified=True tokens=0+0  20261016-002347-cc5f
2026-10-17 17:26  nvidia     nvidia/nemotron-3-super-120b-a12b          completed    steps=54 built=True verified=True tokens=2908122+34340  20261016-002347-cc5f
2026-10-17 20:43  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=64 built=None verified=None tokens=3472025+104978  20261016-002347-cc5f
2026-10-17 21:59  nvidia     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=28 built=True verified=False tokens=1558657+39856  20261016-002347-cc5f
2026-10-17 22:30  nvidia     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=90 built=True verified=False tokens=4999718+73908  20261016-002347-cc5f
2026-10-18 00:33  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=65 built=None verified=None tokens=3663607+77655  20261016-002347-cc5f
2026-10-18 06:11  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=12 built=None verified=None tokens=697415+12515  20261016-002347-cc5f
2026-10-18 06:18  nvidia     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=6 built=True verified=False tokens=345014+7604  20261016-002347-cc5f
2026-10-18 06:28  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=58 built=None verified=None tokens=3346175+79680  20261016-002347-cc5f
2026-10-18 07:16  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=130 built=None verified=None tokens=7521721+179081  20261016-002347-cc5f
2026-10-18 08:56  nvidia     nvidia/nemotron-3-super-120b-a12b          completed    steps=64 built=True verified=True tokens=3599486+88840  20261016-002347-cc5f
2026-10-18 09:54  nvidia     nvidia/nemotron-3-super-120b-a12b          completed    steps=2 built=True verified=True tokens=110940+473  20261016-002347-cc5f
2026-10-18 09:57  nvidia     nvidia/nemotron-3-super-120b-a12b          needs_input  steps=60 built=True verified=False tokens=3369065+54546  20261016-002347-cc5f
2026-10-18 22:49  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=1 built=None verified=None tokens=56446+2163  20261016-002347-cc5f
2026-10-19 03:22  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=43 built=None verified=None tokens=2427410+42528  20261016-002347-cc5f
2026-10-19 03:45  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=34 built=None verified=None tokens=1899189+31682  20261016-002347-cc5f
2026-10-19 03:52  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=8 built=None verified=None tokens=616530+24689  20261016-002347-cc5f
2026-10-19 05:05  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=14 built=None verified=None tokens=791199+14709  20261016-002347-cc5f
2026-10-19 05:13  nvidia     nvidia/nemotron-3-super-120b-a12b          interrupted  steps=25 built=None verified=None tokens=1431437+54252  20261016-002347-cc5f
2026-10-19 20:52  nvidia     nvidia/nemotron-3-ultra-550b-a55b          interrupted  steps=15 built=None verified=None tokens=875510+9548  20261016-002347-cc5f
2026-10-19 21:10  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=4 built=True verified=True tokens=221976+1010  20261016-002347-cc5f
2026-10-19 21:19  nvidia     nvidia/nemotron-3-ultra-550b-a55b          needs_input  steps=150 built=True verified=False tokens=6449728+67294  20261019-211900-5afa
2026-10-20 13:04  nvidia     nvidia/nemotron-3-ultra-550b-a55b          cancelled    steps=15 built=True verified=False tokens=786535+6427  20261019-211900-5afa
2026-10-20 13:40  nvidia     nvidia/nemotron-3-ultra-550b-a55b          interrupted  steps=17 built=None verified=None tokens=966605+4120  20261019-211900-5afa
2026-10-20 13:51  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=12 built=True verified=True tokens=680765+3198  20261019-211900-5afa
2026-10-20 13:58  nvidia     nvidia/nemotron-3-ultra-550b-a55b          needs_input  steps=1 built=True verified=True tokens=56694+283  20261019-211900-5afa
2026-10-20 14:12  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=6 built=True verified=True tokens=335413+1851  20261019-211900-5afa
2026-10-20 15:17  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=9 built=True verified=True tokens=491333+2882  20261019-211900-5afa
2026-10-20 15:33  nvidia     nvidia/nemotron-3-ultra-550b-a55b          cancelled    steps=49 built=False verified=False tokens=1467227+28692  20261020-153312-21fb
2026-10-20 18:04  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=55 built=True verified=True tokens=1507533+24821  20261020-180358-f3b1
2026-10-20 19:19  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=6 built=True verified=True tokens=190214+1464  20261020-180358-f3b1
2026-10-20 19:21  nvidia     nvidia/nemotron-3-ultra-550b-a55b          completed    steps=6 built=True verified=True tokens=325832+1464  20261019-211900-5afa
2026-10-20 22:54  nvidia     nvidia/nemotron-3-ultra-550b-a55b          needs_input  steps=2 built=True verified=True tokens=106468+285  20261019-211900-5afa
```
