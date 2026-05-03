#!/usr/bin/env python3
"""
SoundWave Backend v10 — Multi-Source Fallback Engine
Inspired by VidMate (stream extraction) + Brave (resilient fallback chain)

Extraction Priority:
  1. Piped API (fastest, proxied, no auth needed)
  2. Invidious API (backup, different infrastructure)
  3. yt-dlp CLI (last resort, most reliable but slowest)
"""

import subprocess
import json
import time
import random
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from cachetools import TTLCache

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Instance pools — rotated on failure
# ---------------------------------------------------------------------------
PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.in.projectsegfau.lt',
    'https://api.piped.projectsegfau.lt',
    'https://pipedapi.moomoo.me',
    'https://pipedapi.leptons.xyz',
]

INVIDIOUS_INSTANCES = [
    'https://inv.nadeko.net',
    'https://yewtu.be',
    'https://vid.puffyan.us',
    'https://invidious.nerdvpn.de',
    'https://invidious.perennialte.ch',
]

# TTL caches — stream URLs expire after 30 min, search after 5 min
_stream_cache = TTLCache(maxsize=500, ttl=1800)
_search_cache = TTLCache(maxsize=200, ttl=300)

# Instance health tracker {url: last_failure_timestamp}
_unhealthy = {}
UNHEALTHY_COOLDOWN = 300  # seconds before retrying a failed instance

# Fallback demo audio
FALLBACK_AUDIO = 'https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg'

# Local demo tracks
LOCAL_TRACKS = [
    {'id': '1', 'title': 'Summer Vibes', 'artist': 'NCS', 'thumbnailUrl': 'https://picsum.photos/seed/t1/300/300'},
    {'id': '2', 'title': 'Night Drive', 'artist': 'NCS', 'thumbnailUrl': 'https://picsum.photos/seed/t2/300/300'},
    {'id': '3', 'title': 'Electronic Dream', 'artist': 'NCS', 'thumbnailUrl': 'https://picsum.photos/seed/t3/300/300'},
    {'id': '4', 'title': 'Chill Beats', 'artist': 'Free', 'thumbnailUrl': 'https://picsum.photos/seed/t4/300/300'},
    {'id': '5', 'title': 'Uplifting', 'artist': 'Free', 'thumbnailUrl': 'https://picsum.photos/seed/t5/300/300'},
]


# ===========================================================================
# Helpers
# ===========================================================================

def _healthy_instances(pool):
    """Return instances not marked unhealthy recently."""
    now = time.time()
    healthy = [u for u in pool if now - _unhealthy.get(u, 0) > UNHEALTHY_COOLDOWN]
    return healthy if healthy else pool  # fallback to all if none healthy


def _mark_unhealthy(url):
    _unhealthy[url] = time.time()


def _http_get(url, timeout=12):
    """Simple GET with a user-agent to avoid bot blocking."""
    return requests.get(url, timeout=timeout, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })


# ===========================================================================
# Tier 1 — Piped API
# ===========================================================================

def search_piped(query, limit=10):
    """Search via Piped API — returns music-specific results."""
    for instance in _healthy_instances(PIPED_INSTANCES):
        try:
            url = f'{instance}/search?q={requests.utils.quote(query)}&filter=music_songs'
            resp = _http_get(url)
            if resp.status_code != 200:
                _mark_unhealthy(instance)
                continue

            data = resp.json()
            items = data.get('items', data) if isinstance(data, dict) else data
            tracks = []
            for item in items:
                if len(tracks) >= limit:
                    break
                video_url = item.get('url', '')
                video_id = video_url.replace('/watch?v=', '') if video_url else ''
                if not video_id:
                    continue
                tracks.append({
                    'title': item.get('title', 'Unknown'),
                    'artist': item.get('uploaderName', item.get('uploader', 'Unknown')),
                    'thumbnailUrl': item.get('thumbnail', ''),
                    'videoId': video_id,
                    'duration': item.get('duration', 0),
                    'views': item.get('views', 0),
                })

            if tracks:
                print(f'[Piped] Search OK via {instance}: {len(tracks)} results')
                return tracks

        except Exception as e:
            print(f'[Piped] Search failed on {instance}: {e}')
            _mark_unhealthy(instance)

    return []


