# riot/client.py

import asyncio
import logging
from collections import deque
from typing import Any, Dict, List, Optional
import time

import aiohttp

REGION_GROUPS = {
    "euw1": "europe", "eun1": "europe", "ru": "europe",
    "kr": "asia",     "jp1": "asia",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
}

log = logging.getLogger(__name__)


class RateLimitError(Exception):
    pass


class RiotAPIError(Exception):
    pass


class RiotClient:
    """
    Async Riot API client with token-bucket rate limiting.

    Riot dev keys: 20 req/s  and  100 req/2min.
    We enforce both windows to avoid 429s at startup bursts.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

        # Two windows to match Riot dev-key limits exactly:
        #   short  : 20 req / 1 s
        #   long   : 100 req / 120 s
        self._req_times_short: deque = deque()   # timestamps, 1-second window
        self._req_times_long:  deque = deque()   # timestamps, 120-second window

        self._short_window = 1       # seconds
        self._short_max    = 18      # stay safely under 20/s limit
        self._long_window  = 120
        self._long_max     = 95      # stay safely under 100/2min limit

        # Single lock — all coroutines share it, preventing simultaneous bursts
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-Riot-Token": self.api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _throttle(self):
        """
        Token-bucket throttle covering both Riot rate-limit windows.
        The asyncio.Lock() ensures coroutines queue up one at a time,
        preventing the startup burst that causes 429s.
        """
        async with self._lock:
            while True:
                now = time.time()

                # ── Purge stale timestamps ────────────────────────────
                while self._req_times_short and self._req_times_short[0] <= now - self._short_window:
                    self._req_times_short.popleft()
                while self._req_times_long and self._req_times_long[0] <= now - self._long_window:
                    self._req_times_long.popleft()

                # ── Check limits ──────────────────────────────────────
                short_full = len(self._req_times_short) >= self._short_max
                long_full  = len(self._req_times_long)  >= self._long_max

                if not short_full and not long_full:
                    # Slot available — register and proceed
                    self._req_times_short.append(now)
                    self._req_times_long.append(now)
                    return

                # ── Compute shortest wait ─────────────────────────────
                wait = 0.0
                if short_full:
                    wait = max(wait, self._short_window - (now - self._req_times_short[0]) + 0.05)
                if long_full:
                    wait = max(wait, self._long_window  - (now - self._req_times_long[0])  + 0.05)

                log.warning(f"[throttle] Rate limit window full — waiting {wait:.2f}s")
                await asyncio.sleep(wait)
                # Loop again to re-check after sleep

    async def _request(self, url: str, max_retries: int = 3) -> Any:
        """
        HTTP GET with throttle + retry logic.
        Handles 429 (with Retry-After) and 5xx with exponential backoff.
        """
        session = await self._get_session()

        for attempt in range(max_retries):
            await self._throttle()
            try:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "2")) + 1
                        log.warning(
                            f"[429] Riot enforced rate limit — waiting {retry_after}s "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_after)
                            continue
                        raise RateLimitError(f"Rate limit exceeded after {max_retries} attempts")

                    if resp.status == 404:
                        log.debug(f"[404] Not Found: {url}")
                        return None

                    resp.raise_for_status()
                    return await resp.json()

            except aiohttp.ClientResponseError as e:
                if e.status >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning(f"[{e.status}] Server error — retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise RiotAPIError(f"API error {e.status}: {e.message}") from e

            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning(f"Network error — retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
                raise

        raise RiotAPIError(f"Failed after {max_retries} attempts: {url}")

    # ─── API Methods ──────────────────────────────────────────────────────────

    async def get_summoner_by_name(self, region: str, summoner_name: str) -> Optional[Dict[str, Any]]:
        from urllib.parse import quote
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{quote(summoner_name, safe='')}"
        return await self._request(url)

    async def get_account_by_name_tag(self, region: str, game_name: str, tag_line: str) -> Optional[Dict[str, Any]]:
        group = REGION_GROUPS.get(region.lower(), "europe")
        url = f"https://{group}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._request(url)

    async def get_match_ids(self, region: str, puuid: str, count: int = 5) -> List[str]:
        group = REGION_GROUPS.get(region.lower(), "europe")
        url = (
            f"https://{group}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
        )
        result = await self._request(url)
        return result if result is not None else []

    async def get_match_by_id(self, region: str, match_id: str) -> Optional[Dict[str, Any]]:
        group = REGION_GROUPS.get(region.lower(), "europe")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return await self._request(url)

    async def get_league_entries_by_summoner(self, region: str, summoner_id: str) -> List[Dict[str, Any]]:
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        result = await self._request(url)
        return result if result is not None else []

    async def get_summoner_by_puuid(self, region: str, puuid: str) -> Optional[Dict[str, Any]]:
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return await self._request(url)

    async def get_match_timeline_by_id(self, region: str, match_id: str) -> Optional[Dict[str, Any]]:
        group = REGION_GROUPS.get(region.lower(), "europe")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        return await self._request(url)

    async def get_league_entries_by_puuid(self, region: str, puuid: str) -> List[Dict[str, Any]]:
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        result = await self._request(url)
        return result if result is not None else []
