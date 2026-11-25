# BoulderMesh: notes for Claude

- `MESH_RULES` in backend/server.py marks every mesh made under an older value as old: tagged so to editors, hidden
  from visitors' lists of a boulder's meshes, and counted as to be made again. Ask the user before raising it.
- Before implementing a change that might call for raising it, say so and confirm with the user first. That's any
  change to what an area meshes, or from which photos: what counts as one boulder or a group, pooling, what a boulder's
  mesh leaves out, the off-topic filter's verdicts, harvest sources, or COLMAP/OpenMVS settings that change which
  photos are placed.
- Remake only the meshes a change would actually change (the user, Nov 24): for each change to how meshes are made,
  first look for a check quicker than remaking of which meshes it touches (as `backend/roicrop.py` does for OpenMVS's
  cropping), and give those a tier of their own; raise `MESH_RULES` or `PIPELINE_VERSION` only where no such check can
  be had.
- When pointing the user at meshes or climbs to look at, give full links (https://boulderme.sh/m/<mesh id>,
  https://boulderme.sh/c/<climb slug>), never bare IDs.
- When the user asks to deploy, run `WAIT_MINUTES=100000 ./deploy.sh --after` (in the background), as if they'd answered
  [a] at its question: no new work is taken, and it restarts once what's in progress has finished. The large
  WAIT_MINUTES matches [a] at a terminal, which waits as long as it takes; without it, a run with no terminal restarts
  anyway after 90 min.
