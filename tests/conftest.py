# Copyright 2023 by Garmin Ltd. or its subsidiaries
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import time
import shlex
import re
import subprocess
import logging
import os
from os import getenv, path
from pathlib import Path

import allure
import pytest
from pytest_harvest import get_fixture_store

logger = logging.getLogger(__name__)

device = getenv("LG_ENV", "Unknown").split("/")[-1].split(".")[0]


class VWiFiTimeouts:
    """Timeouts for virtual WiFi (vwifi) setup operations."""
    VWIFI_START_WAIT = 5       # Wait after starting vwifi client
    WIFI_RELOAD_WAIT = 10      # Wait after wifi reload/up
    IW_DEVICE_POLL_INTERVAL = 2  # Interval for polling iw devices


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    result = outcome.get_result()

    if result.when == "call":
        allure.dynamic.parent_suite(device)


def pytest_addoption(parser):
    parser.addoption("--firmware", action="append", default=[],
                     help="Firmware image path. Can be: single path, or 'target=path' for target-specific mapping. "
                          "Can be specified multiple times for different targets.")
    parser.addoption("--flash-firmware", action="store_true", 
                     help="Flash firmware before running tests")
    parser.addoption("--flash-keep-config", action="store_true",
                     help="Keep configuration when flashing")
    parser.addoption("--flash-verify-version", action="store", default=None,
                     help="Verify firmware version after flashing")
    parser.addoption("--flash-sha256", action="store", default=None,
                     help="Expected SHA256 checksum for firmware validation")
    parser.addoption("--flash-skip-if-same", action="store_true",
                     help="Skip flashing if same version already installed")
    parser.addoption("--flash-validate-only", action="store_true",
                     help="Validate firmware without actually flashing (CI mode)")


def pytest_sessionfinish(session):
    """Gather all results and save them to a JSON file."""

    fixture_store = get_fixture_store(session)
    if "results_bag" not in fixture_store:
        return

    results = fixture_store["results_bag"]

    Path("results.json").write_text(json.dumps(results, indent=2))

    alluredir = session.config.getoption("--alluredir")

    if not alluredir or not path.isdir(alluredir):
        return

    # workaround for allure to accept multiple devices as suites
    for json_file in Path(alluredir).glob("*.json"):
        json_data = json.loads(json_file.read_text())
        if "testCaseId" in json_data:
            json_data["parameters"] = [{"name": "device", "value": device}]
            json_data["testCaseId"] = device + json_data["testCaseId"]
            json_data["historyId"] = device + json_data["historyId"]
            json_file.write_text(json.dumps(json_data))

    allure_properties_file = Path(alluredir, "environment.properties")
    allure_properties_file.write_text(
        f"Version={results['tests/test_base.py::test_ubus_system_board']['version']}\n"
        f"Revision={results['tests/test_base.py::test_ubus_system_board']['revision']}\n"
    )


def ubus_call(command, namespace, method, params={}):
    output = command.run_check(f"ubus call {namespace} {method} '{json.dumps(params)}'")

    try:
        return json.loads("\n".join(output))
    except json.JSONDecodeError:
        return {}


@pytest.fixture(scope="session", autouse=True)
def setup_env(env, pytestconfig):
    firmware_list = pytestconfig.getoption("firmware")
    # Store the first firmware in the list for backward compatibility
    env.config.data.setdefault("images", {})["firmware"] = firmware_list[0] if firmware_list else None


@pytest.fixture
def shell_command(strategy):
    try:
        strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)


@pytest.fixture
def ssh_command(shell_command, target):
    ssh = target.get_driver("SSHDriver")
    return ssh


