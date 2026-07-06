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
COPY poi/quantum_drives.json poi/quantum_profiles.json poi/blueprints.json poi/locations.json ./

RUN useradd --system --home /app scnav && chown -R scnav:scnav /data
USER scnav

EXPOSE 8765
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
