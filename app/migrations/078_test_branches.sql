-- 078: test branches (2026-11-07; app/test_branches.py). Dummy - Kopi Calf Sabang (DMY-SBG) reads the ESB data of
-- Kopi Calf Jatiwarna (CTG18), Dummy - Kopi Calf Merauke (DMY-MRK) the one of Kopi Calf To Go Jatiwaringin (CTG47).
-- Their own kopicalf facilities (codes DMY-SBG / DMY-MRK) and outlet accounts are made apart; the calculations over
-- every branch leave them out.
CREATE TABLE IF NOT EXISTS integration_portal.test_branch (
    code           text        PRIMARY KEY,
    name           text        NOT NULL,
    source_branch  text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
INSERT INTO integration_portal.test_branch (code, name, source_branch) VALUES
    ('DMY-SBG', 'Dummy - Kopi Calf Sabang', 'CTG18'),
    ('DMY-MRK', 'Dummy - Kopi Calf Merauke', 'CTG47')
ON CONFLICT (code) DO NOTHING;
