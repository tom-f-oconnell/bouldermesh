# Volunteering a computer to fetch YouTube videos

BoulderMesh makes 3D models of boulders from photos and climbing videos. YouTube stops a server from fetching many videos,
so volunteers fetch some from their own computers, slowly, with a small script. The first part of this page is for
volunteers. The second part is for whoever runs the site: adding volunteers, and connecting the public site to the server
that does the harvesting.

## For volunteers

### What it does, and what it may cost you

The script asks the site for a YouTube video of a climb, fetches it, sends it to the site, and deletes its own copy. It
also searches YouTube for a climb's videos, and reads a climber's channel description, sending back what they say. It
does one thing at a time, 8 to 15 seconds apart, at most 15 an hour and 100 a day. The site only ever sends a video's or
a channel's YouTube id, or a search's words, and the script makes the youtube.com address itself, so it can't be pointed
at anything else.

- **YouTube's terms** don't allow downloading videos except through YouTube's own features. Running this breaks them, and
  the videos are their uploaders' copyright.
- **Your network:** if YouTube decides your address is fetching too much, it may ask everyone on your network to sign in
  or prove they're not a bot for a while. The script then stops for 1 hour, then 2, 4 or 8 if it happens again.
- **What the site learns:** your IP address, which it keeps (the last one you connected from) only to share the limits
  above between everyone at one address. The video's title, uploader, length, date, description and licence go with it,
  a search's results' ids, titles, descriptions, lengths and channels, or a channel's name and description, and nothing
  else about you.

The script explains this again the first time it runs, and asks you to agree.

### What you need

