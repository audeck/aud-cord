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
import datetime

from typing import Dict, List
from dotenv import load_dotenv

# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda: ""

class Timer():
    def __init__(self):
        self.start_date = datetime.datetime.now()
        self.paused_date = None
        self.is_paused = False

    def pause(self):
        if not self.is_paused:
            self.is_paused = True
            self.paused_date = datetime.datetime.now()
            
    def unpause(self):
        if self.is_paused:
            self.is_paused = False
            self.start_date += datetime.datetime.now() - self.paused_date
            self.paused_date = None

    def get_time(self):
        if self.is_paused:
            pause_correction = (datetime.datetime.now() - self.paused_date).total_seconds()
        else:
            pause_correction = 0

        seconds_elapsed = (datetime.datetime.now() - self.start_date).total_seconds() - pause_correction
        return seconds_elapsed
        

class YTDLError(Exception):
    pass


class YTDLSource():
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

    def __init__(self, ctx: discord.ApplicationContext, data: Dict):
        self.requester = ctx.author
        self.channel = ctx.channel

        self.data = data
        self.title = data.get("title")
        self.url = data.get("webpage_url", data.get("url"))
        self.original_name = data.get("original_name")

        # self.stream_url = data.get("url")
        # self.duration = self.parse_duration(int(data.get("duration")))
        # self.thumbnail = data.get("thumbnail")
        self.stream_url = None
        self.duration_in_seconds = None
        self.time_elapsed_timer = None
        self.thumbnail = None

        self.player = None

    def __str__(self):
        return f"**{self.title}**"

    def has_full_source(self):
        return self.stream_url is not None \
           and self.duration_in_seconds is not None \
           and self.thumbnail is not None

    async def get_full_source(self, loop: asyncio.BaseEventLoop):
        partial = functools.partial(self.ytdl.extract_info, self.url, download=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            # Video probably unavailable
            raise YTDLError(f"Couldn't fetch data from {self.url}")

        # Get more data
        self.stream_url = data.get("url")
        self.duration_in_seconds = int(data.get("duration"))
        self.thumbnail = data.get("thumbnail")

    async def get_player(self, volume: float = 0.5, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        if not self.has_full_source():
            await self.get_full_source(loop)

        self.time_elapsed_timer = Timer()

        try:
            return discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(self.stream_url, **self.FFMPEG_OPTIONS), volume)
        except discord.ClientException:
            raise YTDLError("FFmpegPCMAudio subprocess failed to be created. Is one already running?")

    @classmethod
    async def prepare_sources(cls, ctx: discord.ApplicationContext, target: str, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        if cls.is_url(target):
            videos = await cls.get_data_from_url(target, loop)
        else:
            videos = await cls.get_data_from_name(target, loop)

        if len(videos) == 0:
            raise YTDLError(f"Cannot prefetch video(s) at {target}")

        return list(map(lambda video: cls(ctx, video), videos))

    @classmethod
    async def get_data_from_name(cls, name: str, loop: asyncio.BaseEventLoop) -> List[Dict]:
        # Still have to process here to get the actual url.
        partial = functools.partial(cls.ytdl.extract_info, f"ytsearch:{name}", download=False)
        data = await loop.run_in_executor(None, partial)

        if "entries" not in data or len(data["entries"]) == 0:
            return [None]

        entry = data["entries"][0]
        entry["original_name"] = name  # Save the original name too

        return [entry]

    @classmethod
    async def get_data_from_url(cls, url: str, loop: asyncio.BaseEventLoop) -> List[Dict]:
        partial = functools.partial(cls.ytdl.extract_info, url, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data.get("entries") is not None:
            return list(data.get("entries"))

        return [data]
    
    @staticmethod
    def format_time(time_elapsed_in_seconds: int, duration_in_seconds: int) -> str:
        time_elapsed_minutes, time_elapsed_seconds = divmod(time_elapsed_in_seconds, 60)
        time_elapsed_hours, time_elapsed_minutes = divmod(time_elapsed_minutes, 60)

        duration_minutes, duration_seconds = divmod(duration_in_seconds, 60)
        duration_hours, duration_minutes = divmod(duration_minutes, 60)

        time_elapsed_hours = int(time_elapsed_hours)
        time_elapsed_minutes = str(int(time_elapsed_minutes)).zfill(2)
        time_elapsed_seconds = str(int(time_elapsed_seconds)).zfill(2)

        duration_hours = int(duration_hours)
        duration_minutes = str(int(duration_minutes)).zfill(2)
        duration_seconds = str(int(duration_seconds)).zfill(2)

        if duration_hours == 0: 
            time_elapsed = f"{time_elapsed_minutes}:{time_elapsed_seconds}"
            duration = f"{duration_minutes}:{duration_seconds}"
        else:
            time_elapsed = f"{time_elapsed_hours}:{time_elapsed_minutes}:{time_elapsed_seconds}"
            duration = f"{duration_hours}:{duration_minutes}:{duration_seconds}"
            
        return f"{time_elapsed}/{duration}"

    @staticmethod
    def is_url(string: str) -> bool:
        result = urlparse(string)
        return all([result.scheme, result.netloc])

    def create_embed(self):
        embed = discord.Embed(
            title="Now playing",
            description=f"```css\n{self.title}\n```",
            color=discord.Color.blurple()
        )

        embed.add_field(name="Requested by", value=self.requester)

        if self.has_full_source():
            embed.add_field(name="Duration", value=f"{self.format_time(self.time_elapsed_timer.get_time(), self.duration_in_seconds)}")
            embed.set_thumbnail(url=self.thumbnail)

        # Make sure the URL is always the last embed, since it looks weird otherwise
        embed.add_field(name="URL", value=f"[Click]({self.url})")

        return embed


class Song:
    __slots__ = ("source", "requester")

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def __str__(self):
        return str(self.source)


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

    # Don't ask...
    async def move(self, what: int, where: int):
        items = []

        while not self.empty():
            items.append(await self.get())
            self.task_done()

        items.insert(where, items.pop(what))

        for item in items:
            await self.put(item)


class VoiceError(Exception):
    pass


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: discord.ApplicationContext):
        self.bot = bot
        self.ctx = ctx

        self.songs: SongQueue = SongQueue()
        self.current: YTDLSource = None
        self.should_play_next = asyncio.Event()
        self.voice: discord.VoiceClient = None

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
        try:
            while True:
                # Clear flag
                self.should_play_next.clear()

                if not self.loop:
                    try:
                        # Wait for three minutes (180 seconds) while inactive
                        # before leaving the channel for performance reasons.
                        async with timeout(180):
                            song = await self.songs.get()
                            self.current = song.source
                    except asyncio.TimeoutError:
                        # TODO: Also cleaning up this task (and the restarting
                        #       it on play) would be nice.
                        print("Leaving voice channel due to inactivity...")
                        self.bot.loop.create_task(self.stop())
                        self.current = None

                if self.current is not None:
                    try:
                        current_player = await self.current.get_player(self._volume)
                        self.voice.play(current_player, after=self.play_next_song)
                        await self.current.channel.send(embed=self.current.create_embed())
                    except Exception as e:
                        # TODO: Better video unavailable handling (catching a lot of possible exceptions here)
                        print(e)
                        self.play_next_song()

                # Wait until the `after=play_next_song` sets the `self.should_play_next` flag again
                await self.should_play_next.wait()
        except Exception as e:
            print(e)

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.should_play_next.set()

    def skip(self):
        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


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
        try:
            await ctx.interaction.followup.send(f":red_square: An error occurred: {str(error)}")
        except Exception:
            # Probably no interaction to follow up on
            await ctx.send(f":red_square: An error occurred: {str(error)}")

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
            await ctx.respond("The bot isn't connected to a voice channel.", ephemeral=True)
            return

        await ctx.voice_state.stop()
        del self.voice_state

    @commands.slash_command(name="volume")
    async def _volume(self, ctx: discord.ApplicationContext, *, volume: int):
        """(DOESN'T WORK) Adjusts the bot volume. Accepts values from 0 to 100."""

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
            await ctx.respond("Nothing is playing right now :(.", ephemeral=True)
            return

        await ctx.respond(embed=ctx.voice_state.current.create_embed())

    # TODO: Pausing while paused?
    @commands.slash_command(name="pause")
    async def _pause(self, ctx: discord.ApplicationContext):
        """Pauses the current song."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("The bot isn't playing, dum dum!", ephemeral=True)
            return

        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            ctx.voice_state.current.time_elapsed_timer.pause()
            await ctx.respond(":pause_button: Paused... for now!")
        else:
            await ctx.respond("I'm not playing, dum dum!", ephemeral=True)

    @commands.slash_command(name="resume")
    async def _resume(self, ctx: discord.ApplicationContext):
        """Resumes the current song."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("The bot isn't playing, dum dum!", ephemeral=True)
            return

        if ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            ctx.voice_state.current.time_elapsed_timer.unpause()
            await ctx.respond(":arrow_forward: Yay!")
        else:
            await ctx.respond("I'm not paused, dummy!", ephemeral=True)

    @commands.slash_command(name="stop")
    async def _stop(self, ctx: discord.ApplicationContext):
        """Stops playing and clears the queue."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("The bot isn't playing, dum dum!", ephemeral=True)
            return

        ctx.voice_state.songs.clear()
        ctx.voice_state.voice.stop()
        await ctx.respond(":stop_button: :(")

    @commands.slash_command(name="clear")
    async def _clear(self, ctx: discord.ApplicationContext):
        """Clears the current queue."""

        ctx.voice_state.songs.clear()
        await ctx.respond(":white_check_mark: The queue has been cleared!")

    @commands.slash_command(name="skip")
    async def _skip(self, ctx: discord.ApplicationContext):
        """Skips the current song."""

        if not ctx.voice_state.is_playing:
            await ctx.respond("Not playing any music right now.", ephemeral=True)
            return

        ctx.voice_state.skip()
        await ctx.respond(":white_check_mark: Skipped!")

    @commands.slash_command(name="queue")
    async def _queue(self, ctx: discord.ApplicationContext, *, page: int = 1):
        """
        Shows the player's queue.

        You can optionally specify the page to show.
        Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            await ctx.respond("The queue is empty!")
            return

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        if page < 1 or page > pages:
            await ctx.respond("I know! 69420! Very funny!", ephemeral=True)
            return

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ""
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += f"`{i + 1}.` [**{song.source.title}**]({song.source.url})\n"

        embed = (discord.Embed(description=f'**{len(ctx.voice_state.songs)} tracks:**\n\n{queue}')
                 .set_footer(text=f"Viewing page {page}/{pages}."))

        await ctx.respond(embed=embed)

    @commands.slash_command(name="shuffle")
    async def _shuffle(self, ctx: discord.ApplicationContext):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            await ctx.respond("The queue is empty!", ephemeral=True)
            return

        ctx.voice_state.songs.shuffle()
        await ctx.respond(":white_check_mark: Done!")

    @commands.slash_command(name="move")
    async def _move(self, ctx: discord.ApplicationContext, what: int, where: int):
        """Moves a song within the queue."""

        if what == where:
            await ctx.respond("Very funny!", ephemeral=True)
            return

        queue_length = len(ctx.voice_state.songs)
        if what < 1 or where < 1 or what > queue_length or where > queue_length:
            await ctx.respond("How about specifying valid indices, you doofus?", ephemeral=True)
            return

        await ctx.voice_state.songs.move(what - 1, where - 1)
        await ctx.respond(":white_check_mark: Done!")

    @commands.slash_command(name="remove")
    async def _remove(self, ctx: discord.ApplicationContext, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            await ctx.respond("The queue is empty, you wanker!", ephemeral=True)
            return

        if index < 1 or index > len(ctx.voice_state.songs):
            await ctx.respond("Spamming random numbers, are we?", ephemeral=True)
            return

        removed = ctx.voice_state.songs[index - 1]
        ctx.voice_state.songs.remove(index - 1)
        await ctx.respond(f":white_check_mark: Removed {str(removed)} from the queue!")

    @commands.slash_command(name="loop")
    async def _loop(self, ctx: discord.ApplicationContext):
        """
        Loops the currently playing song. Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            await ctx.respond("Nothing being played at the moment.", ephemeral=True)
            return

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop

        if ctx.voice_state.loop:
            await ctx.respond(":repeat: Looping the current song!")
        else:
            await ctx.respond(":arrow_forward: Playing the rest of the queue!")

    @commands.slash_command(name="play")
    async def _play(self, ctx: discord.ApplicationContext, *, name_or_url: str):
        """
        Plays a song. You can either provide a URL, or the name of the song.

        If there are songs in the queue, this will be queued until theother songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        await ctx.interaction.response.defer()
        print(name_or_url)

        # TODO: Might need to check whether the voice_state is in a channel here too
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        # async with ctx.typing():
        # No point in doing `with typing` here, as defering already shows an indeterminate progress
        try:
            pre_sources = await YTDLSource.prepare_sources(ctx, name_or_url, loop=self.bot.loop)
        except YTDLError as e:
            await ctx.interaction.followup.send(f":red_square: An error occurred while processing this request: {str(e)}")  # noqa: E501
        else:
            for pre_source in pre_sources:
                song = Song(pre_source)
                await ctx.voice_state.songs.put(song)

            if len(pre_sources) == 1:
                await ctx.interaction.followup.send(f":white_check_mark: Added {str(pre_source)} to the queue!")
            else:
                await ctx.interaction.followup.send(f":white_check_mark: Added {len(pre_sources)} songs to the queue!")

            # if not ctx.voice_state.is_playing:
            #     ctx.voice_state.play_next_song()

    @commands.slash_command(name="lyrics")
    async def _lyrics(self, ctx: discord.ApplicationContext, name: str = None):
        """Displays lyrics for the given song, or the current song if no name is provided."""

        await ctx.interaction.response.defer()

        if name is None:
            song = None

            if ctx.voice_state.current.original_name is not None:
                song = genius.search_song(ctx.voice_state.current.original_name)

            if song is None:
                song = genius.search_song(ctx.voice_state.current.title)

            if song is None:
                await ctx.interaction.followup.send(":pleading_face: Apologies, I couldn't find lyrics for the current song. Try specifying its name when searching!")  # noqa: E501
                return
        else:
            song = genius.search_song(name)

            if song is None:
                await ctx.interaction.followup.send(f":pleading_face: Apologies, I couldn't find lyrics for {name}.")
                return

        await ctx.interaction.followup.send("Here are the lyrics:\n")
        await self.send_split_message(ctx, song.lyrics)

    async def send_split_message(self, ctx: discord.ApplicationContext, message: str):
        # The maximum message length allowed by discord
        MAX_LENGTH = 2000

        # Split the lyrics into chunks of max_length characters
        chunks = []

        while len(message) > MAX_LENGTH:
            # Find the last newline before the max_length limit
            split_index = message.rfind('\n', 0, MAX_LENGTH)

            if split_index == -1:  # No newline found, split at max_length
                split_index = MAX_LENGTH

            chunks.append(message[:split_index])
            message = message[split_index:]

        chunks.append(message)

        for chunk in chunks:
            await ctx.send(chunk)

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
    bot_token = str(os.getenv("BOT_TOKEN"))

    # TODO: Only in __main__?
    genius_token = str(os.getenv("GENIUS_TOKEN"))
    genius = lyricsgenius.Genius(genius_token)

    bot.add_cog(MusicBot(bot))
    bot.run(bot_token)
