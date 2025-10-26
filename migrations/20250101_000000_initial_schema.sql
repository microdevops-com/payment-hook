-- Migration: initial_schema
-- Created: 2025-01-01T00:00:00 (Initial schema migration)
-- Squashed: includes all schema changes up to 2025-09-03

-- Create sequence for atomic receipt number generation
CREATE SEQUENCE fina_receipt_number_seq START WITH 1;

-- Create the main FINA receipt table for storing fiscal receipts
CREATE TABLE IF NOT EXISTS fina_receipt (
    id SERIAL PRIMARY KEY,
    year INTEGER NOT NULL,
    location_id TEXT NOT NULL,
    register_id TEXT NOT NULL,
    receipt_number INTEGER NOT NULL DEFAULT nextval('fina_receipt_number_seq'),
    order_id TEXT,
    stripe_id TEXT,
    amount NUMERIC(10, 2),
    currency TEXT,
    zki TEXT,
    jir TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    UNIQUE (year, receipt_number)
);

-- Make the sequence owned by the column for automatic cleanup
ALTER SEQUENCE fina_receipt_number_seq OWNED BY fina_receipt.receipt_number;

-- Create indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_fina_receipt_year ON fina_receipt(year);
CREATE INDEX IF NOT EXISTS idx_fina_receipt_stripe_id ON fina_receipt(stripe_id);
CREATE INDEX IF NOT EXISTS idx_fina_receipt_created_at ON fina_receipt(created_at);
CREATE INDEX IF NOT EXISTS idx_fina_receipt_status ON fina_receipt(status);
CREATE INDEX IF NOT EXISTS idx_fina_receipt_year_location_register ON fina_receipt(year, location_id, register_id);