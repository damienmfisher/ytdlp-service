from flask import Flask, request, jsonify
import yt_dlp
import os
import tempfile
import traceback
from supabase import create_client

app = Flask(__name__)

# Initialize Supabase client
supabase_url = os.environ.get('SUPABASE_URL')
supabase_key = os.environ.get('SUPABASE_SERVICE_KEY')
api_secret = os.environ.get('API_SECRET')

supabase = create_client(supabase_url, supabase_key) if supabase_url and supabase_key else None

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'supabase_configured': supabase is not None
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
        "secret": "your-api-secret"
    }
    """
    try:
        data = request.json
        
        # Validate secret
        if data.get('secret') != api_secret:
            return jsonify({'error': 'Unauthorized'}), 401
        
        url = data.get('url')
        media_type = data.get('type', 'audio')  # 'audio' or 'video'
        asset_id = data.get('asset_id')
        artist_id = data.get('artist_id')
        
        if not url or not asset_id or not artist_id:
            return jsonify({'error': 'Missing required fields: url, asset_id, artist_id'}), 400
        
        if not supabase:
            return jsonify({'error': 'Supabase not configured'}), 500
        
        print(f"📥 Starting download: {url} as {media_type}")
        
        # Update status to downloading
        supabase.table('media_assets').update({
            'status': 'downloading'
        }).eq('id', asset_id).execute()
        
        # Create temp directory for this download
        with tempfile.TemporaryDirectory() as temp_dir:
            output_template = os.path.join(temp_dir, f'{asset_id}.%(ext)s')
            
            # Configure yt-dlp options based on media type
            if media_type == 'audio':
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
                bucket = 'audio-files'
                asset_type = 'audio'
            else:
                ydl_opts = {
                    'format': 'best[height<=1080][ext=mp4]/best[height<=1080]/best',
                    'outtmpl': output_template,
                    'quiet': False,
                    'no_warnings': False,
                }
                expected_ext = 'mp4'
                bucket = 'media-assets'
                asset_type = 'video'
            
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
                # Look for any file with our asset_id
                for f in os.listdir(temp_dir):
                    if f.startswith(asset_id):
                        downloaded_file = os.path.join(temp_dir, f)
                        # Update extension based on actual file
                        expected_ext = f.split('.')[-1]
                        break
            
            if not os.path.exists(downloaded_file):
                raise Exception(f"Downloaded file not found in {temp_dir}")
            
            file_size = os.path.getsize(downloaded_file)
            print(f"📁 File size: {file_size / 1024 / 1024:.2f} MB")
            
            # Upload to Supabase Storage
            storage_path = f"{artist_id}/{asset_id}.{expected_ext}"
            
            print(f"☁️ Uploading to {bucket}/{storage_path}")
            
            with open(downloaded_file, 'rb') as f:
                file_data = f.read()
                supabase.storage.from_(bucket).upload(
                    storage_path,
                    file_data,
                    file_options={"content-type": f"{'audio' if media_type == 'audio' else 'video'}/{expected_ext}"}
                )
            
            # Get public URL
            public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
            
            print(f"✅ Uploaded! URL: {public_url}")
            
            # Update media_assets record
            update_data = {
                'asset_url': public_url,
                'title': title[:255] if title else None,  # Truncate long titles
                'duration_seconds': min(duration, 86400) if duration else None,  # Cap at 24 hours
                'status': 'ready',
                'asset_type': asset_type,
                'source_url': url,
                'source_type': 'youtube' if 'youtube' in url or 'youtu.be' in url else 'soundcloud',
                'error_message': None
            }
            
            if thumbnail:
                update_data['thumbnail_url'] = thumbnail
            
            supabase.table('media_assets').update(update_data).eq('id', asset_id).execute()
            
            print(f"✅ Database updated for asset {asset_id}")
            
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
        
        # Update media_assets with error
        if supabase and data.get('asset_id'):
            supabase.table('media_assets').update({
                'status': 'failed',
                'error_message': error_msg
            }).eq('id', data.get('asset_id')).execute()
        
        return jsonify({'error': error_msg}), 500

@app.route('/info', methods=['POST'])
def get_info():
    """
    Get info about a URL without downloading
    
    Expected JSON body:
    {
        "url": "https://youtube.com/watch?v=...",
        "secret": "your-api-secret"
    }
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
