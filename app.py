from flask import Flask, request, jsonify
import yt_dlp
import os
import requests
import tempfile
import logging
from supabase import create_client, Client

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
API_SECRET = os.environ.get('API_SECRET')
PROXY_URL = os.environ.get('PROXY_URL')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': '2.1.0-ios-mweb'})

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    media_type = data.get('type', 'audio')
    asset_id = data.get('asset_id')
    artist_id = data.get('artist_id')
    secret = data.get('secret')
    callback_url = data.get('callback_url')
    upload_url = data.get('upload_url')
    
    if secret != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Latest working config for Dec 2024 - ios,mweb clients
            ydl_opts = {
               'format': 'best/bestvideo+bestaudio' if media_type == 'video' else 'bestaudio/best',
                'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                'quiet': False,
                'no_warnings': False,
                'ignoreerrors': False,
                'retries': 10,
                'fragment_retries': 10,
                'file_access_retries': 5,
                'extractor_retries': 5,
                'socket_timeout': 60,
                'http_chunk_size': 10485760,
                # CRITICAL: Use ios,mweb as per latest yt-dlp fix
                'extractor_args': {
                    'youtube': {
                        'player_client': ['ios', 'mweb'],
                        'player_skip': ['webpage', 'configs'],
                    }
                },
                'http_headers': {
                    'User-Agent': 'com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)',
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                },
            }
            
            if PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL
                logger.info(f"Using proxy: {PROXY_URL[:30]}...")
            
            if media_type == 'audio':
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                
                # Find downloaded file
                ext = 'mp3' if media_type == 'audio' else info.get('ext', 'mp4')
                filename = os.path.join(tmpdir, f"{info['id']}.{ext}")
                
                if not os.path.exists(filename):
                    for f in os.listdir(tmpdir):
                        if f.startswith(info['id']):
                            filename = os.path.join(tmpdir, f)
                            break
                
                # Upload to Supabase Storage via signed URL
                with open(filename, 'rb') as f:
                    file_data = f.read()
                
                upload_response = requests.put(
                    upload_url,
                    data=file_data,
                    headers={'Content-Type': 'video/mp4' if media_type == 'video' else 'audio/mpeg'}
                )
                
                if upload_response.status_code not in [200, 201]:
                    raise Exception(f"Upload failed: {upload_response.status_code}")
                
                # Construct public URL
                bucket = 'media-assets' if media_type == 'video' else 'audio-files'
                file_path = f"{artist_id}/{asset_id}.{'mp4' if media_type == 'video' else 'mp3'}"
                asset_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{file_path}"
                
                # Send callback
                if callback_url:
                    requests.post(callback_url, json={
                        'asset_id': asset_id,
                        'status': 'ready',
                        'asset_url': asset_url,
                        'title': title,
                        'duration': duration,
                        'secret': secret
                    }, timeout=30)
                
                return jsonify({
                    'success': True,
                    'asset_id': asset_id,
                    'title': title,
                    'duration': duration
                })
                
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download failed: {error_msg}")
        
        # Send failure callback
        if callback_url:
            try:
                requests.post(callback_url, json={
                    'asset_id': asset_id,
                    'status': 'failed',
                    'error_message': error_msg,
                    'secret': secret
                }, timeout=30)
            except:
                pass
        
        return jsonify({'error': error_msg, 'assetId': asset_id}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
