"""
BDBM smoke test — portable (Linux/Windows/macOS, x64/arm64).

Run with: python tests/test_smoke.py
Covers: store → step → recall → consolidate → state round-trip → search/edit/forget.
"""
import os
import sys
import tempfile

os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import torch  # noqa: E402
from memory_module import TextMemory, MemoryConfig  # noqa: E402


def main():
    # 1. Store
    mem = TextMemory()
    assert mem.get_stats()['stm_active'] == 0
    mem.store("What is the capital of France?", "Paris")
    mem.store("Petr helped me with the project.", "Petr is a reliable colleague.",
              emotion={"dopamin": 1.5, "serotonin": 0.8, "kortizol": 1.3, "oxytocin": 1.6})
    s = mem.get_stats()
    assert s['stm_active'] == 2 and s['writes'] == 2, s
    # h corresponds to 3·σ(1.0) ≈ 2.1932 for the first store
    h = mem.stm_centers.h[mem.stm_centers.active].tolist()
    assert abs(h[0] - 2.19318) < 0.01, h
    # fatigue = 0.1·intensity / threshold(2.5) on a relative level
    assert abs(s['fatigue'] - 0.07972) < 1e-4, s['fatigue']

    # 2. Step (homeostasis)
    h_before = mem.stm_centers.h[mem.stm_centers.active].clone()
    mem.step()
    h_after = mem.stm_centers.h[mem.stm_centers.active]
    ratio = h_after / h_before
    assert torch.allclose(ratio, torch.full_like(ratio, 1.0 - 0.0035), atol=1e-5), ratio

    # 3. Recall
    r = mem.recall("Capital of France?")
    assert r.text == 'Paris', r
    assert r.source == 'STM' and r.confidence > 0.5, r

    # 4. Search
    results = mem.search("Petr help")
    assert results and results[0]['value'] == 'Petr is a reliable colleague.', results

    # 5. Consolidate
    res = mem.consolidate()
    assert res['status'] == 'success' and res['consolidated_centers'] == 2, res
    assert mem.get_stats()['ltm_active'] == 2, mem.get_stats()
    # LTM h = log1p(0.8·h_stm) (ω = κ·h, pak log-komprese)
    ltm_h = mem.ltm_centers.h[mem.ltm_centers.active]
    assert torch.all(ltm_h > 0.2) and torch.all(ltm_h < 1.2), ltm_h

    # 6. State round-trip (.bdbm + .pt)
    with tempfile.TemporaryDirectory() as td:
        bdbm_path = os.path.join(td, 'mem.bdbm')
        mem.save(bdbm_path)
        assert os.path.exists(bdbm_path)
        mem2 = TextMemory(state_file=bdbm_path, auto_load=True)
        assert mem2.get_stats()['ltm_active'] == 2, mem2.get_stats()
        pt_path = os.path.join(td, 'mem.pt')
        mem.save(pt_path)
        mem3 = TextMemory(state_file=pt_path, auto_load=True)
        assert mem3.get_stats()['ltm_active'] == 2, mem3.get_stats()

    # 7. Edit / forget
    n = mem.edit('Paris', 'Paris (capital)', exact_match=True)
    assert n >= 1, n
    n = mem.forget('Petr', exact_match=False)
    assert n >= 1, n

    print("SMOKE OK — all assertions passed")


def test_complete_feature_access():
    """The complete local product is active and offline."""
    from memory_module.security import SecurityManager
    sec = SecurityManager()
    assert sec.state == 'ACTIVE'
    assert sec.check_command_allowed('retrieve') is None
    assert not sec.is_allowed_origin('https://gemini.google.com')
    assert sec.is_allowed_origin('chrome-extension://biomem-smoke-test')
    from memory_module.settings_manager import SettingsManager
    import tempfile
    settings = SettingsManager(tempfile.mkdtemp())
    assert settings.get_max_associations() >= 3
    print('COMPLETE FEATURE ACCESS OK')


if __name__ == '__main__':
    main()
    test_complete_feature_access()