def _host_ipv4_from_hostname_I() -> str:
    out = subprocess.check_output("hostname -I", shell=True, text=True).strip()
    if not out:
        raise RuntimeError("hostname -I returned nothing")
    # take the first token; if it's not IPv4, fall back to first IPv4 token
    first = out.split()[0]
    if ":" in first:
        first = next((t for t in out.split() if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", t)), "")
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", first or ""):
        raise RuntimeError(f"Could not determine IPv4 from: {out!r}")
    return first

@pytest.fixture
def upload_vwifi(shell_command,target):
    ssh = target.get_driver("SSHDriver")
    ssh.scp(src="vwifi/vwifi-client",dst=":/usr/bin/vwifi-client")
    path = "\n".join(ssh.run("which vwifi-client")[0])
    assert path == "/usr/bin/vwifi-client"

    # compute HOST IPv4 once (on the host)
    host_ip = _host_ipv4_from_hostname_I()
    host_ip_q = shlex.quote(host_ip)

    ssh.run_check("rmmod mac80211_hwsim")
    ssh.run_check("insmod mac80211_hwsim radios=0")
    cmd = f"""sh -lc '
        if command -v start-stop-daemon >/dev/null; then
          start-stop-daemon -S -b -m -p /tmp/vwifi.pid \
            -x /usr/bin/vwifi-client -- {host_ip_q} --number 2 \
            >/tmp/vwifi.log 2>&1
        else
          nohup /usr/bin/vwifi-client {host_ip_q} --number 2 \
            </dev/null >/tmp/vwifi.log 2>&1 & echo $! >/tmp/vwifi.pid
        fi
        '"""
    ssh.run_check(cmd)
    assert "\n".join(ssh.run("ps | grep vwifi")[0]) != ""
    time.sleep(VWiFiTimeouts.VWIFI_START_WAIT)
    ssh.run("wifi reload")
    ssh.run("wifi up")
    time.sleep(VWiFiTimeouts.WIFI_RELOAD_WAIT)
    phy_devices = ssh.run("iw phy | grep phy")[0]
    assert len(phy_devices) == 4 #labgrid tokenize \t 
    iw_devices = "\n".join(ssh.run("iw dev")[0])                                      
    while "wlan0-mesh" not in iw_devices:                                                     
        iw_devices = "\n".join(ssh.run("iw dev")[0])                                  
        time.sleep(VWiFiTimeouts.IW_DEVICE_POLL_INTERVAL)                                                                         
    stations = "\n".join(ssh.run("iw dev wlan0-mesh station dump")[0])                
    assert "02:00:00:00:00:01" in stations                                                    
    assert "02:00:00:00:00:02" in stations                                                    
    assert "02:00:00:00:00:03" in stations 
    return ssh

@pytest.fixture
def shell_command_fast(strategy):
    try:
        strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)

@pytest.fixture
def shell_command_force_cycle(strategy):
    try:
        if hasattr(strategy, 'force_power_cycle'):
            strategy.force_power_cycle()
        else:
            strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)

# Fixture que apaga los routers al final (con env directo)
@pytest.fixture(scope="session", autouse=True)
def auto_router_lifecycle(env):
    """Apaga todos los targets del env al finalizar cada test (salvo KEEP_DUT_ON=1)."""
    yield

    if os.getenv("KEEP_DUT_ON", "0") == "1":
        logger.info("KEEP_DUT_ON=1, skipping power off for all targets")
        return

    for name, target in getattr(env, "targets", {}).items():
        try:
            # Prefer Strategy (aplica quirks); fallback a driver
            try:
                strat = target.get_driver("PhysicalDeviceStrategy")
                strat.transition("off")
                logger.info(f"[teardown] OFF via strategy: {name}")
            except Exception:
                power = target.get_driver("ExternalPowerDriver")
                target.activate(power)
                power.off()
                logger.info(f"[teardown] OFF via ExternalPowerDriver: {name}")
        except Exception as e:
            logger.warning(f"[teardown] Could not power off {name}: {e}")


@pytest.fixture
def belkin1_shell(env):
    """Shell del Belkin RT3200 #1."""
    target = env.get_target("belkin_rt3200_1")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell


@pytest.fixture
def belkin2_shell(env):
    """Shell del Belkin RT3200 #2."""
    target = env.get_target("belkin_rt3200_2")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell

@pytest.fixture
def glinet_shell(env):
    """Shell del GL-iNet MT300N-V2."""
    target = env.get_target("gl_mt300n_v2")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell


@pytest.fixture
def mesh_routers(belkin1_shell, belkin2_shell, glinet_shell):
    """Fixture para tests que requieren múltiples routers (2 Belkins + 1 GL-iNet)."""
    return {
        "belkin1": belkin1_shell,
        "belkin2": belkin2_shell,
        "glinet": glinet_shell
    }


@pytest.fixture(scope="session")
def firmware_image(pytestconfig):
    """
    Returns firmware path for single-target tests.
    For multi-target tests with target-specific mapping, use the flash_firmware_if_requested fixture.
    """
    firmware_list = pytestconfig.getoption("firmware")
    if not firmware_list:
        pytest.skip("No firmware specified. Use --firmware")
    
    # If multiple firmwares specified, try to find a non-mapped one (default)
    default_firmware = None
    for fw_spec in firmware_list:
        if "=" not in fw_spec:
            default_firmware = fw_spec
            break
    
    # If no default, use the first one (could be a mapping)
    if not default_firmware:
        if "=" in firmware_list[0]:
            pytest.skip("Multiple target-specific firmwares specified, but no default. "
                       "This fixture is for single-target tests only.")
        default_firmware = firmware_list[0]
    
    if not os.path.exists(default_firmware):
        pytest.skip(f"Firmware image not found: {default_firmware}")
    
    return default_firmware


