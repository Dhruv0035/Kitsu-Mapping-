from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import os
from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fetch securely from Vercel Environment Variables
UPSTREAM_ADDONS = [val.rstrip("/") for key, val in os.environ.items() if key.startswith("ADDON_")]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

overrides_path = os.path.join(BASE_DIR, "overrides.json")
OVERRIDES_FILE_FOUND = False

try:
    with open(overrides_path, "r", encoding="utf-8") as f:
        MANUAL_OVERRIDES = json.load(f)
        OVERRIDES_FILE_FOUND = True
except (FileNotFoundError, json.JSONDecodeError):
    MANUAL_OVERRIDES = {}
except json.JSONDecodeError:
    MANUAL_OVERRIDES = {}

http_client = None

def get_client():
    global http_client
    # If it doesn't exist yet, or got closed, create a fresh one
    if http_client is None or http_client.is_closed:
        # Optional: Add Limits() to prevent pool exhaustion on high traffic
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        http_client = httpx.AsyncClient(timeout=15.0, limits=limits)
    return http_client

@app.get("/health")
async def health_check():
    return {
        "status": "ok", 
        "message": "Kitsu Proxy backend is awake",
        "overrides_file_found": OVERRIDES_FILE_FOUND,
        "overrides_count": len(MANUAL_OVERRIDES),
        "addons_count": len(UPSTREAM_ADDONS)
    }

async def get_mal_id_from_kitsu(client: httpx.AsyncClient, kitsu_id: str):
    """Fetches the MyAnimeList ID directly from Kitsu's official API."""
    try:
        url = f"https://kitsu.io/api/edge/anime/{kitsu_id}/mappings"
        headers = {"Accept": "application/vnd.api+json"}
        r = await client.get(url, headers=headers, timeout=8.0)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                site = item.get("attributes", {}).get("externalSite", "")
                if site == "myanimelist/anime":
                    return item.get("attributes", {}).get("externalId")
    except Exception:
        pass
    return None

async def resolve_kitsu_id(client: httpx.AsyncClient, kitsu_id: str, ep_num: str):
    try:
        resp = await client.get(f"https://api.ani.zip/mappings?kitsu_id={kitsu_id}", timeout=8.0)
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        data = {}

    if not data.get("mappings"):
        mal_id = await get_mal_id_from_kitsu(client, kitsu_id)
        if mal_id:
            try:
                resp = await client.get(f"https://api.ani.zip/mappings?mal_id={mal_id}", timeout=8.0)
                data = resp.json() if resp.status_code == 200 else {}
            except Exception:
                data = {}

    mappings = data.get("mappings", {})
    imdb_id = mappings.get("imdb_id")

    if not mappings:
        return None

    if not imdb_id:
        return None

    if mappings.get("type") == "MOVIE":
        return {"type": "movie", "imdb": imdb_id}

    ep_data = data.get("episodes", {}).get(str(ep_num), {})
    season = ep_data.get("seasonNumber")
    episode = ep_data.get("episodeNumber")
    
    if not season or not episode:
        return None

    return {
        "type": "series",
        "imdb": f"{imdb_id}:{season}:{episode}" if imdb_id else None
    }

async def fetch_upstream(client: httpx.AsyncClient, base_url: str, media_type: str, target_id: str):
    url = f"{base_url}/stream/{media_type}/{target_id}.json"
    try:
        r = await client.get(url, timeout=8.0)
        if r.status_code == 200:
            return r.json().get("streams", [])
    except Exception:
        pass
    return []

@app.get("/stream/{media_type}/{item_id}.json")
async def get_stream(media_type: str, item_id: str):
    parts = item_id.split(":")
    if parts[0] != "kitsu":
        return {"streams": []}

    kitsu_id = parts[1]
    ep_num = parts[2] if len(parts) > 2 else "1"

    client = get_client()
    tasks = []
        
    # Check manual overrides before querying external APIs
    if kitsu_id in MANUAL_OVERRIDES:
        override_data = MANUAL_OVERRIDES[kitsu_id]
        imdb_id = override_data.get("imdb_id")
        season = override_data.get("season")
        offset = override_data.get("offset", 0)
            
        calculated_ep = str(int(ep_num) + int(offset))
        target_id = f"{imdb_id}:{season}:{calculated_ep}"
            
        for addon_url in UPSTREAM_ADDONS:
            tasks.append(fetch_upstream(client, addon_url, "series", target_id))
    else:
        # Fallback to AniZip routing if no override exists
        resolved = await resolve_kitsu_id(client, kitsu_id, ep_num)
        if resolved and resolved.get("imdb"):
            target_type = resolved["type"]
            for addon_url in UPSTREAM_ADDONS:
                tasks.append(fetch_upstream(client, addon_url, target_type, resolved["imdb"]))

    if tasks:
        results = await asyncio.gather(*tasks,return_exceptions=True)
    else:
        results = []

    combined_streams = []
    for stream_list in results:
        if isinstance(stream_list, Exception):
            continue
        for s in stream_list:
            s["name"] = f"[KitsuProxy] {s.get('name', 'HTTP')}"
            combined_streams.append(s)

    return {"streams": combined_streams}
