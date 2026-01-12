# riot/client.py

import asyncio
import logging
from collections import deque
from typing import Any, Dict, List, Optional
import time

import aiohttp

# Mapping plateforme → région globale pour /match-v5 et /account-v1
REGION_GROUPS = {
    "euw1": "europe", "eun1": "europe", "ru": "europe",
    "kr": "asia",   "jp1": "asia",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas"
}

log = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Raised when rate limit is exceeded and retry fails."""
    pass


class RiotAPIError(Exception):
    """Base exception for Riot API errors."""
    pass


class RiotClient:
    """Async Riot API client with built-in rate limiting and error handling."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

        # Pour throttling : timestamps des dernières requêtes
        self._req_times: deque = deque()
        # Quota dev Riot : 100 reqs / 120 s
        self._quota_window = 120    # secondes
        self._quota_max = 100       # nombre max de requêtes par window
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-Riot-Token": self.api_key},
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()

    async def _throttle(self):
        """Async rate limiting - prevents exceeding Riot API quota."""
        async with self._lock:
            now = time.time()

            # Purge des requêtes trop vieilles
            while self._req_times and self._req_times[0] <= now - self._quota_window:
                self._req_times.popleft()

            if len(self._req_times) >= self._quota_max:
                # On attend que la plus vieille req sorte de la fenêtre
                wait = self._quota_window - (now - self._req_times[0])
                log.warning(f"Rate limit reached, waiting {wait:.1f}s")
                await asyncio.sleep(wait)

            # On enregistre la requête courante
            self._req_times.append(time.time())

    async def _request(self, url: str, max_retries: int = 3) -> Any:
        """
        Make an async HTTP request with retry logic.

        Args:
            url: The full URL to request
            max_retries: Maximum number of retries for 429 responses

        Returns:
            JSON response from the API

        Raises:
            RateLimitError: When rate limit is exceeded after retries
            RiotAPIError: For other API errors
            aiohttp.ClientError: For network errors
        """
        await self._throttle()
        session = await self._get_session()

        for attempt in range(max_retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "1")) + 1
                        if attempt < max_retries - 1:
                            log.warning(f"429 Rate limited, retrying after {retry_after}s (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(retry_after)
                            continue
                        else:
                            raise RateLimitError(f"Rate limit exceeded after {max_retries} attempts")

                    if resp.status == 404:
                        log.debug(f"404 Not Found: {url}")
                        return None

                    resp.raise_for_status()
                    return await resp.json()

            except aiohttp.ClientResponseError as e:
                if e.status >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt  # Exponential backoff
                    log.warning(f"Server error {e.status}, retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise RiotAPIError(f"API error {e.status}: {e.message}") from e
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning(f"Network error, retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
                raise

        raise RiotAPIError(f"Failed after {max_retries} attempts")

    async def get_summoner_by_name(self, region: str, summoner_name: str) -> Optional[Dict[str, Any]]:
        """Get summoner information by summoner name."""
        from urllib.parse import quote
        name_enc = quote(summoner_name, safe="")
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{name_enc}"
        return await self._request(url)

    async def get_account_by_name_tag(self, region: str, game_name: str, tag_line: str) -> Optional[Dict[str, Any]]:
        """
        Get account by Riot ID (game name + tag).
        Account-V1: GET /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
        Routed via region group (americas/europe/asia).
        """
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = (
            f"https://{group}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        return await self._request(url)

    async def get_match_ids(self, region: str, puuid: str, count: int = 5) -> List[str]:
        """Get list of match IDs for a player."""
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = (
            f"https://{group}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
        )
        result = await self._request(url)
        return result if result is not None else []

    async def get_match_by_id(self, region: str, match_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed match information by match ID."""
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return await self._request(url)

    async def get_league_entries_by_summoner(self, region: str, summoner_id: str) -> List[Dict[str, Any]]:
        """Get ranked entries for a summoner."""
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        result = await self._request(url)
        return result if result is not None else []

    async def get_summoner_by_puuid(self, region: str, puuid: str) -> Optional[Dict[str, Any]]:
        """Get summoner information by PUUID."""
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return await self._request(url)

    async def get_match_timeline_by_id(self, region: str, match_id: str) -> Optional[Dict[str, Any]]:
        """
        Get match timeline with detailed event history.
        Match-V5 Timeline: returns event history per minute.
        """
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        return await self._request(url)

    async def get_league_entries_by_puuid(self, region: str, puuid: str) -> List[Dict[str, Any]]:
        """Get ranked league entries by PUUID."""
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        result = await self._request(url)
        return result if result is not None else []