- **A token** from whoever runs the site: one for each person or computer.
- **Python 3.9 or newer.** Check with `python3 --version` (on Windows, `py --version`).
  - macOS: install it from [python.org](https://www.python.org/downloads/), or with Homebrew: `brew install python`.
  - Windows: install it from [python.org](https://www.python.org/downloads/), and tick *Add python.exe to PATH*.
  - Debian or Ubuntu: Python is there, but its `venv` module may not be: `sudo apt install python3-venv`.
- About 150 MB of space, for yt-dlp and Deno (the JavaScript runtime yt-dlp needs for YouTube), which the script installs
  in a folder of its own. Nothing else on your computer is changed.

### Setting it up and running it

1. Download the script, from the address you were given (here, `https://boulderme.sh`):

       curl -O https://boulderme.sh/volunteer/bouldermesh_volunteer.py

   On Windows, use `curl.exe` in place of `curl`. Or open `https://boulderme.sh/volunteer/` in a browser and save the
   script from there. It's the same as `volunteer/bouldermesh_volunteer.py` in this repository, if you'd like to read it.

2. Run it with your token:

       python3 bouldermesh_volunteer.py --server https://boulderme.sh --token YOUR_TOKEN

   On Windows: `py bouldermesh_volunteer.py --server https://boulderme.sh --token YOUR_TOKEN`.

3. Read what it says, and type `yes`. The first time, it then takes a minute or two to install yt-dlp and Deno.

4. Leave it running. It prints what it's doing: fetching a video, sending it, or waiting. Stop it at any time with Ctrl-C.
   Whatever it was fetching goes back to the site for someone else.

The server and token are remembered, so after the first time it's only:

    python3 bouldermesh_volunteer.py

To keep it running after you close the terminal, on macOS or Linux, once you've agreed:

    nohup python3 bouldermesh_volunteer.py >> ~/bouldermesh-volunteer.log 2>&1 &

`tail -f ~/bouldermesh-volunteer.log` shows what it's doing, and `pkill -f bouldermesh_volunteer.py` stops it.

### Options

| Option | What it does |
|---|---|
| `--per-hour N` | at most N videos an hour (15 at most, which is also the default) |
| `--per-day N` | at most N a day (100 at most, the default) |
| `--home DIR` | keep its settings, yt-dlp and work in DIR instead of `~/.bouldermesh-volunteer` (or set `BOULDERMESH_VOLUNTEER_HOME`) |
| `--yt-dlp PATH` | use a yt-dlp you already have, instead of installing one (it must find Deno itself) |
| `--agree` | agree on the first run without being asked |
| `--once` | do one thing and stop |

The site may set lower limits for you, and everyone at one address shares the limits. The script keeps to its own limits
and rests whatever the site says.

### What you'll see

| Message | What it means |
|---|---|
| `nothing to fetch now (...): asking again in N min` | Normal: nothing is wanted, or you've reached a limit. |
| `YouTube refused: resting 1 h` | YouTube's bot check. It rests, longer each time it happens again. |
| `couldn't fetch it: ...` | That video is private, removed, or too big. The site won't ask anyone for it again. |
| `can't reach https://...` | The site is down. It keeps trying. |
| `unknown token` or `this token no longer works` | Ask for a new token, and run it once with `--token`. |
| `this script is too old` | Download it again (step 1). |

If installing fails with a message about `venv` or `ensurepip`, install `python3-venv` (Debian or Ubuntu, above).

### Stopping for good

Stop it with Ctrl-C (or `pkill -f bouldermesh_volunteer.py`), then delete `~/.bouldermesh-volunteer` and the script.
Tell whoever runs the site, so they can revoke your token.

## For whoever runs the site

### Adding and removing volunteers

Volunteers are added on the admin page of the server that harvests: the public server's (below), or hal's if hal
harvests through a tunnel (the other way, further below). Under *Volunteers*:

1. Type the volunteer's name, choose how many videos an hour they may fetch (15 at most), and press *Add volunteer*.
2. Send them the command it shows, privately. It contains their token, which is shown only this once.
3. To stop one, press *Revoke*: their token stops working at once, and whatever they were fetching goes to someone else.

The table shows each volunteer's state (fetching, waiting for work, resting after a refusal, or not connected), when they
were last seen, their address and how many they've fetched.

*Fetch videos from YouTube* sets what's asked of volunteers:

- **Here, and through volunteers while YouTube refuses this server** (the default): the server downloads videos itself,
  and once YouTube refuses it, what harvests and finding beta can't have goes to volunteers.
- **Only here**: never asks volunteers.
- **Only through volunteers**: never asks YouTube for a video itself. Use this on a cloud server, which YouTube refuses.
  `YOUTUBE_VIA=volunteers` in `.env` makes it the starting point.

The buttons:

- *Fetch the videos harvests went without*: videos harvests skipped before there were volunteers, and videos meshes are
  waiting for to find beta.
- *Fetch videos' missing details (licences)*: the details of videos whose licence was never read, without the videos.
- *Drop what's queued*: clears the queue. What's being fetched carries on.

Each climb is harvested again once the videos it wanted have come. An area's climb is queued to *harvest again and
rebuild*. An area that has finished runs that by itself; a stopped area waits until you start it.

### How volunteers reach the server

Volunteers always use the public site's address, `https://boulderme.sh/volunteer/`. Caddy on the public server passes
`/volunteer/` (and nothing else) to a port that answers only that: `VOLUNTEER_PORT`, 47613, on whichever machine harvests:

- **The public server harvests** (below): its own app's port. Its reconstructions run on machines that connect to it for
  work, such as hal, which only ever connects out, over HTTPS, so nothing has to reach into the lab's network.
- **hal harvests** (the other way, further below): an SSH connection from hal to the public server brings `/volunteer/`
  back to hal. That keeps a way into the lab open from outside, and hal's address is the one YouTube keeps refusing.

Volunteers don't notice which it is: their address and the script are the same either way.

### The public server harvests

The public server lists areas, harvests their climbs and keeps everything they make, with volunteers doing its YouTube
searches and fetching its videos, as YouTube refuses a cloud address. It's too small to reconstruct (2 cores, 4 GB), so
its climbs wait for machines that connect to it for work: hal, or any machine with Docker, which fetches a climb's
photos, meshes them and sends the mesh back, through Cloudflare like any visitor; and finds meshes' beta and size the
same way. Its data is capped at 50 GB: over that, volunteers and areas wait until room is made (README, "Putting it on
the public internet").

From hal, in `~/src/bouldermesh`:

1. **Ship the current code, with hal's database, meshes and projects' photos, one last time**, and update the public
   server's checkout (its Caddyfile passes `/worker/` on, and its Compose file sets the ports):

       deploy/build.sh && PHOTOS=1 deploy/ship.sh bouldermesh bouldermesh:public && ssh bouldermesh 'cd bouldermesh && git pull --ff-only'

On the public server (`ssh bouldermesh`), in `~/bouldermesh`:

2. **Settings in `.env`.** Delete the `VOLUNTEER_UPSTREAM` line, if there is one, so that Caddy passes `/volunteer/` to
   this machine's own app, and add these two lines. Then `sudo systemctl restart bouldermesh`.

       HARVESTS_HERE=1
       YOUTUBE_VIA=volunteers

   `HARVESTS_HERE=1` makes `deploy/ship.sh` send only the image from now on, as the database and meshes here are then
   the only copies. `YOUTUBE_VIA=volunteers` keeps it from ever asking YouTube for a video from its cloud address.

3. **Its admin page**, through an SSH tunnel from your own computer: `ssh -N -L 36185:127.0.0.1:36184 bouldermesh`,
   then `http://localhost:36185`. Under *Compute*, set *Reconstructions at once on this server* to 0, *Run more on* to
   *Machines that connect here for work*, and *At most this many other machines at once* to 2, and press *Save*. That's
   how many climbs are under way at once, each harvested here and then waiting for a machine: with 2, the next climb
   is harvested while hal meshes one, and waits for hal after. Then, under *Machines*, add one named hal, and keep the command
   it shows: it has hal's token, shown only this once.

4. **Volunteers.** Each server keeps its own in `data/volunteers.sqlite`, which ship.sh doesn't send. Add them again on
   this admin page, and send each a new command; or copy hal's file across, so their tokens keep working:
   `scp data/volunteers.sqlite bouldermesh:bouldermesh/data/` from hal, with the public server stopped, and then press
   *Drop what's queued* here, as that queue was hal's projects.

Back on hal:

5. **Lend hal to the public server.** The command from step 3, with how many cores its jobs may use, the only images it
   may run (in case the public server is ever taken over), and `--check`:

       python3 worker/bouldermesh_worker.py --server https://boulderme.sh --token bmw_... --cpus 12 \
           --image-prefix ghcr.io/tom-f-oconnell/bouldermesh: --data /data/tom/bouldermesh-worker --check

   It should say the server knows the token as `hal`, and it remembers all of that. `--data` puts its jobs' photos and
   results, and the models they download (a few GB), on the big disk rather than in `~/.bouldermesh-worker`, where its
   settings stay. Then run it whenever hal is up:

       sudo cp deploy/bouldermesh-worker.service /etc/systemd/system/
       sudo systemctl enable --now bouldermesh-worker
       journalctl -u bouldermesh-worker -f

   Its jobs are containers of their own, named `bmw-<job>`, so a `docker update --cpus` of hal's own app doesn't change
   them: `--cpus` is their limit.

6. **Stop hal's own areas**, which would harvest the same climbs, and start them on the public site instead, with the
   editing token. Its admin page then shows hal working or waiting for work, and each climb's log says where it was meshed.

Fetched videos are kept for finding beta and for harvesting again: up to `VIDEO_CACHE_GB` gigabytes, which is 10 in the
public setup and 50 elsewhere. Areas there stop once its disk has less than `CAMPAIGN_MIN_FREE_GB` left, also 10.

### The other way: hal harvests, through a tunnel

This is how volunteers were first connected, on Nov 9, 2026, before hal's place moved to meshing for the public
server. On hal, in `~/src/bouldermesh`:

1. **Run the current code.** hal's Compose setup publishes the volunteer port on loopback only (`127.0.0.1:47613`).
   Rebuild and restart it as usual, from committed code. Check it answers:

       curl -s http://127.0.0.1:47613/volunteer/ | head -3

2. **Tell it its public address**, for the command the admin page gives volunteers. Add this line to `.env`, then restart:

       VOLUNTEER_URL=https://boulderme.sh

3. **Make a key for the tunnel.** It runs as a service, with no one logged in to type a passphrase, so it needs a key of
   its own. Press Enter at both passphrase prompts, to leave it empty:

       ssh-keygen -t ed25519 -C 'volunteer tunnel' -f ~/.ssh/bouldermesh_tunnel

4. **Let the public server accept that key for forwarding only.** No shell, and no command, can be run with it:

       printf 'restrict,port-forwarding,command="/bin/false" %s\n' "$(cat ~/.ssh/bouldermesh_tunnel.pub)" | ssh bouldermesh 'cat >> ~/.ssh/authorized_keys'

On the public server (`ssh bouldermesh`):

5. **Let sshd replace a socket left from a dropped connection.** Otherwise the tunnel can't reconnect:

       echo 'StreamLocalBindUnlink yes' | sudo tee /etc/ssh/sshd_config.d/20-tunnel.conf
       sudo systemctl reload ssh

6. **Make the tunnel's folder yourself**, before Caddy is restarted. If Docker makes it first, it's root's, and the tunnel
   can't use it:

       mkdir -p ~/bouldermesh/data/tunnel

7. **Send Caddy's `/volunteer/` through the tunnel.** Add this line to `~/bouldermesh/.env`:

       VOLUNTEER_UPSTREAM=unix//run/bm-tunnel/volunteer.sock

Back on hal:

8. **Ship the current code to the public server, and restart it.** The pull brings the new Caddyfile and Compose files:

       deploy/build.sh && deploy/ship.sh bouldermesh bouldermesh:public && ssh bouldermesh 'cd bouldermesh && git pull --ff-only && sudo systemctl restart bouldermesh'

9. **Try the tunnel by hand.** Leave it running in one terminal:

       ssh -N -i ~/.ssh/bouldermesh_tunnel -o IdentitiesOnly=yes -R /home/tom/bouldermesh/data/tunnel/volunteer.sock:127.0.0.1:47613 tom@178.105.53.11

   In another terminal, the public page should now come from hal:

       curl -s https://boulderme.sh/volunteer/ | head -5

   It should start with `<!doctype html>` and the title *Volunteer for BoulderMesh*. Then stop the first with Ctrl-C.

10. **Run the tunnel as a service.** The settings at the top of `deploy/volunteer-tunnel.service` (`User`, `HOST`, `KEY`,
    `SOCKET`) are already hal's and the public server's. Then:

        sudo cp deploy/volunteer-tunnel.service /etc/systemd/system/
        sudo systemctl enable --now volunteer-tunnel
        systemctl status volunteer-tunnel

11. **Add yourself as a volunteer** on hal's admin page, and run the script on a computer on another network, such as a
    laptop at home.

If `https://boulderme.sh/volunteer/` gives a 502, Caddy can't reach hal:

- `journalctl -u volunteer-tunnel -n 30` on hal shows why the tunnel isn't up:
  - `Permission denied (publickey)`: step 4's line isn't in the public server's `authorized_keys`.
  - `remote port forwarding failed for listen path`: step 5 isn't in effect (`sudo sshd -T | grep -i streamlocalbindunlink`
    should say `yes`), or the folder belongs to root. Fix that with `sudo chown tom: ~/bouldermesh/data/tunnel`.
- On the public server, `ls -l ~/bouldermesh/data/tunnel/` should show `volunteer.sock` while the tunnel is up, and
  `grep VOLUNTEER_UPSTREAM ~/bouldermesh/.env` should show step 7's line. Caddy only reads `.env` when it's restarted.
- On hal, step 1's `curl` should answer. If it doesn't, the app isn't serving the port: check `VOLUNTEER_PORT` in its
  container (`docker compose exec bouldermesh env | grep VOLUNTEER`).

Nothing in the firewall needs changing: the tunnel uses SSH, which is already open, and Caddy reaches the socket inside
the public server.

### Searches, and climbs waiting for volunteers

*Fetch videos from YouTube* covers searches and channels' descriptions too. With *Only through volunteers* (the public
server), every YouTube search a harvest makes is asked of volunteers: the script, from version 2, searches by the words
the server sends and sends back the results, which the server keeps for 30 days. With the default, the server searches
itself until YouTube refuses its searches, and asks volunteers while it does. Older copies of the script are only given
videos and details: download it again for searches.

An area's climb whose harvest asked volunteers for anything waits for it (*waiting for volunteers* on the Areas tab),
rather than being meshed once without and again with it: once the search's results have come, it's harvested again,
which may ask for the videos they found, and once those have come, it's harvested again and meshed. A climb that has
waited `VOLUNTEER_WAIT_HOURS` (24) is meshed with what has come, and what comes later rebuilds it, as before. An area
with only waiting climbs left stays running, and says how many wait.
