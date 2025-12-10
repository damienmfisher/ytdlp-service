import os
import tempfile
import requests
from flask import Flask, request, jsonify
from supabase import create_client, Client
import yt_dlp

app = Flask(__name__)

# Environment variables
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
API_SECRET = os.environ.get('API_SECRET')
PROXY_URL = os.environ.get('PROXY_URL')  # NEW: Oxylabs proxy

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def send_callback(callback_url, data):
    """Send status update to Lovable callback endpoint"""
    try:
        requests.post(callback_url, json=data, timeout=30)
    except Exception as e:
        print(f"Callback failed: {e}")

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "proxy_configured": bool(PROXY_URL)})

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    
    # Validate request
    url = data.get('url')
    media_type = data.get('type', 'audio')  # 'audio' or 'video'
    asset_id = data.get('asset_id')
    artist_id = data.get('artist_id')
    secret = data.get('secret')
    callback_url = data.get('callback_url')
    
    # Validate secret
    if secret != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    if not url or not asset_id:
        return jsonify({"error": "Missing url or asset_id"}), 400
    
    # Send "downloading" status
    if callback_url:
        send_callback(callback_url, {
            "asset_id": asset_id,
            "status": "downloading",
            "secret": API_SECRET
        })
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, 'output')
            
            # Configure yt-dlp options
            ydl_opts = {
                'outtmpl': output_path + '.%(ext)s',
                'quiet': True,
                'no_warnings': True,
            }
            
            # ADD PROXY SUPPORT
            if PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL
                print(f"Using proxy: {PROXY_URL[:30]}...")  # Log partial URL for debugging
            
            if media_type == 'audio':
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
                bucket = 'audio-files'
                extension = 'mp3'
            else:
                ydl_opts.update({
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'merge_output_format': 'mp4',
                })
                bucket = 'media-assets'
                extension = 'mp4'
            
            # Download with yt-dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
            
            # Find the downloaded file
            downloaded_file = output_path + '.' + extension
            if not os.path.exists(downloaded_file):
                # Try to find any file with the output prefix
                for f in os.listdir(tmpdir):
                    if f.startswith('output'):
                        downloaded_file = os.path.join(tmpdir, f)
                        break
            
            if not os.path.exists(downloaded_file):
                raise Exception("Downloaded file not found")
            
            # Upload to Supabase Storage
            storage_path = f"{artist_id}/{asset_id}.{extension}"
            
            with open(downloaded_file, 'rb') as f:
                file_data = f.read()
            
            # Upload to storage
            supabase.storage.from_(bucket).upload(
                storage_path,
                file_data,
                {"content-type": f"{'audio' if media_type == 'audio' else 'video'}/{extension}"}
            )
            
            # Get public URL
            public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
            
            # Send success callback
            if callback_url:
                send_callback(callback_url, {
                    "asset_id": asset_id,
                    "status": "ready",
                    "asset_url": public_url,
                    "title": title,
                    "duration_seconds": duration,
                    "secret": API_SECRET
                })
            
            return jsonify({
                "success": True,
                "asset_id": asset_id,
                "url": public_url,
                "title": title,
                "duration": duration
            })
            
    except Exception as e:
        error_msg = str(e)
        print(f"Download error: {error_msg}")
        
        # Send failure callback
        if callback_url:
            send_callback(callback_url, {
                "asset_id": asset_id,
                "status": "failed",
                "error_message": error_msg,
                "secret": API_SECRET
            })
        
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
