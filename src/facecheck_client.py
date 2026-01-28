import asyncio
import logging
import time
from typing import Callable, Awaitable
import aiohttp
from src.config import FACECHECK_API_KEY, FACECHECK_BASE_URL

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int], Awaitable[None]]
TIMEOUT = aiohttp.ClientTimeout(total=120)
MIN_REQUEST_INTERVAL = 5  # seconds between requests
MAX_RETRIES = 3


class FaceCheckClient:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or FACECHECK_API_KEY
        self.base_url = FACECHECK_BASE_URL
        self._lock = asyncio.Lock()
        self._last_request_time = 0

    async def _wait_for_rate_limit(self):
        """Ensure minimum interval between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            wait_time = MIN_REQUEST_INTERVAL - elapsed
            logger.info(f"Rate limit: waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
        self._last_request_time = time.time()

    async def _request_with_retry(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        **kwargs
    ) -> aiohttp.ClientResponse | None:
        """Make request with retry on 429."""
        for attempt in range(MAX_RETRIES):
            try:
                await self._wait_for_rate_limit()

                if method == "POST":
                    response = await session.post(url, **kwargs)
                else:
                    response = await session.get(url, **kwargs)

                if response.status == 429:
                    wait_time = 30 * (attempt + 1)  # 30s, 60s, 90s
                    logger.warning(f"Rate limited (429). Waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

                return response

            except asyncio.TimeoutError:
                logger.error(f"Timeout on attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                continue

        return None

    async def upload_image(self, image_bytes: bytes, filename: str = "photo.jpg") -> str | None:
        """Upload image and get search ID."""
        headers = {"Authorization": self.api_key}

        form = aiohttp.FormData()
        form.add_field("images", image_bytes, filename=filename, content_type="image/jpeg")

        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                response = await self._request_with_retry(
                    session, "POST",
                    f"{self.base_url}/upload_pic",
                    headers=headers,
                    data=form
                )

                if not response:
                    return None

                text = await response.text()
                logger.info(f"Upload response: status={response.status}, body={text[:500]}")

                if response.status == 200:
                    data = await response.json()
                    return data.get("id_search")
                return None

        except Exception as e:
            logger.error(f"Upload error: {type(e).__name__}: {e}")
            return None

    async def search(
        self,
        id_search: str,
        demo: bool = True,
        on_progress: ProgressCallback = None
    ) -> dict | None:
        """Execute face search and wait for results."""
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        payload = {
            "id_search": id_search,
            "with_progress": True,
            "status_only": False,
            "demo": demo
        }

        last_progress = -1
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                while True:
                    response = await self._request_with_retry(
                        session, "POST",
                        f"{self.base_url}/search",
                        headers=headers,
                        json=payload
                    )

                    if not response or response.status != 200:
                        logger.error(f"Search failed")
                        return None

                    data = await response.json()
                    progress = data.get("progress", 0) or 0

                    if data.get("error"):
                        logger.error(f"Search error: {data.get('error')}")
                        return {"error": data.get("error")}

                    # Notify progress every 20%
                    if on_progress and progress > last_progress and progress % 20 == 0:
                        await on_progress(progress)
                        last_progress = progress

                    logger.info(f"Search progress: {progress}%")

                    if progress >= 100:
                        output = data.get("output", {})
                        items = output.get("items", [])
                        logger.info(f"Search complete: {len(items)} results")
                        return data

                    await asyncio.sleep(3)  # Poll every 3 seconds

        except Exception as e:
            logger.error(f"Search error: {type(e).__name__}: {e}")
            return {"error": f"Network error: {type(e).__name__}"}

    async def get_info(self) -> dict | None:
        """Get account info including remaining credits."""
        headers = {"Authorization": self.api_key}

        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                response = await self._request_with_retry(
                    session, "POST",
                    f"{self.base_url}/info",
                    headers=headers
                )

                if response and response.status == 200:
                    return await response.json()
                return None

        except Exception as e:
            logger.error(f"Info error: {type(e).__name__}: {e}")
            return None

    async def find_face(
        self,
        image_bytes: bytes,
        demo: bool = True,
        on_progress: ProgressCallback = None
    ) -> dict | None:
        """Full pipeline: upload image and search. Thread-safe."""
        async with self._lock:  # One search at a time
            id_search = await self.upload_image(image_bytes)
            if not id_search:
                return {"error": "Failed to upload image"}

            return await self.search(id_search, demo=demo, on_progress=on_progress)
