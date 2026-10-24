from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import os
from fastapi.responses import HTMLResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fetch securely from Vercel Environment Variables
UPSTREAM_ADDONS = [val.rstrip("/") for key, val in os.environ.items() if key.startswith("ADDON_")]

@app.get("/")
async def root():
    return {"status": "Kitsu Proxy is running", "manifest": "/manifest.json"}

@app.get("/manifest.json")
async def get_manifest():
    return {
        "id": "org.custom.kitsu2http.proxy",
        "version": "1.0.0",
        "name": "Kitsu HTTP Proxy",
        "description": "Translates Kitsu IDs into IMDb SxxExx for HTTP Addons",
        "resources": ["stream"],
        "types": ["anime", "series", "movie"],
        "idPrefixes": ["kitsu"]
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

    async with httpx.AsyncClient() as client:
        tasks = []
        resolved = await resolve_kitsu_id(client, kitsu_id, ep_num)
        
        if not resolved:
            return {"streams": []}

        target_type = resolved["type"]
        for addon_url in UPSTREAM_ADDONS:
            if resolved["imdb"]:
                # Strictly append only the IMDb request task
                tasks.append(fetch_upstream(client, addon_url, target_type, resolved["imdb"]))

        results = await asyncio.gather(*tasks)

    combined_streams = []
    for stream_list in results:
        for s in stream_list:
            s["name"] = f"[KitsuProxy] {s.get('name', 'HTTP')}"
            combined_streams.append(s)

    return {"streams": combined_streams}
