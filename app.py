import os
import logging
import tempfile
import requests
from flask import Flask, request, jsonify
import yt_dlp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
API_SECRET = os.environ.get('API_SECRET', 'your-secret-key')

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "yt-dlp-downloader"})

@app.route('/download', methods=['POST'])
def download_media():
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['url', 'type', 'asset_id', 'artist_id', 'secret', 'callback_url']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate secret
        if data['secret'] != API_SECRET:
            return jsonify({"error": "Invalid secret"}), 401
        
        url = data['url']
        media_type = data['type']  # 'audio' or 'video'
        asset_id = data['asset_id']
        callback_url = data['callback_url']
        upload_url = data.get('upload_url')
        public_url = data.get('public_url')
        content_type = data.get('content_type', 'video/mp4')
        
        logger.info(f"📥 Starting download: {url} as {media_type}")
        
        # Create temp directory for download
        with tempfile.TemporaryDirectory() as temp_dir:
            output_template = os.path.join(temp_dir, '%(id)s.%(ext)s')
            
            # Configure yt-dlp options - CRITICAL: Use format that works with ios client
            ydl_opts = {
                'outtmpl': output_template,
                'quiet': False,
                'no_warnings': False,
                'extract_flat': False,
                'nocheckcertificate': True,
                'ignoreerrors': False,
                'no_color': True,
                'geo_bypass': True,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['ios', 'mweb'],
                        'player_skip': ['webpage', 'configs'],
                    }
                },
                # Headers to appear as mobile device
                'http_headers': {
                    'User-Agent': 'com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                },
            }
            
            if media_type == 'audio':
                # For audio: extract best audio, convert to mp3
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
            else:
                # For video: Use format that ios client actually provides
                # The ios client returns HLS streams, so we need to let yt-dlp handle it
                ydl_opts.update({
                    # Don't specify format - let yt-dlp pick the best available
                    'format': None,  # This tells yt-dlp to use default (best)
                    'merge_output_format': 'mp4',
                    'postprocessors': [{
                        'key': 'FFmpegVideoConvertor',
                        'preferedformat': 'mp4',
                    }],
                })
            
            # Download the media
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"🔄 Extracting info for: {url}")
                info = ydl.extract_info(url, download=True)
                
                if info is None:
                    raise Exception("Failed to extract video info")
                
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                video_id = info.get('id', 'unknown')
                
                logger.info(f"✅ Downloaded: {title} ({duration}s)")
                
                # Find the downloaded file
                downloaded_file = None
                expected_ext = 'mp3' if media_type == 'audio' else 'mp4'
                
                for file in os.listdir(temp_dir):
                    if file.endswith(f'.{expected_ext}'):
                        downloaded_file = os.path.join(temp_dir, file)
                        break
                
                # If mp4 not found, look for any video file
                if not downloaded_file:
                    for file in os.listdir(temp_dir):
                        if file.endswith(('.mp4', '.mkv', '.webm', '.m4a', '.mp3')):
                            downloaded_file = os.path.join(temp_dir, file)
                            break
                
                if not downloaded_file:
                    files_in_dir = os.listdir(temp_dir)
                    logger.error(f"❌ No output file found. Files in temp dir: {files_in_dir}")
                    raise Exception(f"Download completed but no output file found. Files: {files_in_dir}")
                
                file_size = os.path.getsize(downloaded_file)
                logger.info(f"📁 Output file: {downloaded_file} ({file_size} bytes)")
                
                # Upload to Supabase Storage using signed URL
                if upload_url:
                    logger.info(f"☁️ Uploading to Supabase Storage...")
                    with open(downloaded_file, 'rb') as f:
                        upload_response = requests.put(
                            upload_url,
                            data=f,
                            headers={'Content-Type': content_type}
                        )
                        
                        if upload_response.status_code not in [200, 201]:
                            logger.error(f"❌ Upload failed: {upload_response.status_code} - {upload_response.text}")
                            raise Exception(f"Failed to upload to storage: {upload_response.status_code}")
                        
                        logger.info(f"✅ Upload successful!")
                
                # Send success callback
                callback_data = {
                    'asset_id': asset_id,
                    'status': 'ready',
                    'title': title,
                    'duration_seconds': duration,
                    'asset_url': public_url,
                    'secret': data['secret'],
                }
                
                logger.info(f"📞 Sending callback to: {callback_url}")
                callback_response = requests.post(callback_url, json=callback_data, timeout=30)
                logger.info(f"✅ Callback response: {callback_response.status_code}")
                
                return jsonify({
                    "success": True,
                    "title": title,
                    "duration": duration,
                    "asset_id": asset_id
                })
                
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Error: {error_msg}")
        
        # Send failure callback
        if 'callback_url' in data and 'asset_id' in data:
            try:
                callback_data = {
                    'asset_id': data['asset_id'],
                    'status': 'failed',
                    'error_message': error_msg,
                    'secret': data.get('secret', ''),
                }
                requests.post(data['callback_url'], json=callback_data, timeout=10)
            except Exception as callback_error:
                logger.error(f"❌ Callback error: {callback_error}")
        
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
