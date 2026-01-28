import re
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Headers to look like a browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def extract_vk_username(url: str) -> Optional[str]:
    """Extract username or ID from VK URL."""
    patterns = [
        r'(?:https?://)?(?:m\.)?vk\.com/([a-zA-Z0-9_.]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            username = match.group(1)
            # Skip service pages
            if username in ('wall', 'photo', 'video', 'audio', 'feed', 'im', 'friends', 'groups', 'apps'):
                return None
            return username
    return None


async def scrape_vk_name(username: str) -> Optional[str]:
    """Scrape name from VK profile page (no API needed)."""
    url = f"https://vk.com/{username}"

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(url)

            if response.status_code != 200:
                logger.warning(f"VK page fetch failed: {response.status_code}")
                return None

            html = response.text

            # Try to extract name from <title> tag
            # Format: "Имя Фамилия | ВКонтакте" or "Имя Фамилия | VK"
            title_match = re.search(r'<title>([^|<]+)', html)
            if title_match:
                name = title_match.group(1).strip()
                # Filter out non-profile pages
                if name and name not in ('ВКонтакте', 'VK', 'Ошибка', 'Error', 'Страница удалена'):
                    return name

            # Try og:title meta tag
            og_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            if og_match:
                name = og_match.group(1).strip()
                if name and '|' in name:
                    name = name.split('|')[0].strip()
                if name and name not in ('ВКонтакте', 'VK'):
                    return name

            return None

    except Exception as e:
        logger.error(f"VK scrape error for {username}: {e}")
        return None


def guess_name_from_username(username: str) -> Optional[str]:
    """Try to guess name from username patterns like ivan_petrov, ivan.petrov."""
    clean = username.lower()

    # Skip numeric IDs
    for prefix in ['id', 'club', 'public']:
        if clean.startswith(prefix) and clean[len(prefix):].isdigit():
            return None

    # Split by common separators
    parts = re.split(r'[._\-]', username)

    if len(parts) >= 2:
        # Capitalize each part
        name_parts = [p.capitalize() for p in parts if p and len(p) > 1]
        if len(name_parts) >= 2:
            return " ".join(name_parts[:2])

    return None


async def get_name_from_vk_url(url: str) -> Optional[str]:
    """Extract name from VK profile URL."""
    username = extract_vk_username(url)
    if not username:
        return None

    # Try scraping first
    name = await scrape_vk_name(username)
    if name:
        return name

    # Fall back to guessing from username
    return guess_name_from_username(username)


async def extract_names_from_urls(urls: list[str]) -> dict[str, str]:
    """Extract names from list of URLs. Returns {url: name}."""
    names = {}

    for url in urls:
        if "vk.com" in url.lower():
            name = await get_name_from_vk_url(url)
            if name:
                names[url] = name

    return names
