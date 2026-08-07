FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .

# The watcher source is served as a personalized download from the Setup page
# (app.py /download/watcher), so bundle it into the image.
COPY watcher/ ./watcher_src/

# Seed the data cache with the repo's snapshot. /data is mounted as a named
# volume; Docker copies this seed into the volume on first use, so the app
# can start even if starmap.space is unreachable at first boot. Live fetches
# overwrite the cache afterward.
COPY poi/ /data/
ENV SC_NAV_DATA=/data

# Static, code-versioned reference data (#27 quantum, #25 blueprints, #28
# locations). Also bundled INTO the code dir (/app) — not just /data — because
# /data is a named volume that shadows the image's seed and is only populated
# on the volume's first creation, so files added in a later release never reach
# an existing volume. app.load_quantum()/load_blueprints()/
# load_wiki_locations() read the code-dir copy first.
# containers.json rides along for ghost-anchor recovery
# (app.load_shipped_containers): the /data copy is the live-fetch cache, so
# after a degraded upstream fetch it has forgotten the very containers that
# stored survey marks/observations were anchored to.
COPY poi/quantum_drives.json poi/quantum_profiles.json poi/blueprints.json poi/locations.json poi/containers.json ./

# The Strata RS sync tool (not committed data — the tool). The server runs it at
# startup when STRATA_API_KEY is set, writing ore_signatures.json to the data
# volume; with no key nothing runs and the RS card falls back to org scans.
COPY tools/sync_strata.py ./tools/

RUN useradd --system --home /app scnav && chown -R scnav:scnav /data
USER scnav

EXPOSE 8765
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
