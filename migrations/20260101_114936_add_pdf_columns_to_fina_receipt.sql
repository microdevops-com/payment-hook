-- Migration: add_pdf_columns_to_fina_receipt
-- Created: 2026-01-01T11:49:36.800663
-- Description:
--   Add columns to support async PDF receipt generation:
--   1. s3_folder_path - stores the full S3 folder path for the receipt files
--   2. pdf_status - tracks PDF generation status (pending/processing/completed/failed)
--   3. pdf_created - timestamp when PDF was successfully generated

-- Add S3 folder path column to store location of receipt files
ALTER TABLE fina_receipt
  ADD COLUMN s3_folder_path TEXT;

-- Add PDF status tracking column with CHECK constraint
ALTER TABLE fina_receipt
  ADD COLUMN pdf_status VARCHAR(20) DEFAULT 'pending' NOT NULL
    CHECK (pdf_status IN ('pending', 'processing', 'completed', 'failed'));

-- Add timestamp for when PDF was created
ALTER TABLE fina_receipt
  ADD COLUMN pdf_created TIMESTAMP WITH TIME ZONE;

-- Create index on pdf_status to optimize queries for pending PDFs
CREATE INDEX idx_fina_receipt_pdf_status ON fina_receipt(pdf_status);

-- Create index on s3_folder_path for lookups
CREATE INDEX idx_fina_receipt_s3_folder_path ON fina_receipt(s3_folder_path);
