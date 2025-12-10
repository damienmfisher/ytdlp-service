# app.py - Updated with aggressive YouTube anti-detection
import os
import tempfile
import uuid
from flask import Flask, request, jsonify
from supabase import create_client
import yt_dlp
import requests

app = Flask(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
API_SECRET = os.environ.get('API_SECRET')
PROXY_URL = os.environ.get('PROXY_URL')  # Oxylabs proxy

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'proxy_configured': bool(PROXY_URL)})

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    media_type = data.get('type', 'audio')
    asset_id = data.get('asset_id')
    artist_id = data.get('artist_id')
    secret = data.get('secret')
    callback_url = data.get('callback_url')
    
    if secret != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not url or not asset_id:
        return jsonify({'error': 'Missing url or asset_id'}), 400
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, '%(id)s.%(ext)s')
            
            # Aggressive anti-detection settings
            ydl_opts = {
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'retries': 15,
                'fragment_retries': 15,
                'file_access_retries': 10,
                'extractor_retries': 10,
                'socket_timeout': 90,
                'sleep_interval': 2,
                'max_sleep_interval': 8,
                'sleep_interval_requests': 1,
                'sleep_interval_subtitles': 2,
                # Critical: Multiple player client fallbacks
                'extractor_args': {
                    'youtube': {
                        'player_client': ['ios', 'android', 'web'],
                        'player_skip': ['webpage', 'configs'],
                    }
                },
                # Browser-like headers
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                },
                'geo_bypass': True,
                'geo_bypass_country': 'US',
                'age_limit': None,
                'nocheckcertificate': True,
            }
            
            # Add proxy if configured
            if PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL
            
            # Format selection based on type
            if media_type == 'audio':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
                bucket = 'audio-files'
                ext = 'mp3'
            else:
                ydl_opts['format'] = 'best[height<=720][ext=mp4]/best[height<=720]/best'
                ydl_opts['merge_output_format'] = 'mp4'
                bucket = 'media-assets'
                ext = 'mp4'
            
            # Download with yt-dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                
                # Find downloaded file
                downloaded_file = None
                for f in os.listdir(tmpdir):
                    if f.endswith(f'.{ext}'):
                        downloaded_file = os.path.join(tmpdir, f)
                        break
                
                if not downloaded_file:
                    for f in os.listdir(tmpdir):
                        downloaded_file = os.path.join(tmpdir, f)
                        break
                
                if not downloaded_file:
                    raise Exception('No file downloaded')
                
                # Upload to Supabase Storage
                storage_path = f"{artist_id}/{asset_id}.{ext}"
                with open(downloaded_file, 'rb') as f:
                    file_data = f.read()
                
                supabase.storage.from_(bucket).upload(
                    storage_path,
                    file_data,
                    {'content-type': f'{"audio" if media_type == "audio" else "video"}/{ext}'}
                )
                
                public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
                
                # Send callback
                if callback_url:
                    requests.post(callback_url, json={
                        'asset_id': asset_id,
                        'status': 'ready',
                        'asset_url': public_url,
                        'title': title,
                        'duration_seconds': duration,
                        'secret': API_SECRET
                    }, timeout=30)
                
                return jsonify({
                    'success': True,
                    'asset_id': asset_id,
                    'url': public_url,
                    'title': title,
                    'duration': duration
                })
                
    except Exception as e:
        error_msg = str(e)
        # Send failure callback
        if callback_url:
            try:
                requests.post(callback_url, json={
                    'asset_id': asset_id,
                    'status': 'failed',
                    'error_message': error_msg,
                    'secret': API_SECRET
                }, timeout=30)
            except:
                pass
        
        return jsonify({'error': error_msg, 'asset_id': asset_id}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
