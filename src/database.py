import logging
from datetime import datetime
from typing import Optional
import httpx

from src.config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

# Supabase REST API client using httpx
class SupabaseClient:
    def __init__(self):
        self.base_url = f"{SUPABASE_URL}/rest/v1"
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

    async def select(self, table: str, filters: dict = None, columns: str = "*") -> list:
        """Select rows from table."""
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/{table}?select={columns}"
            if filters:
                for key, value in filters.items():
                    url += f"&{key}=eq.{value}"

            response = await client.get(url, headers=self.headers)
            if response.status_code == 200:
                return response.json()
            logger.error(f"Select error: {response.status_code} - {response.text}")
            return []

    async def insert(self, table: str, data: dict) -> Optional[dict]:
        """Insert row into table."""
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/{table}"
            response = await client.post(url, headers=self.headers, json=data)
            if response.status_code in (200, 201):
                result = response.json()
                return result[0] if result else None
            logger.error(f"Insert error: {response.status_code} - {response.text}")
            return None

    async def update(self, table: str, filters: dict, data: dict) -> bool:
        """Update rows in table."""
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/{table}"
            for key, value in filters.items():
                url += f"?{key}=eq.{value}"

            response = await client.patch(url, headers=self.headers, json=data)
            if response.status_code in (200, 204):
                return True
            logger.error(f"Update error: {response.status_code} - {response.text}")
            return False


_client: Optional[SupabaseClient] = None


def get_client() -> SupabaseClient:
    """Get or create Supabase client."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
        _client = SupabaseClient()
    return _client


async def get_or_create_user(telegram_id: int, username: str = None) -> dict:
    """Get user by telegram_id or create if not exists."""
    client = get_client()

    # Try to get existing user
    result = await client.select("users", {"telegram_id": telegram_id})

    if result:
        return result[0]

    # Create new user with 1 free search
    new_user = {
        "telegram_id": telegram_id,
        "username": username,
        "free_searches": 1,
        "paid_searches": 0
    }

    created = await client.insert("users", new_user)
    if created:
        logger.info(f"Created new user: {telegram_id}")
        return created

    # If insert failed, user might have been created by another request
    result = await client.select("users", {"telegram_id": telegram_id})
    return result[0] if result else new_user


async def use_search(telegram_id: int) -> tuple[bool, bool]:
    """
    Use one search credit.
    Returns (success, is_free_search).
    """
    client = get_client()

    result = await client.select("users", {"telegram_id": telegram_id})
    if not result:
        return False, False

    user = result[0]

    # Check free searches first
    if user["free_searches"] > 0:
        await client.update(
            "users",
            {"telegram_id": telegram_id},
            {"free_searches": user["free_searches"] - 1}
        )
        return True, True

    # Then paid searches
    if user["paid_searches"] > 0:
        await client.update(
            "users",
            {"telegram_id": telegram_id},
            {"paid_searches": user["paid_searches"] - 1}
        )
        return True, False

    return False, False


async def get_user_credits(telegram_id: int) -> dict:
    """Get user's remaining credits."""
    client = get_client()

    result = await client.select("users", {"telegram_id": telegram_id}, "free_searches,paid_searches")

    if result:
        return result[0]
    return {"free_searches": 0, "paid_searches": 0}


async def add_paid_searches(telegram_id: int, amount: int) -> bool:
    """Add paid searches to user account."""
    client = get_client()

    result = await client.select("users", {"telegram_id": telegram_id}, "paid_searches")
    if not result:
        return False

    current = result[0]["paid_searches"]
    await client.update(
        "users",
        {"telegram_id": telegram_id},
        {"paid_searches": current + amount}
    )

    logger.info(f"Added {amount} searches to user {telegram_id}")
    return True


async def save_search_result(
    telegram_id: int,
    search_id: str,
    results_count: int,
    is_unlocked: bool = False
) -> Optional[int]:
    """Save search result and return record ID."""
    client = get_client()

    record = {
        "telegram_id": telegram_id,
        "search_id": search_id,
        "results_count": results_count,
        "is_unlocked": is_unlocked
    }

    result = await client.insert("searches", record)
    return result["id"] if result else None


async def unlock_search(search_db_id: int) -> bool:
    """Mark search results as unlocked."""
    client = get_client()

    return await client.update(
        "searches",
        {"id": search_db_id},
        {"is_unlocked": True}
    )


async def reset_user_credits(telegram_id: int) -> bool:
    """Reset user credits for testing (sets free_searches=1, paid_searches=0)."""
    client = get_client()

    return await client.update(
        "users",
        {"telegram_id": telegram_id},
        {"free_searches": 1, "paid_searches": 0}
    )


async def record_payment(
    telegram_id: int,
    stars_amount: int,
    searches_amount: int,
    telegram_payment_id: str
) -> bool:
    """Record a successful payment."""
    client = get_client()

    record = {
        "telegram_id": telegram_id,
        "stars_amount": stars_amount,
        "searches_amount": searches_amount,
        "telegram_payment_id": telegram_payment_id
    }

    result = await client.insert("payments", record)
    if result:
        logger.info(f"Payment recorded: {telegram_id} paid {stars_amount} stars for {searches_amount} searches")
        return True
    return False
