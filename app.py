from flask import Flask, request, jsonify
import yt_dlp
import os
import tempfile
import traceback
import requests

app = Flask(__name__)

# Only need API secret for authorization
api_secret = os.environ.get('API_SECRET')


def send_callback(callback_url, payload):
    """Send status update to Lovable edge function"""
    try:
        print(f"📞 Sending callback to {callback_url}: {payload.get('status')}")
        response = requests.post(callback_url, json=payload, timeout=30)
        print(f"📞 Callback response: {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Callback failed: {e}")
        return False


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'api_secret_configured': api_secret is not None
    })


@app.route('/download', methods=['POST'])
def download():
    """
    Download audio or video from YouTube/SoundCloud
    
    Expected JSON body:
    {
        "url": "https://youtube.com/watch?v=...",
        "type": "audio" | "video",
        "asset_id": "uuid-of-media-asset",
        "artist_id": "uuid-of-artist",
        "secret": "your-api-secret",
        "callback_url": "https://xxx.supabase.co/functions/v1/media-callback",
        "upload_url": "signed-supabase-upload-url",
        "public_url": "final-public-url-after-upload",
        "content_type": "audio/mpeg" | "video/mp4"
    }
    """
    data = request.json or {}
    callback_url = data.get('callback_url')
    asset_id = data.get('asset_id')
    
    try:
        # Validate secret
        if data.get('secret') != api_secret:
            return jsonify({'error': 'Unauthorized'}), 401
        
        url = data.get('url')
        media_type = data.get('type', 'audio')
        artist_id = data.get('artist_id')
        upload_url = data.get('upload_url')
        public_url = data.get('public_url')
        content_type = data.get('content_type', 'audio/mpeg')
        
        if not url or not asset_id or not artist_id:
            return jsonify({'error': 'Missing required fields: url, asset_id, artist_id'}), 400
        
        if not callback_url:
            return jsonify({'error': 'Missing callback_url'}), 400
        
        if not upload_url or not public_url:
            return jsonify({'error': 'Missing upload_url or public_url'}), 400
        
        print(f"📥 Starting download: {url} as {media_type}")
        
        # Send "downloading" status via callback
        send_callback(callback_url, {
            'asset_id': asset_id,
            'status': 'downloading',
            'secret': api_secret
        })
        
        # Create temp directory for this download
        with tempfile.TemporaryDirectory() as temp_dir:
            # Configure yt-dlp options based on media type
            if media_type == 'audio':
                output_template = os.path.join(temp_dir, f'{asset_id}.%(ext)s')
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': output_template,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'quiet': False,
                    'no_warnings': False,
                }
                expected_ext = 'mp3'
            else:
                output_template = os.path.join(temp_dir, f'{asset_id}.%(ext)s')
                ydl_opts = {
                    'format': 'best[height<=1080][ext=mp4]/best[height<=1080]/best',
                    'outtmpl': output_template,
                    'quiet': False,
                    'no_warnings': False,
                }
                expected_ext = 'mp4'
            
            # Download with yt-dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print(f"🔄 Extracting info from {url}")
                info = ydl.extract_info(url, download=True)
                
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                thumbnail = info.get('thumbnail', None)
                
                print(f"✅ Downloaded: {title} ({duration}s)")
            
            # Find the downloaded file
            downloaded_file = os.path.join(temp_dir, f'{asset_id}.{expected_ext}')
            
            # Check if file exists (might have different extension)
            if not os.path.exists(downloaded_file):
                for f in os.listdir(temp_dir):
                    if f.startswith(asset_id):
                        downloaded_file = os.path.join(temp_dir, f)
                        break
            
            if not os.path.exists(downloaded_file):
                raise Exception(f"Downloaded file not found in {temp_dir}")
            
            file_size = os.path.getsize(downloaded_file)
            print(f"📁 File size: {file_size / 1024 / 1024:.2f} MB")
            
            # Upload to Supabase Storage using signed URL (simple PUT request!)
            print(f"☁️ Uploading via signed URL...")
            
            with open(downloaded_file, 'rb') as f:
                file_data = f.read()
                
                upload_response = requests.put(
                    upload_url,
                    data=file_data,
                    headers={'Content-Type': content_type}
                )
                
                if upload_response.status_code not in [200, 201]:
                    raise Exception(f"Upload failed with status {upload_response.status_code}: {upload_response.text[:200]}")
            
            print(f"✅ Uploaded! Public URL: {public_url}")
            
            # Send SUCCESS callback
            callback_payload = {
                'asset_id': asset_id,
                'status': 'ready',
                'asset_url': public_url,
                'title': title[:255] if title else None,
                'duration': min(duration, 86400) if duration else None,
                'secret': api_secret
            }
            
            if thumbnail:
                callback_payload['thumbnail_url'] = thumbnail
            
            send_callback(callback_url, callback_payload)
            
            print(f"✅ Callback sent for asset {asset_id}")
            
            return jsonify({
                'success': True,
                'url': public_url,
                'title': title,
                'duration': duration,
                'file_size': file_size
            })
    
    except Exception as e:
        error_msg = str(e)[:500]
        print(f"❌ Error: {error_msg}")
        print(traceback.format_exc())
        
        # Send FAILURE callback
        if callback_url and asset_id:
            send_callback(callback_url, {
                'asset_id': asset_id,
                'status': 'failed',
                'error_message': error_msg,
                'secret': api_secret
            })
        
        return jsonify({'error': error_msg}), 500


@app.route('/info', methods=['POST'])
def get_info():
    """
    Get info about a URL without downloading
    """
    try:
        data = request.json
        
        if data.get('secret') != api_secret:
            return jsonify({'error': 'Unauthorized'}), 401
        
        url = data.get('url')
        if not url:
            return jsonify({'error': 'Missing url'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            return jsonify({
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': info.get('uploader'),
                'view_count': info.get('view_count'),
                'formats_available': len(info.get('formats', []))
            })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
