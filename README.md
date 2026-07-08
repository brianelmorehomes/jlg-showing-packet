# JLG Showing Packet Builder — Web Edition (Render)

Same tool as the desktop version, packaged to run as a hosted web app on
[Render](https://render.com) so anyone on the team can use it from a browser
&mdash; no install, no Claude session.

Nothing about a showing is stored on the server: listing sheets are held in
the browser between steps and re-sent when you generate the packet; the
merged PDF is built in a temp file for the life of that one request and
streamed straight back.

## What it does

1. Drop in the raw MLS listing sheets for everything on today's showing list.
2. Drag them into showing order and type a time next to each stop.
3. Click Generate. You get back one merged, branded PDF: a cover page with
   the ordered schedule and a numbered route map, followed by each
   listing's full two-page flyer, in showing order.

## Deploy it (one time, ~15 minutes)

### 1. Push this folder to GitHub

If you don't already have a repo for this:

```bash
cd "path/to/this/folder"
git init
git add .
git commit -m "JLG showing packet builder"
```

Then create a new empty repository on [github.com/new](https://github.com/new)
(name it something like `jlg-showing-packet`), and push:

```bash
git remote add origin https://github.com/<your-username>/jlg-showing-packet.git
git branch -M main
git push -u origin main
```

### 2. Create a Render account (skip if you already have one from the flyer app)

Go to [render.com](https://render.com) and sign up (GitHub sign-in is
fastest). Free tier is fine for this.

### 3. Create the web service

- In the Render dashboard, click **New +** &rarr; **Web Service**.
- Connect the GitHub repo you just created.
- Render should auto-detect the `render.yaml` in this folder (a "Blueprint")
  and set the environment to **Docker** automatically. If it asks you to pick
  manually: Environment = **Docker**, Plan = **Free**.
- Under **Environment Variables**, set:
  - `AGENT_NAME` &mdash; e.g. `Brian Elmore`
  - `AGENT_PHONE` &mdash; your phone number, e.g. `312.989.0512`
  - `AGENT_EMAIL` &mdash; e.g. `brian@justinlucasgroup.com`
  (Anyone using the tool can change all three per-session right in the
  browser before generating &mdash; this just sets the default shown when the
  page first loads.)
- Click **Create Web Service**.

First build takes 3-5 minutes. After that, Render gives you a URL like
`https://jlg-showing-packet.onrender.com` &mdash; that's the app, share that
link with the team.

### 4. (Optional) Point a friendlier URL at it

If you want something like `showings.justinlucasgroup.com` instead of the
`.onrender.com` address, add a custom domain in the Render dashboard under
this service's **Settings**, then add the CNAME record it gives you wherever
your domain's DNS is managed.

## Using it day to day

Open the URL, drop in the listing sheets for today's showings, drag them
into order, add times, and click Generate.

## The free-tier tradeoff

Render's free plan spins the service down after ~15 minutes with no traffic.
For occasional, once-a-day use, that just means the first load of the day
takes 30-60 seconds to wake back up; after that it's fast for the rest of
your session.

## About the route map

The map uses OpenStreetMap's free Nominatim geocoding service and OSM map
tiles &mdash; no API key, no billing account, nothing extra to set up. It's
rate-limited to about one address per second by Nominatim's usage policy, so
adding the map takes a few extra seconds per stop. If an address can't be
geocoded (rare, but possible for a brand-new address OSM doesn't know about
yet), that one pin is silently skipped and the rest of the packet still
builds normally &mdash; the schedule table doesn't depend on the map at all.
If you'd rather have Google Maps-quality styling and geocoding, that's a
separate upgrade (needs a Google Maps API key with billing enabled) &mdash;
ask Claude to wire it in if you want to go that route later.

## Making changes later

Edit the files, commit, `git push` &mdash; Render automatically rebuilds and
redeploys on every push to `main`. The parsing logic lives in `parser.py`,
the flyer layout in `templates/flyer.html`, the cover page layout in
`templates/cover.html`, and the packet-building/geocoding/map logic in
`packet.py`.
