"""Imports real ambient/background recordings from Freesound.org, filtered
to CC0 only, as the negative_ambient_freesound category -- real-world
ambience (traffic, cafes, offices, streets) to sit alongside the
procedurally generated noise in negative_noise and the team's own
real-room recordings in negative_real. See LICENSES.md for why CC0-only,
verified per clip via the API's own license field rather than trusting a
dataset-level tag, is the bar this project holds every source to.

Needs a Freesound account and an API key -- also not something this script
can get for you:

    1. Create a Freesound account, then apply for an API key at
       https://freesound.org/apiv2/apply/ (instant, no approval wait as of
       whenever LICENSES.md was written -- check current their process).
    2. docker compose run --rm -e FREESOUND_API_KEY=... generate \\
           python scripts/import_freesound.py

Downloads the HQ MP3 preview (available via a plain API key; the original
uncompressed file needs Freesound's OAuth2 flow, which is more machinery
than this ambient-noise use case justifies) and converts it to
mono/16kHz/16-bit WAV. Writes SOURCES.txt alongside the clips: CC0 needs no
attribution, but keeping the id/author/url per clip costs nothing and
matches this project's own bar for traceable licensing.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(DATA, "negative_ambient_freesound")
RATE = 16000
API_BASE = "https://freesound.org/apiv2"

# Deliberately generic, real-world ambience -- not music, not sound
# effects, not anything that might itself contain speech saying the wake
# word (impossible to check automatically here the way import_common_voice
# can check a transcript, so queries are chosen to bias toward environments,
# not people talking about specific things).
DEFAULT_QUERIES = [
    "room ambience", "office ambience", "cafe ambience", "street ambience",
    "kitchen ambience", "living room ambience", "traffic ambience",
    "restaurant ambience", "park ambience", "rain ambience",
]


def api_get(path, params, api_key):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        "%s%s?%s" % (API_BASE, path, query),
        headers={"Authorization": "Token %s" % api_key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def convert_to_wav(src_path, dst_path):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_path,
        "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16",
        dst_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and os.path.getsize(dst_path) > 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.environ.get("FREESOUND_API_KEY"))
    parser.add_argument("--per-query", type=int, default=8,
                         help="Max clips to pull per search query (default 8)")
    parser.add_argument("--min-duration", type=float, default=15.0,
                         help="Skip clips shorter than this many seconds -- "
                              "short ambience doesn't add much over negative_noise")
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit(
            "no API key -- pass --api-key or set FREESOUND_API_KEY "
            "(see this script's module docstring for how to get one)"
        )

    os.makedirs(OUT_DIR, exist_ok=True)
    sources_path = os.path.join(OUT_DIR, "SOURCES.txt")
    seen_ids = set()
    imported = 0
    failed = 0

    with open(sources_path, "a", encoding="utf-8") as sources_fh:
        for query in DEFAULT_QUERIES:
            try:
                result = api_get(
                    "/search/text/",
                    {
                        "query": query,
                        "filter": 'license:"Creative Commons 0" duration:[%s TO *]'
                                  % args.min_duration,
                        "fields": "id,name,username,license,duration,previews,url",
                        "page_size": args.per_query,
                    },
                    args.api_key,
                )
            except urllib.error.HTTPError as exc:
                print("query %r failed: %s" % (query, exc))
                continue

            for sound in result.get("results", []):
                sid = sound["id"]
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)

                out_path = os.path.join(OUT_DIR, "fs_%d.wav" % sid)
                if os.path.exists(out_path):
                    continue  # already pulled by a previous run

                preview_url = sound.get("previews", {}).get("preview-hq-mp3")
                if not preview_url:
                    continue

                tmp_path = os.path.join(OUT_DIR, "_tmp_%d.mp3" % sid)
                try:
                    urllib.request.urlretrieve(preview_url, tmp_path)
                    ok = convert_to_wav(tmp_path, out_path)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

                if ok:
                    imported += 1
                    sources_fh.write(
                        "%d\t%s\tby %s\t%s\t%s\n"
                        % (sid, sound.get("name", ""), sound.get("username", ""),
                           sound.get("license", ""), sound.get("url", ""))
                    )
                    sources_fh.flush()
                else:
                    failed += 1

                time.sleep(0.2)  # be a polite API citizen, not a scraper

    print("imported %d ambient clips to %s (%d failed), sources logged to %s"
          % (imported, OUT_DIR, failed, sources_path))
    print(
        "next: add a `features_dir: data/features/negative_ambient_freesound` "
        "entry to config/training_parameters.yaml (commented-out template "
        "already there), then re-run build_features.py"
    )


if __name__ == "__main__":
    main()
