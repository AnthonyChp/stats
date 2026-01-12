# riot/client.py

import time
import requests
from collections import deque
from typing import Any, Dict, List

# Mapping plateforme → région globale pour /match-v5 et /account-v1
REGION_GROUPS = {
    "euw1": "europe", "eun1": "europe", "ru": "europe",
    "kr": "asia",   "jp1": "asia",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas"
}


class RiotClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key})

        # Pour throttling : timestamps des dernières requêtes
        self._req_times = deque()
        # Quota dev Riot : 100 reqs / 120 s
        self._quota_window = 120    # secondes
        self._quota_max = 100       # nombre max de requêtes par window

    def _throttle(self):
        now = time.time()
        # Purge des requêtes trop vieilles
        while self._req_times and self._req_times[0] <= now - self._quota_window:
            self._req_times.popleft()

        if len(self._req_times) >= self._quota_max:
            # On attend que la plus vieille req sorte de la fenêtre
            wait = self._quota_window - (now - self._req_times[0])
            time.sleep(wait)

        # On enregistre la requête courante
        self._req_times.append(time.time())

    def _request(self, url: str) -> Any:
        self._throttle()
        resp = self.session.get(url)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "1")) + 1
            time.sleep(retry)
            resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_summoner_by_name(self, region: str, summoner_name: str) -> Dict[str, Any]:
        from requests.utils import quote
        name_enc = quote(summoner_name, safe="")
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{name_enc}"
        return self._request(url)

    def get_account_by_name_tag(self, region: str, game_name: str, tag_line: str) -> Dict[str, Any]:
        """
        Account-V1: GET /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
        Routé via la région globale selon la platform (americas/europe/asia).
        """
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = (
            f"https://{group}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        return self._request(url)

    def get_match_ids(self, region: str, puuid: str, count: int = 5) -> List[str]:
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = (
            f"https://{group}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
        )
        return self._request(url)

    def get_match_by_id(self, region: str, match_id: str) -> Dict[str, Any]:
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return self._request(url)

    def get_league_entries_by_summoner(self, region: str, summoner_id: str) -> List[Dict[str, Any]]:
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        return self._request(url)

    def get_summoner_by_puuid(self, region: str, puuid: str) -> Dict[str, Any]:
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return self._request(url)

    def get_match_timeline_by_id(self, region: str, match_id: str) -> Dict[str, Any]:
        """
        Match-V5 Timeline : retourne l’historique d’événements toutes les minutes.
        """
        group = REGION_GROUPS.get(region.lower(), "americas")
        url = f"https://{group}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        return self._request(url)

    def get_league_entries_by_puuid(self, region: str, puuid: str) -> List[Dict[str, Any]]:
        url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        return self._request(url)
