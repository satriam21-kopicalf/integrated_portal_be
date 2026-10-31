-- 042: the header row of docs/Daily SO & Waste.xlsx ("Product Code" / "Product Name" / "Unit") was seeded by 040 as an
-- item of the Waste and Usage Management lists; remove it so the lists match the sheets (196 waste, 34 usage items).
DELETE FROM integration_portal.waste_item WHERE code = 'Product Code' AND name = 'Product Name';
