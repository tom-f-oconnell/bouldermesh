# Volunteering a computer to fetch YouTube videos

BoulderMesh makes 3D models of boulders from photos and climbing videos. YouTube stops a server from fetching many videos,
so volunteers fetch some from their own computers, slowly, with a small script. The first part of this page is for
volunteers. The second part is for whoever runs the site: adding volunteers, and connecting the public site to the server
that does the harvesting.

## For volunteers

### What it does, and what it may cost you

The script asks the site for a YouTube video of a climb, fetches it, sends it to the site, and deletes its own copy. It does
one video at a time, 8 to 15 seconds apart, at most 15 an hour and 100 a day. The site only ever sends a video's YouTube
id, and the script makes the youtube.com address itself, so it can't be pointed at anything else.

- **YouTube's terms** don't allow downloading videos except through YouTube's own features. Running this breaks them, and
  the videos are their uploaders' copyright.
- **Your network:** if YouTube decides your address is fetching too much, it may ask everyone on your network to sign in
  or prove they're not a bot for a while. The script then stops for 1 hour, then 2, 4 or 8 if it happens again.
- **What the site learns:** your IP address, which it keeps (the last one you connected from) only to share the limits
  above between everyone at one address. The video's title, uploader, length, date, description and licence go with it,
  and nothing else about you.

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

Volunteers are added on the admin page of the server that harvests: hal's while hal harvests (option A below), or the
public server's once it does (option B). Under *Volunteers*:

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
`/volunteer/` (and nothing else) to a port that answers only that: `VOLUNTEER_PORT`, 47613. Which machine's port it reaches
depends on which machine harvests:

- **Option A:** hal harvests. An SSH connection from hal to the public server brings `/volunteer/` back to hal.
- **Option B:** the public server harvests, with reconstructions on AWS workers. Caddy passes `/volunteer/` to the public
  server's own app, and nothing reaches hal.

Volunteers don't notice which it is: their address and the script are the same either way.

### Option A: hal harvests

On hal, in `~/src/bouldermesh`:

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

### Option B: the public server harvests

Once the public server does the harvesting, with reconstructions on AWS workers:

1. **On the public server**, in `~/bouldermesh/.env`, delete the `VOLUNTEER_UPSTREAM` line, so that Caddy passes
   `/volunteer/` to its own app. Add `YOUTUBE_VIA=volunteers`, so that it never asks YouTube for videos from its cloud
   address. `VOLUNTEER_URL` comes from `SITE_HOST`, so it needs nothing. Then restart it:

       sudo systemctl restart bouldermesh

2. **On hal**, stop the tunnel:

       sudo systemctl disable --now volunteer-tunnel

3. **Bring the volunteers over.** Each server keeps its own volunteers in `data/volunteers.sqlite`, which
   `deploy/ship.sh` doesn't send. Either:
   - add them again on the public server's admin page, and send each a new command; or
   - copy hal's file across, so their tokens keep working and they need do nothing:
     `scp data/volunteers.sqlite bouldermesh:bouldermesh/data/`, with both servers stopped. Then press *Drop what's
     queued* on the public server's admin page, as that queue was hal's projects.

4. **Reach the public server's admin page** through an SSH tunnel: `ssh -L 36185:127.0.0.1:36184 bouldermesh`, then open
   `http://localhost:36185`.

Fetched videos are kept for finding beta and for harvesting again: up to `VIDEO_CACHE_GB` gigabytes, which is 10 in the
public setup and 50 elsewhere.

To go back to option A, undo these: put `VOLUNTEER_UPSTREAM` back, start the tunnel again
(`sudo systemctl enable --now volunteer-tunnel`), and restart the public server.

### If YouTube starts refusing searches

Searches stay with the server, as hal's were never refused. A refused search pauses searching for 1 to 8 hours, apart
from downloads. If that starts happening, `/api/status` shows `youtube_searches_refusing`, and searching should move to
volunteers too: design.md, "Volunteers fetching videos", says how.
