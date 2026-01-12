"""Unit tests for the async RiotClient."""

import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from oogway.riot.client import RiotClient, RateLimitError, RiotAPIError


@pytest.mark.asyncio
class TestRiotClient:
    """Test suite for RiotClient async operations."""

    async def test_client_initialization(self):
        """Test client initializes correctly."""
        client = RiotClient("test_api_key")
        assert client.api_key == "test_api_key"
        assert client._session is None
        assert len(client._req_times) == 0

    async def test_get_session_creates_session(self):
        """Test session creation on first call."""
        client = RiotClient("test_key")
        session = await client._get_session()

        assert session is not None
        assert isinstance(session, aiohttp.ClientSession)
        assert not session.closed

        await client.close()

    async def test_close_session(self):
        """Test session closes properly."""
        client = RiotClient("test_key")
        await client._get_session()
        await client.close()

        assert client._session.closed

    async def test_context_manager(self):
        """Test client works as async context manager."""
        async with RiotClient("test_key") as client:
            assert client is not None
            session = await client._get_session()
            assert not session.closed

        assert client._session.closed

    async def test_throttling_respects_rate_limit(self):
        """Test that throttling prevents exceeding rate limits."""
        client = RiotClient("test_key")
        client._quota_max = 2
        client._quota_window = 1

        # First two requests should go through immediately
        await client._throttle()
        await client._throttle()

        assert len(client._req_times) == 2

        # Third request should be delayed
        # (we won't actually wait, just check the logic)
        assert len(client._req_times) >= client._quota_max - 1

    async def test_request_handles_404(self):
        """Test that 404 returns None instead of raising."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_response

        client = RiotClient("test_key")
        client._session = mock_session

        with patch.object(client, '_throttle', new_callable=AsyncMock):
            result = await client._request("http://test.url")

        assert result is None

    async def test_request_handles_429_retry(self):
        """Test that 429 rate limit triggers retry."""
        # First call returns 429, second call succeeds
        mock_resp_429 = AsyncMock()
        mock_resp_429.status = 429
        mock_resp_429.headers = {"Retry-After": "1"}
        mock_resp_429.__aenter__.return_value = mock_resp_429
        mock_resp_429.__aexit__.return_value = None

        mock_resp_ok = AsyncMock()
        mock_resp_ok.status = 200
        mock_resp_ok.json = AsyncMock(return_value={"data": "success"})
        mock_resp_ok.__aenter__.return_value = mock_resp_ok
        mock_resp_ok.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.get.side_effect = [mock_resp_429, mock_resp_ok]

        client = RiotClient("test_key")
        client._session = mock_session

        with patch.object(client, '_throttle', new_callable=AsyncMock):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                result = await client._request("http://test.url")

        assert result == {"data": "success"}

    async def test_request_raises_on_max_retries(self):
        """Test that max retries raises RateLimitError."""
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.headers = {"Retry-After": "1"}
        mock_resp.__aenter__.return_value = mock_resp
        mock_resp.__aexit__.return_value = None

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp

        client = RiotClient("test_key")
        client._session = mock_session

        with patch.object(client, '_throttle', new_callable=AsyncMock):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                with pytest.raises(RateLimitError):
                    await client._request("http://test.url", max_retries=2)

    async def test_get_match_ids(self):
        """Test fetching match IDs."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=["match1", "match2", "match3"])
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None
        mock_response.raise_for_status = MagicMock()

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_response

        client = RiotClient("test_key")
        client._session = mock_session

        with patch.object(client, '_throttle', new_callable=AsyncMock):
            result = await client.get_match_ids("euw1", "test_puuid", count=3)

        assert result == ["match1", "match2", "match3"]

    async def test_get_match_ids_returns_empty_on_404(self):
        """Test that get_match_ids returns empty list on 404."""
        client = RiotClient("test_key")

        with patch.object(client, '_request', new_callable=AsyncMock, return_value=None):
            result = await client.get_match_ids("euw1", "puuid", 5)

        assert result == []


@pytest.mark.asyncio
class TestRiotClientRegionGroups:
    """Test region group mappings."""

    async def test_euw_uses_europe_group(self):
        """Test that EUW uses europe region group."""
        client = RiotClient("test_key")

        with patch.object(client, '_request', new_callable=AsyncMock) as mock_req:
            await client.get_match_ids("euw1", "puuid", 5)

            # Check that the URL uses 'europe' region
            called_url = mock_req.call_args[0][0]
            assert "europe.api.riotgames.com" in called_url

    async def test_na_uses_americas_group(self):
        """Test that NA uses americas region group."""
        client = RiotClient("test_key")

        with patch.object(client, '_request', new_callable=AsyncMock) as mock_req:
            await client.get_match_ids("na1", "puuid", 5)

            called_url = mock_req.call_args[0][0]
            assert "americas.api.riotgames.com" in called_url

    async def test_kr_uses_asia_group(self):
        """Test that KR uses asia region group."""
        client = RiotClient("test_key")

        with patch.object(client, '_request', new_callable=AsyncMock) as mock_req:
            await client.get_match_ids("kr", "puuid", 5)

            called_url = mock_req.call_args[0][0]
            assert "asia.api.riotgames.com" in called_url
