import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio

# Suppress noise about console usage from errors
youtube_dl.utils.bug_reports_message = lambda: ''

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    # Bind to ipv4 since ipv6 addresses cause issues sometimes
    'source_address': '0.0.0.0'
}

ffmpeg_options = {
    'options': '-vn'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)

        self.data = data

        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(url, download=not stream)
        )

        if 'entries' in data:
            # Take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Queue to hold the songs
song_queue = asyncio.Queue()


async def play_next(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("[ERR]: Called `play_next` while the client is playing; might be a timing issue?")
        return

    try:
        next_song = await song_queue.get()
        player = await YTDLSource.from_url(next_song, loop=bot.loop, stream=True)
        ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        await ctx.send(f'Now playing: {player.title}')
    except asyncio.QueueEmpty:
        await ctx.send("End of queue, probably!")
        pass


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')


@bot.command(name='play', help='Plays a song')
async def play(ctx, url: str, idx: int = None):
    # if idx is not None:
    #     # TODO: Doesn't work
    #     idx -= 1  # User indices are 1-based
    #     idx = max(min(0, idx), song_queue.qsize())  # Normalize index
    #     song_queue._queue.insert(idx, url)
    # else:
    await song_queue.put(url)

    if not ctx.voice_client.is_playing():
        await ctx.send("Not playing, playing next song instantly.")
        await play_next(ctx)
    else:
        await ctx.send('Song added to the queue.')


@bot.command(name='join', help='Joins the voice channel')
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send("You are not connected to a voice channel")
        return
    else:
        channel = ctx.message.author.voice.channel
    await channel.connect()


@bot.command(name='leave', help='Leaves the voice channel')
async def leave(ctx):
    await ctx.voice_client.disconnect()
    song_queue._queue.clear()


@bot.command(name='stop', help='Stops the song')
async def stop(ctx):
    ctx.voice_client.stop()


@bot.command(name='pause', help='Pauses the song')
async def pause(ctx):
    ctx.voice_client.pause()


@bot.command(name='resume', help='Resumes the song')
async def resume(ctx):
    ctx.voice_client.resume()


@bot.command(name='queue', help='Lists the current queue')
async def queue(ctx):
    songs = list(song_queue._queue)
    if not songs:
        await ctx.send("The song queue is empty!")
    else:
        queue_list = "\n".join(f"[{i+1}] {item}" for i, item in enumerate(songs))
        await ctx.send(f"Songs in queue:\n{queue_list}")


@bot.command(name='next', help='Plays the next song')
async def next(ctx):
    ctx.voice_client.stop()
    await play_next(ctx)


@bot.command(name='remove', help='Removes a song from the queue at a given index')
async def remove(ctx, index: int):
    await ctx.send("Doesn't work.")
    # TODO: Doesn't work. Using the underlying `_queue` breaks the instance.
    # try:
    #     removed_song = song_queue._queue[index - 1]  # User indices are 1-based
    #     del song_queue._queue[index - 1]
    #     await ctx.send(f"Removed song: {removed_song}")
    # except IndexError:
    #     await ctx.send(f"No song at index {index}")


@bot.command(name='is_playing', help='IDK')
async def is_playing(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("Playing!")
    else:
        await ctx.send("Not playing!")


@play.before_invoke
async def ensure_voice(ctx):
    if ctx.voice_client is None:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")


if __name__ == "__main__":
    bot.run('TOKEN_GOES_HERE')
