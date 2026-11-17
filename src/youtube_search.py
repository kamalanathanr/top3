"""
YouTube search and video streaming functionality
"""

from yt_dlp import YoutubeDL

def search_youtube_videos(query: str, max_results: int = 5) -> list:
    """Search YouTube videos using yt-dlp"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
        }
        with YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{max_results}:{query}"
            results = ydl.extract_info(search_query, download=False)
            videos = []
            for video in results.get('entries', []):
                video_id = video.get('id')
                thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                videos.append({
                    'id': video_id,
                    'title': video.get('title'),
                    'thumbnail': thumbnail,
                    'duration': video.get('duration', 0)
                })
            return videos
    except Exception as e:
        print(f"Error searching YouTube: {e}")
        return []

async def get_video_stream(video_id: str) -> dict:
    """Get video stream URL from YouTube"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'best[ext=mp4]'
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return {
                'url': info.get('url'),
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail')
            }
    except Exception as e:
        print(f"Error getting video: {e}")
        return {'error': str(e)}
