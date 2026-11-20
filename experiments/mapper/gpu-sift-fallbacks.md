# GPU SIFT's fallback to the CPU's SIFT: every case so far

Since GPU SIFT went on (70d2afa, Nov 15; the first fallback is 23:44 PST that night), a GPU-SIFT placing whose model fails its own check, or
places under 70% of the photos with matches, is placed again from the CPU's SIFT (`pipeline.py`, `PLAIN_SIFT_LEAST`).
This lists every such job up to Nov 17 06:32 PST: the server's `data/fallbacks.jsonl` for the 77 that made a mesh, and
the jobs' logs for the 7 that didn't. Photos placed are in the largest model, against those with 20+ verified matches.

## Summary (84 jobs)

- **The CPU's SIFT placed fewer photos in 29, the same in 36, more in 19**; net -193 photos.
  Recorded as better (its check passed where the GPU's failed, or more placed without failing) in 28.
- **Its check:** the GPU's failed and the CPU's passed in 22; the reverse in 2.
- **By trigger:** "check failed" 43 jobs (the CPU's check failed too in 28, net -155 photos);
  "under 70%" 41 jobs (net -38).
- **What the app does now:** it always keeps the CPU's placing once it has fallen back, worse or not, so in the "worse" rows
  the mesh is made from fewer photos than the GPU's placing had. Each fallback also costs the CPU's SIFT and a second
  placing.

**Done (Nov 17):** whichever placing is better is kept: when the CPU's did no better, the GPU's goes back in its place
(`pipeline._swap_back_placing()`), and `fallbacks.jsonl` and `mapper.json` say which was kept (`"kept"`).

**May want, not done (user, Nov 17: "make a note we may want to do 2"):** drop the "check failed" trigger, falling
back only when the GPU's placing is under 70% of the photos with matches. In 28 of its 43 jobs the CPU's check failed
too, and those lost the most photos (net -155). Look again once `fallbacks.jsonl` has more cases with "kept".

**To do (user, Nov 20): compare mesh quality, not only photos placed.** Nothing yet says whether a GPU-SIFT mesh or a
CPU-SIFT one is better: the benchmark's 2-3 boulders matched in surface, and this list counts photos. Take ~10 of the
cases with the largest differences above, mesh both placings, and compare coverage round the rock, distinct
viewpoints, the two surfaces' agreement, and by eye (~20 meshes, 10-20 machine-hours; most cases' set-aside placing went
with their work folders, so most need placing again first).

## Every case

| When (PST) | Boulder or climb | Mesh | Trigger | GPU: placed, check | CPU: placed, check | CPU − GPU | Placement |
|---|---|---|---|---|---|---|---|
| 11-15 23:44 | Nicole Boulder | ebdeeb2837f9 | under 70% | 67 of 100, failed | 67 of 99, passed | 0 | better |
| 11-15 23:51 | Knuckleball Boulder | c2a6a38ed672 | check failed | 37 of 37, failed | 35 of 37, failed | -2 | **worse** |
| 11-15 23:57 | Gram Parsons Memorial Boulder | no mesh | under 70% | 12 of 30, passed | 5 of 30, passed | -7 | **worse** |
| 11-16 01:23 | Intersection Boulder | e1cc083b5b8d | under 70% | 7 of 13, passed | 10 of 13, passed | +3 | better |
| 11-16 01:32 | Johnny Dawes Boulder | 3d7edac79473 | under 70% | 16 of 28, passed | 15 of 28, failed | -1 | **worse** |
| 11-16 03:12 | Wedge Boulder | 3d0ccb3b08d7 | check failed | 50 of 69, failed | 18 of 70, failed | -32 | **worse** |
| 11-16 03:16 | Flight Attendant Rock | ad6dd5aa970c | under 70% | 17 of 29, passed | 17 of 30, passed | 0 | no better |
| 11-16 03:47 | Meadows Boulder | 8d37e6036bb1 | under 70% | 72 of 152, failed | 74 of 151, failed | +2 | no better |
| 11-16 03:50 | Pumping Monzonite Boulder | 4f02aa3e31cd | check failed | 18 of 18, failed | 18 of 18, passed | 0 | better |
| 11-16 03:55 | Turnbuckle Rock | 50eb52aabff0 | check failed | 27 of 28, failed | 28 of 29, failed | +1 | no better |
| 11-16 04:09 | Fidelman Boulder | 3164474ed7d3 | check failed | 31 of 35, failed | 32 of 35, failed | +1 | no better |
| 11-16 04:53 | Strawberry Contraceptives Boulder | d988116340c1 | check failed | 137 of 160, failed | 92 of 158, failed | -45 | **worse** |
| 11-16 05:00 | Pothole Rock | efa401d5df5b | check failed | 12 of 17, failed | 15 of 18, failed | +3 | no better |
| 11-16 05:12 | Chimney Problem (climb) | 396c6acc6eaf | under 70% | 10 of 48, failed | 6 of 34, passed | -4 | **worse** |
| 11-16 05:35 | Capuchon (climb) | no mesh | under 70% | 2 of 30, passed | 2 of 17, passed | 0 | no better |
| 11-16 06:36 | Campfire Circle Boulder | 0f8acdddd6d2 | under 70% | 47 of 75, passed | 47 of 75, passed | 0 | no better |
| 11-16 06:51 | E. J. Deluxe Boulder | no mesh | check failed | 29 of 31, failed | 27 of 31, failed | -2 | **worse** |
| 11-16 06:57 | The Asteroid Boulder | 15ac5946f5eb | under 70% | 38 of 64, passed | 39 of 64, passed | +1 | better |
| 11-16 07:04 | Black Rock Boulder | 448b2425cea6 | under 70% | 32 of 68, failed | 32 of 68, failed | 0 | no better |
| 11-16 07:06 | Blip Boulder | no mesh | under 70% | 7 of 19, passed | 6 of 19, passed | -1 | **worse** |
| 11-16 07:10 | Illicit Sweetie Boulder | a6e40bd1d00e | check failed | 53 of 56, failed | 50 of 56, failed | -3 | **worse** |
| 11-16 07:28 | Stoney Point Boulder | 87a9501bc51f | under 70% | 19 of 33, passed | 19 of 33, passed | 0 | no better |
| 11-16 07:37 | Garden of Earthly Delights (climb) | d0a3d91e8ffd | under 70% | 9 of 53, passed | 15 of 45, failed | +6 | no better |
| 11-16 07:40 | Mulligan Line (climb) | no mesh | under 70% | 6 of 37, passed | 6 of 28, passed | 0 | no better |
| 11-16 07:42 | Geological Justice (climb) | 9e7e27f17f05 | under 70% | 6 of 31, passed | 6 of 23, passed | 0 | no better |
| 11-16 07:44 | The Blockhead | 9eba65b7b20b | check failed | 12 of 13, failed | 13 of 13, passed | +1 | better |
| 11-16 07:46 | Star Boulder | a18c34fe9dec | check failed | 15 of 19, failed | 18 of 18, passed | +3 | better |
| 11-16 08:38 | Piano Rock | fa5ac7dde531 | check failed | 29 of 33, failed | 29 of 33, failed | 0 | no better |
| 11-16 08:50 | Chocolate Boulder 1 | 1d077245eb13 | check failed | 27 of 32, failed | 27 of 31, failed | 0 | no better |
| 11-16 09:05 | Chocolate Chip | 5688b938ddaa | under 70% | 19 of 28, failed | 19 of 26, failed | 0 | no better |
| 11-16 09:12 | Zinger Boulder | a347841ac610 | check failed | 54 of 54, failed | 54 of 54, failed | 0 | no better |
| 11-16 10:26 | The Outcast (aka Don's Dyno) (climb) | 163d2c776e0c | check failed | 37 of 38, failed | 37 of 38, failed | 0 | no better |
| 11-16 10:37 | Gluttony (climb) | ba8aeb0a3ea6 | check failed | 32 of 36, failed | 31 of 33, passed | -1 | **worse** |
| 11-16 10:44 | Campfire Crag Boulder | no mesh | under 70% | 9 of 13, failed | 7 of 13, passed | -2 | **worse** |
| 11-16 10:49 | Lizzie Boulder | dc8206cae7e9 | check failed | 85 of 88, failed | 85 of 88, passed | 0 | better |
| 11-16 10:52 | Triangle Boulder | 43f3d2655f10 | under 70% | 12 of 18, failed | 11 of 18, failed | -1 | **worse** |
| 11-16 10:56 | Lynn Hill Memorial Boulder | 1bbfe4753164 | check failed | 21 of 21, failed | 22 of 23, failed | +1 | no better |
| 11-16 11:07 | Maverick Boulder | 8f1b9906fb8a | check failed | 26 of 37, failed | 26 of 37, passed | 0 | better |
| 11-16 11:14 | Blas Beemer Boulder | e290b8ef85da | check failed | 40 of 40, failed | 40 of 40, passed | 0 | better |
| 11-16 11:44 | Tour Boulder One | 6389a818bfeb | check failed | 50 of 50, failed | 49 of 50, passed | -1 | **worse** |
| 11-16 12:09 | Railer (climb) | 127a02815a25 | under 70% | 17 of 55, passed | 17 of 52, passed | 0 | no better |
| 11-16 12:19 | Nicole Boulder | 6ae71a009a1f | under 70% | 67 of 100, failed | 67 of 99, passed | 0 | better |
| 11-16 12:25 | Salamander Boulder | da2ac251b8b1 | under 70% | 9 of 16, passed | 14 of 16, passed | +5 | better |
| 11-16 12:28 | Cockroach Boulder | 0a5899bcab9b | under 70% | 19 of 35, passed | 19 of 35, passed | 0 | no better |
| 11-16 12:35 | The Angry Beaver (climb) | 5d88dc650a9e | check failed | 61 of 73, failed | 60 of 72, passed | -1 | **worse** |
| 11-16 12:53 | Morgan Crack (climb) | fca61f9f6b92 | under 70% | 52 of 91, passed | 56 of 89, passed | +4 | better |
| 11-16 13:14 | Derivative Boulder | f64d7f233249 | check failed | 15 of 15, failed | 15 of 15, failed | 0 | no better |
| 11-16 13:44 | Seven Boulder | no mesh | under 70% | 16 of 34, failed | 9 of 33, passed | -7 | **worse** |
| 11-16 14:03 | Antikythera Mechanism (climb) | 7c48de5c0dc7 | check failed | 20 of 20, failed | 20 of 20, failed | 0 | no better |
| 11-16 14:17 | Velvet Fog boulder | 662eb0489d14 | under 70% | 7 of 27, passed | 7 of 25, passed | 0 | no better |
| 11-16 14:19 | Commotio Cordis Boulder | a74e05bd1e39 | under 70% | 11 of 17, passed | 11 of 15, passed | 0 | no better |
| 11-16 14:39 | Tribe Called Conquest Boulder | 9b768f2a8257 | under 70% | 6 of 12, passed | 7 of 13, passed | +1 | better |
| 11-16 14:44 | Iceberg 2 | 271e95b52266 | under 70% | 37 of 74, failed | 35 of 74, passed | -2 | **worse** |
| 11-16 14:48 | Re-Animation Boulder | f59e86761efe | check failed | 22 of 30, failed | 22 of 29, failed | 0 | no better |
| 11-16 15:04 | Methane Boulder | 8f27f8023981 | under 70% | 17 of 68, failed | 9 of 64, passed | -8 | **worse** |
| 11-16 15:08 | Attack of the Staph (climb) | 1d267234261b | check failed | 44 of 44, failed | 44 of 44, failed | 0 | no better |
| 11-16 15:12 | Tin Man Traverse (climb) | c25a4085f39e | under 70% | 6 of 15, passed | 6 of 15, passed | 0 | no better |
| 11-16 15:17 | Byron's Roof (climb) | 2e4b661426cf | check failed | 48 of 59, failed | 59 of 59, failed | +11 | no better |
| 11-16 18:15 | Tips Be Gone (climb) | 04d7f4a2def6 | check failed | 21 of 21, failed | 20 of 20, failed | -1 | **worse** |
| 11-16 18:28 | Bluff Boulder | 2b7b210687f8 | check failed | 89 of 90, failed | 89 of 90, failed | 0 | no better |
| 11-16 19:01 | Chuckawalla Boulder | 13be67f76311 | check failed | 130 of 156, failed | 114 of 154, failed | -16 | **worse** |
| 11-16 19:22 | White Rastafarian (climb) | 460fc757e6c2 | check failed | 59 of 71, failed | 55 of 70, passed | -4 | **worse** |
| 11-16 19:35 | Planet X Boulder | 4c4901207efd | check failed | 72 of 77, failed | 49 of 76, failed | -23 | **worse** |
| 11-16 19:42 | Streetcar Named Desire (climb) | 5773ce9c7212 | check failed | 63 of 63, failed | 63 of 63, failed | 0 | no better |
| 11-16 19:46 | Triangle Boulder | faf008e72e3c | check failed | 24 of 30, failed | 24 of 30, failed | 0 | no better |
| 11-16 19:49 | The Inquisition (climb) | e5a52544617c | check failed | 59 of 61, failed | 58 of 59, passed | -1 | **worse** |
| 11-16 19:52 | Off Camber (climb) | edf0593254ed | check failed | 29 of 31, failed | 30 of 31, passed | +1 | better |
| 11-16 19:55 | X-Men (climb) | 436e1bbdcbfb | under 70% | 8 of 18, passed | 9 of 19, passed | +1 | better |
| 11-16 20:24 | Stem Gem Boulder | f0bfd4931334 | under 70% | 77 of 113, passed | 51 of 113, passed | -26 | **worse** |
| 11-16 20:43 | Flight Attendant Rock | d912797f6525 | under 70% | 17 of 33, passed | 17 of 34, passed | 0 | no better |
| 11-16 22:06 | Meadows Boulder | d835e8359402 | under 70% | 135 of 288, failed | 136 of 286, failed | +1 | no better |
| 11-16 22:53 | Raspberry | 2ca9e69d0c0d | check failed | 107 of 111, failed | 108 of 111, passed | +1 | better |
| 11-16 23:47 | Thunder Egg Boulder | 4c72be4ac725 | under 70% | 42 of 62, failed | 42 of 62, failed | 0 | no better |
| 11-17 00:01 | Zarmog | f70968926e33 | under 70% | 26 of 50, passed | 24 of 50, passed | -2 | **worse** |
| 11-17 00:26 | Thunder Egg Boulder | c09fd8a1ed19 | check failed | 73 of 98, failed | 42 of 97, failed | -31 | **worse** |
| 11-17 01:23 | Obe Boulder | fa058217ebc1 | check failed | 15 of 15, failed | 15 of 15, failed | 0 | no better |
| 11-17 02:06 | Scratch-N-Sniff Boulder | 78eee14d4aa8 | under 70% | 29 of 48, failed | 29 of 48, failed | 0 | no better |
| 11-17 02:27 | Gold Rush Boulder | cd93daee9f3f | check failed | 38 of 47, failed | 38 of 47, failed | 0 | no better |
| 11-17 02:58 | Greasefire Boulder | 48d8f6d0d6a3 | under 70% | 33 of 57, failed | 36 of 57, failed | +3 | no better |
| 11-17 03:28 | Wave Boulder | 8d3f7ab84715 | check failed | 140 of 140, failed | 139 of 139, passed | -1 | **worse** |
| 11-17 03:36 | The Broken Boulder | c89fef8d6d7a | under 70% | 19 of 30, failed | 15 of 29, failed | -4 | **worse** |
| 11-17 03:52 | La Marie-Rose (climb) | 91491c783241 | check failed | 56 of 76, failed | 45 of 75, failed | -11 | **worse** |
| 11-17 04:01 | Year of the Cat Boulder | 66673b8138d1 | under 70% | 57 of 84, passed | 57 of 84, passed | 0 | no better |
| 11-17 06:30 | Blue Flame Boulder | 306ce5326be5 | check failed | 83 of 96, failed | 80 of 91, passed | -3 | **worse** |
