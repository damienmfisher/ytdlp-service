import os
import tempfile
import requests
from flask import Flask, request, jsonify
from supabase import create_client
import yt_dlp

app = Flask(__name__)

# Environment variables
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
API_SECRET = os.environ.get('API_SECRET')
PROXY_URL = os.environ.get('PROXY_URL')

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'proxy_configured': bool(PROXY_URL),
        'supabase_configured': bool(SUPABASE_URL and SUPABASE_KEY)
    })

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    
    # Validate secret
    if data.get('secret') != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    url = data.get('url')
    media_type = data.get('type', 'audio')  # 'audio' or 'video'
    asset_id = data.get('asset_id')
    artist_id = data.get('artist_id')
    callback_url = data.get('callback_url')
    
    if not url or not asset_id:
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # yt-dlp options with mweb extractor for better YouTube compatibility
            ydl_opts = {
                'format': 'bestaudio/best' if media_type == 'audio' else 'best[height<=480]/worst',
                'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                'quiet': False,
                'no_warnings': False,
                'retries': 15,
                'fragment_retries': 15,
                'file_access_retries': 10,
                'extractor_retries': 10,
                'socket_timeout': 120,
                'sleep_interval': 3,
                'max_sleep_interval': 10,
                'sleep_interval_requests': 2,
                'geo_bypass': True,
                'geo_bypass_country': 'US',
                'nocheckcertificate': True,
                # Use mweb (mobile web) player - often less restricted
                'extractor_args': {
                    'youtube': {
                        'player_client': ['mweb', 'android', 'ios'],
                        'player_skip': ['webpage', 'configs'],
                    }
                },
                # Mobile browser headers
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Cache-Control': 'max-age=0',
                },
            }
            
            # Add proxy if configured
            if PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL
                print(f"Using proxy: {PROXY_URL[:30]}...")
            
            # Add audio extraction for audio type
            if media_type == 'audio':
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            
            # Download with yt-dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                
                # Find downloaded file
                downloaded_file = None
                for f in os.listdir(tmpdir):
                    downloaded_file = os.path.join(tmpdir, f)
                    break
                
                if not downloaded_file:
                    raise Exception("No file downloaded")
                
                # Determine file extension and bucket
                ext = os.path.splitext(downloaded_file)[1].lower()
                if media_type == 'audio':
                    bucket = 'audio-files'
                    storage_path = f"{artist_id}/{asset_id}.mp3"
                else:
                    bucket = 'media-assets'
                    storage_path = f"{artist_id}/videos/{asset_id}{ext}"
                
                # Upload to Supabase Storage
                with open(downloaded_file, 'rb') as f:
                    file_data = f.read()
                
                supabase.storage.from_(bucket).upload(
                    storage_path,
                    file_data,
                    {'content-type': 'audio/mpeg' if media_type == 'audio' else 'video/mp4'}
                )
                
                # Get public URL
                public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
                
                # Send callback with results
                if callback_url:
                    callback_data = {
                        'asset_id': asset_id,
                        'status': 'ready',
                        'asset_url': public_url,
                        'title': title,
                        'duration': duration,
                        'secret': API_SECRET
                    }
                    requests.post(callback_url, json=callback_data, timeout=30)
                
                return jsonify({
                    'success': True,
                    'asset_id': asset_id,
                    'title': title,
                    'duration': duration,
                    'url': public_url
                })
                
    except Exception as e:
        error_message = str(e)
        print(f"ERROR: {error_message}")
        
        # Send error callback
        if callback_url:
            callback_data = {
                'asset_id': asset_id,
                'status': 'failed',
                'error_message': error_message,
                'secret': API_SECRET
            }
            try:
                requests.post(callback_url, json=callback_data, timeout=30)
            except:
                pass
        
        return jsonify({'error': error_message, 'assetId': asset_id}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
