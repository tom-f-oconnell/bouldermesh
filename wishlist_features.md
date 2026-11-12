# Wishlist features

- **Machines take short jobs while a deploy waits.** While `./deploy.sh` waits for work in progress (its "after"
  mode, up to 90 min), the server gives no machine new work, so idle machines sit while jobs queue (Charcuterie's
  mesh waited 66 min on Nov 11, 2026, in a drain that deadlocked before cf5da07). It could still hand out queued
  jobs whose typical time on machines (`Board.typical()`) fits before the deploy's deadline, and wait for them too.
- **Naming who does each step.** The Areas tab's running steps say only whether the server, a machine or volunteers
  are doing them (`Tracker.at()`). An admin setting could name the machine, or the volunteers whose videos it uses,
  for those that agreed to be shown, as the leaderboard does (`shown_name()`).
