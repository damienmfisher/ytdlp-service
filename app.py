import os
import tempfile
import requests
import yt_dlp
from flask import Flask, request, jsonify
from supabase import create_client, Client

app = Flask(__name__)

# Environment variables
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
API_SECRET = os.environ.get('API_SECRET')
PROXY_URL = os.environ.get('PROXY_URL')

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_ydl_opts(media_type='video', proxy=None):
    """Get yt-dlp options with anti-detection settings"""
    
    # Common anti-detection options
    opts = {
        # Proxy configuration
        'proxy': proxy,
        
        # Anti-detection: Use browser-like headers
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        },
        
        # Retry settings
        'retries': 10,
        'fragment_retries': 10,
        'extractor_retries': 5,
        'file_access_retries': 5,
        
        # Timeout settings
        'socket_timeout': 60,
        
        # Anti-bot bypass options
        'sleep_interval': 1,
        'max_sleep_interval': 5,
        'sleep_interval_requests': 1,
        
        # Don't abort on errors, try to continue
        'ignoreerrors': False,
        'no_warnings': False,
        
        # Use age gate bypass
        'age_limit': None,
        
        # Extractor options for YouTube
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],  # Try multiple clients
                'player_skip': ['webpage', 'configs'],  # Skip some checks
            }
        },
        
        # Force IPv4 (some proxies work better with IPv4)
        'source_address': '0.0.0.0',
        
        # Quiet mode for cleaner logs
        'quiet': False,
        'verbose': True,
        'progress': True,
    }
    
    # Format selection based on media type
    if media_type == 'audio':
        opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'extract_audio': True,
        })
    else:  # video
        opts.update({
            # Prefer lower resolution for faster download and less detection
            'format': 'best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best',
        })
    
    return opts


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'proxy_configured': bool(PROXY_URL)})


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json()
    
    # Validate request
    url = data.get('url')
    media_type = data.get('type', 'video')  # 'audio' or 'video'
    asset_id = data.get('asset_id')
    artist_id = data.get('artist_id')
    secret = data.get('secret')
    callback_url = data.get('callback_url')
    
    # Validate API secret
    if secret != API_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not url or not asset_id:
        return jsonify({'error': 'Missing required fields'}), 400
    
    print(f"[Download] Starting download for asset {asset_id}")
    print(f"[Download] URL: {url}")
    print(f"[Download] Type: {media_type}")
    print(f"[Download] Using proxy: {PROXY_URL[:50] if PROXY_URL else 'None'}...")
    
    try:
        # Create temp directory for download
        with tempfile.TemporaryDirectory() as temp_dir:
            # Get yt-dlp options
            ydl_opts = get_ydl_opts(media_type, PROXY_URL)
            ydl_opts['outtmpl'] = os.path.join(temp_dir, '%(title)s.%(ext)s')
            
            print(f"[Download] yt-dlp options configured with proxy: {bool(PROXY_URL)}")
            
            # Download with yt-dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print("[Download] Extracting info...")
                info = ydl.extract_info(url, download=True)
                
                if info is None:
                    raise Exception("Failed to extract video info")
                
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                
                # Find the downloaded file
                downloaded_file = None
                for file in os.listdir(temp_dir):
                    file_path = os.path.join(temp_dir, file)
                    if os.path.isfile(file_path):
                        downloaded_file = file_path
                        break
                
                if not downloaded_file:
                    raise Exception("Downloaded file not found")
                
                print(f"[Download] Downloaded: {downloaded_file}")
                print(f"[Download] Title: {title}, Duration: {duration}s")
                
                # Determine file extension and bucket
                file_ext = os.path.splitext(downloaded_file)[1].lower()
                if media_type == 'audio':
                    bucket = 'audio-files'
                    storage_path = f"{artist_id}/{asset_id}{file_ext}"
                else:
                    bucket = 'media-assets'
                    storage_path = f"{artist_id}/videos/{asset_id}{file_ext}"
                
                # Upload to Supabase Storage
                print(f"[Upload] Uploading to {bucket}/{storage_path}")
                with open(downloaded_file, 'rb') as f:
                    file_data = f.read()
                    
                # Upload file
                supabase.storage.from_(bucket).upload(
                    storage_path,
                    file_data,
                    file_options={"content-type": f"{'audio' if media_type == 'audio' else 'video'}/{file_ext[1:]}"}
                )
                
                # Get public URL
                public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
                print(f"[Upload] Public URL: {public_url}")
                
                # Send callback if URL provided
                if callback_url:
                    callback_data = {
                        'asset_id': asset_id,
                        'status': 'ready',
                        'asset_url': public_url,
                        'title': title,
                        'duration_seconds': duration,
                        'secret': secret
                    }
                    print(f"[Callback] Sending to {callback_url}")
                    requests.post(callback_url, json=callback_data, timeout=30)
                
                return jsonify({
                    'success': True,
                    'asset_id': asset_id,
                    'url': public_url,
                    'title': title,
                    'duration': duration
                })
                
    except Exception as e:
        error_msg = str(e)
        print(f"[Error] Download failed: {error_msg}")
        
        # Send error callback if URL provided
        if callback_url:
            callback_data = {
                'asset_id': asset_id,
                'status': 'failed',
                'error_message': error_msg,
                'secret': secret
            }
            try:
                requests.post(callback_url, json=callback_data, timeout=30)
            except Exception as cb_error:
                print(f"[Error] Callback failed: {cb_error}")
        
        return jsonify({'error': error_msg, 'assetId': asset_id}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
