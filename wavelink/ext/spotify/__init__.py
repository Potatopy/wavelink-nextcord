"""
MIT License

Copyright (c) 2019-Present PythonistaGuild

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import asyncio
import base64
import enum
import re
import time
from typing import Any, List, Optional, Type, TypeVar, Union, TYPE_CHECKING

import aiohttp
from discord.ext import commands

import wavelink
from wavelink import Node, NodePool

if TYPE_CHECKING:
    from wavelink import Player, Playable


__all__ = ('SpotifySearchType',
           'SpotifyClient',
           'SpotifyTrack',
           'SpotifyRequestError',
           'decode_url')


GRANTURL = 'https://accounts.spotify.com/api/token?grant_type=client_credentials'
URLREGEX = re.compile(r'(https?://open.)?(spotify)(.com/|:)'
                      r'(?P<type>album|playlist|track|artist)([/:])'
                      r'(?P<id>[a-zA-Z0-9]+)(\?si=[a-zA-Z0-9]+)?(&dl_branch=[0-9]+)?')
BASEURL = 'https://api.spotify.com/v1/{entity}s/{identifier}'
RECURL = 'https://api.spotify.com/v1/recommendations?seed_tracks={tracks}'


ST = TypeVar("ST", bound="Playable")


def decode_url(url: str) -> Optional[dict]:
    """Check whether the given URL is a valid Spotify URL and return it's type and ID.

    Parameters
    ----------
    url: str
        The URL to check.

    Returns
    -------
    Optional[dict]
        An mapping of :class:`SpotifySearchType` and Spotify ID. Type will be either track, album or playlist.
        If type is not track, album or playlist, a special unusable type is returned.

        Could return None if the URL is invalid.

    Examples
    --------

    .. code:: python3

        from wavelink.ext import spotify

        ...

        decoded = spotify.decode_url("https://open.spotify.com/track/6BDLcvvtyJD2vnXRDi1IjQ?si=e2e5bd7aaf3d4a2a")

        if decoded and decoded['type'] is spotify.SpotifySearchType.track:
            track = await spotify.SpotifyTrack.search(query=decoded["id"], type=decoded["type"])
    """
    match = URLREGEX.match(url)
    if match:
        try:
            type_ = SpotifySearchType[match['type']]
        except KeyError:
            type_ = SpotifySearchType.unusable

        return {'type': type_, 'id': match['id']}

    return None


class SpotifySearchType(enum.Enum):
    """An enum specifying which type to search for with a given Spotify ID.

    track
        Default search type. Unless specified otherwise this will always be the search type.
    album
        Search for an album.
    playlist
        Search for a playlist.
    """
    track = 0
    album = 1
    playlist = 2
    unusable = 3


class SpotifyAsyncIterator:

    def __init__(self, *, query: str, limit: int, type: SpotifySearchType, node: Node):
        self._query = query
        self._limit = limit
        self._type = type
        self._node = node

        self._first = True
        self._count = 0
        self._queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def fill_queue(self):
        tracks = await self._node._spotify._search(query=self._query, iterator=True, type=self._type)

        for track in tracks:
            await self._queue.put(track)

    async def __anext__(self):
        if self._first:
            await self.fill_queue()
            self._first = False

        if self._limit is not None and self._count == self._limit:
            raise StopAsyncIteration

        try:
            track = self._queue.get_nowait()
        except asyncio.QueueEmpty as e:
            raise StopAsyncIteration from e

        if track is None:
            return await self.__anext__()

        track = SpotifyTrack(track)

        self._count += 1
        return track


class SpotifyRequestError(Exception):
    """Base error for Spotify requests.

    Attributes
    ----------
    status: int
        The status code returned from the request.
    reason: Optional[str]
        The reason the request failed. Could be None.
    """

    def __init__(self, status: int, reason: Optional[str] = None):
        self.status = status
        self.reason = reason


class SpotifyTrack:
    """A track retrieved via Spotify.

    Attributes
    ----------
    raw: dict[str, Any]
        The raw payload from Spotify for this track.
    album: str
        The album name this track belongs to.
    images: list[str]
        A list of URLs to images associated with this track.
    artists: list[str]
        A list of artists for this track.
    genres: list[str]
        A list of genres associated with this tracks artist.
    name: str
        The track name.
    title: str
        An alias to name.
    uri: str
        The URI for this spotify track.
    id: str
        The spotify ID for this track.
    isrc: str | None
        The International Standard Recording Code associated with this track if given.
    length: int
        The track length in milliseconds.
    duration: int
        Alias to length.
    """

    __slots__ = (
        'raw',
        'album',
        'images',
        'artists',
        'name',
        'title',
        'uri',
        'id',
        'length',
        'duration',
        'isrc',
        '__dict__'
    )

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw: dict[str, Any] = data

        album = data['album']
        self.album: str = album['name']
        self.images: list[str] = [i['url'] for i in album['images']]

        artists = data['artists']
        self.artists: list[str] = [a['name'] for a in artists]
        # self.genres: list[str] = [a['genres'] for a in artists]

        self.name: str = data['name']
        self.title: str = self.name
        self.uri: str = data['uri']
        self.id: str = data['id']
        self.length: int = data['duration_ms']
        self.duration: int = self.length

        self.isrc: str | None = data["external_ids"].get("isrc")

    def __eq__(self, other) -> bool:
        return self.id == other.id

    @classmethod
    async def search(
        cls: Type[ST],
            query: str,
            *,
            type: SpotifySearchType = SpotifySearchType.track,
            node: Node | None = None,
            return_first: bool = False,
    ) -> Union[Optional[ST], List[ST]]:
        """|coro|

        Search for tracks with the given query.

        Parameters
        ----------
        query: str
            The song to search for.
        type: Optional[:class:`spotify.SpotifySearchType`]
            An optional enum value to use when searching with Spotify. Defaults to track.
        node: Optional[:class:`wavelink.Node`]
            An optional Node to use to make the search with.
        return_first: Optional[bool]
            An optional bool which when set to True will return only the first track found. Defaults to False.

        Returns
        -------
        Union[Optional[Track], List[Track]]
        """
        if node is None:
            node: Node = NodePool.get_connected_node()

        if type == SpotifySearchType.track:
            tracks = await node._spotify._search(query=query, type=type)

            return tracks[0] if return_first else tracks
        return await node._spotify._search(query=query, type=type)

    @classmethod
    def iterator(cls,
                 *,
                 query: str,
                 limit: int | None = None,
                 type: SpotifySearchType = SpotifySearchType.playlist,
                 node: Node | None = None,
                 ):
        """An async iterator version of search.

        This can be useful when searching for large playlists or albums with Spotify.

        Parameters
        ----------
        query: str
            The Spotify URL or ID to search for. Must be of type Playlist or Album.
        limit: Optional[int]
            Limit the amount of tracks returned.
        type: :class:`SpotifySearchType`
            The type of search. Must be either playlist or album. Defaults to playlist.
        node: Optional[:class:`Node`]
            An optional node to use when querying for tracks. Defaults to best available.

        Examples
        --------

        .. code:: python3

                async for track in spotify.SpotifyTrack.iterator(query=..., type=spotify.SpotifySearchType.playlist):
                    ...
        """

        if type is not SpotifySearchType.album and type is not SpotifySearchType.playlist:
            raise TypeError("Iterator search type must be either album or playlist.")

        if node is None:
            node = NodePool.get_connected_node()

        return SpotifyAsyncIterator(query=query, limit=limit, node=node, type=type)

    @classmethod
    async def convert(cls: Type[ST], ctx: commands.Context, argument: str) -> ST:
        """Converter which searches for and returns the first track.

        Used as a type hint in a discord.py command.
        """
        results = await cls.search(argument)

        if not results:
            raise commands.BadArgument("Could not find any songs matching that query.")

        return results[0]

    async def fulfill(self, *, player: Player, cls: Playable, populate: bool) -> Playable:
        """
        Parameters
        ----------
        player: :class:`wavelink.player.Player`
            If Player.autoplay is enabled, this search will fill the AutoPlay Queue.
        cls
            The class to convert this Spotify Track to.
        """
        try:
            tracks: list[cls] = await cls.search(f'"{self.isrc}"')
        except wavelink.NoTracksError:
            tracks: list[cls] = await cls.search(f'{self.name} - {self.artists[0]}')

        if not player.autoplay or not populate:
            return tracks[0]

        node: Node = player.current_node
        sc: SpotifyClient | None = node._spotify

        if not sc:
            raise RuntimeError(f"There is no spotify client associated with <{node:!r}>")

        if len(player._track_seeds) == 5:
            player._track_seeds.pop(0)

        player._track_seeds.append(self.id)

        url: str = RECURL.format(tracks=','.join(player._track_seeds))
        async with node._session.get(url=url, headers=sc.bearer_headers) as resp:
            if resp.status != 200:
                raise SpotifyRequestError(resp.status, resp.reason)

            data = await resp.json()

        recos = [SpotifyTrack(t) for t in data['tracks']]
        for reco in recos:
            if reco in player.auto_queue or reco in player.auto_queue.history:
                pass

            await player.auto_queue.put_wait(reco)

        return tracks[0]


class SpotifyClient:
    """Spotify client passed to Nodes for searching via Spotify.

    Parameters
    ----------
    client_id: str
        Your spotify application client ID.
    client_secret: str
        Your spotify application secret.
    """

    def __init__(self, *, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret

        self.session = aiohttp.ClientSession()

        self._bearer_token: str = None  # type: ignore
        self._expiry: int = 0

    @property
    def grant_headers(self) -> dict:
        authbytes = f'{self._client_id}:{self._client_secret}'.encode()
        return {'Authorization': f'Basic {base64.b64encode(authbytes).decode()}',
                'Content-Type': 'application/x-www-form-urlencoded'}

    @property
    def bearer_headers(self) -> dict:
        return {'Authorization': f'Bearer {self._bearer_token}'}

    async def _get_bearer_token(self) -> None:
        async with self.session.post(GRANTURL, headers=self.grant_headers) as resp:
            if resp.status != 200:
                raise SpotifyRequestError(resp.status, resp.reason)

            data = await resp.json()
            self._bearer_token = data['access_token']
            self._expiry = time.time() + (int(data['expires_in']) - 10)

    async def _search(self,
                      query: str,
                      type: SpotifySearchType = SpotifySearchType.track,
                      iterator: bool = False,
                      ) -> SpotifyTrack | list[SpotifyTrack]:

        if not self._bearer_token or time.time() >= self._expiry:
            await self._get_bearer_token()

        regex_result = URLREGEX.match(query)

        url = (
            BASEURL.format(
                entity=regex_result['type'], identifier=regex_result['id']
            )
            if regex_result
            else BASEURL.format(entity=type.name, identifier=query)
        )
        async with self.session.get(url, headers=self.bearer_headers) as resp:
            if resp.status != 200:
                raise SpotifyRequestError(resp.status, resp.reason)

            data = await resp.json()

        if data['type'] == 'track':
            return SpotifyTrack(data)

        elif data['type'] == 'album':
            album_data: dict[str, Any]= {
                                        'album_type': data['album_type'],
                                        'artists': data['artists'],
                                        'available_markets': data['available_markets'],
                                        'external_urls': data['external_urls'],
                                        'href': data['href'],
                                        'id': data['id'],
                                        'images': data['images'],
                                        'name': data['name'],
                                        'release_date': data['release_date'],
                                        'release_date_precision': data['release_date_precision'],
                                        'total_tracks': data['total_tracks'],
                                        'type': data['type'],
                                        'uri': data['uri'],
                                        }
            tracks = []
            for track in data['tracks']['items']:
                track['album'] = album_data
                if iterator:
                    tracks.append(track)
                else:
                    tracks.append(SpotifyTrack(track))

            return tracks

        elif data['type'] == 'playlist':
            if iterator:
                if not data['tracks']['next']:
                    return [t['track'] for t in data['tracks']['items']]

                url = data['tracks']['next']

                items = [t['track'] for t in data['tracks']['items']]
                while True:
                    async with self.session.get(url, headers=self.bearer_headers) as resp:
                        data = await resp.json()

                        items.extend([t['track'] for t in data['items']])
                        if not data['next']:
                            return items

                        url = data['next']
            else:
                tracks = data['tracks']['items']
                return [SpotifyTrack(t) for t in tracks]
