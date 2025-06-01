import pytest
import time
from plugin_collection import PluginCollection

@pytest.fixture(scope="module")
def mqtt_host():
    """Fixture that provides a host instance with MQTT broker"""
    pc = PluginCollection(
        'plugins',
        host=True,
        mqtt_broker_ip="localhost",
        mqtt_port=18883  # Different port for testing
    )
    yield pc
    pc.mqtt_client.disconnect()
    if pc.host:
        pc.broker_task.cancel()

@pytest.fixture
def mqtt_node(mqtt_host):
    """Fixture that provides a node connected to the test broker"""
    pc = PluginCollection(
        'plugins',
        host=False,
        mqtt_broker_ip="localhost",
        mqtt_port=18883
    )
    yield pc
    pc.mqtt_client.disconnect()
    
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
    
def test_mqtt_basic_communication(mqtt_host, mqtt_node):
    """Test message passing through MQTT"""
    received = []
    
    # Setup callback
    def on_message(client, userdata, msg):
        received.append(msg.payload.decode())
    
    mqtt_host.mqtt_client.subscribe("test_topic")
    mqtt_host.mqtt_client.on_message = on_message
    
    mqtt_node.mqtt_client.publish("test_topic", "test_payload")
    
    time.sleep(0.5)  # Allow for MQTT delivery
    assert "test_payload" in received

def test_distributed_request(mqtt_host, mqtt_node):
    """Test request between two nodes through MQTT"""
    # Setup plugins
    from plugins.pluginB import PluginB
    mqtt_host.plugins.append(PluginB(mqtt_host._logger, mqtt_host))
    
    # Create request from node to host
    request = mqtt_node.create_request_wait(
        author="Node1",
        target="PluginB.calculate_square",
        args=5,
        timeout=10
    )
    
    assert request.result == 25
    assert request.error is False