@pytest.fixture(scope="session", autouse=True)
def flash_firmware_if_requested(env, pytestconfig):
    """
    Automatically flashes firmware to all targets if --flash-firmware flag is provided.
    
    Supports:
    - Single firmware for all targets
    - Target-specific firmware mapping (target=path)
    - Sequential flashing (one device at a time)
    
    Usage:
        # Single firmware for all targets (homogeneous testbed)
        pytest --lg-env targets/device.yaml --firmware /path/to/image.bin --flash-firmware
        
        # Target-specific mapping (heterogeneous testbed)
        pytest --lg-env targets/mesh_testbed.yaml \
               --firmware belkin_rt3200_1=/path/to/belkin.itb \
               --firmware belkin_rt3200_2=/path/to/belkin.itb \
               --firmware gl_mt300n_v2=/path/to/glinet.bin \
               --flash-firmware
    """
    if not pytestconfig.getoption("flash_firmware", default=False):
        return
    
    firmware_list = pytestconfig.getoption("firmware")
    if not firmware_list:
        pytest.fail("Cannot flash: no firmware specified. Use --firmware")
    
    # Parse firmware mapping: either "path" or "target=path"
    firmware_map = {}
    default_firmware = None
    
    for fw_spec in firmware_list:
        if "=" in fw_spec:
            # Target-specific: "target=path"
            target_name, fw_path = fw_spec.split("=", 1)
            firmware_map[target_name] = fw_path
        else:
            # Default firmware for all targets
            default_firmware = fw_spec
    
    # Build final mapping for each target
    for name in env.targets.keys():
        if name not in firmware_map:
            if default_firmware:
                firmware_map[name] = default_firmware
            else:
                pytest.fail(f"No firmware specified for target '{name}'. "
                           f"Use --firmware {name}=/path/to/image.bin")
    
    # Validate all firmware files exist
    for name, fw_path in firmware_map.items():
        if not os.path.exists(fw_path):
            pytest.fail(f"Firmware image not found for target '{name}': {fw_path}")
    
    keep_config = pytestconfig.getoption("flash_keep_config", default=False)
    verify_version = pytestconfig.getoption("flash_verify_version", default=None)
    expected_sha256 = pytestconfig.getoption("flash_sha256", default=None)
    skip_if_same = pytestconfig.getoption("flash_skip_if_same", default=False)
    validate_only = pytestconfig.getoption("flash_validate_only", default=False)
    
    logger.info(f"Session firmware flash requested for {len(firmware_map)} target(s)")
    logger.info(f"  keep_config={keep_config}, skip_if_same={skip_if_same}, validate_only={validate_only}")
    if expected_sha256:
        logger.info(f"  SHA256: {expected_sha256}")
    for name, fw_path in firmware_map.items():
        logger.info(f"  {name}: {fw_path}")
    
    # Flash targets sequentially (one at a time)
    logger.info("Starting SEQUENTIAL firmware flash")
    failures = []
    
    for name, target in env.targets.items():
        firmware_path = firmware_map[name]
        try:
            # Get strategy first
            strategy = target.get_driver("PhysicalDeviceStrategy")
            
            # Ensure device is powered on and shell is available
            logger.info(f"[{name}] Ensuring device is powered on and shell is available")
            strategy.transition("shell")
            
            # Get sysupgrade driver (shell is already active)
            sysupgrade = target.get_driver("SysupgradeDriver")
            
            # Configure skip behavior
            original_skip = sysupgrade.skip_if_installed
            sysupgrade.skip_if_installed = skip_if_same
            
            logger.info(f"[{name}] Processing firmware: {firmware_path}")
            
            if validate_only:
                # Validate-only mode: run all checks without flashing
                logger.info(f"[{name}] Running validation-only mode")
                sysupgrade.flash(
                    firmware_path,
                    keep_config=keep_config,
                    expected_sha256=expected_sha256,
                    expected_version=verify_version,
                    validate_only=True
                )
                logger.info(f"[{name}] Validation completed successfully")
            else:
                # Full flash with provisioning (includes network config)
                logger.info(f"[{name}] Flashing firmware")
                strategy.provision_with_firmware(
                    image_path=firmware_path,
                    keep_config=keep_config,
                    verify_version=verify_version
                )
                logger.info(f"[{name}] Firmware flash completed successfully")
            
            # Restore original skip setting
            sysupgrade.skip_if_installed = original_skip
            
        except Exception as e:
            logger.error(f"[{name}] Failed to process firmware: {e}")
            import traceback
            logger.error(traceback.format_exc())
            failures.append((name, str(e)))
    
    # Check if any failures occurred
    if failures:
        failure_msg = "\n".join([f"  - {name}: {error}" for name, error in failures])
        pytest.fail(f"Firmware flash failed on {len(failures)} target(s):\n{failure_msg}")


@pytest.fixture
def flash_clean_firmware(strategy, firmware_image):
    """
    Flashes clean firmware (without preserving configuration) before a specific test.
    
    This fixture can be used by individual tests to ensure a known clean state.
    
    Usage:
        def test_fresh_install(flash_clean_firmware, ssh_command):
            # Router has clean firmware flashed
            ...
    """
    strategy.provision_with_firmware(firmware_image, keep_config=False)
    return strategy


@pytest.fixture
def sysupgrade_driver(target):
    """Provides direct access to the SysupgradeDriver for manual flashing operations."""
    return target.get_driver("SysupgradeDriver")
