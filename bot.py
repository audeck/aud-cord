import asyncio
import functools
import itertools
import math
import random
import os

import discord
import yt_dlp
import lyricsgenius
from discord.ext import commands
from async_timeout import timeout
from urllib.parse import urlparse

from typing import Dict, List, Optional
from dotenv import load_dotenv

# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda: ""


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        "format": "bestaudio/best",
        "extract_audio": True,
        "audioformat": "mp3",
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

    FFMPEG_OPTIONS = {
        # Try to reconnect on packet transfer error
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn",
    }

    ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: discord.ApplicationContext, source: discord.FFmpegPCMAudio, *, data: Dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel

        self.data = data
        self.title = data.get("title")
        self.url = data.get("webpage_url")
        self.stream_url = data.get("url")
        self.original_name = data.get("original_name")
        self.duration = self.parse_duration(int(data.get("duration")))
        self.thumbnail = data.get("thumbnail")

    def __str__(self):
        return f"**{self.title}**"

    @classmethod
    async def create_source(cls, ctx: discord.ApplicationContext, search_term: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search_term, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(f"Didn't find any matches for `{search_term}`")

        if "entries" not in data:
            process_info = data
        else:
            process_info = None

            for entry in data["entries"]:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(f"Didn't find any matches for `{search_term}`")

        webpage_url = process_info["webpage_url"]
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError(f"Couldn't fetch {webpage_url}")

        if "entries" not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info["entries"].pop(0)
                except IndexError:
                    raise YTDLError(f"Didn't find video at {webpage_url}")

        try:
            cls = cls(ctx, discord.FFmpegPCMAudio(info["url"], **cls.FFMPEG_OPTIONS), data=info)
        except discord.ClientException:
            raise YTDLError("FFmpegPCMAudio subprocess failed to be created. Is one already running?")

        return cls

    # async def from_data(self, data, *, stream=False):  # data is ytdl data
    #     filename = data["url"] if stream else self.ytdl.prepare_filename(data)
    #     return self(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

    @staticmethod
    def parse_duration(duration: int) -> str:
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ("source", "requester")

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        return (discord.Embed(title="Now playing",
                              description=f"```css\n{self.source.title}\n```",
                              color=discord.Color.blurple())
                .add_field(name="Duration", value=self.source.duration)
                .add_field(name="Requested by", value=self.source.requester)
                .add_field(name="URL", value=f"[Click]({self.source.url})")
                .set_thumbnail(url=self.source.thumbnail))


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceError(Exception):
    pass


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: discord.ApplicationContext):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear(self.next)

            if not self.loop or self.current is None:
                try:
                    # Wait for three minutes (180 seconds) while inactive
                    # before leaving the channel for performance reasons.
                    async with timeout(180):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait(self.next)

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set(self.next)

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None

        if self.audio_player:
            self.audio_player.cancel()
            self.audio_player = None


class MusicBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_state = None

    def get_voice_state(self, ctx: discord.ApplicationContext):
        if not self.voice_state:
            self.voice_state = VoiceState(self.bot, ctx)

        return self.voice_state

    def cog_unload(self):
        self.bot.loop.create_task(self.voice_state.stop())

    def cog_check(self, ctx: discord.ApplicationContext):
        if not ctx.guild:
            raise commands.NoPrivateMessage("This command cannot be used in private channels.")

        return True

    async def cog_before_invoke(self, ctx: discord.ApplicationContext):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: discord.ApplicationContext, error: commands.CommandError):
        await ctx.send(f"An error occurred: {str(error)}")

    @commands.slash_command(name="join", invoke_without_subcommand=True)
    async def _join(self, ctx: discord.ApplicationContext):
        """Joins the user's voice channel."""

        destination = ctx.author.voice.channel

        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.slash_command(name="leave", aliases=["disconnect"])
    # @commands.has_permissions(manage_guild=True) TODO: Permissions?
    async def _leave(self, ctx: discord.ApplicationContext):
        """Clears the song queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            await ctx.respond("The bot isn't connected to a voice channel.")
            return

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.slash_command(name="volume")
    async def _volume(self, ctx: discord.ApplicationContext, *, volume: int):
        """Adjusts the bot volume. Accepts values from 0 to 100."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("The bot isn't playing at the moment.")
            return

        if 0 > volume > 100:
            await ctx.respond("The volume value must be between 0 and 100.")
            return

        ctx.voice_state.volume = volume / 100
        await ctx.respond(f"Volume of the player has been set to {volume}")

    @commands.slash_command(name="np")
    async def _np(self, ctx: discord.ApplicationContext):
        """Displays the currently playing song."""

        if ctx.voice_state.current is None:
            await ctx.respond("Nothing is playing right now...")
            return

        await ctx.respond(embed=ctx.voice_state.current.create_embed())

    # TODO: Pausing while paused?
    @commands.slash_command(name="pause")
    async def _pause(self, ctx: discord.ApplicationContext):
        """Pauses the current song."""

        # TODO: This might be wrong
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction("⏯")

    @commands.slash_command(name="resume")
    async def _resume(self, ctx: discord.ApplicationContext):
        """Resumes the current song."""

        # TODO: This might be wrong
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction("⏯")

    @commands.slash_command(name="stop")
    async def _stop(self, ctx: discord.ApplicationContext):
        """Stops playing and clears the queue."""

        # TODO: You can do ephemeral responses
        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction("⏹")

    @commands.slash_command(name="skip")
    async def _skip(self, ctx: discord.ApplicationContext):
        """Skips the current song."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("Not playing any music right now.")
            return

        await ctx.message.add_reaction("⏭")
        ctx.voice_state.skip()

    @commands.slash_command(name="queue")
    async def _queue(self, ctx: discord.ApplicationContext, *, page: int = 1):
        """Shows the player's queue.

        You can optionally specify the page to show.
        Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.respond("The queue is empty queue!")

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ""
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += f"`{i + 1}.` [**{song.source.title}**]({song.source.url})\n"

        embed = (discord.Embed(description=f'**{len(ctx.voice_state.songs)} tracks:**\n\n{queue}')
                 .set_footer(text=f"Viewing page {page}/{pages}"))

        await ctx.respond(embed=embed)

    @commands.slash_command(name="shuffle")
    async def _shuffle(self, ctx: discord.ApplicationContext):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.respond("The queue is empty!")

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction("✅")

    @commands.slash_command(name="remove")
    async def _remove(self, ctx: discord.ApplicationContext, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.respond("The queue is empty!")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction("✅")

    @commands.slash_command(name="loop")
    async def _loop(self, ctx: discord.ApplicationContext):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        # TODO: Can the comment above really not be formatted better?

        if not ctx.voice_state.is_playing:
            return await ctx.respond("Nothing being played at the moment.")

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction("✅")

    @commands.slash_command(name="play")
    async def _play(self, ctx: discord.ApplicationContext, *, search: str):
        """Plays a song. You can either provide a URL, or the name of the song.
        If there are songs in the queue, this will be queued until theother songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        print(search)

        # TODO: Might need to check whether the voice_state is in a channel here too
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.respond(f"An error occurred while processing this request: {str(e)}")
            else:
                song = Song(source)
                await ctx.voice_state.songs.put(song)
                await ctx.respond(f"Added {str(source)}")
                if not ctx.voice_state.is_playing:
                    ctx.voice_state.play_next_song()

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: discord.ApplicationContext):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError("You are not connected to a voice channel.")

        if ctx.voice_client and ctx.voice_client.channel != ctx.author.voice.channel:
            raise commands.CommandError("The bot is already in a voice channel.")


if __name__ == "__main__":
    intents = discord.Intents.all()
    intents.presences = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

    @bot.event
    async def on_ready():
        print(f"Logged in as {bot.user.name} ({bot.user.id})")

    load_dotenv()
    token = str(os.getenv("BOT_TOKEN"))

    bot.add_cog(MusicBot(bot))
    bot.run(token)


# class MusicBot(commands.Cog):
#     def __init__(self, bot):
#         self.bot = bot
#         self.song_queue: List[Dict] = []
#         self.current_player: Optional[YTDLPlayer] = None
#
#     @classmethod
#     def is_url(string: str) -> bool:
#         result = urlparse(string)
#         return all([result.scheme, result.netloc])
#
#     async def play_next(self, ctx):
#         if ctx.voice_client.is_playing():
#             ctx.voice_client.stop()
#             return  # `voice_client` should have an `after` lambda attached already
#
#         if len(self.song_queue) == 0:
#             await ctx.send("End of queue, probably!")
#             return
#
#         next_song = self.song_queue.pop(0)
#         data = self.ytdl.extract_info(next_song["url"], download=False)
#
#         # This happens if the video is private (probably)
#         if data is None:
#             await self.play_next(ctx)
#             return
#
#         player = await YTDLPlayer.from_data(data, stream=True)
#
#         ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop))
#         await ctx.send(f"Now playing: {player.title}")
#
#         self.current_player = player
#
#     @commands.command(name="play", help="Plays a song")
#     async def play(self, ctx, url: str, *args):
#         if not self.is_url(url):
#             original_name = f"{url} {' '.join(args)}"
#             data = await self.get_video_data_by_name(original_name)
#         else:
#             original_name = None
#             data = await self.get_video_data_by_url(url)
#
#         title = ""
#         count = 0
#
#         for video in data:
#             song_data = self.ytdl_to_song_data(video, original_name)
#             self.song_queue.append(song_data)
#             count += 1
#             title = song_data.get("title")
#
#         if not ctx.voice_client.is_playing():
#             await self.play_next(ctx)
#             return
#
#         message = (
#             f"Added \"{title}\" to the queue!" if count == 1 else
#             f"Added {count} song(s) to the queue!")
#         await ctx.send(message)
#
#     @commands.command(name="join", help="Joins the voice channel")
#     async def join(self, ctx):
#         if not ctx.message.author.voice:
#             await ctx.send("You are not connected to a voice channel!")
#             return
#         else:
#             channel = ctx.message.author.voice.channel
#         await channel.connect()
#
#     @commands.command(name="leave", help="Leaves the voice channel")
#     async def leave(self, ctx):
#         await ctx.voice_client.disconnect()
#         self.song_queue = []
#
#     @commands.command(name="stop", help="Stops the song")
#     async def stop(self, ctx):
#         ctx.voice_client.stop()
#
#     @commands.command(name="move", help="Moves the song to the desired position in the queue.")
#     async def move(self, ctx, what: int, where: int):
#         if what != where:
#             moved = self.song_queue[what - 1]
#             self.song_queue.insert(where - 1, self.song_queue.pop(what - 1))
#         await ctx.send(f"Moved {moved.get('title')} to [{where}].")
#
#     @commands.command(name="pause", help="Pauses the song.")
#     async def pause(self, ctx):
#         ctx.voice_client.pause()
#
#     @commands.command(name="resume", help="Resumes the song.")
#     async def resume(self, ctx):
#         ctx.voice_client.resume()
#
#     @commands.command(name="queue", help="Lists the current queue.")
#     async def queue(self, ctx):
#         if not self.song_queue:
#             await ctx.send("The song queue is empty!")
#         else:
#             queue_list = "\n".join(f"[{i+1}] {item.get('title')}" for i, item in enumerate(self.song_queue))
#             if len(queue_list) > 1900:
#                 queue_list = queue_list[:1900] + "... (and more songs!)"
#             await ctx.send(f"Songs in queue:\n{queue_list}")
#
#     @commands.command(name="next", help="Plays the next song.")
#     async def next(self, ctx):
#         await self.play_next(ctx)
#
#     @commands.command(name="skip", help="Plays the next song.")
#     async def skip(self, ctx):
#         await self.play_next(ctx)
#
#     @commands.command(name="remove", help="Removes a song from the queue at a given index")
#     async def remove(self, ctx, index: int):
#         try:
#             removed_song = self.song_queue.pop(index - 1)
#             await ctx.send(f"Removed song: {removed_song.get('title')}.")
#         except IndexError:
#             await ctx.send(f"No song at index {index}!")
#
#     @commands.command(name="clear", help="Clears the queue.")
#     async def clear(self, ctx):
#         self.song_queue = []
#         await ctx.send("The queue has been cleared!")
#
#     @commands.command(name="np", help="Displays the currently playing song.")
#     async def np(self, ctx):
#         await ctx.send(f"Now playing: {self.current_player.title}")
#
#     async def get_video_data_by_name(self, name: str) -> List[Dict[str, str]]:
#         search_results = self.ytdl.extract_info(f"ytsearch:{name}", download=False)
#         if "entries" not in search_results or len(search_results["entries"]) == 0:
#             return [None]
#         return [search_results["entries"][0]]
#
#     async def get_video_data_by_url(self, url: str) -> List[Dict[str, str]]:
#         data = self.ytdl.extract_info(url, download=False, process=False)
#         if data.get("entries") is not None:
#             return list(data.get("entries"))
#         return [data]
#
#     @classmethod
#     def ytdl_to_song_data(ytdl_object) -> Dict[str, str]:
#         url = ytdl_object.get("original_url", ytdl_object.get("url"))
#         title = ytdl_object.get("title")
#         if url is None or title is None:
#             raise ValueError("Cannot extract ytdl data.")
#         return {"url": url, "title": title}
#
#     @commands.Cog.listener()
#     async def on_ready(self):
#         print(f"Logged in as {self.bot.user.name}")
#
#     @commands.before_invoke
#     async def ensure_voice(self, ctx):
#         if ctx.voice_client is None:
#             if ctx.author.voice:
#                 await ctx.author.voice.channel.connect()
#             else:
#                 await ctx.send("You are not connected to a voice channel.")
#                 raise commands.CommandError("Author not connected to a voice channel.")



lyrics_token = "Jt8q6PJwaBEU4KNPrlUoYGsnIntE30L0paNxpkecOcJv4JuyRMAdruvpAgLukcsCpyYOGeqBL7sCRQW_iVU1AQ"
genius = lyricsgenius.Genius("your-genius-api-token")

