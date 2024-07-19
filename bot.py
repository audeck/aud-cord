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
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': False,
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

    # @classmethod
    # async def from_url(self, url, *, loop=None, stream=False):
    #     loop = loop or asyncio.get_event_loop()
    #     data = await loop.run_in_executor(
    #         None,
    #         lambda: ytdl.extract_info(url, download=not stream)
    #     )
    #
    #     if 'entries' in data:
    #         # Take first item from a playlist
    #         for entry in data['entries']:
    #
    #         data = data['entries'][0]
    #
    #     filename = data['url'] if stream else ytdl.prepare_filename(data)
    #     return self(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

    @classmethod
    async def from_data(self, data, *, stream=False):  # ytdl data
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return self(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)


song_queue = []  # List of ytdl data
current_player: YTDLSource = None


async def play_next(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("[ERR]: Called `play_next` while the client is playing; might be a timing issue?")
        return

    if len(song_queue) == 0:
        await ctx.send("End of queue, probably!")

    next_song = song_queue.pop(0)

    # Download the extensive video data
    loop = bot.loop or asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(next_song['url'], download=False)
    )

    player = await YTDLSource.from_data(data, stream=True)

    ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
    await ctx.send(f'Now playing: {player.title}')

    global current_player
    current_player = player


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')


@bot.command(name='play', help='Plays a song')
async def play(ctx, url: str):
    # Download the preview video data (using `process=False`)
    loop = bot.loop or asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(url, download=False, process=False)
    )

    count = 0

    # If we're dealing with a YT playlist
    if 'entries' in data:
        for entry in data['entries']:
            # Happens when ytdl fails to fetch the video data (usually private videos, etc.)
            if entry is None:
                continue
            song_queue.append(entry)
            count += 1
    else:
        song_queue.append(data)
        count += 1

    if not ctx.voice_client.is_playing():
        await play_next(ctx)
    else:
        await ctx.send(f"Added {count} song(s) to the queue!")


@bot.command(name='join', help='Joins the voice channel')
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send("You are not connected to a voice channel!")
        return
    else:
        channel = ctx.message.author.voice.channel
    await channel.connect()


@bot.command(name='leave', help='Leaves the voice channel')
async def leave(ctx):
    await ctx.voice_client.disconnect()

    global song_queue
    song_queue = []


@bot.command(name='stop', help='Stops the song')
async def stop(ctx):
    ctx.voice_client.stop()


@bot.command(name='move', help='Moves the song to the desired position in the queue.')
async def move(ctx, what: int, where: int):
    if what != where:
        moved = song_queue.pop(what - 1)
        song_queue.insert(where - 2 if what < where else 1, moved)
    await ctx.send(f"Moved {moved.get('title')} to [{where}].")


@bot.command(name='pause', help='Pauses the song.')
async def pause(ctx):
    ctx.voice_client.pause()


@bot.command(name='resume', help='Resumes the song.')
async def resume(ctx):
    ctx.voice_client.resume()


@bot.command(name='queue', help='Lists the current queue.')
async def queue(ctx):
    if not song_queue:
        await ctx.send("The song queue is empty!")
    else:
        queue_list = "\n".join(f"[{i+1}] {item.get('title')}" for i, item in enumerate(song_queue))
        await ctx.send(f"Songs in queue:\n{queue_list}")


@bot.command(name='next', help='Plays the next song.')
async def next(ctx):
    ctx.voice_client.stop()
    await play_next(ctx)


@bot.command(name='skip', help='Plays the next song.')
async def skip(ctx):
    ctx.voice_client.stop()
    await play_next(ctx)


@bot.command(name='remove', help='Removes a song from the queue at a given index')
async def remove(ctx, index: int):
    try:
        removed_song = song_queue.pop(index - 1)  # User indices are 1-based
        await ctx.send(f"Removed song: {removed_song.get('title')}.")
    except IndexError:
        await ctx.send(f"No song at index {index}!")


@bot.command(name='is_playing', help='IDK')
async def is_playing(ctx):
    if ctx.voice_client.is_playing():
        await ctx.send("Playing!")
    else:
        await ctx.send("Not playing!")


@bot.command(name='np', help='Displays the currently playing song.')
async def np(ctx):
    await ctx.send(f'Now playing: {current_player.title}')


@play.before_invoke
async def ensure_voice(ctx):
    if ctx.voice_client is None:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")


if __name__ == "__main__":
    bot.run('TOKEN_HERE')
