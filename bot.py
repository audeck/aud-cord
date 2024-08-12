import discord
from discord.ext import commands
import yt_dlp
import asyncio
from urllib.parse import urlparse
from typing import Dict, List
import lyricsgenius

# TODO: Clean up shit code

# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda: ""

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
    "source_address": "0.0.0.0",
}

ffmpeg_options = {
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


class YTDLPlayer(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, song_name,volume=0.5):
        super().__init__(source, volume)

        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")
        self.song_name = song_name

    @classmethod
    async def from_data(self, data, *, stream=False,song_name):  # data is ytdl data
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        return self(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data,song_name=song_name)


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)


song_queue = []  # List of `{"url": <yt_url>, "title": <title>, "song_name": <song_name|"">}`
current_player: YTDLPlayer = None

lyrics_token = "Jt8q6PJwaBEU4KNPrlUoYGsnIntE30L0paNxpkecOcJv4JuyRMAdruvpAgLukcsCpyYOGeqBL7sCRQW_iVU1AQ"
genius = lyricsgenius.Genius("your-genius-api-token")

def is_url(string: str) -> bool:
    result = urlparse(string)
    return all([result.scheme, result.netloc])


async def play_next(ctx):
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        return  # `voice_client` should have an `after` lambda attached already
        # await ctx.send("[ERR]: Called `play_next` while the client is playing; might be a timing issue?")
        # return

    if len(song_queue) == 0:
        await ctx.send("End of queue, probably!")
        return

    next_song = song_queue.pop(0)
    data = ytdl.extract_info(next_song["url"], download=False)

    # This happens if the video is private (probably)
    if data is None:
        await play_next(ctx)
        return

    player = await YTDLPlayer.from_data(data, stream=True,song_name=next_song["song_name"])

    ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
    await ctx.send(f"Now playing: {player.title}")

    global current_player
    current_player = player


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

def split_lyrics(lyrics, max_length=2000):
    # Split the lyrics into chunks of max_length characters
    chunks = []
    while len(lyrics) > max_length:
        # Find the last newline before the max_length limit
        split_index = lyrics.rfind('\n', 0, max_length)
        if split_index == -1:  # No newline found, split at max_length
            split_index = max_length
        chunks.append(lyrics[:split_index])
        lyrics = lyrics[split_index:]  # Remove the split part and any leading whitespace

    chunks.append(lyrics)  # Add the remaining lyrics as the last chunk
    return chunks

@bot.command(name="lyrics_search", help="Given song name will try to print its lyrics.")
async def display_lyrics_by_name(ctx,*args):
    song = genius.search_song(" ".join(args))
    if(song == None):
        await ctx.send("I'm sorry, I could find the lyrics.\nTry different name...")
    else:
        chunks = split_lyrics(song.lyrics)
        for chunk in chunks:
            await ctx.send(chunk)
    return


@bot.command(name="lyrics", help="3=========D")
async def display_lyrics(ctx):
    song = genius.search_song(current_player.song_name)
    if(song == None):
        song = genius.search_song(current_player.title)
    if(song == None):
        await ctx.send("I'm sorry, I could find the lyrics.\nTry to search for the lyrics by name with command:\n*!lyrics_search <name of the song>*")
        return
    chunks = split_lyrics(song.lyrics)
    for chunk in chunks:
        await ctx.send(chunk)



def ytdl_to_song_data(ytdl_object) -> Dict[str, str]:
    # Single videos extracted using yt-dlp contain `original_url`, while
    # playlist videos contain `url`.
    url = ytdl_object.get("original_url", ytdl_object.get("url"))
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


# TODO: There are better ways of getting just the title for single videos
async def get_video_data_by_url(url: str) -> List[Dict[str, str]]:
    # This does NOT have to be processed, as we have the original URL and
    # yt-dlp is so kind to also include the title. If we need more data in
    # the future, this may need to be processed as well. However, processing
    # would be costly especially for playlists.
    data = ytdl.extract_info(url, download=False, process=False)

    # Should be a playlist
    if data.get("entries") is not None:
        # `entries` is a `YoutubeTabBaseInfoExtractor._entries` generator
        return list(data.get("entries"))

    return [data]


@bot.command(name="play", help="Plays a song")
async def play(ctx, url: str, *args):
    if not is_url(url):
        song_name = f"{url} {' '.join(args)}"
        data = await get_video_data_by_name(song_name)
    else:
        song_name = ""
        data = await get_video_data_by_url(url)

    title = ""
    count = 0

    for video in data:
        print("\n")
        print("\n")
        # print(video)
        song_data = ytdl_to_song_data(video)
        song_data["song_name"] = song_name
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
        # `what` and `where` are 1-based
        moved = song_queue[what - 1]
        song_queue.insert(where - 1, song_queue.pop(what - 1))
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
    await play_next(ctx)


@bot.command(name="skip", help="Plays the next song.")
async def skip(ctx):
    await play_next(ctx)


@bot.command(name="remove", help="Removes a song from the queue at a given index")
async def remove(ctx, index: int):
    try:
        removed_song = song_queue.pop(index - 1)  # User indices are 1-based
        await ctx.send(f"Removed song: {removed_song.get('title')}.")
    except IndexError:
        await ctx.send(f"No song at index {index}!")


@bot.command(name="clear", help="Clears the queue.")
async def clear(ctx):
    global song_queue
    song_queue = []
    await ctx.send("The queue has been cleared!")


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
    bot.run("TOKEN_HERE")