def stream_piped(video_id, quality='medium'):
    """Get audio stream URL via Piped API."""
    for instance in _healthy_instances(PIPED_INSTANCES):
        try:
            url = f'{instance}/streams/{video_id}'
            resp = _http_get(url)
            if resp.status_code != 200:
                _mark_unhealthy(instance)
                continue

            data = resp.json()
            audio_streams = data.get('audioStreams', [])
            if not audio_streams:
                continue

            # Sort by bitrate (highest first)
            audio_streams.sort(key=lambda s: s.get('bitrate', 0), reverse=True)

            # Pick quality tier
            if quality == 'high':
                stream = audio_streams[0]
            elif quality == 'low':
                stream = audio_streams[-1]
            else:
                # Medium — pick middle bitrate or ~128kbps
                mid = len(audio_streams) // 2
                stream = audio_streams[mid] if len(audio_streams) > 2 else audio_streams[0]

            stream_url = stream.get('url', '')
            if stream_url:
                print(f'[Piped] Stream OK via {instance}: {stream.get("quality", "?")}')
                return {
                    'audioUrl': stream_url,
                    'quality': stream.get('quality', ''),
                    'format': stream.get('format', ''),
                    'bitrate': stream.get('bitrate', 0),
                    'mimeType': stream.get('mimeType', ''),
                    'duration': data.get('duration', 0),
                    'title': data.get('title', ''),
                    'artist': data.get('uploader', ''),
                    'thumbnailUrl': data.get('thumbnailUrl', ''),
                }

        except Exception as e:
            print(f'[Piped] Stream failed on {instance}: {e}')
            _mark_unhealthy(instance)

    return None


def trending_piped(region='IN'):
    """Get trending music via Piped."""
    for instance in _healthy_instances(PIPED_INSTANCES):
        try:
            url = f'{instance}/trending?region={region}'
            resp = _http_get(url)
            if resp.status_code != 200:
                _mark_unhealthy(instance)
                continue

            data = resp.json()
            tracks = []
            for item in data:
                video_url = item.get('url', '')
                video_id = video_url.replace('/watch?v=', '') if video_url else ''
                if not video_id:
                    continue
                tracks.append({
                    'title': item.get('title', 'Unknown'),
                    'artist': item.get('uploaderName', item.get('uploader', 'Unknown')),
                    'thumbnailUrl': item.get('thumbnail', ''),
                    'videoId': video_id,
                    'duration': item.get('duration', 0),
                    'views': item.get('views', 0),
                })
            if tracks:
                print(f'[Piped] Trending OK via {instance}: {len(tracks)} results')
                return tracks

        except Exception as e:
            print(f'[Piped] Trending failed on {instance}: {e}')
            _mark_unhealthy(instance)

    return []


