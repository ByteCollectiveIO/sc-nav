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

RUN useradd --system --home /app scnav && chown -R scnav:scnav /data
USER scnav

EXPOSE 8765
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
