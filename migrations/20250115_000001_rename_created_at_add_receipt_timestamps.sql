-- Migration: rename_created_at_add_receipt_timestamps
-- Created: 2025-01-15
-- Description:
--   1. Rename created_at to payment_time (this is the Stripe payment time, not record creation time)
--   2. Add receipt_created and receipt_updated columns to track database row operations

-- Rename created_at to payment_time for clarity
ALTER TABLE fina_receipt RENAME COLUMN created_at TO payment_time;

-- Add new columns for tracking database row operations
ALTER TABLE fina_receipt
  ADD COLUMN receipt_created TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  ADD COLUMN receipt_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Backfill receipt_created with payment_time for existing records (best approximation we have)
UPDATE fina_receipt SET receipt_created = payment_time WHERE receipt_created IS NULL;

-- Backfill receipt_updated with receipt_created for existing records
UPDATE fina_receipt SET receipt_updated = receipt_created WHERE receipt_updated IS NULL;

-- Make columns NOT NULL after backfilling
ALTER TABLE fina_receipt
  ALTER COLUMN receipt_created SET NOT NULL,
  ALTER COLUMN receipt_updated SET NOT NULL;

-- Rename index to match new column name
ALTER INDEX idx_fina_receipt_created_at RENAME TO idx_fina_receipt_payment_time;