def suggestions_piped(query):
    """Get search suggestions via Piped."""
    for instance in _healthy_instances(PIPED_INSTANCES):
        try:
            url = f'{instance}/suggestions?query={requests.utils.quote(query)}'
            resp = _http_get(url, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            _mark_unhealthy(instance)
    return []


# ===========================================================================
# Tier 2 — Invidious API
# ===========================================================================

def search_invidious(query, limit=10):
    """Search via Invidious API."""
    for instance in _healthy_instances(INVIDIOUS_INSTANCES):
        try:
            url = f'{instance}/api/v1/search?q={requests.utils.quote(query)}&type=video'
            resp = _http_get(url)
            if resp.status_code != 200:
                _mark_unhealthy(instance)
                continue

            data = resp.json()
            tracks = []
            for item in data:
                if len(tracks) >= limit:
                    break
                video_id = item.get('videoId', '')
                if not video_id:
                    continue

                # Get best thumbnail
                thumbs = item.get('videoThumbnails', [])
                thumb_url = thumbs[0].get('url', '') if thumbs else ''

                tracks.append({
                    'title': item.get('title', 'Unknown'),
                    'artist': item.get('author', 'Unknown'),
                    'thumbnailUrl': thumb_url,
                    'videoId': video_id,
                    'duration': item.get('lengthSeconds', 0),
                    'views': item.get('viewCount', 0),
                })

            if tracks:
                print(f'[Invidious] Search OK via {instance}: {len(tracks)} results')
                return tracks

        except Exception as e:
            print(f'[Invidious] Search failed on {instance}: {e}')
            _mark_unhealthy(instance)

    return []


def stream_invidious(video_id):
    """Get audio stream URL via Invidious API."""
    for instance in _healthy_instances(INVIDIOUS_INSTANCES):
        try:
            url = f'{instance}/api/v1/videos/{video_id}'
            resp = _http_get(url)
            if resp.status_code != 200:
                _mark_unhealthy(instance)
                continue

            data = resp.json()

            # Try adaptiveFormats for audio-only streams
            for fmt in data.get('adaptiveFormats', []):
                mime = fmt.get('type', '')
                if 'audio' in mime:
                    stream_url = fmt.get('url', '')
                    if stream_url:
                        print(f'[Invidious] Stream OK via {instance}')
                        return {
                            'audioUrl': stream_url,
                            'quality': fmt.get('audioQuality', ''),
                            'format': fmt.get('container', ''),
                            'bitrate': int(fmt.get('bitrate', '0')),
                            'mimeType': mime,
                            'duration': data.get('lengthSeconds', 0),
                            'title': data.get('title', ''),
                            'artist': data.get('author', ''),
                            'thumbnailUrl': '',
                        }

        except Exception as e:
            print(f'[Invidious] Stream failed on {instance}: {e}')
            _mark_unhealthy(instance)

    return None


# ===========================================================================
# Tier 3 — yt-dlp CLI
# ===========================================================================

def search_ytdlp(query, limit=6):
    """Search via yt-dlp CLI (slowest but most reliable)."""
    try:
        cmd = [
            'yt-dlp', '--dump-json', '--no-warnings',
            '--no-playlist', '--flat-playlist',
            f'ytsearch{limit}:{query}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        tracks = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('{'):
                data = json.loads(line)
                tracks.append({
                    'title': data.get('title', 'Unknown'),
                    'artist': data.get('uploader', data.get('channel', 'Unknown')),
                    'thumbnailUrl': data.get('thumbnail', ''),
                    'videoId': data.get('id', ''),
                    'duration': data.get('duration', 0),
                    'views': data.get('view_count', 0),
                })
                if len(tracks) >= limit:
                    break

        if tracks:
            print(f'[yt-dlp] Search OK: {len(tracks)} results')
            return tracks

    except Exception as e:
        print(f'[yt-dlp] Search failed: {e}')

    return []


def stream_ytdlp(video_id):
    """Get audio stream URL via yt-dlp CLI."""
    try:
        cmd = [
            'yt-dlp', '--get-url', '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '--no-warnings',
            f'https://www.youtube.com/watch?v={video_id}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        for line in result.stdout.strip().split('\n'):
            if line.startswith('http'):
                print(f'[yt-dlp] Stream OK')
                return {'audioUrl': line}

    except Exception as e:
        print(f'[yt-dlp] Stream failed: {e}')

    return None


# ===========================================================================
# Unified fallback chain
# ===========================================================================

def search_all(query, limit=10):
    """Search with full fallback: Piped → Invidious → yt-dlp → local."""
    cache_key = f'search:{query}:{limit}'
    if cache_key in _search_cache:
        return _search_cache[cache_key]

    # Tier 1: Piped
    tracks = search_piped(query, limit)
    if tracks:
        _search_cache[cache_key] = tracks
        return tracks

    # Tier 2: Invidious
    tracks = search_invidious(query, limit)
    if tracks:
        _search_cache[cache_key] = tracks
        return tracks

    # Tier 3: yt-dlp
    tracks = search_ytdlp(query, limit)
    if tracks:
        _search_cache[cache_key] = tracks
        return tracks

    return []


def stream_all(video_id, quality='medium'):
    """Get stream URL with full fallback: Piped → Invidious → yt-dlp."""
    cache_key = f'stream:{video_id}:{quality}'
    if cache_key in _stream_cache:
        return _stream_cache[cache_key]

    # Tier 1: Piped
    result = stream_piped(video_id, quality)
    if result:
        _stream_cache[cache_key] = result
        return result

    # Tier 2: Invidious
    result = stream_invidious(video_id)
    if result:
        _stream_cache[cache_key] = result
        return result

    # Tier 3: yt-dlp
    result = stream_ytdlp(video_id)
    if result:
        _stream_cache[cache_key] = result
        return result

    return {'audioUrl': FALLBACK_AUDIO}


# ===========================================================================
# Flask routes
# ===========================================================================

@app.route('/')
def index():
    return jsonify({
        'name': 'SoundWave API v10',
        'method': 'Multi-source fallback (Piped → Invidious → yt-dlp)',
        'endpoints': ['/search', '/stream/<id>', '/trending', '/suggestions', '/health'],
    })


@app.route('/search')
def search():
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 10))

    if not query:
        return jsonify({'tracks': LOCAL_TRACKS[:limit]})

    tracks = search_all(query, limit)
    if tracks:
        return jsonify({'tracks': tracks})

    # Fallback to local filter
    q = query.lower()
    local = [t for t in LOCAL_TRACKS if q in t['title'].lower() or q in t['artist'].lower()]
    return jsonify({'tracks': local if local else LOCAL_TRACKS[:limit]})


@app.route('/stream/<video_id>')
def stream(video_id):
    quality = request.args.get('quality', 'medium')
    result = stream_all(video_id, quality)
    result['videoId'] = video_id
    return jsonify(result)


@app.route('/trending')
def trending():
    region = request.args.get('region', 'IN')
    tracks = trending_piped(region)
    if tracks:
        return jsonify({'tracks': tracks})
    return jsonify({'tracks': LOCAL_TRACKS})


@app.route('/suggestions')
def suggestions():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    return jsonify(suggestions_piped(query))


@app.route('/health')
def health():
    now = time.time()
    piped_status = {
        inst: 'healthy' if now - _unhealthy.get(inst, 0) > UNHEALTHY_COOLDOWN else 'unhealthy'
        for inst in PIPED_INSTANCES
    }
    invidious_status = {
        inst: 'healthy' if now - _unhealthy.get(inst, 0) > UNHEALTHY_COOLDOWN else 'unhealthy'
        for inst in INVIDIOUS_INSTANCES
    }
    return jsonify({
        'status': 'ok',
        'cache': {
            'streams': len(_stream_cache),
            'searches': len(_search_cache),
        },
        'instances': {
            'piped': piped_status,
            'invidious': invidious_status,
        }
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)