import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio
from urllib.parse import urlparse
from typing import Dict, List

# TODO: Clean up shit code

# Suppress noise about console usage from errors
youtube_dl.utils.bug_reports_message = lambda: ""

ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,  # LIES!
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": False,
    "no_warnings": True,
    "default_search": "auto",
    # Bind to ipv4 since ipv6 addresses cause issues sometimes
    "source_address": "0.0.0.0"
}

ffmpeg_options = {
    "options": "-vn"
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


class YTDLPlayer(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)

        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def from_data(self, data, *, stream=False):  # data is ytdl data
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        return self(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


song_queue = []  # List of `{"url": <yt_url>, "title": <title>}`
current_player: YTDLPlayer = None


def is_url(string: str) -> bool:
    result = urlparse(string)
    return all([result.scheme, result.netloc])


async def play_next(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("[ERR]: Called `play_next` while the client is playing; might be a timing issue?")
        return

    if len(song_queue) == 0:
        await ctx.send("End of queue, probably!")
        return

    next_song = song_queue.pop(0)
    data = ytdl.extract_info(next_song["url"], download=False)

    # This happens if the video is private (probably)
    if data is None:
        await play_next(ctx)
        return

    player = await YTDLPlayer.from_data(data, stream=True)

    ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
    await ctx.send(f"Now playing: {player.title}")

    global current_player
    current_player = player


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")


def ytdl_to_song_data(ytdl_object) -> Dict[str, str]:
    url = ytdl_object.get("original_url")
    title = ytdl_object.get("title")

    if url is None or title is None:
        raise ValueError("Cannot extract ytdl data.")

    return {"url": url, "title": title}


async def get_video_data_by_name(name: str) -> List[Dict[str, str]]:
    # This has to be processed to get the actual video data, which means we do
    # more work than necessary (since ytdl also gets the stream URLs, etc.).
    # Maybe more digging through the yt-dlp docs would yield results, but alas.
    search_results = ytdl.extract_info(f"ytsearch:{name}", download=False)

    if "entries" not in search_results or len(search_results["entries"]) == 0:
        return [None]

    return [search_results["entries"][0]]


async def get_video_data_by_url(url: str) -> List[Dict[str, str]]:
    # This does NOT have to be processed, as we have the original URL and
    # yt-dlp is so kind to also include the title. If we need more data in
    # the future, this may need to be processed as well. However, processing
    # would be costly especially for playlists.
    data = ytdl.extract_info(url, download=False, process=False)

    # data["entries"] should be a list for playlists(?)
    return data.get("entries", [data])


@bot.command(name="play", help="Plays a song")
async def play(ctx, url: str, *args):
    if not is_url(url):
        song_name = f"{url} {' '.join(args)}"
        data = await get_video_data_by_name(song_name)
    else:
        data = await get_video_data_by_url(url)

    title = ""
    count = 0

    for video in data:
        song_data = ytdl_to_song_data(video)
        song_queue.append(song_data)
        count += 1
        title = song_data.get("title")

    if not ctx.voice_client.is_playing():
        await play_next(ctx)
        return

    message = (
        f"Added \"{title}\" to the queue!" if count == 1 else
        f"Added {count} song(s) to the queue!")
    await ctx.send(message)


@bot.command(name="join", help="Joins the voice channel")
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send("You are not connected to a voice channel!")
        return
    else:
        channel = ctx.message.author.voice.channel
    await channel.connect()


@bot.command(name="leave", help="Leaves the voice channel")
async def leave(ctx):
    await ctx.voice_client.disconnect()

    global song_queue
    song_queue = []


@bot.command(name="stop", help="Stops the song")
async def stop(ctx):
    ctx.voice_client.stop()


@bot.command(name="move", help="Moves the song to the desired position in the queue.")
async def move(ctx, what: int, where: int):
    if what != where:
        moved = song_queue.pop(what - 1)
        song_queue.insert(where - (2 if what < where else 1), moved)
    await ctx.send(f"Moved {moved.get('title')} to [{where}].")


@bot.command(name="pause", help="Pauses the song.")
async def pause(ctx):
    ctx.voice_client.pause()


@bot.command(name="resume", help="Resumes the song.")
async def resume(ctx):
    ctx.voice_client.resume()


# TODO: Paging
@bot.command(name="queue", help="Lists the current queue.")
async def queue(ctx):
    if not song_queue:
        await ctx.send("The song queue is empty!")
    else:
        queue_list = "\n".join(f"[{i+1}] {item.get('title')}" for i, item in enumerate(song_queue))
        if len(queue_list) > 1900:
            queue_list = queue_list[:1900] + "... (and more songs!)"
        await ctx.send(f"Songs in queue:\n{queue_list}")


@bot.command(name="next", help="Plays the next song.")
async def next(ctx):
    ctx.voice_client.stop()
    await play_next(ctx)


@bot.command(name="skip", help="Plays the next song.")
async def skip(ctx):
    ctx.voice_client.stop()
    await play_next(ctx)


@bot.command(name="remove", help="Removes a song from the queue at a given index")
async def remove(ctx, index: int):
    try:
        removed_song = song_queue.pop(index - 1)  # User indices are 1-based
        await ctx.send(f"Removed song: {removed_song.get('title')}.")
    except IndexError:
        await ctx.send(f"No song at index {index}!")


@bot.command(name="is_playing", help="IDK")
async def is_playing(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("Playing!")
    else:
        await ctx.send("Not playing!")


@bot.command(name="np", help="Displays the currently playing song.")
async def np(ctx):
    await ctx.send(f"Now playing: {current_player.title}")


@play.before_invoke
async def ensure_voice(ctx):
    if ctx.voice_client is None:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")


if __name__ == "__main__":
    bot.run("TOKEN HERE")
