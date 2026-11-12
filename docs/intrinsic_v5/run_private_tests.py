"""Run requested test paths after isolating bridge import-time state and events."""
import os
from pathlib import Path
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='sensehome-private-tests-') as folder:
    os.environ['SENSEHOME_STATE_FILE'] = str(Path(folder) / 'state.json')
    os.environ['SENSEHOME_EVENT_LOG'] = str(Path(folder) / 'events.jsonl')
    prior_cli = os.environ.get('SENSEHOME_POLICY_CLI')
    os.environ['SENSEHOME_POLICY_CLI'] = str(Path(folder) / 'missing-import-cli')
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'openvela_smart_home/dashboard'))
    import bridge_server
    if prior_cli is None:
        del os.environ['SENSEHOME_POLICY_CLI']
    else:
        os.environ['SENSEHOME_POLICY_CLI'] = prior_cli
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
