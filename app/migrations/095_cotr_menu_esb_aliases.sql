-- 095: the ESB products and workbook names of a COTR menu, mapped by hand in Master Menu (recommendation #5, 2026-11-12):
-- a new ESB product ("Smooth Banana - COTR (250ml)") is mapped once instead of being guessed from its name. Lower case,
-- spaces collapsed (cotr_sheet.menu_of compares them so).
ALTER TABLE integration_portal.cotr_menu ADD COLUMN IF NOT EXISTS esb_aliases text[] NOT NULL DEFAULT '{}';
