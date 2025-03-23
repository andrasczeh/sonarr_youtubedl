import logging
import yt_dlp
ytdlopts = {
            'ignoreerrors': True,
            'matchtitle': "Pingu at School - Pingu Official Channel",
            'quiet': True,

        }

result = yt_dlp.YoutubeDL(ytdlopts).extract_info("https://www.youtube.com/channel/UCM88mtSE0zRTn5ae4EbYcuw/videos", download=False)

log = logging.getLogger('sonarr_youtubedl')

log.info(result)