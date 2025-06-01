import pytest
import time
import asyncio
from plugin_collection import PluginCollection
from plugins.pluginA import PluginA
from plugins.pluginB import PluginB
from plugins.bluetooth.bluetooth import Bluetooth
from utils import Request
from logging import Logger
import threading

@pytest.fixture
def plugin_collection():
    """Fixture providing a initialized PluginCollection"""
    pc = PluginCollection('plugins', host=True)
    pc.plugins = []  # Clear automatically loaded plugins
    return pc

@pytest.fixture
def logger():
    return PluginCollection._logger

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop()
    yield loop
    loop.close()

def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.addinivalue_line("markers", "mqtt: mark test as requiring MQTT")

def test_plugin_loading(plugin_collection):
    """Test if plugins can be loaded properly"""
    plugin_collection.plugins.append(PluginA(plugin_collection._logger, plugin_collection))
    plugin_collection.plugins.append(PluginB(plugin_collection._logger, plugin_collection))
    
    assert len(plugin_collection.plugins) == 2
    assert isinstance(plugin_collection.plugins[0], PluginA)
    assert isinstance(plugin_collection.plugins[1], PluginB)

def test_basic_request_flow(plugin_collection):
    """Test successful request between PluginA and PluginB"""
    # Setup
    plugin_a = PluginA(plugin_collection._logger, plugin_collection)
    plugin_b = PluginB(plugin_collection._logger, plugin_collection)
    plugin_collection.plugins.extend([plugin_a, plugin_b])
    
    # Create and process request
    request = plugin_collection.create_request_wait(
        author="Test",
        target="PluginB.calculate_square",
        args=4,
        timeout=15
    )
    
    assert request.result == 16
    assert request.error is False
    assert request.timeout is False

def test_request_error_handling(plugin_collection):
    """Test error propagation through requests"""
    plugin_a = PluginA(plugin_collection._logger, plugin_collection)
    plugin_collection.plugins.append(plugin_a)
    
    # Create invalid request (non-existing plugin)
    request = plugin_collection.create_request_wait(
        author="Test",
        target="NonExistentPlugin.some_method",
        args=None,
        timeout=5
    )
    
    with pytest.raises(Exception):
        result = request.result  # Should raise exception
    assert request.error is True

def test_request_timeout(plugin_collection):
    """Test request timeout handling"""
    plugin_b = PluginB(plugin_collection._logger, plugin_collection)
    plugin_collection.plugins.append(plugin_b)
    
    # Create request that will timeout (PluginB needs 10 seconds)
    request = plugin_collection.create_request_wait(
        author="Test",
        target="PluginB.calculate_square",
        args=2,
        timeout=1  # Too short timeout
    )
    
    assert request.timeout is True
    with pytest.raises(Exception):
        result = request.result  # Should raise timeout exception

def test_async_plugin_operations():
    """Test asynchronous plugin functionality (Bluetooth)"""
    async def run_test():
        pc = PluginCollection('plugins', host=True)
        bt = Bluetooth(pc._logger, pc)
        pc.plugins.append(bt)
        
        # Start Bluetooth discovery
        bt.loop_start()
        
        # Give some time for discovery
        await asyncio.sleep(5)
        assert bt.loop_running is True

    asyncio.run(run_test())

def test_request_object_lifecycle(plugin_collection):
    """Test complete lifecycle of a Request object"""
    # Setup
    plugin_b = PluginB(plugin_collection._logger, plugin_collection)
    plugin_collection.plugins.append(plugin_b)
    
    # Create request
    request = plugin_collection.create_request(
        author="Test",
        target="PluginB.calculate_square",
        args=3
    )
    
    # Wait manually
    result, error, timeout = request.wait_for_result()
    
    assert result == 9
    assert error is False
    assert timeout is False
    
    # Test cleanup
    plugin_collection.cleanup_requests()
    assert request.id not in plugin_collection.requests

def test_concurrent_requests(plugin_collection):
    """Test handling of multiple concurrent requests"""
    plugin_b = PluginB(plugin_collection._logger, plugin_collection)
    plugin_collection.plugins.append(plugin_b)
    
    results = []
    
    def worker(number):
        request = plugin_collection.create_request_wait(
            author=f"Worker-{number}",
            target="PluginB.calculate_square",
            args=number,
            timeout=15
        )
        results.append(request.result)
    
    # Create multiple threads
    threads = []
    for i in range(1, 6):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    
    # Wait for all threads
    for t in threads:
        t.join()
    
    assert sorted(results) == [1, 4, 9, 16, 25]