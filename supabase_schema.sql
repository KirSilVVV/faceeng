-- Supabase Schema for FaceCheck Telegram Bot
-- Run this in Supabase SQL Editor

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    free_searches INTEGER DEFAULT 1,
    paid_searches INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index for fast telegram_id lookup
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

-- Searches table (for tracking and unlocking)
CREATE TABLE IF NOT EXISTS searches (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    search_id TEXT NOT NULL,
    results_count INTEGER DEFAULT 0,
    is_unlocked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_searches_telegram_id ON searches(telegram_id);
CREATE INDEX IF NOT EXISTS idx_searches_search_id ON searches(search_id);

-- Payments table
CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    stars_amount INTEGER NOT NULL,
    searches_amount INTEGER DEFAULT 0,
    telegram_payment_id TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payments_telegram_id ON payments(telegram_id);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE searches ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Policy to allow service role full access
CREATE POLICY "Service role access" ON users FOR ALL USING (true);
CREATE POLICY "Service role access" ON searches FOR ALL USING (true);
CREATE POLICY "Service role access" ON payments FOR ALL USING (true);
