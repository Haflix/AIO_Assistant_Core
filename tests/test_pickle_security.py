# tests/test_pickle_security.py
import pytest
from plugin_collection import PluginCollection
import pickle

def test_pickle_vulnerability():
    class Malicious:
        def __reduce__(self):
            import os
            return (os.system, ('echo "Hacked!"',))

    pc = PluginCollection('plugins')
    malicious = Malicious()
    
    with pytest.raises(Exception) as e:
        pc._handle_mqtt_request(None, None, pickle.dumps(malicious))
    
    assert "Dangerous pickle content" in str(e.value)