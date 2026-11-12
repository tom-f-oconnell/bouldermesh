# BoulderMesh: notes for Claude

- `MESH_RULES` in backend/server.py marks every mesh made under an older value as old: tagged so to editors, hidden
  from visitors' lists of a boulder's meshes, and counted as to be made again. Ask the user before raising it.
- Before implementing a change that might call for raising it, say so and confirm with the user first. That's any
  change to what an area meshes, or from which photos: what counts as one boulder or a group, pooling, what a boulder's
  mesh leaves out, the off-topic filter's verdicts, harvest sources, or COLMAP/OpenMVS settings that change which
  photos are placed.
