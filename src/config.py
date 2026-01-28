import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FACECHECK_API_KEY = os.getenv("FACECHECK_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Your Telegram ID for alerts

FACECHECK_BASE_URL = "https://facecheck.id/api"

# Pricing (aggressive x3 margin)
SEARCH_COST_STARS = 75          # Single search
SEARCH_PACK_5_STARS = 300       # 5 searches pack
UNLOCK_SINGLE_STARS = 10        # Unlock 1 link
UNLOCK_ALL_STARS = 35           # Unlock all 10 links

# Alert settings
API_BALANCE_ALERT_THRESHOLD = 50  # Alert when credits drop to this level
