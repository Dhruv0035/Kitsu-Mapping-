from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paste your HTTP addon base URLs here (everything BEFORE /manifest.json)
# Do NOT put a trailing slash "/" at the end of the URLs
UPSTREAM_ADDONS = [
    "https://pengu.uk/%7B%22auth_token%22%3A%22AbDWpMr0T1rs81OOaEfXF6oZQVs3ofjH5a-H1RE78QA%22%7D" # Replace with your PenguPlay URL
]

@app.get("/manifest.json")
async def get_manifest():
    return {
        "id": "org.custom.kitsu2http.proxy",
        "version": "1.0.0",
        "name": "Kitsu HTTP Proxy",
        "description": "Translates Kitsu IDs into SxxExx for HTTP Addons",
        "resources": ["stream"],
        "types": ["anime", "series", "movie"],
        "idPrefixes": ["kitsu"]
    }

async def resolve_kitsu_id(client: httpx.AsyncClient, kitsu_id: str, ep_num: str):
    resp = await client.get(f"https://api.ani.zip/mappings?kitsu_id={kitsu_id}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    mappings = data.get("mappings", {})
    imdb_id = mappings.get("imdb_id")
    tmdb_id = mappings.get("themoviedb_id")

    if mappings.get("type") == "MOVIE":
        return {"type": "movie", "imdb": imdb_id, "tmdb": f"tmdb:{tmdb_id}" if tmdb_id else None}

    ep_data = data.get("episodes", {}).get(str(ep_num), {})
    season = ep_data.get("seasonNumber")
    episode = ep_data.get("episodeNumber")
    if not season or not episode:
        return None

    return {
        "type": "series",
        "imdb": f"{imdb_id}:{season}:{episode}" if imdb_id else None,
        "tmdb": f"tmdb:{tmdb_id}:{season}:{episode}" if tmdb_id else None
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
        resolved = await resolve_kitsu_id(client, kitsu_id, ep_num)
        if not resolved:
            return {"streams": []}

        target_type = resolved["type"]
        tasks = []
        for addon_url in UPSTREAM_ADDONS:
            if resolved["imdb"]:
                tasks.append(fetch_upstream(client, addon_url, target_type, resolved["imdb"]))
            if resolved["tmdb"]:
                tasks.append(fetch_upstream(client, addon_url, target_type, resolved["tmdb"]))
        results = await asyncio.gather(*tasks)

    combined_streams = []
    for stream_list in results:
        for s in stream_list:
            s["name"] = f"[KitsuProxy] {s.get('name', 'HTTP')}"
            combined_streams.append(s)

    return {"streams": combined_streams}
